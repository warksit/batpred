# -----------------------------------------------------------------------------
# Predbat Home Battery System - Curtailment Calculator Tests
# Tests for the pure algorithm in curtailment_calc.py
# Validates against 6 real-world SMA CSV data files
#
# Run: cd apps/predbat && python3 tests/test_curtailment.py
# -----------------------------------------------------------------------------

import os
import re
import sys

# Ensure apps/predbat is on the path when run standalone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from curtailment_calc import compute_remaining_overflow, compute_target_soc, should_activate

# Battery constants (Mum's SIG system)
BATTERY_KWH = 18.08
MAX_CHARGE_KW = 5.5
MAX_DISCHARGE_KW = 5.5
DNO_LIMIT = 4.0
STEP_MINUTES = 5
START_SOC_PCT = 0.40

# CSV data directory (relative to this file)
CSV_DIR = os.path.join(os.path.dirname(__file__), "data", "curtailment")

# Validation days: (label, filename, watts_format, expected)
# Expected: (overflow_kwh_approx, dawn_target_pct_approx, curtailed_kwh_max, end_soc_pct_approx)
VALIDATION_DAYS = [
    ("Jun 19 — 65kWh peak", "Energy_Balance_2025_06_19.csv", True, {"overflow_approx": 13.0, "dawn_target_approx": 28, "end_soc_approx": 93}),
    ("Jul 12 — 68kWh peak", "Energy_Balance_2025_07_12.csv", False, {"overflow_approx": 15.5, "dawn_target_approx": 14, "end_soc_approx": 92}),
    ("Jul 9  — 45kWh cloudy", "Energy_Balance_2025_07_09.csv", False, {"overflow_approx": 3.6, "dawn_target_approx": 80, "end_soc_approx": 91}),
    ("Jul 15 — 23kWh poor", "Energy_Balance_2025_07_15.csv", False, {"overflow_approx": 0.0, "dawn_target_approx": 100, "end_soc_approx": 83}),
    ("Jul 1  — 39kWh moderate", "Energy_Balance_2025_07_01.csv", False, {"overflow_approx": 1.3, "dawn_target_approx": 93, "end_soc_approx": 87}),
    ("May 30 — 47kWh", "Energy_Balance_2025_05_30.csv", False, {"overflow_approx": 3.8, "dawn_target_approx": 79, "end_soc_approx": 92}),
]


def _parse_csv_value(val):
    """Parse a CSV value, handling quoted strings and comma decimals."""
    val = val.strip().strip('"')
    val = val.replace(",", "")
    try:
        return float(val)
    except ValueError:
        return 0.0


def _load_csv_to_forecasts(filepath, watts=False, step_minutes=STEP_MINUTES):
    """
    Load SMA 15-min CSV file and convert to minute-indexed forecast dicts.

    Returns:
        (pv_forecast, load_forecast) — dicts {minute_from_midnight: kW}
    """
    pv_forecast = {}
    load_forecast = {}

    with open(filepath, "r") as f:
        lines = f.readlines()

    for line in lines[1:]:
        parts = line.strip().split(";")
        if len(parts) < 7:
            continue

        # Parse time (format: "=""HH:MM""" or "HH:MM")
        match = re.search(r"(\d{2}):(\d{2})", parts[0])
        if not match:
            continue
        hour, minute = int(match.group(1)), int(match.group(2))
        end_minute = hour * 60 + minute  # This is end of the 15-min interval

        # Parse load (col 3) and PV (col 6)
        load_kw = _parse_csv_value(parts[3])
        pv_kw = _parse_csv_value(parts[6])
        if watts:
            load_kw /= 1000.0
            pv_kw /= 1000.0

        # Fill 5-minute steps for this 15-min interval
        # CSV time is interval END, so fill [end-15, end-10, end-5]
        start_minute = end_minute - 15
        for m in range(start_minute, end_minute, step_minutes):
            if m >= 0:
                pv_forecast[m] = pv_kw
                load_forecast[m] = load_kw

    return pv_forecast, load_forecast


def _simulate_day(pv_forecast, load_forecast, dno_limit=DNO_LIMIT, battery_kwh=BATTERY_KWH, max_charge_kw=MAX_CHARGE_KW, max_discharge_kw=MAX_DISCHARGE_KW, start_soc_pct=START_SOC_PCT, step_minutes=STEP_MINUTES):
    """
    Simulate a full day using the curtailment calc pure functions.

    Mirrors the logic from curtailment_model_v5.py but uses 5-minute steps
    and the pure function interface.

    Returns:
        dict with keys: results, total_curtailed, total_export, end_soc,
                        dawn_target_pct, max_export, initial_overflow
    """
    step_hours = step_minutes / 60.0
    soc = battery_kwh * start_soc_pct
    end_minute = 1440  # Full day

    # Compute initial (dawn) overflow — total for the whole day
    initial_overflow = compute_remaining_overflow(pv_forecast, load_forecast, dno_limit, start_minute=0, end_minute=end_minute, step_minutes=step_minutes)
    dawn_target = compute_target_soc(initial_overflow, battery_kwh)
    dawn_target_pct = dawn_target / battery_kwh * 100

    results = []
    total_curtailed = 0.0
    total_export = 0.0
    max_export_kw = 0.0

    for m in range(0, end_minute, step_minutes):
        pv = pv_forecast.get(m, 0.0)
        load = load_forecast.get(m, 0.0)
        excess = pv - load

        # Remaining overflow from NEXT step onward
        remaining = compute_remaining_overflow(pv_forecast, load_forecast, dno_limit, start_minute=m + step_minutes, end_minute=end_minute, step_minutes=step_minutes)
        target_soc = compute_target_soc(remaining, battery_kwh)

        # Battery constraints
        remaining_cap = max(0, battery_kwh - soc)
        max_charge_slot = min(max_charge_kw, remaining_cap / step_hours) if remaining_cap > 0.01 else 0
        available_soc = max(0, soc)
        max_discharge_slot = min(max_discharge_kw, available_soc / step_hours) if available_soc > 0.01 else 0

        soc_above_target = max(0, soc - target_soc)
        soc_below_target = max(0, target_soc - soc)

        export = 0.0
        curtailed = 0.0
        charge = 0.0
        discharge = 0.0

        if excess >= dno_limit:
            # HIGH PV: export DNO limit, absorb overflow into battery
            export = dno_limit
            overflow = excess - dno_limit
            charge = min(overflow, max_charge_slot)
            curtailed = max(0, overflow - charge)

        elif excess > 0:
            # MODERATE PV
            if soc > target_soc + 0.1:
                # Above target — drain battery toward target
                drain_wanted = min(soc_above_target / step_hours, max_discharge_kw)
                discharge = min(drain_wanted, max_discharge_slot)
                total_export_wanted = excess + discharge
                export = min(total_export_wanted, dno_limit)
                if excess + discharge > dno_limit:
                    discharge = max(0, dno_limit - excess)
                    export = dno_limit
                    leftover = excess - (export - discharge)
                    if leftover > 0:
                        charge = min(leftover, max_charge_slot)
            elif soc < target_soc - 0.1:
                # Below target — charge from PV toward target
                charge_wanted = min(soc_below_target / step_hours, max_charge_kw)
                charge = min(charge_wanted, excess, max_charge_slot)
                after_charge = excess - charge
                export = min(after_charge, dno_limit)
            else:
                # At target — export excess
                export = min(excess, dno_limit)

        else:
            # DEFICIT: battery covers load
            load_deficit = -excess
            discharge = min(load_deficit, max_discharge_slot)

        # Update SOC
        soc += charge * step_hours - discharge * step_hours
        soc = max(0, min(battery_kwh, soc))

        total_curtailed += curtailed * step_hours
        total_export += export * step_hours
        if export > max_export_kw:
            max_export_kw = export

        results.append(
            {
                "minute": m,
                "pv": pv,
                "load": load,
                "soc": soc,
                "soc_pct": soc / battery_kwh * 100,
                "target_soc": target_soc,
                "target_pct": target_soc / battery_kwh * 100,
                "export": export,
                "curtailed": curtailed,
                "charge": charge,
                "discharge": discharge,
            }
        )

    return {
        "results": results,
        "total_curtailed": total_curtailed,
        "total_export": total_export,
        "end_soc": soc,
        "end_soc_pct": soc / battery_kwh * 100,
        "dawn_target_pct": dawn_target_pct,
        "max_export_kw": max_export_kw,
        "initial_overflow": initial_overflow,
    }


# ============================================================================
# Pure function unit tests
# ============================================================================


def test_compute_remaining_overflow_basic():
    """Test overflow computation with simple known data."""
    # 3 steps of 5 minutes each. PV=8kW, load=1kW, excess=7kW, overflow=3kW per step
    # Overflow per step = (7 - 4) * 5/60 = 0.25 kWh
    pv = {0: 8.0, 5: 8.0, 10: 8.0}
    load = {0: 1.0, 5: 1.0, 10: 1.0}
    result = compute_remaining_overflow(pv, load, dno_limit=4.0, start_minute=0, end_minute=15, step_minutes=5)
    expected = 3 * (3.0 * 5 / 60)  # 3 steps × 0.25 kWh = 0.75 kWh
    assert abs(result - expected) < 0.001, f"Expected {expected}, got {result}"
    print("  test_compute_remaining_overflow_basic: PASSED")


def test_compute_remaining_overflow_no_overflow():
    """No overflow when excess < DNO limit."""
    pv = {0: 3.0, 5: 3.0, 10: 3.0}
    load = {0: 1.0, 5: 1.0, 10: 1.0}
    result = compute_remaining_overflow(pv, load, dno_limit=4.0, start_minute=0, end_minute=15, step_minutes=5)
    assert result == 0.0, f"Expected 0.0, got {result}"
    print("  test_compute_remaining_overflow_no_overflow: PASSED")


def test_compute_remaining_overflow_partial():
    """Mixed slots: some with overflow, some without."""
    pv = {0: 8.0, 5: 2.0, 10: 6.0}  # excess: 7, 1, 5
    load = {0: 1.0, 5: 1.0, 10: 1.0}
    result = compute_remaining_overflow(pv, load, dno_limit=4.0, start_minute=0, end_minute=15, step_minutes=5)
    # Slot 0: overflow = (7-4)*5/60 = 0.25
    # Slot 5: overflow = 0 (excess 1 < 4)
    # Slot 10: overflow = (5-4)*5/60 = 0.0833
    expected = 0.25 + 0.0 + 1.0 * 5 / 60
    assert abs(result - expected) < 0.001, f"Expected {expected:.4f}, got {result:.4f}"
    print("  test_compute_remaining_overflow_partial: PASSED")


def test_compute_remaining_overflow_start_offset():
    """Verify start_minute skips earlier slots."""
    pv = {0: 8.0, 5: 8.0, 10: 8.0}
    load = {0: 1.0, 5: 1.0, 10: 1.0}
    # Starting from minute 5, should only include 2 slots
    result = compute_remaining_overflow(pv, load, dno_limit=4.0, start_minute=5, end_minute=15, step_minutes=5)
    expected = 2 * (3.0 * 5 / 60)  # 0.5 kWh
    assert abs(result - expected) < 0.001, f"Expected {expected}, got {result}"
    print("  test_compute_remaining_overflow_start_offset: PASSED")


def test_compute_target_soc():
    """Test target SOC computation."""
    # No overflow → target = 100%
    assert compute_target_soc(0.0, 18.08) == 18.08

    # 5 kWh overflow → target = 13.08 kWh
    assert abs(compute_target_soc(5.0, 18.08) - 13.08) < 0.001

    # Overflow > battery → target = 0
    assert compute_target_soc(20.0, 18.08) == 0.0

    # Negative overflow (shouldn't happen but handle gracefully)
    assert compute_target_soc(-1.0, 18.08) == 18.08

    print("  test_compute_target_soc: PASSED")


def test_should_activate():
    """Test activation logic."""
    assert should_activate(0.0) is False
    assert should_activate(0.001) is True
    assert should_activate(10.0) is True
    print("  test_should_activate: PASSED")


# ============================================================================
# Edge case tests (synthetic data)
# ============================================================================


def test_no_overflow_day():
    """Low PV day — plugin stays inactive, target = 100%."""
    # PV never exceeds load + DNO limit (4kW)
    pv = {}
    load = {}
    for m in range(0, 1440, STEP_MINUTES):
        hour = m / 60
        if 6 <= hour <= 18:
            pv[m] = 3.0  # 3kW PV — modest
        else:
            pv[m] = 0.0
        load[m] = 0.5

    overflow = compute_remaining_overflow(pv, load, DNO_LIMIT, 0, 1440, STEP_MINUTES)
    assert overflow == 0.0, f"Expected no overflow, got {overflow}"
    assert should_activate(overflow) is False

    sim = _simulate_day(pv, load)
    assert sim["total_curtailed"] < 0.01, f"Expected no curtailment, got {sim['total_curtailed']:.3f}"
    assert sim["dawn_target_pct"] > 99.9, f"Expected ~100% target, got {sim['dawn_target_pct']:.1f}%"
    print("  test_no_overflow_day: PASSED")


def test_battery_starts_full():
    """Battery at 100% with moderate PV — overnight load drains battery to make room."""
    # PV=6kW, load=1kW, excess=5kW, overflow=(5-4)=1kW per slot
    # Total overflow ~8kWh. Overnight load (8h × 1kW) drains 8kWh from full battery.
    # Battery at dawn: 10kWh ≈ target. Zero curtailment achievable.
    pv = {}
    load = {}
    for m in range(0, 1440, STEP_MINUTES):
        hour = m / 60
        if 8 <= hour <= 16:
            pv[m] = 6.0  # Moderate PV — 1kW overflow above DNO
        else:
            pv[m] = 0.0
        load[m] = 1.0

    sim = _simulate_day(pv, load, start_soc_pct=1.0)
    # Allow tiny curtailment (<0.1 kWh) from 5-min step boundary effects
    assert sim["total_curtailed"] < 0.1, f"Curtailment: {sim['total_curtailed']:.3f} kWh"
    assert sim["max_export_kw"] <= DNO_LIMIT + 0.01, f"Max export {sim['max_export_kw']:.2f} exceeds DNO limit"
    assert sim["dawn_target_pct"] < 100, f"Expected target < 100%, got {sim['dawn_target_pct']:.1f}%"
    print("  test_battery_starts_full: PASSED")


def test_battery_starts_empty():
    """Battery at 0% with moderate PV — charges from PV, absorbs overflow."""
    pv = {}
    load = {}
    for m in range(0, 1440, STEP_MINUTES):
        hour = m / 60
        if 8 <= hour <= 16:
            pv[m] = 6.0
        else:
            pv[m] = 0.0
        load[m] = 1.0

    sim = _simulate_day(pv, load, start_soc_pct=0.0)
    assert sim["total_curtailed"] < 0.01, f"Curtailment: {sim['total_curtailed']:.3f} kWh"
    assert sim["max_export_kw"] <= DNO_LIMIT + 0.01, f"Max export {sim['max_export_kw']:.2f} exceeds DNO limit"
    print("  test_battery_starts_empty: PASSED")


def test_export_never_exceeds_dno():
    """Extreme PV — export must never exceed DNO limit even with full battery."""
    pv = {}
    load = {}
    for m in range(0, 1440, STEP_MINUTES):
        hour = m / 60
        if 10 <= hour <= 14:
            pv[m] = 15.0  # Extreme PV
        else:
            pv[m] = 0.0
        load[m] = 0.5

    sim = _simulate_day(pv, load, start_soc_pct=0.5)
    for r in sim["results"]:
        assert r["export"] <= DNO_LIMIT + 0.01, f"Minute {r['minute']}: export {r['export']:.2f} exceeds DNO limit {DNO_LIMIT}"
    print("  test_export_never_exceeds_dno: PASSED")


# ============================================================================
# CSV validation tests (real-world data)
# ============================================================================


def _run_csv_day_test(label, filename, watts, expected):
    """Run a single day validation test from CSV data."""
    filepath = os.path.join(CSV_DIR, filename)
    if not os.path.exists(filepath):
        print(f"  {label}: SKIPPED (CSV not found: {filepath})")
        return False

    pv_forecast, load_forecast = _load_csv_to_forecasts(filepath, watts=watts)
    sim = _simulate_day(pv_forecast, load_forecast)

    errors = []

    # 1. Zero curtailment (the main goal)
    if sim["total_curtailed"] > 0.01:
        errors.append(f"curtailment={sim['total_curtailed']:.3f} kWh (expected ~0)")

    # 2. Export never exceeds DNO limit
    for r in sim["results"]:
        if r["export"] > DNO_LIMIT + 0.01:
            errors.append(f"minute {r['minute']}: export={r['export']:.2f}kW > DNO {DNO_LIMIT}kW")
            break

    # 3. Dawn target SOC (±10% tolerance due to 5-min vs 30-min resolution)
    if abs(sim["dawn_target_pct"] - expected["dawn_target_approx"]) > 10:
        errors.append(f"dawn target={sim['dawn_target_pct']:.0f}% " f"(expected ~{expected['dawn_target_approx']}%)")

    # 4. End SOC (±10% tolerance)
    if abs(sim["end_soc_pct"] - expected["end_soc_approx"]) > 10:
        errors.append(f"end SOC={sim['end_soc_pct']:.0f}% " f"(expected ~{expected['end_soc_approx']}%)")

    # 5. Overflow kWh (±3 kWh tolerance for 5-min vs 30-min resolution)
    if abs(sim["initial_overflow"] - expected["overflow_approx"]) > 3:
        errors.append(f"overflow={sim['initial_overflow']:.1f} kWh " f"(expected ~{expected['overflow_approx']})")

    if errors:
        detail = "; ".join(errors)
        print(f"  {label}: FAILED — {detail}")
        print(f"    overflow={sim['initial_overflow']:.1f}kWh dawn_target={sim['dawn_target_pct']:.0f}% " f"curtailed={sim['total_curtailed']:.3f}kWh end_soc={sim['end_soc_pct']:.0f}%")
        return True  # failed

    print(f"  {label}: PASSED " f"(overflow={sim['initial_overflow']:.1f}kWh target={sim['dawn_target_pct']:.0f}% " f"curtailed={sim['total_curtailed']:.3f}kWh end_soc={sim['end_soc_pct']:.0f}%)")
    return False  # passed


# ============================================================================
# Plugin integration tests (on_update deferral logic)
# ============================================================================

from curtailment_plugin import CurtailmentPlugin, PREDICT_STEP as PLUGIN_STEP


class MockBase:
    """Minimal mock of Predbat base for plugin tests."""

    def __init__(self, pv_step=None, load_step=None, soc_kw=5.0, soc_max=18.08,
                 minutes_now=720, forecast_minutes=1440, charge_window_best=None,
                 charge_limit_best=None, reserve_percent=4):
        step_kwh_factor = PLUGIN_STEP / 60.0  # kW→kWh per step
        # Store as kWh per step (matching Predbat convention)
        self.pv_forecast_minute_step = {k: v * step_kwh_factor for k, v in (pv_step or {}).items()}
        self.load_minutes_step = {k: v * step_kwh_factor for k, v in (load_step or {}).items()}
        self.soc_kw = soc_kw
        self.soc_max = soc_max
        self.minutes_now = minutes_now
        self.forecast_minutes = forecast_minutes
        self.charge_window_best = charge_window_best or []
        self.charge_limit_best = charge_limit_best or []
        self.reserve_percent = reserve_percent
        self.set_read_only = False
        self.config_index = {}
        self.prefix = "predbat"
        self.logs = []
        self.published = {}
        self.services = []

    def log(self, msg, *args, **kwargs):
        self.logs.append(msg)

    def get_state_wrapper(self, entity, default=None):
        if entity == "input_boolean.curtailment_manager_enable":
            return "on"
        if entity == "input_number.curtailment_manager_buffer":
            return 1.0
        return default

    def get_arg(self, key, default=None, index=None):
        if key == "export_limit":
            return 4000
        return default

    def dashboard_item(self, entity, value, attrs=None):
        self.published[entity] = {"value": value, "attrs": attrs or {}}

    def call_service_wrapper(self, service, **kwargs):
        self.services.append((service, kwargs))

    def in_charge_window(self, charge_window, minute_abs):
        for i, window in enumerate(charge_window):
            if window["start"] <= minute_abs < window["end"]:
                return i
        return -1

    def is_freeze_charge(self, charge_limit_kwh):
        # Freeze = charge limit equals reserve (simplified)
        limit_pct = round(charge_limit_kwh / self.soc_max * 100)
        return limit_pct == self.reserve_percent


def _make_overflow_pv(minutes_now=720):
    """Create PV/load forecasts that produce overflow (PV=8kW, load=1kW, excess=7kW > DNO 4kW)."""
    pv = {}
    load = {}
    # Fill future slots with overflow-producing values
    for m in range(0, 1440 - minutes_now, PLUGIN_STEP):
        pv[m] = 8.0
        load[m] = 1.0
    return pv, load


def test_plugin_pv_below_threshold_blocks_activation():
    """Plugin should stay off when PV < 0.1kW even if overflow is predicted."""
    pv, load = _make_overflow_pv(minutes_now=300)  # 5am
    # Set current PV (minute 0) to near-zero (pre-dawn)
    pv[0] = 0.0
    base = MockBase(pv_step=pv, load_step=load, soc_kw=5.0, minutes_now=300)
    plugin = CurtailmentPlugin(base)

    plugin.on_update()

    # Phase should be "off" — plugin doesn't control inverter
    phase_sensor = base.published.get("sensor.predbat_curtailment_phase", {})
    assert phase_sensor.get("value") == "off", \
        f"Expected phase='off' with no PV, got '{phase_sensor.get('value')}'"
    # read_only should NOT be set
    assert base.set_read_only is False, "read_only should be False when plugin is off"
    # But overflow sensor should still show the calculated overflow
    overflow_sensor = base.published.get("sensor.predbat_curtailment_overflow_kwh", {})
    # Overflow is published as the raw calculated value (not zeroed out)
    # The target SOC sensor should show a value < 100% indicating overflow exists
    target_sensor = base.published.get("sensor.predbat_curtailment_target_soc", {})
    assert target_sensor.get("value") is not None, "Target SOC sensor should be published"
    print("  test_plugin_pv_below_threshold_blocks_activation: PASSED")


def test_plugin_pv_above_threshold_allows_activation():
    """Plugin should activate normally when PV >= 0.1kW and overflow exists."""
    pv, load = _make_overflow_pv(minutes_now=720)
    # Current PV is already 8kW (well above threshold)
    base = MockBase(pv_step=pv, load_step=load, soc_kw=5.0, minutes_now=720)
    plugin = CurtailmentPlugin(base)

    plugin.on_update()

    phase_sensor = base.published.get("sensor.predbat_curtailment_phase", {})
    assert phase_sensor.get("value") != "off", \
        f"Expected active phase with high PV, got '{phase_sensor.get('value')}'"
    assert base.set_read_only is True, "read_only should be True when plugin is active"
    print("  test_plugin_pv_above_threshold_allows_activation: PASSED")


def test_plugin_defers_to_charge_window():
    """Plugin should defer to Predbat during a planned grid charge window."""
    pv, load = _make_overflow_pv(minutes_now=300)
    # PV is generating (above threshold)
    pv[0] = 2.0
    base = MockBase(
        pv_step=pv, load_step=load, soc_kw=5.0, minutes_now=300,
        # Charge window active right now: 4am-7am (240-420)
        charge_window_best=[{"start": 240, "end": 420}],
        charge_limit_best=[10.0],  # 10kWh target — real charge, not freeze
    )
    plugin = CurtailmentPlugin(base)

    plugin.on_update()

    phase_sensor = base.published.get("sensor.predbat_curtailment_phase", {})
    assert phase_sensor.get("value") == "off", \
        f"Expected phase='off' during charge window, got '{phase_sensor.get('value')}'"
    assert base.set_read_only is False, "read_only should be False when deferring to charge window"
    print("  test_plugin_defers_to_charge_window: PASSED")


def test_plugin_ignores_freeze_charge_window():
    """Plugin should NOT defer to a freeze charge window (freeze = hold at reserve)."""
    pv, load = _make_overflow_pv(minutes_now=720)
    # Freeze charge: limit = reserve SOC (4% of 18.08 = 0.7232 kWh)
    reserve_kwh = 18.08 * 4 / 100  # 0.7232
    base = MockBase(
        pv_step=pv, load_step=load, soc_kw=5.0, minutes_now=720,
        charge_window_best=[{"start": 700, "end": 800}],
        charge_limit_best=[reserve_kwh],
        reserve_percent=4,
    )
    plugin = CurtailmentPlugin(base)

    plugin.on_update()

    phase_sensor = base.published.get("sensor.predbat_curtailment_phase", {})
    assert phase_sensor.get("value") != "off", \
        f"Expected active phase during freeze window, got '{phase_sensor.get('value')}'"
    print("  test_plugin_ignores_freeze_charge_window: PASSED")


def test_plugin_no_charge_window_activates_normally():
    """Plugin activates when no charge window is active."""
    pv, load = _make_overflow_pv(minutes_now=720)
    base = MockBase(
        pv_step=pv, load_step=load, soc_kw=5.0, minutes_now=720,
        # Charge window exists but not active right now (starts later)
        charge_window_best=[{"start": 1400, "end": 1440}],
        charge_limit_best=[10.0],
    )
    plugin = CurtailmentPlugin(base)

    plugin.on_update()

    phase_sensor = base.published.get("sensor.predbat_curtailment_phase", {})
    assert phase_sensor.get("value") != "off", \
        f"Expected active phase outside charge window, got '{phase_sensor.get('value')}'"
    print("  test_plugin_no_charge_window_activates_normally: PASSED")


# ============================================================================
# Test runner
# ============================================================================


def run_curtailment_tests(my_predbat=None):
    """Run all curtailment calculator tests. Returns True if any failed."""
    print("**** Running curtailment calculator tests ****")
    failed = False

    # All tests to run
    tests = [
        # Pure function tests
        test_compute_remaining_overflow_basic,
        test_compute_remaining_overflow_no_overflow,
        test_compute_remaining_overflow_partial,
        test_compute_remaining_overflow_start_offset,
        test_compute_target_soc,
        test_should_activate,
        # Edge case tests (synthetic data)
        test_no_overflow_day,
        test_battery_starts_full,
        test_battery_starts_empty,
        test_export_never_exceeds_dno,
        # Plugin integration tests
        test_plugin_pv_below_threshold_blocks_activation,
        test_plugin_pv_above_threshold_allows_activation,
        test_plugin_defers_to_charge_window,
        test_plugin_ignores_freeze_charge_window,
        test_plugin_no_charge_window_activates_normally,
    ]

    for test_fn in tests:
        try:
            test_fn()
        except AssertionError as e:
            print(f"  {test_fn.__name__}: FAILED — {e}")
            failed = True

    # CSV validation tests (real-world data)
    csv_available = os.path.exists(CSV_DIR)
    if csv_available:
        for label, filename, watts, expected in VALIDATION_DAYS:
            day_failed = _run_csv_day_test(label, filename, watts, expected)
            if day_failed:
                failed = True
    else:
        print(f"  CSV validation tests: SKIPPED (directory not found: {CSV_DIR})")

    if not failed:
        print("**** All curtailment tests PASSED ****")
    return failed


if __name__ == "__main__":
    sys.exit(1 if run_curtailment_tests() else 0)
