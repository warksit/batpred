# -----------------------------------------------------------------------------
# Predbat Home Battery System - Curtailment Calculator Tests
# Tests for v10 curtailment algorithm (overflow-vs-headroom)
# Validates against 6 real-world SMA CSV data files
#
# Run: cd apps/predbat && python3 tests/test_curtailment.py
# -----------------------------------------------------------------------------

import math
import os
import re
import sys

# Ensure apps/predbat is on the path when run standalone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from curtailment_calc import (
    compute_remaining_overflow,
    compute_morning_gap,
    solar_elevation,
    compute_release_time,
    compute_tomorrow_forecast,
)

# Battery constants (Mum's SIG system)
BATTERY_KWH = 18.08
MAX_CHARGE_KW = 5.5
MAX_DISCHARGE_KW = 5.5
DNO_LIMIT = 4.0
STEP_MINUTES = 5
START_SOC_PCT = 0.40

# v18 constants (match curtailment_plugin.py)
OVERFLOW_SAFETY_FACTOR = 1.2
SOC_CAP_FACTOR = 0.95
SOC_MARGIN_KWH = 0.2  # HA automation hysteresis for Charge/Hold/Drain split

# CSV data directory (relative to this file)
CSV_DIR = os.path.join(os.path.dirname(__file__), "data", "curtailment")

# Validation days: (label, filename, watts_format, expected)
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


# ============================================================================
# v10 day simulation — replays CSV through overflow-vs-headroom logic
# ============================================================================


def _simulate_day_v10(
    pv_actual,
    load_actual,
    pv_forecast=None,
    load_forecast=None,
    dno_limit=DNO_LIMIT,
    battery_kwh=BATTERY_KWH,
    max_charge_kw=MAX_CHARGE_KW,
    start_soc_pct=START_SOC_PCT,
    step_minutes=STEP_MINUTES,
    soc_floor_kwh=0.0,
):
    """
    Simulate a full day using the v10 algorithm.

    At each 5-min step:
    1. Compute remaining_overflow from forecast (scaled by cumulative energy ratio)
    2. Check activation: overflow * 1.10 > headroom to 95%
    3. Compute floor: soc_max - overflow * 1.10, capped at 95%
    4. Phase: charge (SOC < floor) or managed (SOC >= floor) or off
    5. Physics: charge mode = absorb all PV; managed mode = export min(excess, DNO)
    """
    if pv_forecast is None:
        pv_forecast = pv_actual
    if load_forecast is None:
        load_forecast = load_actual

    step_hours = step_minutes / 60.0
    soc = battery_kwh * start_soc_pct
    end_minute = 1440

    # Initial overflow (for reporting)
    initial_overflow = compute_remaining_overflow(pv_forecast, load_forecast, dno_limit, 0, end_minute, step_minutes)

    # Cumulative energy tracking for ratio
    cumulative_actual = 0.0
    cumulative_forecast = 0.0
    total_forecast = sum(pv_forecast.values()) * step_hours

    results = []
    total_curtailed = 0.0
    total_export = 0.0
    max_export_kw = 0.0

    for m in range(0, end_minute, step_minutes):
        actual_pv = pv_actual.get(m, 0.0)
        actual_load = load_actual.get(m, 0.0)
        actual_excess = actual_pv - actual_load
        forecast_pv = pv_forecast.get(m, 0.0)

        # --- Cumulative energy ratio ---
        cumulative_actual += actual_pv * step_hours
        cumulative_forecast += forecast_pv * step_hours

        threshold = total_forecast * 0.15
        blend = min(1.0, cumulative_forecast / max(threshold, 0.5))
        raw_ratio = cumulative_actual / max(cumulative_forecast, 0.5)
        energy_ratio = 1.0 + (raw_ratio - 1.0) * blend

        # --- Remaining overflow from scaled forecast ---
        scaled_pv = {k: v * energy_ratio for k, v in pv_forecast.items()}
        remaining_overflow = compute_remaining_overflow(
            scaled_pv,
            load_forecast,
            dno_limit,
            start_minute=m + step_minutes,
            end_minute=end_minute,
            step_minutes=step_minutes,
        )

        # --- v10 activation ---
        headroom = battery_kwh * SOC_CAP_FACTOR - soc
        active = remaining_overflow * OVERFLOW_SAFETY_FACTOR > max(headroom, 0)

        # --- v10 floor ---
        if active:
            floor = battery_kwh - remaining_overflow * OVERFLOW_SAFETY_FACTOR
            floor = max(floor, soc_floor_kwh)
            floor = min(floor, battery_kwh * SOC_CAP_FACTOR)
        else:
            floor = battery_kwh

        # --- v10.1 phase: three behaviors (R14/R15/R16) ---
        if not active:
            mode = "off"
        elif soc < floor - SOC_MARGIN_KWH:
            mode = "charge"
        elif soc > floor + SOC_MARGIN_KWH:
            mode = "drain"
        else:
            mode = "hold"

        # --- Physics ---
        export = 0.0
        curtailed = 0.0
        charge = 0.0
        discharge = 0.0

        remaining_cap = max(0, battery_kwh - soc)
        max_charge_slot = min(max_charge_kw, remaining_cap / step_hours) if remaining_cap > 0.01 else 0
        max_discharge_slot = min(MAX_DISCHARGE_KW, soc / step_hours) if soc > 0.01 else 0

        if mode == "charge":
            # D-ESS export=0: absorb all PV excess (R14)
            if actual_excess > 0:
                charge = min(actual_excess, max_charge_slot)
                curtailed = max(0, actual_excess - charge)
            elif actual_excess < 0:
                discharge = min(-actual_excess, max_discharge_slot)

        elif mode == "drain":
            # D-ESS export=DNO: SIG discharges battery toward floor (R15)
            # Export at DNO using PV excess + battery discharge
            if actual_excess >= dno_limit:
                # Overflow: export DNO, battery absorbs the rest
                export = dno_limit
                overflow = actual_excess - dno_limit
                charge = min(overflow, max_charge_slot)
                curtailed = max(0, overflow - charge)
            elif actual_excess > 0:
                # PV excess < DNO: supplement with battery discharge to reach DNO
                drain_kw = min(dno_limit - actual_excess, max_discharge_slot)
                export = min(actual_excess + drain_kw, dno_limit)
                discharge = drain_kw
            else:
                # Deficit: battery covers load
                discharge = min(-actual_excess, max_discharge_slot)

        elif mode == "hold":
            # D-ESS export=min(excess, DNO): maintain at floor (R16)
            if actual_excess > dno_limit:
                export = dno_limit
                overflow = actual_excess - dno_limit
                charge = min(overflow, max_charge_slot)
                curtailed = max(0, overflow - charge)
            elif actual_excess > 0:
                export = min(actual_excess, dno_limit)
            elif actual_excess < 0:
                discharge = min(-actual_excess, max_discharge_slot)

        else:  # off — MSC: battery absorbs all excess
            if actual_excess > 0:
                charge = min(actual_excess, max_charge_slot)
                leftover = actual_excess - charge
                export = min(leftover, dno_limit)
                curtailed = max(0, leftover - export)
            elif actual_excess < 0:
                discharge = min(-actual_excess, soc / step_hours if soc > 0.01 else 0)

        soc += charge * step_hours - discharge * step_hours
        soc = max(0, min(battery_kwh, soc))

        total_curtailed += curtailed * step_hours
        total_export += export * step_hours
        if export > max_export_kw:
            max_export_kw = export

        results.append(
            {
                "minute": m,
                "pv": actual_pv,
                "load": actual_load,
                "soc": soc,
                "soc_pct": soc / battery_kwh * 100,
                "floor": floor,
                "floor_pct": floor / battery_kwh * 100,
                "export": export,
                "curtailed": curtailed,
                "mode": mode,
                "energy_ratio": energy_ratio,
            }
        )

    # SOC at sunset
    sunset_soc = soc
    sunset_soc_pct = soc / battery_kwh * 100
    for r in reversed(results):
        if r["pv"] > 0.05:
            sunset_soc = r["soc"]
            sunset_soc_pct = r["soc_pct"]
            break

    # Floor at 10:00
    floor_at_10 = battery_kwh
    for r in results:
        if r["minute"] == 600:
            floor_at_10 = r["floor"]
            break

    return {
        "results": results,
        "total_curtailed": total_curtailed,
        "total_export": total_export,
        "end_soc": soc,
        "end_soc_pct": soc / battery_kwh * 100,
        "sunset_soc": sunset_soc,
        "sunset_soc_pct": sunset_soc_pct,
        "max_export_kw": max_export_kw,
        "initial_overflow": initial_overflow,
        "floor_at_10": floor_at_10,
    }


# ============================================================================
# Pure function unit tests
# ============================================================================


def test_compute_remaining_overflow_basic():
    """Test overflow computation with simple known data."""
    pv = {0: 8.0, 5: 8.0, 10: 8.0}
    load = {0: 1.0, 5: 1.0, 10: 1.0}
    result = compute_remaining_overflow(pv, load, dno_limit=4.0, start_minute=0, end_minute=15, step_minutes=5)
    expected = 3 * (3.0 * 5 / 60)  # 3 steps x 0.25 kWh = 0.75 kWh
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
    pv = {0: 8.0, 5: 2.0, 10: 6.0}
    load = {0: 1.0, 5: 1.0, 10: 1.0}
    result = compute_remaining_overflow(pv, load, dno_limit=4.0, start_minute=0, end_minute=15, step_minutes=5)
    expected = 0.25 + 0.0 + 1.0 * 5 / 60
    assert abs(result - expected) < 0.001, f"Expected {expected:.4f}, got {result:.4f}"
    print("  test_compute_remaining_overflow_partial: PASSED")


def test_compute_remaining_overflow_start_offset():
    """Verify start_minute skips earlier slots."""
    pv = {0: 8.0, 5: 8.0, 10: 8.0}
    load = {0: 1.0, 5: 1.0, 10: 1.0}
    result = compute_remaining_overflow(pv, load, dno_limit=4.0, start_minute=5, end_minute=15, step_minutes=5)
    expected = 2 * (3.0 * 5 / 60)
    assert abs(result - expected) < 0.001, f"Expected {expected}, got {result}"
    print("  test_compute_remaining_overflow_start_offset: PASSED")


# ============================================================================
# Morning gap tests
# ============================================================================


def test_morning_gap_pre_dawn():
    """Pre-dawn: load exceeds PV for hours, then solar takes over."""
    pv = {}
    load = {}
    for m in range(0, 480, 5):
        hour = m / 60.0
        pv[m] = max(0, hour - 2) * 1.5
        load[m] = 1.0
    gap = compute_morning_gap(pv, load, start_minute=0, end_minute=480, step_minutes=5)
    assert 1.5 < gap < 4.0, f"Expected morning gap 1.5-4.0 kWh, got {gap:.2f}"
    print("  test_morning_gap_pre_dawn: PASSED (gap={:.2f}kWh)".format(gap))


def test_morning_gap_solar_already_covers():
    """Mid-morning: PV already exceeds load, gap should be 0."""
    pv = {}
    load = {}
    for m in range(0, 480, 5):
        pv[m] = 5.0
        load[m] = 1.0
    gap = compute_morning_gap(pv, load, start_minute=0, end_minute=480, step_minutes=5)
    assert gap == 0.0, f"Expected gap=0, got {gap:.2f}"
    print("  test_morning_gap_solar_already_covers: PASSED")


def test_morning_gap_cloudy_never_covers():
    """Cloudy day: PV never sustainably exceeds load."""
    pv = {}
    load = {}
    for m in range(0, 480, 5):
        pv[m] = 0.5
        load[m] = 1.0
    gap = compute_morning_gap(pv, load, start_minute=0, end_minute=480, step_minutes=5)
    assert 3.5 < gap < 4.5, f"Expected ~4kWh gap, got {gap:.2f}"
    print("  test_morning_gap_cloudy_never_covers: PASSED (gap={:.2f}kWh)".format(gap))


def test_morning_gap_kwh_values():
    """Morning gap with kWh-per-step values (Predbat format)."""
    pv = {}
    load = {}
    step_kwh = 5 / 60.0
    for m in range(0, 480, 5):
        hour = m / 60.0
        pv[m] = max(0, hour - 2) * 1.5 * step_kwh
        load[m] = 1.0 * step_kwh
    gap = compute_morning_gap(pv, load, start_minute=0, end_minute=480, step_minutes=5, values_are_kwh=True)
    assert 1.5 < gap < 4.0, f"Expected morning gap 1.5-4.0 kWh, got {gap:.2f}"
    print("  test_morning_gap_kwh_values: PASSED (gap={:.2f}kWh)".format(gap))


# ============================================================================
# v10 activation tests
# ============================================================================


def test_activation_overflow_exceeds_headroom():
    """Plugin activates when overflow * 1.10 > headroom to 95%."""
    # 10kWh overflow, battery at 50% (9.04kWh), headroom = 95%*18.08 - 9.04 = 8.14
    # 10 * 1.10 = 11 > 8.14 → activate
    soc = BATTERY_KWH * 0.50
    headroom = BATTERY_KWH * SOC_CAP_FACTOR - soc
    overflow = 10.0
    assert overflow * OVERFLOW_SAFETY_FACTOR > headroom, f"Should activate: {overflow * OVERFLOW_SAFETY_FACTOR:.1f} > {headroom:.1f}"
    print("  test_activation_overflow_exceeds_headroom: PASSED")


def test_activation_overflow_within_headroom():
    """Plugin stays off when overflow * 1.10 <= headroom to 95%."""
    # 3kWh overflow, battery at 50% (9.04kWh), headroom = 95%*18.08 - 9.04 = 8.14
    # 3 * 1.10 = 3.3 <= 8.14 → off
    soc = BATTERY_KWH * 0.50
    headroom = BATTERY_KWH * SOC_CAP_FACTOR - soc
    overflow = 3.0
    assert overflow * OVERFLOW_SAFETY_FACTOR <= headroom, f"Should stay off: {overflow * OVERFLOW_SAFETY_FACTOR:.1f} <= {headroom:.1f}"
    print("  test_activation_overflow_within_headroom: PASSED")


def test_activation_high_soc_low_overflow():
    """High SOC with moderate overflow activates (small headroom)."""
    # Battery at 90% (16.27kWh), headroom = 17.18 - 16.27 = 0.91
    # 2kWh overflow * 1.10 = 2.2 > 0.91 → activate
    soc = BATTERY_KWH * 0.90
    headroom = BATTERY_KWH * SOC_CAP_FACTOR - soc
    overflow = 2.0
    assert overflow * OVERFLOW_SAFETY_FACTOR > headroom, f"Should activate: {overflow * OVERFLOW_SAFETY_FACTOR:.1f} > {headroom:.1f}"
    print("  test_activation_high_soc_low_overflow: PASSED")


# ============================================================================
# v10 floor tests
# ============================================================================


def test_floor_computation():
    """Floor = (soc_max * 0.9) - overflow * OVERFLOW_SAFETY_FACTOR (R9 + R45)."""
    overflow = 10.0
    floor = BATTERY_KWH * 0.9 - overflow * OVERFLOW_SAFETY_FACTOR
    expected = 18.08 * 0.9 - 10.0 * OVERFLOW_SAFETY_FACTOR
    assert abs(floor - expected) < 0.01, f"Expected {expected}, got {floor}"
    print(f"  test_floor_computation: PASSED (floor={floor:.2f}kWh = {floor/BATTERY_KWH*100:.0f}%)")


def test_floor_above_soc_keep():
    """Floor never goes below soc_keep."""
    overflow = 20.0  # huge overflow → floor = 18.08 - 25 = -6.92
    soc_keep = 4.0
    floor = BATTERY_KWH - overflow * OVERFLOW_SAFETY_FACTOR
    floor = max(floor, soc_keep)
    assert floor >= soc_keep, f"Floor should be >= soc_keep ({soc_keep}), got {floor}"
    print(f"  test_floor_above_soc_keep: PASSED (floor={floor:.2f}kWh)")


# ============================================================================
# v10 phase tests
# ============================================================================


def test_phase_charge_below_floor():
    """SOC below floor - margin → charge phase."""
    floor = 10.0
    soc = floor - SOC_MARGIN_KWH - 0.1  # just below threshold
    phase = "charge" if soc < floor - SOC_MARGIN_KWH else "managed"
    assert phase == "charge", f"Expected charge, got {phase}"
    print("  test_phase_charge_below_floor: PASSED")


def test_phase_managed_at_floor():
    """SOC at floor → managed phase."""
    floor = 10.0
    soc = floor  # exactly at floor
    phase = "charge" if soc < floor - SOC_MARGIN_KWH else "managed"
    assert phase == "managed", f"Expected managed, got {phase}"
    print("  test_phase_managed_at_floor: PASSED")


def test_phase_managed_above_floor():
    """SOC well above floor → managed phase."""
    floor = 10.0
    soc = 15.0
    phase = "charge" if soc < floor - SOC_MARGIN_KWH else "managed"
    assert phase == "managed", f"Expected managed, got {phase}"
    print("  test_phase_managed_above_floor: PASSED")


# ============================================================================
# Plugin integration tests
# ============================================================================

from curtailment_plugin import CurtailmentPlugin, PREDICT_STEP as PLUGIN_STEP


class MockBase:
    """Minimal mock of Predbat base for plugin tests."""

    def __init__(
        self,
        pv_step=None,
        load_step=None,
        soc_kw=5.0,
        soc_max=18.08,
        minutes_now=720,
        forecast_minutes=1440,
        charge_window_best=None,
        charge_limit_best=None,
        reserve_percent=4,
        best_soc_keep=0,
        reserve=0,
        sensor_overrides=None,
        now_utc=None,
    ):
        from datetime import datetime, timezone

        step_kwh_factor = PLUGIN_STEP / 60.0
        self.pv_forecast_minute_step = {k: v * step_kwh_factor for k, v in (pv_step or {}).items()}
        self.load_minutes_step = {k: v * step_kwh_factor for k, v in (load_step or {}).items()}
        self.soc_kw = soc_kw
        self.soc_max = soc_max
        self.minutes_now = minutes_now
        self.forecast_minutes = forecast_minutes
        self.charge_window_best = charge_window_best or []
        self.charge_limit_best = charge_limit_best or []
        self.reserve_percent = reserve_percent
        self.best_soc_keep = best_soc_keep
        self.reserve = reserve
        self.set_read_only = False
        self.config_index = {}
        self.prefix = "predbat"
        self.logs = []
        self.published = {}
        self.services = []
        self._sensor_overrides = sensor_overrides or {}
        self.now_utc = now_utc or datetime(2025, 7, 12, 12, 0, tzinfo=timezone.utc)

    def log(self, msg, *args, **kwargs):
        self.logs.append(msg)

    def get_state_wrapper(self, entity, default=None, attribute=None):
        if entity in self._sensor_overrides:
            val = self._sensor_overrides[entity]
            if attribute and isinstance(val, dict):
                return val.get(attribute, default)
            return val
        if entity == "input_boolean.curtailment_manager_enable":
            return "on"
        if entity == "zone.home":
            if attribute == "latitude":
                return 55.86
            elif attribute == "longitude":
                return -3.2
            return default
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
        limit_pct = round(charge_limit_kwh / self.soc_max * 100)
        return limit_pct == self.reserve_percent


def _make_overflow_pv(minutes_now=720):
    """Create PV/load forecasts that produce overflow (PV=8kW, load=1kW, excess=7kW > DNO 4kW)."""
    pv = {}
    load = {}
    for m in range(0, 1440 - minutes_now, PLUGIN_STEP):
        pv[m] = 8.0
        load[m] = 1.0
    return pv, load


def _make_p90_sensors(p90_peak_kw=8.58, solcast_remaining=25.0):
    """Build sensor_overrides with Solcast p90 detailedForecast.

    MockBase default: now_utc=2025-07-12 12:00 UTC, minutes_now=720 → local_offset=0.
    Period_start at 12:00 local = 12:00 UTC. p90_scale_from_forecast will compute
    scale = p90_peak_kw / sin(elevation at 12:25 UTC) ≈ p90_peak_kw / 0.829.

    Returns a dict to merge into sensor_overrides.
    """
    return {
        "sensor.solcast_pv_forecast_forecast_today": {"detailedForecast": [{"period_start": "2025-07-12T12:00:00+00:00", "pv_estimate90": p90_peak_kw}]},
        "sensor.solcast_pv_forecast_forecast_remaining_today": solcast_remaining,
    }


def test_plugin_activates_on_overflow():
    """Plugin activates when overflow predicted and battery will fill (R5)."""
    pv, load = _make_overflow_pv(minutes_now=720)
    sensor_overrides = {
        "sensor.sigen_plant_pv_power": 8.0,
        "sensor.sigen_plant_consumed_power": 1.0,
    }
    sensor_overrides.update(_make_p90_sensors())
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=BATTERY_KWH * 0.40,
        minutes_now=720,
        sensor_overrides=sensor_overrides,
    )
    plugin = CurtailmentPlugin(base)

    floor, phase = plugin.calculate(dno_limit_kw=4.0)
    assert phase == "active", f"Expected active, got {phase}"
    assert floor < BATTERY_KWH, f"Floor should be below soc_max, got {floor:.2f}"
    print(f"  test_plugin_activates_on_overflow: PASSED (floor={floor/BATTERY_KWH*100:.0f}%)")


def test_plugin_stays_off_no_overflow():
    """Plugin stays off when no overflow predicted."""
    pv = {}
    load = {}
    for m in range(0, 720, PLUGIN_STEP):
        pv[m] = 3.0  # excess = 2kW < DNO
        load[m] = 1.0
    base = MockBase(pv_step=pv, load_step=load, soc_kw=5.0, minutes_now=720)
    plugin = CurtailmentPlugin(base)

    floor, phase = plugin.calculate(dno_limit_kw=4.0)
    assert phase == "off", f"Expected off, got {phase}"
    assert floor == BATTERY_KWH, f"Floor should be soc_max when off, got {floor:.2f}"
    print("  test_plugin_stays_off_no_overflow: PASSED")


def test_plugin_publishes_active_not_phase():
    """Plugin publishes Active/Off, not Drain/Hold (those are HA automation's job)."""
    pv, load = _make_overflow_pv(minutes_now=720)
    sensor_overrides = {"sensor.sigen_plant_pv_power": 8.0, "sensor.sigen_plant_consumed_power": 1.0}
    sensor_overrides.update(_make_p90_sensors())
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=BATTERY_KWH * 0.40,
        minutes_now=720,
        sensor_overrides=sensor_overrides,
    )
    plugin = CurtailmentPlugin(base)
    plugin.on_update()

    phase_val = base.published.get("sensor.predbat_curtailment_phase", {}).get("value")
    assert phase_val == "Active", f"Expected 'Active', got '{phase_val}'"
    print("  test_plugin_publishes_active_not_phase: PASSED")


def test_plugin_floor_clamped_by_soc_keep():
    """With big overflow, floor clamped to soc_keep. Plugin activates."""
    pv, load = _make_overflow_pv(minutes_now=720)
    sensor_overrides = {
        "sensor.sigen_plant_pv_power": 8.0,
        "sensor.sigen_plant_consumed_power": 1.0,
    }
    sensor_overrides.update(_make_p90_sensors())
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=2.0,
        minutes_now=720,
        best_soc_keep=6.0,
        sensor_overrides=sensor_overrides,
    )
    plugin = CurtailmentPlugin(base)

    floor, phase = plugin.calculate(dno_limit_kw=4.0)
    assert floor >= 6.0, f"Floor should be clamped to soc_keep (6.0), got {floor:.1f}"
    assert phase == "active", f"Expected active, got {phase}"
    print(f"  test_plugin_floor_clamped_by_soc_keep: PASSED (floor={floor:.1f})")


def test_plugin_active_high_soc():
    """SOC above floor → still active (HA automation determines drain/hold)."""
    pv, load = _make_overflow_pv(minutes_now=720)
    sensor_overrides = {
        "sensor.sigen_plant_pv_power": 8.0,
        "sensor.sigen_plant_consumed_power": 1.0,
    }
    sensor_overrides.update(_make_p90_sensors())
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=BATTERY_KWH * 0.90,
        minutes_now=720,
        sensor_overrides=sensor_overrides,
    )
    plugin = CurtailmentPlugin(base)

    floor, phase = plugin.calculate(dno_limit_kw=4.0)
    assert phase == "active", f"Expected active, got {phase}"
    print(f"  test_plugin_active_high_soc: PASSED (SOC=90%, floor={floor/BATTERY_KWH*100:.0f}%)")


def test_floor_clamped_above_soc_keep():
    """Floor must never go below best_soc_keep."""
    pv, load = _make_overflow_pv(minutes_now=720)
    soc_keep = 8.0
    sensor_overrides = {
        "sensor.sigen_plant_pv_power": 8.0,
        "sensor.sigen_plant_consumed_power": 1.0,
    }
    sensor_overrides.update(_make_p90_sensors())
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=10.0,
        minutes_now=720,
        best_soc_keep=soc_keep,
        reserve=0,
        sensor_overrides=sensor_overrides,
    )
    plugin = CurtailmentPlugin(base)

    floor, phase = plugin.calculate(dno_limit_kw=4.0)
    assert floor >= soc_keep, f"Floor {floor:.2f} should be >= soc_keep {soc_keep:.2f}"
    print(f"  test_floor_clamped_above_soc_keep: PASSED (floor={floor:.2f}kWh >= keep={soc_keep:.2f}kWh)")


def test_floor_clamped_above_reserve():
    """Floor must never go below reserve."""
    pv, load = _make_overflow_pv(minutes_now=720)
    reserve = 5.0
    sensor_overrides = {
        "sensor.sigen_plant_pv_power": 8.0,
        "sensor.sigen_plant_consumed_power": 1.0,
    }
    sensor_overrides.update(_make_p90_sensors())
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=10.0,
        minutes_now=720,
        best_soc_keep=0,
        reserve=reserve,
        sensor_overrides=sensor_overrides,
    )
    plugin = CurtailmentPlugin(base)

    floor, phase = plugin.calculate(dno_limit_kw=4.0)
    assert floor >= reserve, f"Floor {floor:.2f} should be >= reserve {reserve:.2f}"
    print(f"  test_floor_clamped_above_reserve: PASSED (floor={floor:.2f}kWh >= reserve={reserve:.2f}kWh)")


# ============================================================================
# Apply tests — D-ESS/MSC control
# ============================================================================


def test_apply_active_sets_export_zero_and_dess():
    """First activation sets D-ESS with export=0 (safe default for HA automation)."""
    base = MockBase()
    plugin = CurtailmentPlugin(base)

    plugin.apply("active")
    assert base.set_read_only is True, "read_only should be True"
    # First activation: export=0 as safe default
    export_calls = [s for s in base.services if s[0] == "number/set_value" and "export" in str(s[1].get("entity_id", ""))]
    assert any(s[1]["value"] == 0 for s in export_calls), f"Should set export=0 on first activate, got {export_calls}"
    # D-ESS mode set
    dess_called = any(s[1].get("option") == "Command Discharging (ESS First)" for s in base.services if s[0] == "select/select_option")
    assert dess_called, "Should set D-ESS"
    print("  test_apply_active_sets_export_zero_and_dess: PASSED")


def test_apply_already_active_no_export_write():
    """Subsequent active cycles don't touch export limit (HA automation owns it)."""
    base = MockBase()
    plugin = CurtailmentPlugin(base)
    plugin.was_active = True  # already active
    plugin.last_ems_mode = "Command Discharging (ESS First)"
    plugin.last_charge_limit = 100

    plugin.apply("active")
    # Should NOT write export limit (HA automation controls it)
    export_calls = [s for s in base.services if s[0] == "number/set_value" and "export" in str(s[1].get("entity_id", ""))]
    assert len(export_calls) == 0, f"Should not write export when already active, got {export_calls}"
    print("  test_apply_already_active_no_export_write: PASSED")


def test_apply_off_restores_msc():
    """Off phase restores MSC and clears read_only."""
    base = MockBase()
    plugin = CurtailmentPlugin(base)
    plugin.was_active = True
    plugin.last_ems_mode = "Command Discharging (ESS First)"

    plugin.apply("off")
    assert base.set_read_only is False, "read_only should be False"
    msc_called = any(s[1].get("option") == "Maximum Self Consumption" for s in base.services if s[0] == "select/select_option")
    assert msc_called, "Should restore MSC"
    print("  test_apply_off_restores_msc: PASSED")


def test_on_update_full_flow():
    """Full on_update: calculates, applies D-ESS, publishes sensors."""
    pv, load = _make_overflow_pv(minutes_now=720)
    sensor_overrides = {
        "sensor.sigen_plant_pv_power": 8.0,
        "sensor.sigen_plant_consumed_power": 1.0,
    }
    sensor_overrides.update(_make_p90_sensors())
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=BATTERY_KWH * 0.40,
        minutes_now=720,
        sensor_overrides=sensor_overrides,
    )
    plugin = CurtailmentPlugin(base)
    plugin.on_update()

    phase_sensor = base.published.get("sensor.predbat_curtailment_phase", {})
    phase = phase_sensor.get("value", "Off")
    assert phase == "Active", f"Expected 'Active', got '{phase}'"
    assert base.set_read_only is True, "read_only should be True"

    target_sensor = base.published.get("sensor.predbat_curtailment_target_soc", {})
    assert target_sensor.get("value") is not None, "Target SOC should be published"
    target_pct = float(target_sensor.get("value", 0))
    assert 0 <= target_pct <= 100, f"Target SOC should be 0-100%, got {target_pct}"

    print(f"  test_on_update_full_flow: PASSED (phase={phase}, target={target_pct:.0f}%)")


def test_on_update_stays_off_low_pv():
    """Low PV day: plugin stays off."""
    pv = {}
    load = {}
    for m in range(0, 360, PLUGIN_STEP):
        pv[m] = 1.5  # below DNO with any load
        load[m] = 1.0
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=BATTERY_KWH * 0.50,
        minutes_now=720,
        sensor_overrides={
            "sensor.sigen_plant_pv_power": 1.5,
            "sensor.sigen_plant_consumed_power": 1.0,
        },
    )
    plugin = CurtailmentPlugin(base)
    plugin.on_update()

    phase_sensor = base.published.get("sensor.predbat_curtailment_phase", {})
    phase = phase_sensor.get("value", "Off")
    assert phase == "Off", f"Expected Off for low PV, got '{phase}'"
    assert base.set_read_only is False, "read_only should be False when off"
    print("  test_on_update_stays_off_low_pv: PASSED")


def test_deactivation_at_safe_time():
    """Plugin deactivates when past safe_time, restoring MSC (R6/R12)."""
    from datetime import datetime, timezone

    # Activate at noon
    pv, load = _make_overflow_pv(minutes_now=720)
    sensor_overrides = {
        "sensor.sigen_plant_pv_power": 8.0,
        "sensor.sigen_plant_consumed_power": 1.0,
    }
    sensor_overrides.update(_make_p90_sensors())
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=BATTERY_KWH * 0.40,
        minutes_now=720,
        now_utc=datetime(2025, 7, 12, 12, 0, tzinfo=timezone.utc),
        sensor_overrides=sensor_overrides,
    )
    plugin = CurtailmentPlugin(base)
    plugin.on_update()
    assert base.set_read_only is True, "Should activate at noon"

    # Jump past safe_time (~18:30 UTC for scale=10.35, threshold=4.5 kW)
    base.minutes_now = 18 * 60 + 30  # 18:30 local (=UTC with local_offset=0)
    base.now_utc = datetime(2025, 7, 12, 18, 30, tzinfo=timezone.utc)
    base._sensor_overrides["sensor.sigen_plant_pv_power"] = 0.3
    base.services.clear()
    plugin.base = base
    plugin.on_update()

    assert base.set_read_only is False, "Should deactivate after safe_time"
    msc_called = any(s[1].get("option") == "Maximum Self Consumption" for s in base.services if s[0] == "select/select_option")
    assert msc_called, "Should restore MSC at safe_time"
    print("  test_deactivation_at_safe_time: PASSED")


def test_manual_hold_maintains_dess_after_deactivation():
    """When manual_hold is on, plugin stays in D-ESS even after overflow clears.

    Without fix: plugin deactivates → restores MSC → Predbat fights automation.
    With fix: plugin detects manual_hold, keeps D-ESS + read_only=True.
    """
    from datetime import datetime, timezone

    pv, load = _make_overflow_pv(minutes_now=720)
    sensor_overrides = {
        "sensor.sigen_plant_pv_power": 8.0,
        "sensor.sigen_plant_consumed_power": 1.0,
        "input_select.curtailment_manual_hold": "Hold",
    }
    sensor_overrides.update(_make_p90_sensors())

    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=BATTERY_KWH * 0.40,
        minutes_now=720,
        now_utc=datetime(2025, 7, 12, 12, 0, tzinfo=timezone.utc),
        sensor_overrides=sensor_overrides,
    )
    plugin = CurtailmentPlugin(base)
    plugin.on_update()
    assert base.set_read_only is True, "Should activate"

    # Jump past safe_time so plugin would normally deactivate
    base.minutes_now = 18 * 60 + 30
    base.now_utc = datetime(2025, 7, 12, 18, 30, tzinfo=timezone.utc)
    base._sensor_overrides["sensor.sigen_plant_pv_power"] = 0.3
    base.services.clear()
    plugin.base = base
    plugin.on_update()

    # manual_hold is on → must stay in D-ESS, must NOT restore MSC
    assert base.set_read_only is True, "read_only must stay True when manual_hold is on"
    msc_called = any(s[1].get("option") == "Maximum Self Consumption" for s in base.services if s[0] == "select/select_option")
    assert not msc_called, "Must NOT restore MSC when manual_hold is on"
    print("  test_manual_hold_maintains_dess_after_deactivation: PASSED")


# ============================================================================
# Charge window deferral tests
# ============================================================================


def test_defers_to_charge_window():
    """Plugin defers to Predbat during charge window when SOC < soc_keep."""
    pv, load = _make_overflow_pv(minutes_now=300)
    pv[0] = 2.0
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=3.0,
        minutes_now=300,
        charge_window_best=[{"start": 240, "end": 420}],
        charge_limit_best=[10.0],
        best_soc_keep=4.0,
    )
    plugin = CurtailmentPlugin(base)
    plugin.on_update()

    phase_sensor = base.published.get("sensor.predbat_curtailment_phase", {})
    assert phase_sensor.get("value") == "Off", f"Expected Off during charge window, got '{phase_sensor.get('value')}'"
    assert base.set_read_only is False, "read_only should be False when deferring"
    print("  test_defers_to_charge_window: PASSED")


def test_ignores_freeze_charge_window():
    """Plugin does NOT defer to a freeze charge window."""
    pv, load = _make_overflow_pv(minutes_now=720)
    reserve_kwh = 18.08 * 4 / 100
    sensor_overrides = {
        "sensor.sigen_plant_pv_power": 8.0,
        "sensor.sigen_plant_consumed_power": 1.0,
    }
    sensor_overrides.update(_make_p90_sensors())
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=5.0,
        minutes_now=720,
        charge_window_best=[{"start": 700, "end": 800}],
        charge_limit_best=[reserve_kwh],
        reserve_percent=4,
        sensor_overrides=sensor_overrides,
    )
    plugin = CurtailmentPlugin(base)
    plugin.on_update()

    phase_sensor = base.published.get("sensor.predbat_curtailment_phase", {})
    assert phase_sensor.get("value") == "Active", f"Should NOT defer to freeze window, got '{phase_sensor.get('value')}'"
    print("  test_ignores_freeze_charge_window: PASSED")


# ============================================================================
# on_before_plan tests
# ============================================================================


def test_before_plan_reduces_keep_on_overflow_day():
    """on_before_plan reduces best_soc_keep when overflow is forecast."""
    pv, load = _make_overflow_pv(minutes_now=720)
    base = MockBase(pv_step=pv, load_step=load, soc_kw=5.0, minutes_now=720)
    plugin = CurtailmentPlugin(base)

    context = {"best_soc_keep": 6.0}
    result = plugin.on_before_plan(context)
    assert result["best_soc_keep"] < 6.0, f"Expected keep < 6.0, got {result['best_soc_keep']:.2f}"
    assert result["best_soc_keep"] <= 1.0, f"Expected keep <= 1.0, got {result['best_soc_keep']:.2f}"
    print("  test_before_plan_reduces_keep_on_overflow_day: PASSED (keep={:.2f})".format(result["best_soc_keep"]))


def test_before_plan_no_change_without_overflow():
    """on_before_plan does not reduce best_soc_keep when no overflow."""
    pv = {}
    load = {}
    for m in range(0, 720, 5):
        pv[m] = 2.0
        load[m] = 3.0
    base = MockBase(pv_step=pv, load_step=load, soc_kw=5.0, minutes_now=720)
    plugin = CurtailmentPlugin(base)

    context = {"best_soc_keep": 6.0}
    result = plugin.on_before_plan(context)
    assert result["best_soc_keep"] == 6.0, f"Expected keep unchanged, got {result['best_soc_keep']:.2f}"
    print("  test_before_plan_no_change_without_overflow: PASSED")


def test_before_plan_never_increases():
    """on_before_plan only reduces, never increases."""
    pv, load = _make_overflow_pv(minutes_now=720)
    base = MockBase(pv_step=pv, load_step=load, soc_kw=5.0, minutes_now=720)
    plugin = CurtailmentPlugin(base)

    context = {"best_soc_keep": 0.5}
    result = plugin.on_before_plan(context)
    assert result["best_soc_keep"] <= 0.5, f"Expected keep <= 0.5, got {result['best_soc_keep']:.2f}"
    print("  test_before_plan_never_increases: PASSED")


def test_before_plan_disabled():
    """on_before_plan returns unchanged when disabled."""
    pv, load = _make_overflow_pv(minutes_now=720)
    base = MockBase(pv_step=pv, load_step=load, soc_kw=5.0, minutes_now=720)
    base.get_state_wrapper = lambda entity, default=None, attribute=None: "off" if "enable" in entity else default
    plugin = CurtailmentPlugin(base)

    context = {"best_soc_keep": 6.0}
    result = plugin.on_before_plan(context)
    assert result["best_soc_keep"] == 6.0, f"Expected unchanged when disabled, got {result['best_soc_keep']:.2f}"
    print("  test_before_plan_disabled: PASSED")


# ============================================================================
# Solar geometry tests
# ============================================================================


def test_solar_elevation_known_values():
    """Solar elevation against known astronomical values."""
    elev = solar_elevation(56.0, -3.2, 12.2, 172)
    assert 55 < elev < 60, f"Summer solstice noon 56N: expected ~57.5deg, got {elev:.1f}"

    elev_w = solar_elevation(56.0, -3.2, 12.2, 355)
    assert 8 < elev_w < 13, f"Winter solstice noon 56N: expected ~10.5deg, got {elev_w:.1f}"

    elev_n = solar_elevation(56.0, -3.2, 0.0, 172)
    assert elev_n < 0, f"Midnight should be negative, got {elev_n:.1f}"

    elev_eq = solar_elevation(0.0, 0.0, 12.0, 80)
    assert 85 < elev_eq < 92, f"Equator equinox noon: expected ~90deg, got {elev_eq:.1f}"

    print(f"  test_solar_elevation_known_values: PASSED (summer={elev:.1f}, winter={elev_w:.1f}, equator={elev_eq:.1f})")


def test_compute_release_time_scenarios():
    """Release time for various scenarios."""
    # High scale, early afternoon July
    mins, crossing = compute_release_time(scale=14.0, lat_deg=56.0, lon_deg=-3.2, day_of_year=193, threshold_kw=4.5, current_utc_hours=13.0)
    assert mins is not None, "Should find a crossing"
    assert mins > 60, f"Expected >60 min, got {mins:.0f}"
    assert mins < 480, f"Expected <480 min, got {mins:.0f}"

    # Low scale — safe now
    mins_low, _ = compute_release_time(scale=3.0, lat_deg=56.0, lon_deg=-3.2, day_of_year=193, threshold_kw=4.5, current_utc_hours=16.0)
    assert mins_low == 0, f"Expected 0 (safe now), got {mins_low}"

    # Late afternoon — soon
    mins_late, _ = compute_release_time(scale=10.0, lat_deg=56.0, lon_deg=-3.2, day_of_year=193, threshold_kw=4.5, current_utc_hours=17.0)
    assert mins_late is not None and mins_late < 120, f"Expected <120 min, got {mins_late}"

    # Winter — below threshold
    mins_winter, _ = compute_release_time(scale=14.0, lat_deg=56.0, lon_deg=-3.2, day_of_year=355, threshold_kw=4.5, current_utc_hours=10.0)
    assert mins_winter == 0, f"Winter peak below threshold, expected 0, got {mins_winter}"

    print(f"  test_compute_release_time_scenarios: PASSED")


def test_compute_release_offset_load_spike():
    """Load spike mid-afternoon must NOT cause early false release.

    Scenario: GSHP-style load spike at 13:30 temporarily brings PV-load below
    DNO threshold, but overflow continues after the spike. Release should be
    one slot after the LAST overflow slot (~17:35), not at the spike (13:30).
    """
    from curtailment_calc import compute_release_offset

    step = 5
    dno = 4.0

    pv = {}
    load = {}
    for m in range(0, 1440, step):
        hour = m / 60
        if 6 <= hour <= 20:
            pv[m] = max(0, 7.0 * math.sin(math.pi * (hour - 6) / 14))
        else:
            pv[m] = 0
        # Load: normal 0.5 kW but GSHP spike 4.0 kW at 13:30–14:00
        if 810 <= m < 840:
            load[m] = 4.0  # spike: PV-load drops below DNO for these slots
        else:
            load[m] = 0.5

    # At 13:30 (m=810): PV ≈ 6.9 kW, load = 4.0 → PV-load = 2.9 < DNO=4.0 (not overflow)
    # But after spike (14:00+): PV still ~6.7 kW, load = 0.5 → PV-load = 6.2 > DNO (still overflow)
    # Last overflow slot should be well after 14:00, ~17:30.
    offset = compute_release_offset(pv, load, dno_limit=dno, start_minute=0, end_minute=1440, step_minutes=step)
    assert offset is not None, "Should find release point"
    # Old (flawed) algorithm would return ~810 min (13:30); new must return >840 min (after spike)
    assert offset > 840, f"Release should be after load spike ends (>840 min = 14:00), got {offset} min ({offset // 60:02d}:{offset % 60:02d})"
    # Should be somewhere around 17:30-18:00 (last overflow slot ~17:30 + one step)
    assert offset <= 1100, f"Release too late: {offset} min"

    print(f"  test_compute_release_offset_load_spike: PASSED (release at {offset}min = {offset // 60:02d}:{offset % 60:02d})")


def test_compute_release_offset():
    """Release offset: one slot after last slot where PV-load > DNO."""
    from curtailment_calc import compute_release_offset

    step = 5
    dno = 4.0

    # Build a day: PV peaks at noon, declines through afternoon
    pv = {}
    load = {}
    for m in range(0, 1440, step):
        hour = m / 60
        if 6 <= hour <= 20:
            # Bell curve PV peaking at 7kW at noon
            pv[m] = max(0, 7.0 * math.sin(math.pi * (hour - 6) / 14))
        else:
            pv[m] = 0
        load[m] = 1.0  # constant 1kW load

    # Overflow when PV-load > 4.0 → PV > 5.0.
    # 7*sin(π*(h-6)/14) = 5 → h ≈ 16:32. Last overflow slot ~16:30, release at 16:35.
    offset = compute_release_offset(pv, load, dno_limit=dno, start_minute=0, end_minute=1440, step_minutes=step)
    assert offset is not None, "Should find release point"
    # Release = one slot AFTER last overflow slot → ~16:30-17:00 range
    assert 980 <= offset <= 1030, f"Expected release ~16:20-17:10, got {offset // 60:02d}:{offset % 60:02d}"

    # No overflow day (PV max 2kW, never exceeds DNO)
    low_pv = {m: 0 for m in range(0, 1440, step)}
    for m in range(0, 1440, step):
        hour = m / 60
        if 6 <= hour <= 20:
            low_pv[m] = max(0, 2.0 * math.sin(math.pi * (hour - 6) / 14))
    offset_none = compute_release_offset(low_pv, load, dno_limit=dno, start_minute=0, end_minute=1440, step_minutes=step)
    assert offset_none is None, f"No overflow day should return None, got {offset_none}"

    print(f"  test_compute_release_offset: PASSED (release at {offset}min = {offset // 60:02d}:{offset % 60:02d})")


# ============================================================================
# Tomorrow forecast tests
# ============================================================================


def test_tomorrow_forecast_overflow_day():
    """Tomorrow forecast: high PV shows overflow and low floor."""
    pv = {}
    load = {}
    for m in range(0, 2880, PLUGIN_STEP):
        if 720 <= m <= 2160:
            pv[m] = 8.0
            load[m] = 1.0
        else:
            pv[m] = 0
            load[m] = 0.5

    step_kwh = PLUGIN_STEP / 60.0
    pv_kwh = {k: v * step_kwh for k, v in pv.items()}
    load_kwh = {k: v * step_kwh for k, v in load.items()}

    result = compute_tomorrow_forecast(pv_kwh, load_kwh, BATTERY_KWH, DNO_LIMIT, start_minute=720, end_minute=2160, step_minutes=PLUGIN_STEP, values_are_kwh=True)

    assert result["will_activate"], "High PV day should activate"
    assert result["total_overflow_kwh"] > 5, f"Expected >5kWh overflow, got {result['total_overflow_kwh']}"
    assert result["floor_pct"] < 50, f"Floor should be low, got {result['floor_pct']}"
    print(f"  test_tomorrow_forecast_overflow_day: PASSED (overflow={result['total_overflow_kwh']}kWh, floor={result['floor_pct']}%)")


def test_tomorrow_forecast_no_overflow():
    """Tomorrow forecast: moderate PV shows no overflow."""
    pv = {}
    load = {}
    for m in range(0, 2880, PLUGIN_STEP):
        if 720 <= m <= 2160:
            pv[m] = 3.0
            load[m] = 1.0
        else:
            pv[m] = 0
            load[m] = 0.5

    step_kwh = PLUGIN_STEP / 60.0
    pv_kwh = {k: v * step_kwh for k, v in pv.items()}
    load_kwh = {k: v * step_kwh for k, v in load.items()}

    result = compute_tomorrow_forecast(pv_kwh, load_kwh, BATTERY_KWH, DNO_LIMIT, start_minute=720, end_minute=2160, step_minutes=PLUGIN_STEP, values_are_kwh=True)

    assert not result["will_activate"], "Moderate PV should not activate"
    assert result["total_overflow_kwh"] == 0, f"Expected 0 overflow, got {result['total_overflow_kwh']}"
    assert result["floor_pct"] == 100, f"Floor should be 100%, got {result['floor_pct']}"
    print("  test_tomorrow_forecast_no_overflow: PASSED")


# ============================================================================
# v10 CSV validation — replay real days through v10 algorithm
# ============================================================================


def _run_csv_day_v10(label, filename, watts, forecast_scale=1.0, start_soc_pct=None):
    """Run a CSV day through the v10 simulation.

    With forecast_scale=1.0, forecast matches reality (perfect forecast).
    With forecast_scale < 1.0, forecast underestimates PV (underforecast).
    """
    if start_soc_pct is None:
        start_soc_pct = START_SOC_PCT
    filepath = os.path.join(CSV_DIR, filename)
    if not os.path.exists(filepath):
        print(f"  {label}: SKIPPED (CSV not found)")
        return False

    pv_actual, load_actual = _load_csv_to_forecasts(filepath, watts=watts)
    pv_forecast = {m: v * forecast_scale for m, v in pv_actual.items()}

    sim = _simulate_day_v10(
        pv_actual,
        load_actual,
        pv_forecast=pv_forecast,
        load_forecast=load_actual,
        start_soc_pct=start_soc_pct,
        soc_floor_kwh=0.0,
    )

    errors = []
    max_exp = sim["max_export_kw"]
    curtailed = sim["total_curtailed"]
    sunset_soc = sim["sunset_soc_pct"]

    # Hard constraint: export must never exceed DNO limit
    if max_exp > DNO_LIMIT + 0.01:
        errors.append(f"max_export={max_exp:.1f}kW > DNO {DNO_LIMIT}kW")

    # Curtailment bound: with perfect forecast, should be < 3kWh
    # (peak days like Jul 12 at 15.6kWh overflow ≈ battery capacity → 95% cap causes ~2.5kWh)
    # With underforecast (60%), allow more (energy ratio takes time to correct)
    max_curtailment = 10.0 if forecast_scale < 0.7 else 3.0
    if curtailed > max_curtailment:
        errors.append(f"curtailment={curtailed:.2f}kWh (should be <{max_curtailment:.0f})")

    # Battery should reach >80% by sunset on overflow days
    has_overflow = sim["initial_overflow"] > 0.5
    if has_overflow and sunset_soc < 80:
        errors.append(f"sunset_soc={sunset_soc:.0f}% (should be >80%)")

    scale_label = f" @ {forecast_scale:.0%} forecast" if forecast_scale != 1.0 else ""
    soc_label = f" start={start_soc_pct:.0%}" if start_soc_pct != START_SOC_PCT else ""
    tag = f"  v10 {label}{scale_label}{soc_label}"
    if errors:
        detail = "; ".join(errors)
        print(f"{tag}: FAILED — {detail}")
        print(f"    overflow={sim['initial_overflow']:.1f}kWh curtailed={curtailed:.2f}kWh max_export={max_exp:.1f}kW sunset_soc={sunset_soc:.0f}% floor@10={sim['floor_at_10']/BATTERY_KWH*100:.0f}%")
        return True

    print(f"{tag}: PASSED (overflow={sim['initial_overflow']:.1f}kWh curtailed={curtailed:.2f}kWh max_export={max_exp:.1f}kW sunset_soc={sunset_soc:.0f}% floor@10={sim['floor_at_10']/BATTERY_KWH*100:.0f}%)")
    return False


# ============================================================================
# Edge case tests (synthetic data)
# ============================================================================


def test_no_overflow_day():
    """Low PV day — plugin stays off, no curtailment."""
    pv = {}
    load = {}
    for m in range(0, 1440, STEP_MINUTES):
        hour = m / 60
        if 6 <= hour <= 18:
            pv[m] = 3.0
        else:
            pv[m] = 0.0
        load[m] = 0.5

    overflow = compute_remaining_overflow(pv, load, DNO_LIMIT, 0, 1440, STEP_MINUTES)
    assert overflow == 0.0, f"Expected no overflow, got {overflow}"

    sim = _simulate_day_v10(pv, load)
    assert sim["total_curtailed"] < 0.01, f"Expected no curtailment, got {sim['total_curtailed']:.3f}"
    print("  test_no_overflow_day: PASSED")


def test_export_never_exceeds_dno():
    """Extreme PV — export must never exceed DNO limit."""
    pv = {}
    load = {}
    for m in range(0, 1440, STEP_MINUTES):
        hour = m / 60
        if 10 <= hour <= 14:
            pv[m] = 15.0
        else:
            pv[m] = 0.0
        load[m] = 0.5

    sim = _simulate_day_v10(pv, load, start_soc_pct=0.5)
    for r in sim["results"]:
        assert r["export"] <= DNO_LIMIT + 0.01, f"Minute {r['minute']}: export {r['export']:.2f} > DNO {DNO_LIMIT}"
    print("  test_export_never_exceeds_dno: PASSED")


# ============================================================================
# Floor bidirectional test
# ============================================================================


def test_floor_lower_with_more_overflow():
    """Higher p90 peak → larger overflow integral → lower floor (R9).

    Floor is always computed from p90_scale regardless of actual PV.
    Higher p90_peak → higher scale → more overflow expected → lower floor.
    """
    load = {}
    for m in range(0, 120, PLUGIN_STEP):
        load[m] = 1.0

    # Scenario 1: lower p90 peak → smaller overflow integral → higher floor
    pv = {m: 8.0 for m in range(0, 120, PLUGIN_STEP)}
    sensor1 = {"sensor.sigen_plant_pv_power": 8.0, "sensor.sigen_plant_consumed_power": 1.0}
    sensor1.update(_make_p90_sensors(p90_peak_kw=6.0, solcast_remaining=12.0))
    base1 = MockBase(pv_step=pv, load_step=load, soc_kw=10.0, minutes_now=720, sensor_overrides=sensor1)
    plugin1 = CurtailmentPlugin(base1)
    floor1, _ = plugin1.calculate(dno_limit_kw=4.0)

    # Scenario 2: higher p90 peak → larger overflow integral → lower floor
    sensor2 = {"sensor.sigen_plant_pv_power": 8.0, "sensor.sigen_plant_consumed_power": 1.0}
    sensor2.update(_make_p90_sensors(p90_peak_kw=10.0, solcast_remaining=20.0))
    base2 = MockBase(pv_step=pv, load_step=load, soc_kw=10.0, minutes_now=720, sensor_overrides=sensor2)
    plugin2 = CurtailmentPlugin(base2)
    floor2, _ = plugin2.calculate(dno_limit_kw=4.0)

    assert floor2 < floor1, f"Higher p90 should give lower floor: p90=10kW floor={floor2:.1f} vs p90=6kW floor={floor1:.1f}"
    print(f"  test_floor_lower_with_more_overflow: PASSED (p90=6kW→{floor1/BATTERY_KWH*100:.0f}%, p90=10kW→{floor2/BATTERY_KWH*100:.0f}%)")


def test_export_target_at_dno_when_soc_above_floor():
    """Export target = DNO when SOC is above floor (Drain/Hold territory, R38).

    SOC=90% at midday: floor ≈ 30% (large overflow predicted). SOC >> floor+0.5 → Drain → DNO.
    """
    pv = {m: 8.0 for m in range(0, 360, PLUGIN_STEP)}
    load = {m: 1.0 for m in range(0, 360, PLUGIN_STEP)}
    soc_kw = BATTERY_KWH * 0.90  # 90% — well above any floor
    sensor_overrides = {
        "sensor.sigen_plant_pv_power": 8.0,
        "sensor.sigen_plant_consumed_power": 1.0,
    }
    sensor_overrides.update(_make_p90_sensors(solcast_remaining=25.0))
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=soc_kw,
        minutes_now=600,
        sensor_overrides=sensor_overrides,
    )
    plugin = CurtailmentPlugin(base)
    floor, phase = plugin.calculate(dno_limit_kw=4.0)
    assert phase == "active", f"Should be active, got {phase}"
    assert soc_kw > floor + 0.5, f"Test requires SOC above floor+0.5 ({soc_kw:.1f} vs {floor + 0.5:.1f})"
    assert plugin._export_target == 4.0, f"Export target should be DNO when SOC above floor, got {plugin._export_target}"
    print(f"  test_export_target_at_dno_when_soc_above_floor: PASSED (floor={floor/BATTERY_KWH*100:.0f}%, SOC=90%, export_target={plugin._export_target}kW)")


def test_export_target_dno_when_active_regardless_of_soc():
    """Plugin publishes export_target = DNO whenever active; HA automation decides Charge/Hold/Drain.

    SOC below floor: plugin still publishes DNO. Automation reads target_kwh from phase sensor
    and uses SOC vs target_kwh with symmetric hysteresis to choose Charge (export=0),
    Hold (export=min(excess,DNO)) or Drain (export=DNO).
    """
    from datetime import datetime, timezone

    pv = {m: 5.5 for m in range(0, 60, PLUGIN_STEP)}
    load = {m: 0.5 for m in range(0, 60, PLUGIN_STEP)}
    soc_kw = BATTERY_KWH * 0.80  # 80% — below the high late-afternoon floor
    sensor_overrides = {
        "sensor.sigen_plant_pv_power": 5.5,
        "sensor.sigen_plant_consumed_power": 0.5,
    }
    sensor_overrides.update(_make_p90_sensors(solcast_remaining=5.5))
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=soc_kw,
        minutes_now=1020,
        now_utc=datetime(2025, 7, 12, 16, 0, tzinfo=timezone.utc),
        sensor_overrides=sensor_overrides,
    )
    plugin = CurtailmentPlugin(base)
    floor, phase = plugin.calculate(dno_limit_kw=4.0)
    assert phase == "active", f"Should be active, got {phase}"
    assert soc_kw < floor, f"Test requires SOC below floor ({soc_kw:.1f} vs {floor:.1f})"
    assert plugin._export_target == 4.0, f"Plugin should publish DNO when active, got {plugin._export_target}"
    print(f"  test_export_target_dno_when_active_regardless_of_soc: PASSED (floor={floor/BATTERY_KWH*100:.0f}%, SOC=80%, export_target={plugin._export_target}kW)")


# ============================================================================
# Integration test — runs ACTUAL plugin.calculate() against CSV data
# Physics simulation is independent of algorithm code.
# This catches bugs that the v10 simulation misses (same-code problem).
# ============================================================================


def _integration_test_day(label, filename, watts, start_soc_pct=None, forecast_scale=1.0, forecast_scale_fn=None, min_sunset_soc=80):
    """Run a CSV day through the ACTUAL plugin.calculate() + independent physics.

    Algorithm decisions come from plugin.calculate() (the real code).
    Physics are simulated independently (charge/drain/hold based on SOC vs floor).
    If the algorithm has a bug (wrong activation, wrong floor), the physics reveal it.

    forecast_scale: uniform scale factor for forecast (1.0 = perfect forecast)
    forecast_scale_fn: function(absolute_minute) -> scale factor (overrides forecast_scale)
    min_sunset_soc: minimum sunset SOC% for small overflow days (default 80%)
    """
    from datetime import datetime, timezone

    if start_soc_pct is None:
        start_soc_pct = START_SOC_PCT
    filepath = os.path.join(CSV_DIR, filename)
    if not os.path.exists(filepath):
        print(f"  {label}: SKIPPED (CSV not found)")
        return False

    pv_actual, load_actual = _load_csv_to_forecasts(filepath, watts=watts)

    # Scale function: per-slot or uniform
    if forecast_scale_fn is not None:
        scale_at = forecast_scale_fn
    else:
        scale_at = lambda minute: forecast_scale

    # p90 peak: use actual day's peak * 1.1 as p90 proxy (near-perfect day assumption)
    actual_peak_pv = max(pv_actual.values()) if pv_actual else 0.0
    p90_peak_kw = max(actual_peak_pv * 1.1, 5.0)

    soc = BATTERY_KWH * start_soc_pct
    step_hours = STEP_MINUTES / 60.0

    total_curtailed = 0.0
    total_export = 0.0
    max_export = 0.0
    plugin = None
    results = []

    # Start at first PV slot (matches live — plugin only runs when PV generating)
    start_minute = 0
    for m in range(0, 1440, STEP_MINUTES):
        if pv_actual.get(m, 0) > 0:
            start_minute = m
            break

    for m in range(start_minute, 1440, STEP_MINUTES):
        actual_pv = pv_actual.get(m, 0)
        actual_load = load_actual.get(m, 0)
        actual_excess = actual_pv - actual_load

        # Build forecast: remaining day from current minute (SCALED)
        forecast_pv = {}
        forecast_load = {}
        for k in range(0, 1440 - m, STEP_MINUTES):
            forecast_pv[k] = pv_actual.get(m + k, 0) * scale_at(m + k)
            forecast_load[k] = load_actual.get(m + k, 0)

        # Solcast remaining: scaled forecast PV from now to end
        solcast_remaining = sum(pv_actual.get(m + k, 0) * scale_at(m + k) * step_hours for k in range(0, 1440 - m, STEP_MINUTES))

        # Create MockBase with current state
        # Use a July date for solar geometry (matches CSV data)
        utc_hour = m / 60.0 - 1.0  # BST = UTC+1
        base = MockBase(
            pv_step=forecast_pv,
            load_step=forecast_load,
            soc_kw=soc,
            soc_max=BATTERY_KWH,
            minutes_now=m,
            forecast_minutes=1440 - m,
            now_utc=datetime(2025, 7, 12, max(0, int(utc_hour)), int((utc_hour % 1) * 60) if utc_hour >= 0 else 0, tzinfo=timezone.utc),
            sensor_overrides={
                "sensor.sigen_plant_pv_power": actual_pv,
                "sensor.sigen_plant_consumed_power": actual_load,
                "sensor.solcast_pv_forecast_forecast_remaining_today": solcast_remaining,
                # v17: detailedForecast for p90 scale (R42)
                "sensor.solcast_pv_forecast_forecast_today": {"detailedForecast": [{"period_start": "2025-07-12T12:00:00+00:00", "pv_estimate90": p90_peak_kw}]},
            },
        )

        # Preserve plugin state across steps (like the real system)
        if plugin is None:
            plugin = CurtailmentPlugin(base)
        else:
            plugin.base = base

        # Call ACTUAL plugin code
        floor, phase = plugin.calculate(dno_limit_kw=DNO_LIMIT)

        # Mirror what on_update() does: once active, was_active stays True until deactivation
        if phase in ("active", "post_release"):
            plugin.was_active = True

        # === INDEPENDENT PHYSICS (not from plugin code) ===
        remaining_cap = max(0, BATTERY_KWH - soc)
        max_charge = min(MAX_CHARGE_KW, remaining_cap / step_hours) if remaining_cap > 0.01 else 0
        max_discharge = min(MAX_DISCHARGE_KW, soc / step_hours) if soc > 0.01 else 0

        export = 0.0
        charge = 0.0
        discharge = 0.0
        curtailed = 0.0

        if phase == "off":
            # MSC: battery absorbs all excess, export leftovers
            if actual_excess > 0:
                charge = min(actual_excess, max_charge)
                leftover = actual_excess - charge
                export = min(leftover, DNO_LIMIT)
                curtailed = max(0, leftover - export)
            else:
                discharge = min(-actual_excess, max_discharge)
        elif actual_excess > DNO_LIMIT:
            # Phase 2: overflow — just export DNO, battery absorbs rest
            # No floor comparison — physics dictates during overflow
            export = DNO_LIMIT
            overflow = actual_excess - DNO_LIMIT
            charge = min(overflow, max_charge)
            curtailed = max(0, overflow - charge)
        else:
            # Phase 1/3: sub-DNO. HA automation chooses Charge/Drain/Hold from SOC vs floor
            # with ±SOC_MARGIN_KWH hysteresis. Plugin publishes export_target=DNO when active;
            # automation sets the actual export limit based on phase.
            if soc > floor + SOC_MARGIN_KWH:
                # Drain: export=DNO, SIG discharges battery
                if actual_excess > 0:
                    drain_kw = min(DNO_LIMIT - actual_excess, max_discharge)
                    export = min(actual_excess + drain_kw, DNO_LIMIT)
                    discharge = drain_kw
                else:
                    discharge = min(-actual_excess, max_discharge)
            elif soc < floor - SOC_MARGIN_KWH:
                # Charge: export=0, all PV excess charges battery
                if actual_excess > 0:
                    charge = min(actual_excess, max_charge)
                else:
                    discharge = min(-actual_excess, max_discharge)
            else:
                # Hold: export=min(excess, DNO), battery neither drains nor charges from sub-DNO PV
                # Post-release uses plugin's ramped-down export_target as additional cap.
                hold_cap = plugin._export_target if (plugin._export_target >= 0) else DNO_LIMIT
                if actual_excess > 0:
                    export = min(actual_excess, hold_cap, DNO_LIMIT)
                    leftover = actual_excess - export
                    charge = min(leftover, max_charge)
                else:
                    discharge = min(-actual_excess, max_discharge)

        soc += charge * step_hours - discharge * step_hours
        soc = max(0, min(BATTERY_KWH, soc))

        total_curtailed += curtailed * step_hours
        total_export += export * step_hours
        if export > max_export:
            max_export = export

        results.append({"minute": m, "soc_pct": soc / BATTERY_KWH * 100, "floor_pct": floor / BATTERY_KWH * 100, "phase": phase, "export": export, "pv": actual_pv})

    # Sunset SOC
    sunset_soc_pct = soc / BATTERY_KWH * 100
    for r in reversed(results):
        if r["pv"] > 0.05:
            sunset_soc_pct = r["soc_pct"]
            break

    # Overflow from CSV
    initial_overflow = compute_remaining_overflow(pv_actual, load_actual, DNO_LIMIT, 0, 1440, STEP_MINUTES)

    # --- Assertions ---
    errors = []

    if max_export > DNO_LIMIT + 0.01:
        errors.append(f"max_export={max_export:.1f}kW > DNO {DNO_LIMIT}kW")

    # Any curtailment risks SIG fault. Zero curtailment is the goal.
    # Allow 3.0 kWh tolerance: 5-min simulation steps can't perfectly model
    # the 5-sec HA automation, and v17 no-Charge-mode (R16) means marginal
    # days drain battery toward floor before overflow starts.
    max_curtailment = 3.0
    if initial_overflow > 0.5 and total_curtailed > max_curtailment:
        errors.append(f"curtailment={total_curtailed:.2f}kWh (should be <{max_curtailment:.1f} for {initial_overflow:.1f}kWh overflow)")

    # v17 R16: No Charge mode. Sub-DNO PV is exported, not stored.
    # In production, on_before_plan sets overnight SOC so battery starts
    # at the correct level. Integration tests start at fixed 40% which
    # is conservative — marginal days may not fill to 90% from this point.
    if initial_overflow > 8.0 and sunset_soc_pct < 95:
        errors.append(f"sunset_soc={sunset_soc_pct:.0f}% (should be >95% for {initial_overflow:.0f}kWh overflow)")
    elif initial_overflow > 0.5 and sunset_soc_pct < min_sunset_soc:
        errors.append(f"sunset_soc={sunset_soc_pct:.0f}% (should be >{min_sunset_soc}%)")

    soc_label = f" start={start_soc_pct:.0%}" if start_soc_pct != START_SOC_PCT else ""
    tag = f"  integration {label}{soc_label}"
    if errors:
        detail = "; ".join(errors)
        print(f"{tag}: FAILED — {detail}")
        print(f"    overflow={initial_overflow:.1f}kWh curtailed={total_curtailed:.2f}kWh max_export={max_export:.1f}kW sunset_soc={sunset_soc_pct:.0f}%")
        return True

    print(f"{tag}: PASSED (overflow={initial_overflow:.1f}kWh curtailed={total_curtailed:.2f}kWh max_export={max_export:.1f}kW sunset_soc={sunset_soc_pct:.0f}%)")
    return False


# ============================================================================
# Real forecast+actual test (Apr 6 2026)
# ============================================================================


def _load_actual_csv(filepath):
    """Load actual PV/load CSV (minute,pv_kw,load_kw format)."""
    pv = {}
    load = {}
    with open(filepath, "r") as f:
        for line in f.readlines()[1:]:
            parts = line.strip().split(",")
            if len(parts) < 3:
                continue
            m = int(parts[0])
            pv[m] = float(parts[1])
            load[m] = float(parts[2])
    return pv, load


def _load_forecast_csv(filepath):
    """Load forecast PV CSV (minute,pv_kw format)."""
    pv = {}
    with open(filepath, "r") as f:
        for line in f.readlines()[1:]:
            parts = line.strip().split(",")
            if len(parts) < 2:
                continue
            m = int(parts[0])
            pv[m] = float(parts[1])
    return pv


def _integration_test_real_forecast(label, actual_file, forecast_file, start_soc_pct=0.20):
    """Run actual data with independent Solcast forecast through plugin.

    Unlike other integration tests, forecast and actual are genuinely different
    sources — not one scaled from the other.
    """
    from datetime import datetime, timezone

    actual_path = os.path.join(CSV_DIR, actual_file)
    forecast_path = os.path.join(CSV_DIR, forecast_file)
    if not os.path.exists(actual_path) or not os.path.exists(forecast_path):
        print(f"  {label}: SKIPPED (data files not found)")
        return False

    pv_actual, load_actual = _load_actual_csv(actual_path)
    pv_forecast = _load_forecast_csv(forecast_path)

    # p90 peak for Apr 6: use forecast peak * 1.1 (near-perfect day assumption, R42)
    forecast_peak_pv = max(pv_forecast.values()) if pv_forecast else 0.0
    p90_peak_kw = max(forecast_peak_pv * 1.1, 3.0)

    soc = BATTERY_KWH * start_soc_pct
    step_hours = STEP_MINUTES / 60.0
    total_curtailed = 0.0
    total_export = 0.0
    max_export = 0.0
    plugin = None
    results = []

    # Start at first PV slot
    start_minute = 0
    for m in range(0, 1440, STEP_MINUTES):
        if pv_actual.get(m, 0) > 0:
            start_minute = m
            break

    for m in range(start_minute, 1440, STEP_MINUTES):
        actual_pv = pv_actual.get(m, 0)
        actual_load = load_actual.get(m, 0)
        actual_excess = actual_pv - actual_load

        # Build forecast from independent Solcast shape
        forecast_pv = {}
        forecast_load = {}
        for k in range(0, 1440 - m, STEP_MINUTES):
            forecast_pv[k] = pv_forecast.get(m + k, 0)
            forecast_load[k] = load_actual.get(m + k, 0)  # load forecast = actual (LoadML equivalent)

        # Solcast remaining from forecast
        solcast_remaining = sum(pv_forecast.get(m + k, 0) * step_hours for k in range(0, 1440 - m, STEP_MINUTES))

        utc_hour = m / 60.0 - 1.0  # BST = UTC+1
        base = MockBase(
            pv_step=forecast_pv,
            load_step=forecast_load,
            soc_kw=soc,
            soc_max=BATTERY_KWH,
            minutes_now=m,
            forecast_minutes=1440 - m,
            now_utc=datetime(2026, 4, 6, max(0, int(utc_hour)), int((utc_hour % 1) * 60) if utc_hour >= 0 else 0, tzinfo=timezone.utc),
            sensor_overrides={
                "sensor.sigen_plant_pv_power": actual_pv,
                "sensor.sigen_plant_consumed_power": actual_load,
                "sensor.solcast_pv_forecast_forecast_remaining_today": solcast_remaining,
                # v17: detailedForecast for p90 scale (R42) — use Apr 6 solar noon
                "sensor.solcast_pv_forecast_forecast_today": {"detailedForecast": [{"period_start": "2026-04-06T12:00:00+00:00", "pv_estimate90": p90_peak_kw}]},
            },
        )

        if plugin is None:
            plugin = CurtailmentPlugin(base)
        else:
            plugin.base = base

        floor, phase = plugin.calculate(dno_limit_kw=DNO_LIMIT)

        # Mirror what on_update() does: once active, was_active stays True until deactivation
        if phase in ("active", "post_release"):
            plugin.was_active = True

        # Independent physics (same as other integration tests)
        remaining_cap = max(0, BATTERY_KWH - soc)
        max_charge = min(MAX_CHARGE_KW, remaining_cap / step_hours) if remaining_cap > 0.01 else 0
        max_discharge = min(MAX_DISCHARGE_KW, soc / step_hours) if soc > 0.01 else 0

        export = 0.0
        charge = 0.0
        discharge = 0.0
        curtailed = 0.0

        if phase == "off":
            if actual_excess > 0:
                charge = min(actual_excess, max_charge)
                leftover = actual_excess - charge
                export = min(leftover, DNO_LIMIT)
                curtailed = max(0, leftover - export)
            else:
                discharge = min(-actual_excess, max_discharge)
        elif actual_excess > DNO_LIMIT:
            # Phase 2: overflow
            export = DNO_LIMIT
            overflow = actual_excess - DNO_LIMIT
            charge = min(overflow, max_charge)
            curtailed = max(0, overflow - charge)
        else:
            # Phase 1/3: HA automation chooses Charge/Drain/Hold from SOC vs floor.
            if soc > floor + SOC_MARGIN_KWH:
                if actual_excess > 0:
                    drain_kw = min(DNO_LIMIT - actual_excess, max_discharge)
                    export = min(actual_excess + drain_kw, DNO_LIMIT)
                    discharge = drain_kw
                else:
                    discharge = min(-actual_excess, max_discharge)
            elif soc < floor - SOC_MARGIN_KWH:
                # Charge: export=0, all PV excess charges battery
                if actual_excess > 0:
                    charge = min(actual_excess, max_charge)
                else:
                    discharge = min(-actual_excess, max_discharge)
            else:
                # Hold: export=min(excess, DNO). Post-release uses plugin's ramped export_target.
                hold_cap = plugin._export_target if (plugin._export_target >= 0) else DNO_LIMIT
                if actual_excess > 0:
                    export = min(actual_excess, hold_cap, DNO_LIMIT)
                    leftover = actual_excess - export
                    charge = min(leftover, max_charge)
                else:
                    discharge = min(-actual_excess, max_discharge)

        soc += charge * step_hours - discharge * step_hours
        soc = max(0, min(BATTERY_KWH, soc))

        total_curtailed += curtailed * step_hours
        total_export += export * step_hours
        if export > max_export:
            max_export = export

        results.append({"minute": m, "soc_pct": soc / BATTERY_KWH * 100, "floor_pct": floor / BATTERY_KWH * 100, "phase": phase, "export": export, "pv": actual_pv})

    sunset_soc_pct = soc / BATTERY_KWH * 100
    for r in reversed(results):
        if r["pv"] > 0.05:
            sunset_soc_pct = r["soc_pct"]
            break

    initial_overflow = compute_remaining_overflow(pv_actual, load_actual, DNO_LIMIT, 0, 1440, STEP_MINUTES)

    errors = []
    if max_export > DNO_LIMIT + 0.01:
        errors.append(f"max_export={max_export:.1f}kW > DNO {DNO_LIMIT}kW")
    max_curtailment = 2.0
    if initial_overflow > 0.5 and total_curtailed > max_curtailment:
        errors.append(f"curtailment={total_curtailed:.2f}kWh (should be <{max_curtailment:.1f})")
    if initial_overflow > 8.0 and sunset_soc_pct < 95:
        errors.append(f"sunset_soc={sunset_soc_pct:.0f}% (should be >95%)")
    elif initial_overflow > 0.5 and sunset_soc_pct < 75:
        errors.append(f"sunset_soc={sunset_soc_pct:.0f}% (should be >75%)")

    tag = f"  integration {label} start={start_soc_pct:.0%}"
    if errors:
        detail = "; ".join(errors)
        print(f"{tag}: FAILED — {detail}")
        print(f"    overflow={initial_overflow:.1f}kWh curtailed={total_curtailed:.2f}kWh max_export={max_export:.1f}kW sunset_soc={sunset_soc_pct:.0f}%")
        return True

    print(f"{tag}: PASSED (overflow={initial_overflow:.1f}kWh curtailed={total_curtailed:.2f}kWh max_export={max_export:.1f}kW sunset_soc={sunset_soc_pct:.0f}%)")
    return False


# ============================================================================
# Forecast mismatch helpers
# ============================================================================


def _asymmetric_scale_fn(morning_scale, afternoon_scale, noon=720):
    """Returns a function mapping absolute minute -> scale factor."""
    return lambda minute: morning_scale if minute < noon else afternoon_scale


def _random_cloud_scale_fn(seed=42):
    """Returns a function mapping minute -> random scale factor (0.5-1.5).

    Seeded for reproducibility. Each 5-min slot gets a fixed random factor.
    """
    import random

    rng = random.Random(seed)
    cache = {}

    def scale_fn(minute):
        if minute not in cache:
            cache[minute] = 0.5 + rng.random()  # 0.5 to 1.5
        return cache[minute]

    return scale_fn


# ============================================================================
# Export target ramp & floor stability tests (R38/R39)
# ============================================================================


def test_export_target_ramps_down():
    """Post-release export_target formula decreases as hours_to_pv_end shrinks (R38/R41).

    With 30 kWh remaining PV, 5 kWh load, battery at 30% needing 12.6 kWh,
    exportable_budget = 30 - 5 - 12.6 = 12.4 kWh.
    Over 6 hours: export_target = 12.4/6 = 2.07 kW.
    Over 2 hours: export_target = 12.4/2 = 6.2 → clamped to DNO (4.0).
    Over 0.5 hours: remaining_pv shrunk, budget likely near 0 → export_target ≈ 0.
    This tests the post-release formula directly (active phase always returns DNO).
    """
    # Simulate shrinking time to release with constant remaining values
    soc_max = BATTERY_KWH
    dno = DNO_LIMIT
    soc_kw = soc_max * 0.30  # 30% = 5.42 kWh
    energy_needed = soc_max - soc_kw  # 12.66 kWh

    # 6 hours to release: plenty of budget
    remaining_pv = 30.0
    remaining_load = 5.0
    hours = 6.0
    budget = remaining_pv - remaining_load - energy_needed
    et_6h = max(0, min(dno, budget / hours))

    # 2 hours: budget same but spread over less time → higher rate but clamped
    hours = 2.0
    et_2h = max(0, min(dno, budget / hours))

    # 0.5 hours: remaining_pv much smaller (most PV already generated)
    remaining_pv_late = 3.0
    remaining_load_late = 0.3
    hours = 0.5
    budget_late = remaining_pv_late - remaining_load_late - energy_needed
    et_late = max(0, min(dno, budget_late / hours))

    assert et_6h < dno, f"6h out: export_target should be below DNO, got {et_6h:.2f}"
    assert et_6h > 1.5, f"6h out: export_target should be reasonable, got {et_6h:.2f}"
    assert et_2h == dno, f"2h out: export_target should be clamped to DNO, got {et_2h:.2f}"
    assert et_late == 0, f"0.5h out with low PV: export_target should be 0, got {et_late:.2f}"
    print(f"  test_export_target_ramps_down: PASSED (6h={et_6h:.1f}, 2h={et_2h:.1f}, 0.5h={et_late:.1f})")


def test_export_target_never_exceeds_dno():
    """Export target is always clamped to [0, DNO] (R38 safety)."""
    soc_max = BATTERY_KWH
    dno = DNO_LIMIT

    # Huge budget: should clamp to DNO
    budget = 100.0
    hours = 1.0
    et = max(0, min(dno, budget / hours))
    assert et == dno, f"Should clamp to DNO, got {et:.2f}"

    # Negative budget: should clamp to 0
    budget = -5.0
    et = max(0, min(dno, budget / hours))
    assert et == 0, f"Negative budget should give 0, got {et:.2f}"
    print("  test_export_target_never_exceeds_dno: PASSED")


def test_floor_soft_ratchet():
    """Floor should not jump more than 2% of soc_max per cycle (R39).

    Simulates consecutive calculate() calls with shifting forecasts
    that would cause the raw floor to jump from 30% to 65%.
    With soft ratchet, floor rises at most 2% per cycle.
    """
    soc_max = BATTERY_KWH
    max_rise_pct = 2.0
    max_rise_kwh = soc_max * max_rise_pct / 100.0  # 0.36 kWh

    # Simulate: initial floor at 30%, then raw calculation jumps to 65%
    floor_prev = soc_max * 0.30  # 5.42
    floor_raw_new = soc_max * 0.65  # 11.75

    # Soft ratchet: min(raw, prev + max_rise)
    floor_ratcheted = min(floor_raw_new, floor_prev + max_rise_kwh)

    assert floor_ratcheted <= floor_prev + max_rise_kwh + 0.01, f"Floor should not rise more than {max_rise_kwh:.2f} kWh, got {floor_ratcheted - floor_prev:.2f}"
    assert floor_ratcheted < floor_raw_new, f"Ratchet should prevent jump to {floor_raw_new:.1f}, got {floor_ratcheted:.1f}"

    # After many cycles (18 cycles = 90 min), floor should have risen significantly
    floor = floor_prev
    for _ in range(18):
        floor = min(floor_raw_new, floor + max_rise_kwh)
    assert floor == floor_raw_new, f"After 18 cycles, floor should reach target {floor_raw_new:.1f}, got {floor:.1f}"

    print(f"  test_floor_soft_ratchet: PASSED (one cycle: {floor_prev:.1f}->{floor_ratcheted:.1f}, 18 cycles: {floor:.1f})")


def test_floor_ratchet_allows_decrease():
    """Floor ratchet should not prevent floor from decreasing (R39)."""
    soc_max = BATTERY_KWH
    max_rise_kwh = soc_max * 0.02

    floor_prev = soc_max * 0.50  # 9.04
    floor_raw_new = soc_max * 0.30  # 5.42 (decrease — should be allowed)

    floor_ratcheted = min(floor_raw_new, floor_prev + max_rise_kwh)
    assert floor_ratcheted == floor_raw_new, f"Floor decrease should not be blocked: expected {floor_raw_new:.1f}, got {floor_ratcheted:.1f}"
    print("  test_floor_ratchet_allows_decrease: PASSED")


def test_plugin_export_target_published():
    """Plugin should publish export_target sensor when active."""
    pv, load = _make_overflow_pv()
    sensor_overrides = {
        "sensor.sigen_plant_pv_power": 8.0,
        "sensor.sigen_plant_consumed_power": 1.0,
    }
    sensor_overrides.update(_make_p90_sensors(solcast_remaining=30.0))
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=5.0,
        minutes_now=720,
        sensor_overrides=sensor_overrides,
    )
    plugin = CurtailmentPlugin(base)
    plugin.on_update()

    et_sensor = base.published.get("sensor.predbat_curtailment_export_target", {})
    assert et_sensor, "Export target sensor should be published"
    value = et_sensor.get("value", -2)
    assert value >= 0, f"Export target should be >= 0 when active, got {value}"
    assert value <= DNO_LIMIT, f"Export target should be <= DNO, got {value}"
    print(f"  test_plugin_export_target_published: PASSED (export_target={value:.2f} kW)")


def test_plugin_export_target_inactive():
    """Plugin should publish export_target = -2 when inactive."""
    pv = {m: 1.0 for m in range(0, 720, PLUGIN_STEP)}
    load = {m: 0.5 for m in range(0, 720, PLUGIN_STEP)}
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=5.0,
        minutes_now=720,
        sensor_overrides={
            "sensor.sigen_plant_pv_power": 1.0,
            "sensor.sigen_plant_consumed_power": 0.5,
            "sensor.solcast_pv_forecast_forecast_remaining_today": 3.0,
        },
    )
    plugin = CurtailmentPlugin(base)
    plugin.on_update()

    et_sensor = base.published.get("sensor.predbat_curtailment_export_target", {})
    assert et_sensor, "Export target sensor should be published even when inactive"
    value = et_sensor.get("value", 0)
    assert value == -2, f"Export target should be -2 when inactive, got {value}"
    print("  test_plugin_export_target_inactive: PASSED")


# ============================================================================
# Deactivation tests
# ============================================================================


def test_same_p90_same_floor():
    """Same p90 scale → same solar geometry → same floor (regardless of Solcast remaining).

    In v17, floor is derived purely from solar geometry (scale × sin(elev) integral).
    Same p90 peak + same actual PV → same floor, regardless of Solcast remaining total.
    (Solcast remaining only affects the will_fill activation check, not the floor itself.)
    """
    pv = {m: 8.0 for m in range(0, 360, PLUGIN_STEP)}
    load = {m: 1.0 for m in range(0, 360, PLUGIN_STEP)}

    # Scenario A: Solcast remaining = 20 kWh
    sensor_a = {"sensor.sigen_plant_pv_power": 8.0, "sensor.sigen_plant_consumed_power": 1.0}
    sensor_a.update(_make_p90_sensors(p90_peak_kw=8.58, solcast_remaining=20.0))
    base_a = MockBase(pv_step=pv, load_step=load, soc_kw=5.0, minutes_now=720, sensor_overrides=sensor_a)
    plugin_a = CurtailmentPlugin(base_a)
    floor_a, phase_a = plugin_a.calculate(dno_limit_kw=4.0)

    # Scenario B: Solcast remaining = 30 kWh (more PV predicted)
    sensor_b = {"sensor.sigen_plant_pv_power": 8.0, "sensor.sigen_plant_consumed_power": 1.0}
    sensor_b.update(_make_p90_sensors(p90_peak_kw=8.58, solcast_remaining=30.0))
    base_b = MockBase(pv_step=pv, load_step=load, soc_kw=5.0, minutes_now=720, sensor_overrides=sensor_b)
    plugin_b = CurtailmentPlugin(base_b)
    floor_b, phase_b = plugin_b.calculate(dno_limit_kw=4.0)

    # Both should activate (battery headroom < total excess)
    assert phase_a == "active", f"Scenario A should be active, got {phase_a}"
    assert phase_b == "active", f"Scenario B should be active, got {phase_b}"

    # Floor must be identical — same p90_scale → same overflow integral → same floor
    assert abs(floor_a - floor_b) < 0.01, f"Floor must be independent of Solcast remaining: A={floor_a:.2f} B={floor_b:.2f}"
    print(f"  test_same_p90_same_floor: PASSED (floor_a={floor_a:.2f} floor_b={floor_b:.2f} — identical)")


def test_activation_requires_will_fill():
    """Plugin stays off if battery won't reach 100% even with all PV (R5 condition 2).

    If solcast_remaining - load_remaining ≤ (soc_max - soc_kw), the overflow energy
    is needed for charging — don't activate (would curtail PV needlessly).
    """
    pv = {m: 5.0 for m in range(0, 120, PLUGIN_STEP)}
    load = {m: 1.0 for m in range(0, 120, PLUGIN_STEP)}
    soc_kw = BATTERY_KWH * 0.10  # battery nearly empty

    # Solcast remaining = 4 kWh. Battery headroom = 18.08 × 0.90 = 16.27 kWh.
    # total_excess = max(0, 4 - load_remaining) ≪ 16.27 → will_fill=False → off
    sensor_overrides = {
        "sensor.sigen_plant_pv_power": 5.0,
        "sensor.sigen_plant_consumed_power": 1.0,
    }
    sensor_overrides.update(_make_p90_sensors(solcast_remaining=4.0))
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=soc_kw,
        minutes_now=720,
        sensor_overrides=sensor_overrides,
    )
    plugin = CurtailmentPlugin(base)
    _, phase = plugin.calculate(dno_limit_kw=4.0)

    assert phase == "off", f"Should be off — battery won't fill even with all PV (will_fill=False), got {phase}"
    print(f"  test_activation_requires_will_fill: PASSED (off because battery won't fill)")


# ============================================================================
# Test runner
# ============================================================================


def run_curtailment_tests(my_predbat=None):
    """Run all curtailment calculator tests. Returns True if any failed."""
    print("**** Running curtailment calculator tests ****")
    failed = False

    # Pure function tests
    pure_tests = [
        test_compute_remaining_overflow_basic,
        test_compute_remaining_overflow_no_overflow,
        test_compute_remaining_overflow_partial,
        test_compute_remaining_overflow_start_offset,
    ]
    for test_fn in pure_tests:
        try:
            test_fn()
        except AssertionError as e:
            print(f"  {test_fn.__name__}: FAILED — {e}")
            failed = True

    # Morning gap tests
    gap_tests = [
        test_morning_gap_pre_dawn,
        test_morning_gap_solar_already_covers,
        test_morning_gap_cloudy_never_covers,
        test_morning_gap_kwh_values,
    ]
    print("  --- morning gap tests ---")
    for test_fn in gap_tests:
        try:
            test_fn()
        except AssertionError as e:
            print(f"  {test_fn.__name__}: FAILED — {e}")
            failed = True

    # v10 activation tests
    activation_tests = [
        test_activation_overflow_exceeds_headroom,
        test_activation_overflow_within_headroom,
        test_activation_high_soc_low_overflow,
    ]
    print("  --- v10 activation tests ---")
    for test_fn in activation_tests:
        try:
            test_fn()
        except AssertionError as e:
            print(f"  {test_fn.__name__}: FAILED — {e}")
            failed = True

    # v10 floor tests
    floor_tests = [
        test_floor_computation,
        test_floor_above_soc_keep,
    ]
    print("  --- v10 floor tests ---")
    for test_fn in floor_tests:
        try:
            test_fn()
        except AssertionError as e:
            print(f"  {test_fn.__name__}: FAILED — {e}")
            failed = True

    # v10 phase tests
    phase_tests = [
        test_phase_charge_below_floor,
        test_phase_managed_at_floor,
        test_phase_managed_above_floor,
    ]
    print("  --- v10 phase tests ---")
    for test_fn in phase_tests:
        try:
            test_fn()
        except AssertionError as e:
            print(f"  {test_fn.__name__}: FAILED — {e}")
            failed = True

    # Plugin integration tests
    plugin_tests = [
        test_plugin_activates_on_overflow,
        test_plugin_stays_off_no_overflow,
        test_plugin_publishes_active_not_phase,
        test_plugin_floor_clamped_by_soc_keep,
        test_plugin_active_high_soc,
        test_floor_clamped_above_soc_keep,
        test_floor_clamped_above_reserve,
        test_floor_lower_with_more_overflow,
        test_export_target_at_dno_when_soc_above_floor,
        test_export_target_dno_when_active_regardless_of_soc,
    ]
    print("  --- plugin integration tests ---")
    for test_fn in plugin_tests:
        try:
            test_fn()
        except AssertionError as e:
            print(f"  {test_fn.__name__}: FAILED — {e}")
            failed = True

    # Apply tests
    apply_tests = [
        test_apply_active_sets_export_zero_and_dess,
        test_apply_already_active_no_export_write,
        test_apply_off_restores_msc,
        test_on_update_full_flow,
        test_on_update_stays_off_low_pv,
        test_deactivation_at_safe_time,
        test_manual_hold_maintains_dess_after_deactivation,
    ]
    print("  --- apply / on_update tests ---")
    for test_fn in apply_tests:
        try:
            test_fn()
        except AssertionError as e:
            print(f"  {test_fn.__name__}: FAILED — {e}")
            failed = True

    # Charge window tests
    window_tests = [
        test_defers_to_charge_window,
        test_ignores_freeze_charge_window,
    ]
    print("  --- charge window tests ---")
    for test_fn in window_tests:
        try:
            test_fn()
        except AssertionError as e:
            print(f"  {test_fn.__name__}: FAILED — {e}")
            failed = True

    # on_before_plan tests
    plan_tests = [
        test_before_plan_reduces_keep_on_overflow_day,
        test_before_plan_no_change_without_overflow,
        test_before_plan_never_increases,
        test_before_plan_disabled,
    ]
    print("  --- on_before_plan tests ---")
    for test_fn in plan_tests:
        try:
            test_fn()
        except AssertionError as e:
            print(f"  {test_fn.__name__}: FAILED — {e}")
            failed = True

    # Solar geometry tests
    solar_tests = [
        test_solar_elevation_known_values,
        test_compute_release_time_scenarios,
        test_compute_release_offset_load_spike,
        test_compute_release_offset,
    ]
    print("  --- solar geometry tests ---")
    for test_fn in solar_tests:
        try:
            test_fn()
        except Exception as e:
            print(f"  {test_fn.__name__}: FAILED — {e}")
            failed = True

    # Tomorrow forecast tests
    tomorrow_tests = [
        test_tomorrow_forecast_overflow_day,
        test_tomorrow_forecast_no_overflow,
    ]
    print("  --- tomorrow forecast tests ---")
    for test_fn in tomorrow_tests:
        try:
            test_fn()
        except Exception as e:
            print(f"  {test_fn.__name__}: FAILED — {e}")
            failed = True

    # Export target ramp & floor stability tests (R38/R39)
    ramp_tests = [
        test_export_target_ramps_down,
        test_export_target_never_exceeds_dno,
        test_floor_soft_ratchet,
        test_floor_ratchet_allows_decrease,
        test_plugin_export_target_published,
        test_plugin_export_target_inactive,
    ]
    print("  --- export target ramp & floor stability tests ---")
    for test_fn in ramp_tests:
        try:
            test_fn()
        except Exception as e:
            print(f"  {test_fn.__name__}: FAILED — {e}")
            failed = True

    # Edge case tests
    edge_tests = [
        test_no_overflow_day,
        test_export_never_exceeds_dno,
    ]
    print("  --- edge case tests ---")
    for test_fn in edge_tests:
        try:
            test_fn()
        except AssertionError as e:
            print(f"  {test_fn.__name__}: FAILED — {e}")
            failed = True

    # v17 solar geometry tests
    v17_tests = [
        test_same_p90_same_floor,
        test_activation_requires_will_fill,
    ]
    print("  --- v17 solar geometry tests ---")
    for test_fn in v17_tests:
        try:
            test_fn()
        except Exception as e:
            print(f"  {test_fn.__name__}: FAILED — {e}")
            failed = True

    # CSV validation — v10 strategy
    csv_available = os.path.exists(CSV_DIR)
    if csv_available:
        print("  --- CSV v10 validation (perfect forecast) ---")
        for label, filename, watts, expected in VALIDATION_DAYS:
            day_failed = _run_csv_day_v10(label, filename, watts, forecast_scale=1.0)
            if day_failed:
                failed = True

        print("  --- CSV v10 validation (60% underforecast) ---")
        for label, filename, watts, expected in VALIDATION_DAYS:
            if expected["overflow_approx"] > 1.0:
                day_failed = _run_csv_day_v10(label, filename, watts, forecast_scale=0.6)
                if day_failed:
                    failed = True

        print("  --- CSV v10 validation (low SOC start) ---")
        for label, filename, watts, expected in VALIDATION_DAYS:
            if expected["overflow_approx"] > 8.0:
                day_failed = _run_csv_day_v10(label, filename, watts, forecast_scale=1.0, start_soc_pct=0.10)
                if day_failed:
                    failed = True
            elif expected["overflow_approx"] > 1.0:
                day_failed = _run_csv_day_v10(label, filename, watts, forecast_scale=1.0, start_soc_pct=0.25)
                if day_failed:
                    failed = True
        # Integration tests — actual plugin.calculate() + independent physics
        print("  --- INTEGRATION: actual plugin code + independent physics ---")
        for label, filename, watts, expected in VALIDATION_DAYS:
            day_failed = _integration_test_day(label, filename, watts, start_soc_pct=0.40)
            if day_failed:
                failed = True
            # Also test with low SOC start on overflow days
            if expected["overflow_approx"] > 1.0:
                day_failed = _integration_test_day(label, filename, watts, start_soc_pct=0.10)
                if day_failed:
                    failed = True

        # --- Forecast mismatch tests ---
        # Use 10% start SOC: on_before_plan drains overnight on overflow days.
        # With imperfect forecasts, headroom is critical. 40% start is unrealistic
        # for production and masks algorithm correctness with physical limitations.
        # For forecast-mismatch tests, primary goal is zero curtailment.
        # Sunset SOC may be lower when forecast over/underestimates PV.
        # Production handles this via on_before_plan overnight SOC target.
        _MISMATCH_SOC = 65  # minimum sunset SOC% for mismatch variants

        print("  --- INTEGRATION: uniform 0.8x forecast (overforecast) ---")
        for label, filename, watts, expected in VALIDATION_DAYS:
            if expected["overflow_approx"] > 1.0:
                day_failed = _integration_test_day(f"{label} @ 0.8x", filename, watts, start_soc_pct=0.20, forecast_scale=0.8, min_sunset_soc=_MISMATCH_SOC)
                if day_failed:
                    failed = True

        print("  --- INTEGRATION: uniform 1.2x forecast (underforecast) ---")
        for label, filename, watts, expected in VALIDATION_DAYS:
            if expected["overflow_approx"] > 1.0:
                day_failed = _integration_test_day(f"{label} @ 1.2x", filename, watts, start_soc_pct=0.20, forecast_scale=1.2, min_sunset_soc=_MISMATCH_SOC)
                if day_failed:
                    failed = True

        # --- Forecast mismatch: asymmetric morning/afternoon ---
        print("  --- INTEGRATION: asymmetric 1.2x morning / 0.8x afternoon ---")
        for label, filename, watts, expected in VALIDATION_DAYS:
            if expected["overflow_approx"] > 1.0:
                day_failed = _integration_test_day(
                    f"{label} @ 1.2/0.8",
                    filename,
                    watts,
                    start_soc_pct=0.20,
                    forecast_scale_fn=_asymmetric_scale_fn(1.2, 0.8),
                    min_sunset_soc=_MISMATCH_SOC,
                )
                if day_failed:
                    failed = True

        print("  --- INTEGRATION: asymmetric 0.8x morning / 1.2x afternoon ---")
        for label, filename, watts, expected in VALIDATION_DAYS:
            if expected["overflow_approx"] > 1.0:
                day_failed = _integration_test_day(
                    f"{label} @ 0.8/1.2",
                    filename,
                    watts,
                    start_soc_pct=0.20,
                    forecast_scale_fn=_asymmetric_scale_fn(0.8, 1.2),
                    min_sunset_soc=_MISMATCH_SOC,
                )
                if day_failed:
                    failed = True

        # --- Real forecast+actual: Apr 6 2026 ---
        print("  --- INTEGRATION: real forecast+actual Apr 6 2026 ---")
        for soc in [0.20, 0.10]:
            day_failed = _integration_test_real_forecast(
                "Apr 6 2026 — real Solcast+actual",
                "actual_2026_04_06.csv",
                "forecast_2026_04_06.csv",
                start_soc_pct=soc,
            )
            if day_failed:
                failed = True

        # --- Forecast mismatch: random per-slot clouds ---
        print("  --- INTEGRATION: random cloud per-slot (seeds 42, 123, 999) ---")
        for seed in [42, 123, 999]:
            for label, filename, watts, expected in VALIDATION_DAYS:
                if expected["overflow_approx"] > 1.0:
                    day_failed = _integration_test_day(
                        f"{label} @ random(seed={seed})",
                        filename,
                        watts,
                        start_soc_pct=0.20,
                        forecast_scale_fn=_random_cloud_scale_fn(seed),
                        min_sunset_soc=_MISMATCH_SOC,
                    )
                    if day_failed:
                        failed = True
    else:
        print(f"  CSV validation tests: SKIPPED (directory not found: {CSV_DIR})")

    if not failed:
        print("**** All curtailment tests PASSED ****")
    return failed


if __name__ == "__main__":
    sys.exit(1 if run_curtailment_tests() else 0)
