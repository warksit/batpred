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
        self.soc_max = 18.08
        self.all_active_keep = {}
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
    plugin._morning_min_soc = 999.0
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
# on_before_plan tests (alert_keep injection)
# ============================================================================


def test_before_plan_injects_alert_keep():
    """on_before_plan should inject tapered alert_keep during cheap rate only."""
    plugin = _make_plugin_with_history([])
    plugin.base._states["input_boolean.cold_weather_gshp_heating"] = "on"
    plugin.base.minutes_now = 720  # Noon → target tomorrow
    plugin.base.all_active_keep = {}

    # No model → uses DEFAULT_KEEP_KWH=6.0
    context = {"best_soc_keep": 2.0}
    result = plugin.on_before_plan(context)

    # Should inject for tomorrow: (24+4)*60=1680 to (24+7)*60=1860 (cheap rate only)
    max_pct = (6.0 + 0.5) / 18.08 * 100.0  # ~35.9%
    assert len(plugin.base.all_active_keep) == 180, "Expected 180 entries (04:00-07:00), got {}".format(len(plugin.base.all_active_keep))
    assert 1680 in plugin.base.all_active_keep, "Expected minute 1680 (04:00 tomorrow)"
    assert 1859 in plugin.base.all_active_keep, "Expected minute 1859 (06:59 tomorrow)"
    assert 1860 not in plugin.base.all_active_keep, "Should not include minute 1860 (07:00)"
    # Start of window: full keep
    assert abs(plugin.base.all_active_keep[1680] - max_pct) < 0.1, "Start: expected {:.1f}%, got {:.1f}%".format(max_pct, plugin.base.all_active_keep[1680])
    # 06:00 (1800): remaining=(8-6)/(8-4)=0.5 → 50% of keep
    assert abs(plugin.base.all_active_keep[1800] - max_pct * 0.5) < 0.2, "06:00: expected {:.1f}%, got {:.1f}%".format(max_pct * 0.5, plugin.base.all_active_keep[1800])
    # 06:59 (1859): remaining=(8-6.98)/(8-4)≈0.255 → ~25% of keep
    assert abs(plugin.base.all_active_keep[1859] - max_pct * 0.25) < 1.0, "06:59: expected ~{:.1f}%, got {:.1f}%".format(max_pct * 0.25, plugin.base.all_active_keep[1859])
    # best_soc_keep should NOT be boosted
    assert result["best_soc_keep"] == 2.0, "best_soc_keep should be unchanged, got {}".format(result["best_soc_keep"])
    print("  test_before_plan_injects_alert_keep: PASSED (keep_pct={:.1f}%->{:.1f}%, entries={})".format(max_pct, plugin.base.all_active_keep[1859], len(plugin.base.all_active_keep)))


def test_before_plan_morning_targets_today():
    """Before noon, should target today's cheap rate window (04:00-07:00)."""
    plugin = _make_plugin_with_history([])
    plugin.base._states["input_boolean.cold_weather_gshp_heating"] = "on"
    plugin.base.minutes_now = 180  # 03:00 → target today
    plugin.base.all_active_keep = {}

    context = {"best_soc_keep": 2.0}
    plugin.on_before_plan(context)

    # Today's window: 4*60=240 to 7*60=420 (cheap rate only)
    assert 240 in plugin.base.all_active_keep, "Expected minute 240 (04:00 today)"
    assert 419 in plugin.base.all_active_keep, "Expected minute 419 (06:59 today)"
    assert 420 not in plugin.base.all_active_keep, "Should not include minute 420 (07:00)"
    assert 1680 not in plugin.base.all_active_keep, "Should not have tomorrow's entries"
    print("  test_before_plan_morning_targets_today: PASSED")


def test_before_plan_respects_existing_keep():
    """Should not lower existing all_active_keep entries."""
    plugin = _make_plugin_with_history([])
    plugin.base._states["input_boolean.cold_weather_gshp_heating"] = "on"
    plugin.base.minutes_now = 180  # 03:00
    plugin.base.all_active_keep = {240: 99.0}  # Existing high keep at 04:00

    context = {"best_soc_keep": 2.0}
    plugin.on_before_plan(context)

    assert plugin.base.all_active_keep[240] == 99.0, "Should not lower existing 99%, got {}".format(plugin.base.all_active_keep[240])
    print("  test_before_plan_respects_existing_keep: PASSED")


def test_before_plan_below_threshold():
    """Should not inject when prediction is below MIN_KEEP_KWH."""
    plugin = _make_plugin_with_history([])
    plugin.coefficients = (2.0, 0, 0, 0)  # Constant model: always 2.0 kWh
    plugin.base._states["input_boolean.cold_weather_gshp_heating"] = "on"
    plugin.base.minutes_now = 720
    plugin.base.all_active_keep = {}

    # Mock _get_tonight_prediction to return 2.0 (below MIN_KEEP_KWH=3.0)
    plugin._prediction = 2.0
    original_method = plugin._get_tonight_prediction
    plugin._get_tonight_prediction = lambda: 2.0

    context = {"best_soc_keep": 2.0}
    result = plugin.on_before_plan(context)

    assert len(plugin.base.all_active_keep) == 0, "Should not inject below threshold, got {} entries".format(len(plugin.base.all_active_keep))
    plugin._get_tonight_prediction = original_method
    print("  test_before_plan_below_threshold: PASSED")


def test_before_plan_disabled():
    """on_before_plan should return context unchanged when heating toggle is off."""
    plugin = _make_plugin_with_history([])
    plugin.base._states["input_boolean.cold_weather_gshp_heating"] = "off"
    plugin.base.all_active_keep = {}

    context = {"best_soc_keep": 3.0}
    result = plugin.on_before_plan(context)
    assert result["best_soc_keep"] == 3.0, "Expected unchanged 3.0, got {}".format(result["best_soc_keep"])
    assert len(plugin.base.all_active_keep) == 0, "Should not inject when disabled"
    print("  test_before_plan_disabled: PASSED")


def test_before_plan_with_model_prediction():
    """With a trained model, should inject during cheap rate with GSHP-window taper."""
    plugin = _make_plugin_with_history([])
    plugin.coefficients = (5.0, 0, 0, 0)  # Constant model: always 5.0 kWh
    plugin.base._states["input_boolean.cold_weather_gshp_heating"] = "on"
    plugin.base.minutes_now = 720
    plugin.base.all_active_keep = {}

    # Mock prediction
    plugin._get_tonight_prediction = lambda: 5.0

    context = {"best_soc_keep": 2.0}
    plugin.on_before_plan(context)

    max_pct = (5.0 + 0.5) / 18.08 * 100.0  # ~30.4%
    assert len(plugin.base.all_active_keep) == 180, "Expected 180 entries (04:00-07:00)"
    # Start: full keep
    assert abs(plugin.base.all_active_keep[1680] - max_pct) < 0.1, "Start: expected {:.1f}%, got {:.1f}%".format(max_pct, plugin.base.all_active_keep[1680])
    # 06:59 (1859): remaining=(8-6.98)/(8-4)≈0.255 → ~25% of keep
    assert abs(plugin.base.all_active_keep[1859] - max_pct * 0.25) < 1.0, "06:59: expected ~{:.1f}%, got {:.1f}%".format(max_pct * 0.25, plugin.base.all_active_keep[1859])
    # 07:00 should not exist
    assert 1860 not in plugin.base.all_active_keep, "Should not inject at 07:00"
    # best_soc_keep unchanged
    assert context["best_soc_keep"] == 2.0, "best_soc_keep should be unchanged"
    print("  test_before_plan_with_model_prediction: PASSED (keep={:.1f}%->{:.1f}%)".format(max_pct, plugin.base.all_active_keep[1859]))


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
        # on_before_plan (alert_keep injection)
        test_before_plan_injects_alert_keep,
        test_before_plan_morning_targets_today,
        test_before_plan_respects_existing_keep,
        test_before_plan_below_threshold,
        test_before_plan_disabled,
        test_before_plan_with_model_prediction,
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
