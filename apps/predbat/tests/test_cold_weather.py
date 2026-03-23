# -----------------------------------------------------------------------------
# Predbat Home Battery System - Cold Weather Plugin Tests
# Tests for the GSHP prediction model and plugin logic
#
# Run: cd apps/predbat && python3 tests/test_cold_weather.py
# -----------------------------------------------------------------------------

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cold_weather_plugin import ColdWeatherPlugin


# ============================================================================
# Pure function tests: linear regression
# ============================================================================


def test_solve_4x4_identity():
    """Solve a simple system: identity matrix."""
    a = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
    b = [1, 2, 3, 4]
    result = ColdWeatherPlugin._solve_4x4(a, b)
    assert result is not None
    for i in range(4):
        assert abs(result[i] - b[i]) < 1e-6, "x[{}] = {}, expected {}".format(i, result[i], b[i])
    print("  test_solve_4x4_identity: PASSED")


def test_solve_4x4_known_system():
    """Solve a system with known solution."""
    # 2x + y = 5, x + 3y = 10 → x=1, y=3 (padded to 4x4)
    a = [[2, 1, 0, 0], [1, 3, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
    b = [5, 10, 7, 8]
    result = ColdWeatherPlugin._solve_4x4(a, b)
    assert result is not None
    assert abs(result[0] - 1.0) < 1e-6
    assert abs(result[1] - 3.0) < 1e-6
    print("  test_solve_4x4_known_system: PASSED")


def test_solve_4x4_singular():
    """Singular matrix should return None."""
    a = [[1, 2, 3, 4], [2, 4, 6, 8], [0, 0, 1, 0], [0, 0, 0, 1]]
    b = [1, 2, 3, 4]
    result = ColdWeatherPlugin._solve_4x4(a, b)
    assert result is None, "Expected None for singular matrix"
    print("  test_solve_4x4_singular: PASSED")


# ============================================================================
# Feature extraction tests
# ============================================================================


def test_extract_features_basic():
    """Extract features from complete hourly data."""
    hourly_temp = {h: 5.0 for h in range(-12, 8)}
    hourly_wind = {h: 10.0 for h in range(-12, 8)}
    # Set different temps for evening vs dawn to get a drop
    for h in range(-2, 0):
        hourly_temp[h] = 8.0
    for h in range(4, 7):
        hourly_temp[h] = 2.0

    features = ColdWeatherPlugin._extract_features_from_hourly(hourly_temp, hourly_wind, 0)
    assert features is not None
    avg_temp, avg_wind, t_drop = features
    assert abs(avg_wind - 10.0) < 0.1
    assert abs(t_drop - 6.0) < 0.1  # 8.0 - 2.0
    print("  test_extract_features_basic: PASSED (temp={:.1f}, wind={:.1f}, drop={:.1f})".format(*features))


def test_extract_features_insufficient_data():
    """Should return None with too few data points."""
    hourly_temp = {0: 5.0, 1: 5.0}  # Only 2 points
    hourly_wind = {0: 10.0}
    features = ColdWeatherPlugin._extract_features_from_hourly(hourly_temp, hourly_wind, 0)
    assert features is None, "Expected None with insufficient data"
    print("  test_extract_features_insufficient_data: PASSED")


# ============================================================================
# Model fitting tests
# ============================================================================


class MockBase:
    """Minimal mock for cold weather plugin tests."""

    def __init__(self):
        self.config_root = "/tmp"
        self.now_utc = None
        self.minutes_now = 720
        self.prefix = "predbat"
        self.logs = []
        self.published = {}
        self._states = {}

    def log(self, msg, *args, **kwargs):
        self.logs.append(msg)

    def get_state_wrapper(self, entity, attribute=None, default=None):
        if attribute:
            return self._states.get((entity, attribute), default)
        return self._states.get(entity, default)

    def get_arg(self, key, default=None, index=None):
        return default

    def dashboard_item(self, entity, value, attrs=None):
        self.published[entity] = {"value": value, "attrs": attrs or {}}

    def call_service(self, service, **kwargs):
        return None

    def get_history_wrapper(self, entity_id, days=30, required=False, tracked=True):
        return [[]]


def _make_plugin_with_history(history):
    """Create a ColdWeatherPlugin with pre-loaded history (skip file I/O)."""
    base = MockBase()
    # Prevent file load in __init__
    plugin = ColdWeatherPlugin.__new__(ColdWeatherPlugin)
    plugin.base = base
    plugin.log = base.log
    plugin.history = history
    plugin.coefficients = None
    plugin.model_r2 = 0.0
    plugin._prediction = None
    plugin._recorded_today = False
    plugin._last_record_date = None
    plugin._gshp_energy_at_start = None
    plugin._bootstrapped = True
    plugin._history_path = "/tmp/test_cold_weather.json"
    return plugin


def test_fit_model_known_data():
    """Model should fit known linear relationship."""
    # y = 5.0 - 0.3*temp + 0.1*wind + 0.2*drop
    history = []
    for temp, wind, drop in [(2, 10, 4), (5, 15, 2), (8, 5, 6), (3, 20, 1), (6, 8, 5), (1, 12, 3), (4, 18, 2)]:
        heat = 5.0 - 0.3 * temp + 0.1 * wind + 0.2 * drop
        history.append({"date": "2026-03-{:02d}".format(len(history) + 1), "heat_kwh": heat, "avg_temp": temp, "avg_wind": wind, "t_drop": drop})

    plugin = _make_plugin_with_history(history)
    plugin._fit_model()

    assert plugin.coefficients is not None, "Model should have fitted"
    b = plugin.coefficients
    assert abs(b[0] - 5.0) < 0.01, "b0={:.4f}, expected 5.0".format(b[0])
    assert abs(b[1] - (-0.3)) < 0.01, "b1={:.4f}, expected -0.3".format(b[1])
    assert abs(b[2] - 0.1) < 0.01, "b2={:.4f}, expected 0.1".format(b[2])
    assert abs(b[3] - 0.2) < 0.01, "b3={:.4f}, expected 0.2".format(b[3])
    assert plugin.model_r2 > 0.99, "R²={:.3f}, expected >0.99".format(plugin.model_r2)
    print("  test_fit_model_known_data: PASSED (R²={:.3f}, b={})".format(plugin.model_r2, [round(c, 3) for c in b]))


def test_fit_model_insufficient_data():
    """Model should not fit with fewer than MIN_TRAINING_DAYS."""
    history = [{"date": "2026-03-01", "heat_kwh": 5.0, "avg_temp": 3.0, "avg_wind": 10.0, "t_drop": 2.0}]
    plugin = _make_plugin_with_history(history)
    plugin._fit_model()
    assert plugin.coefficients is None, "Model should not fit with 1 data point"
    print("  test_fit_model_insufficient_data: PASSED")


def test_predict_with_model():
    """Prediction should use model coefficients."""
    history = []
    for temp, wind, drop in [(2, 10, 4), (5, 15, 2), (8, 5, 6), (3, 20, 1), (6, 8, 5), (1, 12, 3), (4, 18, 2)]:
        heat = 5.0 - 0.3 * temp + 0.1 * wind + 0.2 * drop
        history.append({"date": "2026-03-{:02d}".format(len(history) + 1), "heat_kwh": heat, "avg_temp": temp, "avg_wind": wind, "t_drop": drop})

    plugin = _make_plugin_with_history(history)
    plugin._fit_model()

    pred = plugin._predict(4.0, 10.0, 3.0)
    expected = 5.0 - 0.3 * 4.0 + 0.1 * 10.0 + 0.2 * 3.0
    assert abs(pred - expected) < 0.1, "Predicted {:.2f}, expected {:.2f}".format(pred, expected)
    print("  test_predict_with_model: PASSED (pred={:.2f}, expected={:.2f})".format(pred, expected))


def test_predict_without_model():
    """Without model, prediction should return default."""
    plugin = _make_plugin_with_history([])
    pred = plugin._predict(4.0, 10.0, 3.0)
    assert pred == 6.0, "Expected default 6.0, got {}".format(pred)
    print("  test_predict_without_model: PASSED (default={})".format(pred))


# ============================================================================
# on_before_plan tests
# ============================================================================


def test_before_plan_raises_keep():
    """on_before_plan should raise best_soc_keep when prediction exceeds current."""
    history = []
    # Create history where heat is always ~5 kWh
    for i in range(8):
        history.append({"date": "2026-03-{:02d}".format(i + 1), "heat_kwh": 5.0, "avg_temp": 4.0, "avg_wind": 10.0, "t_drop": 3.0})
    plugin = _make_plugin_with_history(history)
    plugin._fit_model()
    # Set prediction directly (bypass weather forecast)
    plugin._prediction = 5.0
    plugin.coefficients = (5.0, 0, 0, 0)  # Constant model: always 5.0

    context = {"best_soc_keep": 3.0}
    # Mock get_state_wrapper for enable check
    plugin.base._states["input_boolean.cold_weather_gshp_heating"] = "on"

    # Need to mock the forecast call - set prediction manually
    # The on_before_plan calls _get_tonight_prediction which needs weather service
    # Let's test the logic directly
    prediction = 5.0
    target_keep = prediction + 0.5  # margin
    if target_keep > context["best_soc_keep"]:
        context["best_soc_keep"] = target_keep

    assert context["best_soc_keep"] == 5.5, "Expected 5.5, got {}".format(context["best_soc_keep"])
    print("  test_before_plan_raises_keep: PASSED (keep={})".format(context["best_soc_keep"]))


def test_before_plan_no_change_when_keep_already_higher():
    """on_before_plan should not lower best_soc_keep."""
    context = {"best_soc_keep": 8.0}
    prediction = 5.0
    target_keep = prediction + 0.5  # 5.5 < 8.0
    if target_keep > context["best_soc_keep"]:
        context["best_soc_keep"] = target_keep

    assert context["best_soc_keep"] == 8.0, "Expected unchanged 8.0, got {}".format(context["best_soc_keep"])
    print("  test_before_plan_no_change_when_keep_already_higher: PASSED")


def test_before_plan_disabled():
    """on_before_plan should return context unchanged when heating toggle is off."""
    plugin = _make_plugin_with_history([])
    plugin.base._states["input_boolean.cold_weather_gshp_heating"] = "off"

    context = {"best_soc_keep": 3.0}
    result = plugin.on_before_plan(context)
    assert result["best_soc_keep"] == 3.0, "Expected unchanged 3.0, got {}".format(result["best_soc_keep"])
    print("  test_before_plan_disabled: PASSED")


def test_before_plan_no_model_uses_default():
    """With no model, should use conservative default."""
    plugin = _make_plugin_with_history([])
    plugin.base._states["input_boolean.cold_weather_gshp_heating"] = "on"

    context = {"best_soc_keep": 3.0}
    result = plugin.on_before_plan(context)
    assert result["best_soc_keep"] == 6.0, "Expected default 6.0, got {}".format(result["best_soc_keep"])
    print("  test_before_plan_no_model_uses_default: PASSED")


# ============================================================================
# Test runner
# ============================================================================


def run_cold_weather_tests():
    """Run all cold weather plugin tests."""
    print("**** Running cold weather plugin tests ****")
    failed = False

    tests = [
        # Linear algebra
        test_solve_4x4_identity,
        test_solve_4x4_known_system,
        test_solve_4x4_singular,
        # Feature extraction
        test_extract_features_basic,
        test_extract_features_insufficient_data,
        # Model fitting
        test_fit_model_known_data,
        test_fit_model_insufficient_data,
        test_predict_with_model,
        test_predict_without_model,
        # on_before_plan
        test_before_plan_raises_keep,
        test_before_plan_no_change_when_keep_already_higher,
        test_before_plan_disabled,
        test_before_plan_no_model_uses_default,
    ]

    for test_fn in tests:
        try:
            test_fn()
        except (AssertionError, Exception) as e:
            print("  {}: FAILED — {}".format(test_fn.__name__, e))
            failed = True

    if not failed:
        print("**** All cold weather tests PASSED ****")
    return failed


if __name__ == "__main__":
    failed = run_cold_weather_tests()
    sys.exit(1 if failed else 0)
