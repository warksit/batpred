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
from datetime import datetime
from datetime import datetime as _dt
from datetime import timezone as _tz

# Ensure apps/predbat is on the path when run standalone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from curtailment_calc import (
    compute_remaining_overflow,
    compute_morning_gap,
    solar_elevation,
    compute_release_time,
    compute_tomorrow_forecast,
    p_scales_from_forecast,
    compute_expected_overflow,
    compute_pv_start_time,
    apply_no_surplus_drain_hold,
    compute_charge_below,
    compute_drain_above,
    compute_drain_above_source,
    classify_forecast_tracking,
    forecast_energy_to_now,
    day_tracking_ratio,
    p_scales_from_forecast,
    p90_scale_from_forecast,
    estimate_session_end_kwh,
    DEEP_DISCHARGE_FLOOR_KWH,
    compute_proposed_phase,
    compute_p10_recovery_floor,
    compute_shed_rate,
    compute_overflow_fits_margin,
    smooth_overflow_samples,
    required_headroom_kwh,
    compute_no_overflow_charge_target,
    compute_session_reserve,
    compute_max_sheddable,
    drain_deadline_breached,
    compute_effective_export_cap,
    compute_floor_with_source,
    should_defer_to_charge,
)

# Battery constants (Mum's SIG system)
BATTERY_KWH = 18.08
MAX_CHARGE_KW = 5.5
MAX_DISCHARGE_KW = 5.5
DNO_LIMIT = 4.0
STEP_MINUTES = 5
START_SOC_PCT = 0.40

# v18 constants (match curtailment_plugin.py)
# The v10 SIMULATOR's safety factor, deliberately frozen at the historical 1.2.
# This is NOT the production constant — that is curtailment_plugin.
# OVERFLOW_SAFETY_FACTOR = 1.05, pinned by test_r9_safety_factor_is_1_05.
#
# It was called OVERFLOW_SAFETY_FACTOR here and shadowed the real one at module
# scope, so a test that computed an expected value with it silently disagreed
# with the function under test (2026-08-06, RD28). Renamed so that can't recur.
# The scenario tests below model the OLD activation/floor behaviour, so they keep
# 1.2 on purpose — do not "fix" the value.
V10_SIM_SAFETY_FACTOR = 1.2
MAX_RESERVED_KWH = 1.8  # v19: ceiling on tapered buffer (= 10% of soc_max)
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
        active = remaining_overflow * V10_SIM_SAFETY_FACTOR > max(headroom, 0)

        # --- v10 floor ---
        if active:
            floor = battery_kwh - remaining_overflow * V10_SIM_SAFETY_FACTOR
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


def test_morning_gap_cloudy_dawn():
    """Cloudy/late dawn: PV stays below sunrise threshold (0.3 kW) all
    morning. With PV-magnitude state machine, walk starts in PHASE_NIGHT
    (first slot pv=0 < 0.1) and never breaks (pv never sustained ≥ 0.3),
    so we accumulate the full window's deficit."""
    pv = {}
    load = {}
    for m in range(0, 480, 5):
        pv[m] = 0.05  # below pv_off_threshold (0.1) → counted as night
        load[m] = 1.0
    gap = compute_morning_gap(pv, load, start_minute=0, end_minute=480, step_minutes=5)
    # 8 hours * (1.0 - 0.05) = 7.6 kWh
    assert 7.0 < gap < 8.0, f"Expected ~7.6 kWh gap (continuous night, no sunrise), got {gap:.2f}"
    print("  test_morning_gap_cloudy_dawn: PASSED (gap={:.2f}kWh)".format(gap))


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


def test_morning_gap_zero_zero_slots_do_not_terminate_walk():
    """Bug 2026-05-03 (live): when forecast slots have BOTH pv=0 and load=0
    (e.g. sparse LoadML data overnight), the walk treats `pv >= load` as
    'solar covering load' and breaks after 6 consecutive such slots.

    Result on live: morning_gap collapsed to 0.39 kWh instead of full
    overnight load (~5 kWh), driving target_soc to 5%.

    Scenario: deficit, then 1h zero-zero (sparse data), then deficit again.
    Walk must continue through zero-zero stretches and accumulate the
    full deficit on either side."""
    pv = {}
    load = {}
    # 0-2h: real deficit (load > pv)
    # 2-3h: zero-zero (sparse forecast data)
    # 3-5h: real deficit again
    for m in range(0, 300, 5):
        hour = m / 60.0
        if 2 <= hour < 3:
            pv[m] = 0.0
            load[m] = 0.0
        else:
            pv[m] = 0.0
            load[m] = 0.5
    gap = compute_morning_gap(pv, load, start_minute=0, end_minute=300, step_minutes=5)

    # Real deficit total: (2h + 2h) * 0.5 kW = 2.0 kWh.
    # Buggy code breaks after the 2-3h zero-zero stretch, returning ~1.0 kWh.
    assert gap > 1.5, f"Zero-zero slots must not terminate walk; got gap={gap:.2f} kWh (expected ≥ 2.0)"
    print(f"  test_morning_gap_zero_zero_slots_do_not_terminate_walk: PASSED (gap={gap:.2f} kWh)")


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
    assert overflow * V10_SIM_SAFETY_FACTOR > headroom, f"Should activate: {overflow * V10_SIM_SAFETY_FACTOR:.1f} > {headroom:.1f}"
    print("  test_activation_overflow_exceeds_headroom: PASSED")


def test_activation_overflow_within_headroom():
    """Plugin stays off when overflow * 1.10 <= headroom to 95%."""
    # 3kWh overflow, battery at 50% (9.04kWh), headroom = 95%*18.08 - 9.04 = 8.14
    # 3 * 1.10 = 3.3 <= 8.14 → off
    soc = BATTERY_KWH * 0.50
    headroom = BATTERY_KWH * SOC_CAP_FACTOR - soc
    overflow = 3.0
    assert overflow * V10_SIM_SAFETY_FACTOR <= headroom, f"Should stay off: {overflow * V10_SIM_SAFETY_FACTOR:.1f} <= {headroom:.1f}"
    print("  test_activation_overflow_within_headroom: PASSED")


def test_activation_high_soc_low_overflow():
    """High SOC with moderate overflow activates (small headroom)."""
    # Battery at 90% (16.27kWh), headroom = 17.18 - 16.27 = 0.91
    # 2kWh overflow * 1.10 = 2.2 > 0.91 → activate
    soc = BATTERY_KWH * 0.90
    headroom = BATTERY_KWH * SOC_CAP_FACTOR - soc
    overflow = 2.0
    assert overflow * V10_SIM_SAFETY_FACTOR > headroom, f"Should activate: {overflow * V10_SIM_SAFETY_FACTOR:.1f} > {headroom:.1f}"
    print("  test_activation_high_soc_low_overflow: PASSED")


# ============================================================================
# R59 — P10 recovery floor (proposed addition to R54)
#
# Lower-bound on floor: even on a P10 (worst-case PV) day the battery must
# still recover to overnight_target by sundown. P10 = 90% chance actual PV
# exceeds this, so it's the conservative-charging case.
# ============================================================================


def test_p10_recovery_floor_huge_pv_runway():
    """Lots of P10 PV ahead → floor near zero (we'll easily recover).

    R59b note (2026-07-28): the charge side uses this same (PV - load) form.
    R59a briefly split them so charge_below netted against overflow instead;
    that pinned the floor at the overnight target from dawn and blocked the
    morning drain. See test_charge_recovery_floor_nets_against_generation_not_overflow.
    """
    floor = compute_p10_recovery_floor(overnight_target_kwh=9.4, p10_pv_remaining_kwh=20.0, load_remaining_kwh=7.0)
    # potential = 20-7 = 13, target - potential = -3.6 → clamped to 0
    assert floor == 0.0, f"Expected 0.0 (huge runway), got {floor}"

    charge_floor = compute_p10_recovery_floor(overnight_target_kwh=9.4, p10_pv_remaining_kwh=20.0, load_remaining_kwh=7.0)
    assert charge_floor == floor, f"charge side must agree with drain side, got {charge_floor} vs {floor}"
    print(f"  test_p10_recovery_floor_huge_pv_runway: PASSED (drain={floor}, charge={charge_floor})")


def test_p10_recovery_floor_no_pv_remaining():
    """Sunset: no PV ahead, load still drains battery → floor must cover both
    overnight_target AND remaining load."""
    floor = compute_p10_recovery_floor(overnight_target_kwh=9.4, p10_pv_remaining_kwh=0.0, load_remaining_kwh=2.0)
    # net = 0 - 2 = -2 (deficit). floor = 9.4 - (-2) = 11.4
    assert abs(floor - 11.4) < 0.001, f"Expected 11.4 (target+load deficit), got {floor}"
    print(f"  test_p10_recovery_floor_no_pv_remaining: PASSED (floor={floor})")


def test_p10_recovery_floor_partial_charging():
    """Mid-afternoon: P10 PV partly covers → floor = remainder.

    R59b (2026-07-28): the charge side shares this form. The rising remainder is
    exactly what walks the Schmitt band from Hold into Solar Charge as the day
    runs out — see test_charge_recovery_floor_ramps_up_as_generation_runs_out.
    """
    floor = compute_p10_recovery_floor(overnight_target_kwh=9.4, p10_pv_remaining_kwh=10.0, load_remaining_kwh=5.0)
    # potential = 5, floor = 9.4 - 5 = 4.4
    assert abs(floor - 4.4) < 0.001, f"Expected 4.4 (partial), got {floor}"
    print(f"  test_p10_recovery_floor_partial_charging: PASSED (floor={floor})")


def test_p10_recovery_floor_load_exceeds_pv():
    """Cloudy day: load > P10 PV. Battery DRAINS through the day, so the floor
    must be RAISED above overnight_target by the deficit to compensate.

    Bug fix 2026-05-08: previously clamped potential to 0 and returned target,
    ignoring the through-day deficit. Real example today: P10=7.97, load=10.46,
    target=7.42 → old formula said 7.42, real answer is 9.91.
    """
    floor = compute_p10_recovery_floor(overnight_target_kwh=9.4, p10_pv_remaining_kwh=2.0, load_remaining_kwh=5.0)
    # net = 2 - 5 = -3 (deficit). floor = max(0, 9.4 - (-3)) = 12.4
    assert abs(floor - 12.4) < 0.001, f"Expected 12.4 (target raised to cover deficit), got {floor}"
    print(f"  test_p10_recovery_floor_load_exceeds_pv: PASSED (floor={floor}, deficit raises floor)")


def test_drain_above_curtailment_buffer_only():
    """v31: drain_above is PURE curtailment (overflow_floor) — effective_keep is
    no longer a drain target (Predbat owns the overnight/evening reserve; the
    recovery floor in charge_below is the handback backstop).
    """
    # effective_keep (7.42) must NOT pull the drain target down — drain to the
    # curtailment buffer (overflow_floor) only.
    drain = compute_drain_above(reserve=0.0, overflow_floor=17.87, effective_keep=7.42)
    assert abs(drain - 17.87) < 0.001, f"Expected 17.87 (overflow_floor, effective_keep ignored), got {drain}"
    print(f"  test_drain_above_curtailment_buffer_only: PASSED ({drain})")


def test_drain_above_overflow_floor_wins_on_big_overflow():
    """Big overflow day: overflow_floor < effective_keep → drain to overflow_floor."""
    drain = compute_drain_above(reserve=0.0, overflow_floor=10.0, effective_keep=15.0)
    assert abs(drain - 10.0) < 0.001, f"Expected 10.0 (overflow_floor wins), got {drain}"
    print(f"  test_drain_above_overflow_floor_wins_on_big_overflow: PASSED ({drain})")


def test_drain_above_reserve_floor():
    """Reserve never breached."""
    drain = compute_drain_above(reserve=2.0, overflow_floor=1.0, effective_keep=1.5)
    assert abs(drain - 2.0) < 0.001, f"Expected 2.0 (reserve), got {drain}"
    print(f"  test_drain_above_reserve_floor: PASSED ({drain})")


def test_drain_above_deep_discharge_floor():
    """Extreme-overflow day: overflow_floor=0 and R48 relaxed effective_keep to 0.5.

    The inner min(overflow_floor=0, effective_keep=0.5) is 0, which would drain
    the battery to absolute empty. drain_above must not drop below the 0.5 kWh
    deep-discharge buffer — 0.5 kWh of headroom is negligible against a multi-kWh
    overflow but protects the cell from a full bottom-out. R48 itself relaxes
    keep to 0.5 (not 0) for exactly this reason; the inner min must not undo it.
    """
    drain = compute_drain_above(reserve=0.0, overflow_floor=0.0, effective_keep=0.5)
    assert abs(drain - 0.5) < 0.001, f"Expected 0.5 (deep-discharge floor), got {drain}"
    print(f"  test_drain_above_deep_discharge_floor: PASSED ({drain})")


def test_charge_below_deep_discharge_floor():
    """Sunny-tomorrow day: R26 has relaxed best_soc_keep to 0; p10 recovery 0.

    The soc_keep clamp (REQUIREMENTS "soc_keep floor", 2026-05-08) protects
    against reporting charge_below < overnight need — but evaporates when
    overnight need itself is 0. The deep-discharge floor must apply
    symmetrically to charge_below so charge_target = min(charge_below,
    drain_above) never drops below 0.5 kWh. Without this, the YAML stays
    in Hold (exporting) while SOC = 0 — observed 2026-06-04 with battery
    at 0% during PV-load surplus + kettle transient → grid import.
    """
    cb = compute_charge_below(p10_recovery_floor=0.0, soc_keep=0.0)
    assert abs(cb - 0.5) < 0.001, f"Expected 0.5 (deep-discharge floor), got {cb}"
    print(f"  test_charge_below_deep_discharge_floor: PASSED ({cb})")


def test_charge_below_soc_keep_wins():
    """When soc_keep > deep-discharge floor, soc_keep wins (existing clamp)."""
    cb = compute_charge_below(p10_recovery_floor=0.2, soc_keep=2.0)
    assert abs(cb - 2.0) < 0.001, f"Expected 2.0 (soc_keep), got {cb}"
    print(f"  test_charge_below_soc_keep_wins: PASSED ({cb})")


def test_charge_below_p10_recovery_wins():
    """When p10_recovery > soc_keep, p10_recovery wins (cloudy-tomorrow day)."""
    cb = compute_charge_below(p10_recovery_floor=4.0, soc_keep=1.5)
    assert abs(cb - 4.0) < 0.001, f"Expected 4.0 (p10_recovery), got {cb}"
    print(f"  test_charge_below_p10_recovery_wins: PASSED ({cb})")


def test_R50a_floor_uses_p90_not_the_confidence_blend():
    """R50a: the live floor uses overflow_p90 (R7/R42/R43), not the R50 blend.

    Live 2026-07-28 09:00: p10=0.0, p50=1.57, p90=13.03, confidence=0.35,
    SOC 8.05 kWh, headroom 10.03 kWh against a p90 overflow of 13.03 kWh — already
    short of the headroom needed, and the blend still said Hold:

        blend (c=0.35)  expected  0.92 -> drain_above 16.07 kWh (88.9%) -> HOLD
        pure p90        expected 13.03 -> drain_above  0.64 kWh ( 3.6%) -> MAX EXPORT

    R25 forbids assuming no overflow: headroom is cheap to create early and
    impossible to create late. R43 is deliberately asymmetric toward MORE drain.
    Blending toward p10 inverted both.
    """
    soc_max, reserve = 18.08, 0.542
    p10, p50, p90 = 0.0, 1.57, 13.03

    def floor_from(overflow):
        return max(0.0, (soc_max - min(MAX_RESERVED_KWH, overflow)) - overflow * V10_SIM_SAFETY_FACTOR)

    # The blend is what we are moving AWAY from — assert it would have held.
    blended = compute_expected_overflow(p10, p50, p90, 0.35, 0.60, 0.85)
    assert abs(blended - 0.92) < 0.05, f"expected the documented 0.92 blend, got {blended}"
    assert 8.05 < compute_drain_above(reserve, floor_from(blended)), "blend must be the HOLD case this test exists to replace"

    # R50a: p90 is the live path -> Drain.
    drain_above_p90 = compute_drain_above(reserve, floor_from(p90))
    assert drain_above_p90 < 1.0, f"p90 floor should be near-empty, got {drain_above_p90}"
    assert 8.05 > drain_above_p90, "SOC above drain_above -> Max Export, which is the point"
    print(f"  test_R50a_floor_uses_p90_not_the_confidence_blend: PASSED (p90 drain_above={drain_above_p90:.2f})")


def test_R50a_incident_day_still_floored_by_r59b():
    """R50a: 2026-04-28 (R50's own justification) is safe under p90 + R59b.

    R50 exists because the battery hit 1.9% that day. Replaying its Solcast
    fixture through today's formula, the p90 floor stops the drain at 39.9% —
    so the p90 overflow estimate cannot have caused the bottom-out.

    R59b then has to leave a VALID Schmitt band (charge_below < drain_above) on
    a bright day, i.e. it must not pin charge_below at the overnight target the
    way R59a did. Mid-morning on an overflow day there is a large PV runway
    ahead, so the recovery floor is 0 and soc_keep carries charge_below.
    """
    soc_max, reserve = 18.08, 0.542
    p90_2804 = 7.56  # overflow from solcast_2026_04_28.json, DNO 4.0 (pre-swap)

    floor = max(0.0, (soc_max - min(MAX_RESERVED_KWH, p90_2804)) - p90_2804 * V10_SIM_SAFETY_FACTOR)
    drain_above = compute_drain_above(reserve, floor)

    # Bright late-April morning: generation still to come comfortably exceeds
    # the 7.0 kWh overnight target, so the recovery floor collapses to 0.
    recovery = compute_p10_recovery_floor(overnight_target_kwh=7.0, p10_pv_remaining_kwh=15.0, load_remaining_kwh=6.0)
    assert recovery == 0.0, f"PV runway ahead -> recovery floor 0, got {recovery}"
    charge_below = compute_charge_below(recovery, 4.0)

    assert drain_above / soc_max > 0.30, f"p90 must floor the incident day well above 1.9%, got {drain_above / soc_max:.1%}"
    assert charge_below < drain_above, f"R59b floor must sit below the drain target: {charge_below:.2f} vs {drain_above:.2f}"
    print(f"  test_R50a_incident_day_still_floored_by_r59b: PASSED (band [{charge_below / soc_max:.1%}, {drain_above / soc_max:.1%}])")


def test_override_keeps_cm_driving_even_when_the_select_says_predbat():
    """RD13a: under manual override the WRITER ROLE must follow the override,
    not the policy select.

    Live failure 2026-07-29 08:56. The user held override = "Hold Battery", but:
      - the plugin had earlier written "Predbat" to sig_dispatch_policy (RD4
        low-SOC handover at 3% SOC),
      - the manual branch then read that SELECT to decide the writer role,
      - concluded CM should not drive, and DISABLED the heartbeat.

    Nothing was left driving the dispatch register. It froze at 2.89 kW while PV
    climbed to 3.46, so the battery discharged 0.775 kW to make up the
    difference — at 3.1% SOC. That is not Hold, it is a stale setpoint.

    The override select has no "Predbat" option by design (RD13a), so holding
    ANY override means CM's executor must be driving.
    """
    base = MockBase()
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    base._sensor_overrides[SIG_OVERRIDE_SELECT] = "Hold Battery"
    base._sensor_overrides[SIG_POLICY_SELECT] = "Predbat"  # stale, from the low-SOC handover
    plugin = CurtailmentPlugin(base)
    plugin._charge_below, plugin._drain_above = 0.5, 0.54
    plugin._policy_override = None
    base.services.clear()
    plugin._publish_dispatch_policy(True, floor_kwh=0.54, soc_kwh=0.56, soc_max=18.08)

    svc = [(s, k.get("entity_id")) for s, k in base.services]
    assert ("automation/turn_on", "automation.sig_dispatch_heartbeat") in svc, f"override must keep the heartbeat driving, got {svc}"
    assert ("automation/turn_off", "automation.sig_dispatch_heartbeat") not in svc, f"must NOT disable the executor while an override is held: {svc}"
    print("  test_override_keeps_cm_driving_even_when_the_select_says_predbat: PASSED")


def test_intended_policy_reports_the_override_not_the_plugins_wish():
    """Under manual override the sensor must report what will ACTUALLY happen.

    Observed live 2026-07-29 08:44:
        intended = "Max Export"
        reason   = "manual override — user owns policy select"
        override = "Hold Battery"   (what the heartbeat was really dispatching)

    The reason was swapped for the override message but the STATE was left as
    the plugin's own choice, so the sensor contradicted itself and disagreed
    with the inverter. Third time in two days that three views of one decision
    disagreed; the Charter rule is that the card reports, it does not invent.

    The plugin's preference is still useful — it says what resumes when the
    override clears — so it moves into the reason rather than being dropped.
    """
    base = MockBase()
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    base._sensor_overrides[SIG_OVERRIDE_SELECT] = "Hold Battery"
    plugin = CurtailmentPlugin(base)
    # Band that would otherwise produce Drain -> Max Export.
    plugin._charge_below, plugin._drain_above = 0.5, 0.54
    plugin._policy_override = None
    plugin._publish_dispatch_policy(True, floor_kwh=0.54, soc_kwh=5.0, soc_max=18.08)

    pub = base.published["sensor.predbat_curtailment_intended_policy"]
    assert pub["value"] == "Hold Battery", f"state must be the override actually in force, got {pub['value']}"
    assert pub["attrs"]["manual_override"] is True
    assert "Max Export" in pub["attrs"]["reason"], f"reason must still say what the plugin would choose: {pub['attrs']['reason']}"
    # The reason must flag that a HUMAN owns the policy. A0 (f9316ccc) reworded
    # this from "manual override — …" to the at-a-glance "manual · <choice>
    # (plugin would <x>)"; the word "override" went with it. Assert the property,
    # not the old wording (R37: production was right, the assertion was stale).
    assert "manual" in pub["attrs"]["reason"].lower(), f"reason must show a human is in charge: {pub['attrs']['reason']}"
    print("  test_intended_policy_reports_the_override_not_the_plugins_wish: PASSED")


def test_override_is_the_select_alone_no_boolean():
    """RD13a (2026-07-28): manual override is ONE entity — `input_select.sig_override`.
    Override is on iff the select is anything but "Off".

    The boolean is gone. It was redundant state derivable from the select, so the
    only thing it could ever add was divergence: select says "Max Export" while
    the boolean says off (plugin quietly back in control), or the reverse. The
    first version of this change kept both and bridged them with an automation —
    a shim for a problem that only existed because of the second entity.
    """
    import curtailment_plugin as _cp

    assert not hasattr(_cp, "SIG_OVERRIDE_SELECT") or "input_boolean" not in getattr(_cp, "SIG_OVERRIDE_SELECT", ""), "the override boolean must be gone, not merely unused"
    assert _cp.SIG_OVERRIDE_SELECT == "input_select.sig_override"

    def override_state(value):
        base = MockBase()
        base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
        base._sensor_overrides[_cp.SIG_OVERRIDE_SELECT] = value
        plugin = CurtailmentPlugin(base)
        plugin._charge_below, plugin._drain_above = 8.0, 15.0
        plugin._policy_override = None
        base.services.clear()
        plugin._publish_dispatch_policy(True, floor_kwh=15.0, soc_kwh=5.0, soc_max=18.08)
        return base.published.get("sensor.predbat_curtailment_intended_policy", {}).get("attrs", {}).get("manual_override")

    assert override_state("Off") is False, "Off -> plugin drives"
    for held in ("Max Export", "Hold Battery", "Solar Charge Battery"):
        assert override_state(held) is True, f"{held} -> override active"
    print("  test_override_is_the_select_alone_no_boolean: PASSED")


def test_session_dispatch_belongs_to_the_heartbeat_not_the_plugin():
    """RD14c: the plugin must NOT drive the policy select for a saving session.

    Two separate concerns, and mixing them broke the end edge:
      - PLANNING (plugin): reserve energy ahead of the session so there is
        something to sell — `_session_protect_kwh` raising `drain_above`.
      - DISPATCH (heartbeat): dump it at the cap during the session window,
        driven natively by the Octoplus calendar.

    While the plugin also forced `_policy_override = "max_export"`, it PINNED the
    select to Max Export. At session end the heartbeat computes
    `policy = raw_policy` — still Max Export — so dumping continued until the
    plugin's next 5-minute cycle. Measured 2026-07-28: session ended 19:30:00,
    released 19:35:46, 5 min 46 s of exporting the battery past the paid window.

    The heartbeat cannot fix that edge while the plugin overrides the select, so
    the dispatch half moves out of the plugin entirely.
    """
    pv = {m: 8.0 for m in range(0, 480, PLUGIN_STEP)}
    load = {m: 1.0 for m in range(0, 480, PLUGIN_STEP)}
    overrides = {"sensor.sigen_plant_pv_power": 8.0, "sensor.sigen_plant_consumed_power": 1.0}
    overrides.update(_make_p90_sensors(p90_peak_kw=10.0, solcast_remaining=45.0))
    overrides[SIG_SAVING_SESSION_ENTITY] = "on"
    base = MockBase(pv_step=pv, load_step=load, soc_kw=BATTERY_KWH * 0.55, minutes_now=720, best_soc_keep=4.0, sensor_overrides=overrides)
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    plugin = CurtailmentPlugin(base)
    plugin._peak_pv = 9.0
    plugin._overnight_target_kwh = 6.0
    plugin.calculate(dno_limit_kw=3.68)

    assert plugin._is_saving_session_active(), "fixture must have a live session"
    assert plugin._policy_override != "max_export" or plugin._r63_engaged, f"a live session must not by itself force max_export — that is the heartbeat's job now (override={plugin._policy_override})"
    print("  test_session_dispatch_belongs_to_the_heartbeat_not_the_plugin: PASSED")


def _session_protect_run(hours_ahead, active=False):
    """calculate() with a joined saving session `hours_ahead` in the future."""
    from datetime import timedelta

    now = _dt(2025, 7, 12, 6, 0, tzinfo=_tz.utc)
    start = (now + timedelta(hours=hours_ahead)).isoformat()
    pv = {m: 8.0 for m in range(0, 480, PLUGIN_STEP)}
    load = {m: 1.0 for m in range(0, 480, PLUGIN_STEP)}
    overrides = {"sensor.sigen_plant_pv_power": 1.2, "sensor.sigen_plant_consumed_power": 0.6}
    overrides.update(_make_p90_sensors(p90_peak_kw=10.0, solcast_remaining=45.0))
    overrides.update(_saving_session_sensors(active=active, next_mins=120, next_start=start))
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=BATTERY_KWH * 0.50,
        minutes_now=360,
        now_utc=now,  # MUST match minutes_now — see the R63 rig note
        best_soc_keep=4.0,
        sensor_overrides=overrides,
    )
    # RD37: the reserve now stands aside when forecast PV can refill it before
    # the session. These tests are about the HORIZON, so give them a P10 profile
    # that CANNOT refill — otherwise reachability decides the outcome and the
    # horizon is never exercised.
    base.pv_forecast_minute_step10 = {m: 0.05 * (PLUGIN_STEP / 60.0) for m in range(0, 24 * 60, PLUGIN_STEP)}
    plugin = CurtailmentPlugin(base)
    plugin._peak_pv = 1.2
    plugin._overnight_target_kwh = 6.0
    plugin.calculate(dno_limit_kw=3.68)
    # Publish too, so the DISPLAY attributes can be asserted alongside the floor.
    plugin._publish_dispatch_policy(True, floor_kwh=0.0, soc_kwh=BATTERY_KWH * 0.50, soc_max=BATTERY_KWH)
    return plugin


def test_session_reserve_is_not_advertised_when_it_is_not_held():
    """Display must follow the CONTROL value, not the raw sensor.

    Live 2026-08-11 12:16 the card read "43.0% held for the 18:00 saving
    session" while `session_protect_kwh` was None and `drain_above` was 1.0%
    (source `reserve`) — i.e. exactly 0% was being held. The horizon gated the
    USE of the reserve but `_get_session_reserve_kwh` is published raw and
    unconditionally, so the card faithfully rendered a number that no longer
    meant anything.

    Same class as every other display defect this week: the card deriving from
    a different source than the controller.
    """
    plugin = _session_protect_run(hours_ahead=35.5)
    attrs = plugin.base.published["sensor.predbat_curtailment_intended_policy"]["attrs"]
    assert attrs["session_reserve_kwh"] == 0.0, "nothing is held, so nothing may be advertised: got {}".format(attrs["session_reserve_kwh"])
    assert attrs["session_reserve_pct"] == 0.0, "got {}".format(attrs["session_reserve_pct"])
    print("  test_session_reserve_is_not_advertised_when_it_is_not_held: PASSED")


def test_session_reserve_is_advertised_once_actually_held():
    """And it must still show when it IS held — the gag must not be blanket."""
    plugin = _session_protect_run(hours_ahead=12.0)
    attrs = plugin.base.published["sensor.predbat_curtailment_intended_policy"]["attrs"]
    assert attrs["session_reserve_kwh"] > 0, "an armed reserve must be shown"
    assert attrs["session_reserve_pct"] > 0
    print("  test_session_reserve_is_advertised_once_actually_held: PASSED ({}%)".format(attrs["session_reserve_pct"]))


def test_session_reserve_does_not_block_a_drain_pv_will_refill():
    """RD37 — the session reserve must not protect energy PV will supply anyway.

    Live 2026-08-12 06:59: SOC 1.16 kWh, `drain_above` pinned to 15.29 kWh
    (84.6%, source session_protect) for the 18:00 session, so the morning drain
    from 6.4% to 1% was suppressed — while 60.6 kWh of PV was forecast for the
    day and overflow_p90 was 16.0 kWh. The reserve was protecting 15.29 kWh that
    did not exist, against a refill that was never in doubt, and cost exactly the
    headroom the day needed.

    Same shape as the horizon bug: the reserve applied without asking whether it
    is actually at risk. Judged from the DRAIN FLOOR, not current SOC — the
    question is "if I drain to the floor, can PV still refill before the
    session", which is the decision being made.

    Uses the P10 band: do not bet a paid session on optimistic sun.
    """
    from curtailment_calc import session_reserve_is_reachable

    # 6 h to the session, 4 kW of PV against 0.4 kW of load = ~21.6 kWh net.
    pv = {m: 4.0 * (5 / 60.0) for m in range(0, 360, 5)}
    load = {m: 0.4 * (5 / 60.0) for m in range(0, 360, 5)}
    assert session_reserve_is_reachable(pv, load, 360, deficit_kwh=14.1, step_minutes=5, values_are_kwh=True) is True
    print("  test_session_reserve_does_not_block_a_drain_pv_will_refill: PASSED")


def test_session_reserve_still_binds_when_pv_cannot_refill():
    """The reserve must still hold on a dull day — that is what it is for."""
    from curtailment_calc import session_reserve_is_reachable

    pv = {m: 0.6 * (5 / 60.0) for m in range(0, 360, 5)}  # ~1.8 kWh net over 6 h
    load = {m: 0.4 * (5 / 60.0) for m in range(0, 360, 5)}
    assert session_reserve_is_reachable(pv, load, 360, deficit_kwh=14.1, step_minutes=5, values_are_kwh=True) is False
    print("  test_session_reserve_still_binds_when_pv_cannot_refill: PASSED")


def test_session_reserve_reachability_ignores_pv_after_the_session():
    """Only PV BEFORE the session can refill it. A dull morning followed by a
    blazing evening must not count as reachable."""
    from curtailment_calc import session_reserve_is_reachable

    pv = {m: (0.5 if m < 360 else 8.0) * (5 / 60.0) for m in range(0, 720, 5)}
    load = {m: 0.4 * (5 / 60.0) for m in range(0, 720, 5)}
    assert session_reserve_is_reachable(pv, load, 360, deficit_kwh=14.1, step_minutes=5, values_are_kwh=True) is False
    print("  test_session_reserve_reachability_ignores_pv_after_the_session: PASSED")


def test_session_reserve_does_not_arm_days_ahead():
    """The session floor must not arm the moment Octopus announces a session.

    Live 2026-08-11 06:30: a session at Wed 12/08 18:00 — **35.5 hours away** —
    had already pinned `drain_above` to 14.71 kWh (81.4%), source
    `session_protect`, on a day CM had just armed 1.9 kWh SHORT of headroom.
    That silently disables the pre-emptive drain: CM will not drain until the
    battery is 81% full, which does not happen until around the peak, by which
    point PV surplus exceeds the cap and draining is impossible (R25).

    `_get_session_reserve_kwh` reads `next_joined_event_duration_in_minutes`
    with no horizon at all. `_session_imminent` right next door already has the
    correct instinct in its docstring — "a session six hours out must not pin CM
    active all evening" — the drain-floor arm just never used it.

    Cost nothing on the day it was found only because Predbat had already
    emptied the pack overnight, so there was nothing to drain either way.
    """
    plugin = _session_protect_run(hours_ahead=35.5)
    assert plugin._session_protect_kwh == 0.0, "a session 35.5 h out must not hold the drain floor, got {:.2f} kWh".format(plugin._session_protect_kwh)
    print("  test_session_reserve_does_not_arm_days_ahead: PASSED")


def test_session_reserve_arms_in_time_for_the_morning_drain():
    """It must still arm early enough to matter.

    The drain it guards against is the same-day pre-dawn/morning one — roughly
    10-15 h before a typical 16:00-19:00 session — so the horizon has to cover
    the whole session day from before dawn, not just the hours around it.
    """
    plugin = _session_protect_run(hours_ahead=12.0)
    assert plugin._session_protect_kwh > 0, "a session 12 h out (same day) must hold the reserve"
    print("  test_session_reserve_arms_in_time_for_the_morning_drain: PASSED ({:.2f} kWh)".format(plugin._session_protect_kwh))


def test_session_reserve_horizon_boundary():
    """Assert the boundary specifically, so the horizon cannot drift unnoticed."""
    inside = _session_protect_run(hours_ahead=SESSION_PROTECT_HORIZON_HOURS - 1)._session_protect_kwh
    outside = _session_protect_run(hours_ahead=SESSION_PROTECT_HORIZON_HOURS + 1)._session_protect_kwh
    assert inside > 0, "just inside the horizon must arm"
    assert outside == 0.0, "just outside must not, got {:.2f}".format(outside)
    print("  test_session_reserve_horizon_boundary: PASSED ({}h)".format(SESSION_PROTECT_HORIZON_HOURS))


def test_session_reserve_still_protects_the_drain_floor():
    """The PLANNING half stays: ahead of a known session, keep duration x cap in
    the battery so there is something to sell. Without this the curtailment drain
    would empty the battery before the session ever starts."""
    reserve = compute_session_reserve(30.0, 3.68)
    assert abs(reserve - 1.84) < 0.01, f"30 min at 3.68 kW = 1.84 kWh, got {reserve}"
    protect = min(18.08, 6.6 + reserve)
    without = compute_drain_above(0.54, 2.0, None, 0.0)
    with_session = compute_drain_above(0.54, 2.0, None, protect)
    assert with_session > without, f"an upcoming session must raise the drain floor: {without} -> {with_session}"
    assert abs(with_session - protect) < 0.01, f"drain floor must be the protect level, got {with_session}"
    print(f"  test_session_reserve_still_protects_the_drain_floor: PASSED ({without} -> {with_session})")


def test_required_headroom_is_defined_once():
    """Charter: one quantity, one definition. "How much headroom does the forecast
    overflow require?" was expressed in FIVE places in THREE different formulas —
    two with no buffer term, two with the MAX_RESERVED constant, one with the
    R49-reduced effective_max_reserved. On 2026-07-28 the weakest of them vetoed a
    drain the strongest had correctly called (no_drain vs the Headroom Floor),
    leaving the battery 1.67 kWh short of its p90 defence.

    Every site must now call required_headroom_kwh(). Differences between sites
    must be explicit ARGUMENTS, not separate expressions that can drift.
    """
    # NOTE these assert the SHAPE against V10_SIM_SAFETY_FACTOR, not a literal.
    # The factor is expected to be retuned as the overflow meters bank real
    # DC-coupled days (see test_R9_overflow_safety_factor_is_1_05), and a
    # hardcoded literal here would fail on every retune while proving nothing
    # about the invariant this test exists for -- one definition, differences
    # expressed as arguments.
    from curtailment_plugin import OVERFLOW_SAFETY_FACTOR as SF

    # Matches the Headroom Floor: safety x overflow + tapered reserve.
    assert abs(required_headroom_kwh(6.6, 1.8) - (SF * 6.6 + 1.8)) < 1e-9
    # Tapered: reserve cannot exceed the overflow itself.
    assert abs(required_headroom_kwh(1.0, 1.8) - (SF * 1.0 + 1.0)) < 1e-9
    # R49-reduced buffer must flow through, not be hardcoded to the constant.
    assert required_headroom_kwh(6.6, 1.26) < required_headroom_kwh(6.6, 1.8)
    # Planning sites deliberately carry no reserve — expressed as an argument.
    assert abs(required_headroom_kwh(6.6, 0.0) - SF * 6.6) < 1e-9
    # Degenerate inputs are safe.
    assert required_headroom_kwh(0.0, 1.8) == 0.0
    assert required_headroom_kwh(-5.0, 1.8) == 0.0
    print("  test_required_headroom_is_defined_once: PASSED")


def test_no_drain_and_floor_agree_when_r49_reduces_the_buffer():
    """The R49 inconsistency left behind on 2026-07-28: no_drain used the
    MAX_RESERVED constant (1.8) while the floor used effective_max_reserved,
    which R49 cuts to 1.26 on confirmed-cloudy afternoons. They then disagreed by
    up to 0.54 kWh on exactly those days — a latent repeat of the bug being fixed.

    Both must consume the same buffer value.
    """
    from curtailment_plugin import OVERFLOW_SAFETY_FACTOR as SF

    headroom, overflow, reduced = 8.05, 6.6, 1.26
    # Both sides must use the SAME factor, or this compares two different
    # requirements and the agreement it claims to prove is accidental.
    fits = compute_overflow_fits_margin(headroom, overflow, SF, reduced)
    assert abs(fits - (headroom - required_headroom_kwh(overflow, reduced, SF))) < 1e-9, "fits-margin must be headroom minus the shared requirement"
    # And it must actually differ from the constant-buffer answer, or the test proves nothing.
    assert abs(compute_overflow_fits_margin(headroom, overflow, SF, 1.8) - fits) > 0.5
    print("  test_no_drain_and_floor_agree_when_r49_reduces_the_buffer: PASSED")


def test_recovery_floor_is_a_single_quantity():
    """R59b made the charge-side and drain-side recovery floors identical — same
    inputs, same formula. Two names for one number is drift waiting to happen, so
    there is now one function and one state field."""
    import curtailment_calc as _cc

    assert not hasattr(_cc, "compute_charge_recovery_floor"), "the duplicate charge-side function must be gone"
    a = compute_p10_recovery_floor(overnight_target_kwh=7.07, p10_pv_remaining_kwh=16.77, load_remaining_kwh=5.92)
    assert a == 0.0
    print("  test_recovery_floor_is_a_single_quantity: PASSED")


def test_overflow_smoothing_rejects_a_single_spike():
    """R64: a median window rejects a one-cycle forecast spike outright, where a
    mean would fold ~1/N of it into the floor. Solcast revisions arrive as
    single-slot jumps, which is exactly the shape a median kills."""
    base = [(0, 10.0), (5, 10.1), (10, 9.9), (15, 10.0)]
    clean = smooth_overflow_samples(base, now_minutes=15, window_minutes=30)
    spiked = smooth_overflow_samples(base + [(20, 18.0)], now_minutes=20, window_minutes=30)
    assert abs(spiked - clean) < 0.2, f"a single 18 kWh spike must not move the estimate: {clean} -> {spiked}"
    print(f"  test_overflow_smoothing_rejects_a_single_spike: PASSED ({clean:.2f} -> {spiked:.2f})")


def test_overflow_smoothing_tracks_the_real_trend():
    """It must not be so heavy that it stops following the day burning off.
    Today's trace fell 13.01 -> 6.53 kWh over 5h40m; the smoothed value has to
    follow that within roughly half the window."""
    samples = [(i * 5, 13.0 - i * 0.1) for i in range(20)]  # steady decline
    now = samples[-1][0]
    sm = smooth_overflow_samples(samples, now_minutes=now, window_minutes=30)
    raw = samples[-1][1]
    lag = sm - raw
    assert 0 < lag < 0.5, f"lag on a falling series should be small and positive (conservative), got {lag:.2f}"
    print(f"  test_overflow_smoothing_tracks_the_real_trend: PASSED (raw {raw:.2f}, smoothed {sm:.2f}, lag +{lag:.2f})")


def test_overflow_smoothing_lags_conservatively_not_optimistically():
    """R25 direction check: on a FALLING series the smoothed value must sit ABOVE
    the raw one (more overflow assumed -> lower floor -> more drain -> safer).
    A filter that lagged the other way would under-provision headroom."""
    falling = [(i * 5, 12.0 - i * 0.5) for i in range(8)]
    now = falling[-1][0]
    assert smooth_overflow_samples(falling, now, 30) > falling[-1][1], "must lag high on a falling series"
    rising = [(i * 5, 4.0 + i * 0.5) for i in range(8)]
    now = rising[-1][0]
    assert smooth_overflow_samples(rising, now, 30) < rising[-1][1], "lags low on a rising series (raw already used for activation)"
    print("  test_overflow_smoothing_lags_conservatively_not_optimistically: PASSED")


def test_overflow_smoothing_degrades_safely_on_short_history():
    """After a deploy the history is empty. Smoothing must return the current
    value rather than 0 — plugin restarts happen constantly during live work."""
    assert smooth_overflow_samples([], now_minutes=100, window_minutes=30) is None
    assert smooth_overflow_samples([(100, 7.5)], 100, 30) == 7.5
    # Samples older than the window are dropped entirely.
    assert smooth_overflow_samples([(0, 99.0), (100, 7.5)], 100, 30) == 7.5
    print("  test_overflow_smoothing_degrades_safely_on_short_history: PASSED")


def test_R11_removed_floor_follows_the_formula_down():
    """R11 REMOVED (2026-07-28): the overflow floor must track its formula in BOTH
    directions. It used to be clamped by `max(overflow_floor, previous_floor)`, so
    it could only rise within a day.

    Live failure: the ratchet locked at 15.76 kWh (87%) from an early-morning
    moment when remaining overflow was 0.44 kWh, and held there all day while p90
    overflow climbed to 12.28 kWh. Formula value was 8.55 kWh (47%). No drain
    could fire until the persisted value was cleared by hand.

    Its bypass fired only when `floor_scale` rose (R43) — and R43 is gone, so the
    clamp could never release whatever the forecast did.
    """
    pv = {m: 8.0 for m in range(0, 480, PLUGIN_STEP)}
    load = {m: 1.0 for m in range(0, 480, PLUGIN_STEP)}

    def floor_for(solcast_remaining, peak_kw):
        overrides = {"sensor.sigen_plant_pv_power": 8.0, "sensor.sigen_plant_consumed_power": 1.0}
        overrides.update(_make_p90_sensors(p90_peak_kw=peak_kw, solcast_remaining=solcast_remaining))
        base = MockBase(pv_step=pv, load_step=load, soc_kw=BATTERY_KWH * 0.55, minutes_now=720, best_soc_keep=4.0, sensor_overrides=overrides)
        base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
        plugin = CurtailmentPlugin(base)
        plugin._peak_pv = peak_kw
        plugin._overnight_target_kwh = 6.0
        plugin.on_update()
        return plugin, base.published.get("sensor.predbat_curtailment_drain_above", {}).get("value")

    # Small forecast overflow -> high floor (little headroom needed).
    plugin, high = floor_for(solcast_remaining=8.0, peak_kw=5.0)
    # Same plugin instance, forecast now says a BIG overflow -> floor must DROP.
    overrides = {"sensor.sigen_plant_pv_power": 8.0, "sensor.sigen_plant_consumed_power": 1.0}
    overrides.update(_make_p90_sensors(p90_peak_kw=10.0, solcast_remaining=45.0))
    plugin.base._sensor_overrides.update(overrides)
    plugin._peak_pv = 9.0
    plugin.on_update()
    low = plugin.base.published.get("sensor.predbat_curtailment_drain_above", {}).get("value")

    assert low < high, f"floor must fall when forecast overflow rises: {high} -> {low} (ratchet would have held it at {high})"
    assert not hasattr(plugin, "_floor_ratchet") or plugin._floor_ratchet is None, "the ratchet state must be gone, not merely bypassed"
    print(f"  test_R11_removed_floor_follows_the_formula_down: PASSED ({high} -> {low})")


def test_no_drain_uses_the_same_safety_margin_as_the_headroom_floor():
    """The `no_drain` veto must apply the SAME headroom requirement as the
    Headroom Floor, or a weaker test silently overrules the stronger one.

    Live 2026-07-28 14:37: SOC 10.03 kWh, headroom 8.05, p90 overflow 6.60.
      no_drain (bare p90 + flat 1.5 buffer): 6.60 <= 8.05 - 1.5  -> "fits", veto
      Headroom Floor (R42/R43 safety):      1.2*6.60 + 1.8 = 9.72 > 8.05 -> drain
    The band put SOC above drain_above (46.8%) and no_drain still forced Hold,
    leaving us 1.67 kWh short of the p90 defence. Same defect shape as R59a:
    an override derived from a different, weaker quantity beating the mechanism.

    R25/R42/R43 are one-directional — bigger overflow estimate, more drain,
    safer — so the margin-carrying test must win.
    """
    soc_max, p90 = 18.08, 6.60
    headroom = soc_max - 10.03  # 8.05

    required = V10_SIM_SAFETY_FACTOR * p90 + min(MAX_RESERVED_KWH, p90)
    assert required > headroom, "fixture must be a day that does NOT genuinely fit"

    # The safety-factored margin is what no_drain must key off.
    assert compute_overflow_fits_margin(headroom, p90) < 0, "must not report 'fits' when short of the p90 defence"

    # A genuinely low-overflow day must still report fits, so RD17's
    # evening-reserve Charge is preserved on overcast days.
    assert compute_overflow_fits_margin(soc_max - 2.0, 1.5) > 0, "low-overflow day must still suppress the pointless drain"
    print("  test_no_drain_uses_the_same_safety_margin_as_the_headroom_floor: PASSED")


def test_R16a_schmitt_hysteresis_stops_the_drain_flap():
    """R16a: entering Drain needs SOC above drain_above by OUTER_THRESHOLD_KWH;
    once draining it runs all the way TO drain_above.

    R16a has been in REQUIREMENTS since v19, but its implementation lived in the
    5-second HA automation that v30 retired — so since then the plugin has done a
    bare `soc > drain_above` every cycle with no deadband.

    Observed live 2026-07-29 07:50-08:33: policy flapped Max Export <-> Predbat
    EIGHT times in 45 minutes while SOC oscillated 2.8-3.1% around a drain_above
    of 0.54 kWh (3.0%). Sigen quantises SOC to 0.1% (0.018 kWh), and the RD4
    low-SOC handover sits at 2.8% — 0.2% below the drain target — so every micro
    drain tripped the handover, MSC charged it back, and it drained again.
    """
    da, cb = 0.54, 0.50

    # Not draining yet: sitting just above drain_above must NOT start a drain.
    assert compute_proposed_phase(0.55, cb, da, True, was_draining=False) == "Hold", "0.01 kWh above must not trigger Drain"
    assert compute_proposed_phase(0.71, cb, da, True, was_draining=False) == "Hold", "still inside the deadband"
    # Clear of the deadband -> Drain.
    assert compute_proposed_phase(0.73, cb, da, True, was_draining=False) == "Drain", "above drain_above + threshold must drain"

    # Once draining, run TO the target rather than stopping at the deadband edge.
    assert compute_proposed_phase(0.60, cb, da, True, was_draining=True) == "Drain", "must run all the way to target"
    assert compute_proposed_phase(0.54, cb, da, True, was_draining=True) == "Hold", "reaching target exits to Hold"

    # The live flap: SOC 3.0% (0.54) with drain_above 0.54 must be Hold, not Drain.
    assert compute_proposed_phase(0.542, cb, da, True, was_draining=False) == "Hold", "the 2026-07-29 flap case must read Hold"
    print("  test_R16a_schmitt_hysteresis_stops_the_drain_flap: PASSED")


def test_R63_does_not_force_drain_when_nothing_is_drainable():
    """R63 must not demand headroom the battery cannot give.

    Observed live 2026-07-29 07:39: SOC 0.54 kWh sitting exactly ON drain_above
    0.54 — nothing left to shed — yet R63 was still engaged and the policy read
    "active Drain (override max_export)". The Schmitt band was already returning
    Hold (SOC is not > drain_above); R63 was overriding the correct answer with
    an instruction that could not be carried out.

    Benign that morning only because PV was below the export cap so Max Export
    had nothing to push. With PV above the cap it becomes a real instruction
    against an empty battery, with only the 2.8% drain-floor clamp in the way.

    R63 asks "can I still MAKE this headroom in time?". If there is no headroom
    left to make, the question is moot and it must release to the band.
    """
    # Plenty of time, big requirement — but the battery is already at its floor.
    # needed 6.0 > sheddable 2.0 -> a genuine deadline breach.
    assert drain_deadline_breached(headroom_needed_kwh=6.0, max_sheddable_kwh=2.0, drainable_kwh=4.0), "with room to drain, a real shortfall must still fire"
    assert not drain_deadline_breached(headroom_needed_kwh=6.0, max_sheddable_kwh=2.0, drainable_kwh=0.0), "nothing drainable -> must NOT force Max Export"
    assert not drain_deadline_breached(headroom_needed_kwh=6.0, max_sheddable_kwh=2.0, drainable_kwh=-0.1), "below the floor -> must NOT force Max Export"
    # Engaged state must also release once the battery reaches the floor.
    assert not drain_deadline_breached(headroom_needed_kwh=6.0, max_sheddable_kwh=2.0, engaged=True, drainable_kwh=0.0), "must release when the drain is exhausted, not latch on"
    # The live 2026-07-29 07:39 numbers: SOC exactly on the floor.
    assert not drain_deadline_breached(headroom_needed_kwh=2.5, max_sheddable_kwh=0.0, engaged=True, drainable_kwh=0.0), "SOC on the floor must read Hold, not Drain"
    print("  test_R63_does_not_force_drain_when_nothing_is_drainable: PASSED")


def test_R63_shed_rate_inverts_once_pv_exceeds_the_cap():
    """R63 — the drain lever's authority is `cap − max(0, PV − load)`, which goes
    NEGATIVE once PV-load clears the export cap. Past that point we export flat
    out and the battery still charges from the excess: no headroom can be made.
    """
    cap = 3.68
    # Dawn: no PV, full authority.
    assert abs(compute_shed_rate(pv_kw=0.0, load_kw=0.5, export_cap_kw=cap) - cap) < 1e-9
    # Mid-morning: PV eating into it.
    assert abs(compute_shed_rate(pv_kw=3.0, load_kw=0.5, export_cap_kw=cap) - (cap - 2.5)) < 1e-9
    # Peak: PV-load 7.5 kW > cap -> lever inverted, we are filling.
    rate = compute_shed_rate(pv_kw=8.0, load_kw=0.5, export_cap_kw=cap)
    assert rate < 0, f"past lockout the shed rate must be negative, got {rate}"
    assert abs(rate - (cap - 7.5)) < 1e-9
    print(f"  test_R63_shed_rate_inverts_once_pv_exceeds_the_cap: PASSED (peak rate {rate:.2f} kW)")


def test_R63_max_sheddable_integrates_a_falling_rate():
    """R63: `shed_rate` falls continuously as PV climbs, so sampling the current
    rate over-states what is still achievable. Must integrate to T_lockout."""
    lat, lon, doy, cap = 52.33, -1.32, 209, 3.68
    scale = 9.0
    # 05:00 UTC, integrating to a lockout two hours out.
    sheddable = compute_max_sheddable(scale, lat, lon, doy, from_utc_hours=5.0, lockout_utc_hours=7.0, export_cap_kw=cap)
    naive = compute_shed_rate(scale * 0.0, 0.5, cap) * 2.0  # instantaneous-at-start × hours

    assert sheddable > 0, f"there must be real drain capacity before lockout, got {sheddable}"
    assert sheddable < naive, f"integral must be BELOW the naive constant-rate estimate ({sheddable:.2f} vs {naive:.2f})"
    # Zero-width and inverted windows are safe.
    assert compute_max_sheddable(scale, lat, lon, doy, 7.0, 7.0, cap) == 0.0
    assert compute_max_sheddable(scale, lat, lon, doy, 8.0, 7.0, cap) == 0.0
    print(f"  test_R63_max_sheddable_integrates_a_falling_rate: PASSED ({sheddable:.2f} kWh vs naive {naive:.2f})")


def test_R63_deadline_breach_fires_only_when_drain_is_unachievable():
    """R63: the gate is achievability. Slack -> silent (behaviour unchanged);
    behind -> fire. This is what makes it safe on a live control path: it can
    only ever fire EARLIER than the plain energy test, never later."""
    # Plenty of capacity to shed what's needed -> must NOT fire.
    assert not drain_deadline_breached(headroom_needed_kwh=2.0, max_sheddable_kwh=6.0)
    # Needed exceeds what's still achievable -> fire.
    assert drain_deadline_breached(headroom_needed_kwh=6.0, max_sheddable_kwh=2.0)
    # Nothing needed -> never fires, even with zero capacity left (post-lockout).
    assert not drain_deadline_breached(headroom_needed_kwh=0.0, max_sheddable_kwh=0.0)
    assert not drain_deadline_breached(headroom_needed_kwh=-1.5, max_sheddable_kwh=0.0)
    # Exactly on the boundary is not a breach.
    assert not drain_deadline_breached(headroom_needed_kwh=3.0, max_sheddable_kwh=3.0)
    print("  test_R63_deadline_breach_fires_only_when_drain_is_unachievable: PASSED")


def test_R63_draining_clears_the_breach_it_must_not_latch():
    """R63 is a CLOSED LOOP and must not latch. Draining is precisely what
    clears the breach: headroom grows, so headroom_needed falls.

    A one-way latch — which an early draft of R63 specified, on the reasoning
    "once behind, more PV only makes it worse" — would hold Max Export after the
    drain had succeeded and empty the battery. This test exists to stop that
    reasoning being reinstated.
    """
    soc_max, sheddable = 18.08, 3.0
    remaining_overflow = 7.0

    def needed_at(soc):
        return 1.2 * remaining_overflow + min(1.8, remaining_overflow) - (soc_max - soc)

    # Battery full-ish: badly behind, R63 engages.
    assert drain_deadline_breached(needed_at(14.0), sheddable), "full battery must breach"
    # Now drain 6 kWh. The SAME overflow and the SAME deadline, but the breach
    # must clear — otherwise Max Export never stops.
    assert not drain_deadline_breached(needed_at(8.0), sheddable, engaged=True), "draining must clear the breach even while engaged"
    print("  test_R63_draining_clears_the_breach_it_must_not_latch: PASSED")


def test_R63_hysteresis_band_stops_boundary_chatter():
    """Engage at needed > sheddable; release only once needed < sheddable - hyst.
    Inside the band an engaged R63 stays engaged, so it can't flap Max Export
    against Hold every cycle at the crossing."""
    sheddable = 3.0
    # Just inside the band, coming from disengaged -> stays off.
    assert not drain_deadline_breached(2.8, sheddable, engaged=False)
    # Same value, coming from engaged -> stays ON (that's the hysteresis).
    assert drain_deadline_breached(2.8, sheddable, engaged=True)
    # Clear of the band -> releases regardless.
    assert not drain_deadline_breached(2.4, sheddable, engaged=True, hyst_kwh=0.5)
    # Above the engage threshold -> on regardless.
    assert drain_deadline_breached(3.5, sheddable, engaged=False)
    print("  test_R63_hysteresis_band_stops_boundary_chatter: PASSED")


def test_R63_fires_before_the_plain_energy_test_would():
    """R63's whole purpose: on a morning where the surplus still 'fits' today,
    but won't be sheddable by the time it stops fitting, R63 must act while the
    lever still has authority. The plain energy test stays silent here.
    """
    soc_max, cap = 18.08, 3.68
    lat, lon, doy = 52.33, -1.32, 209
    scale = 10.2
    lockout = 7.33  # UTC hour where shed_rate crosses zero on this fixture
    soc, remaining_overflow = 12.0, 5.5
    headroom = soc_max - soc  # 6.08

    # The plain energy test is SILENT here: 5.5 kWh of overflow fits in 6.08.
    assert remaining_overflow <= headroom, "fixture must be a day where the plain test is still silent"
    needed = 1.2 * remaining_overflow + min(1.8, remaining_overflow) - headroom  # 2.32

    # 05:00 UTC — still 3.88 kWh of drain capacity before lockout. Not behind.
    early = compute_max_sheddable(scale, lat, lon, doy, 5.0, lockout, cap)
    assert not drain_deadline_breached(needed, early), f"at 05:00 there is still time: need {needed:.2f}, can shed {early:.2f}"

    # 06:00 UTC — one hour later PV has eaten the lever; only 1.20 kWh left.
    # Same day, same overflow, same SOC: only the DEADLINE has moved.
    late = compute_max_sheddable(scale, lat, lon, doy, 6.0, lockout, cap)
    assert late > 0, "fixture must still be pre-lockout, not the trivial zero-capacity case"
    assert drain_deadline_breached(needed, late), f"by 06:00 the drain is unachievable: need {needed:.2f}, can shed {late:.2f}"

    assert late < early, "drain capacity must fall as lockout approaches"
    print(f"  test_R63_fires_before_the_plain_energy_test_would: PASSED (need {needed:.2f}; 05:00 shed {early:.2f} ok, 06:00 shed {late:.2f} behind)")


def test_charge_recovery_floor_nets_against_generation_not_overflow():
    """R59b — the floor is about P10 GENERATION available to refill the battery,
    NOT P10 overflow.

    Overflow is a curtailment quantity (PV above load + export cap). Generation
    is what can actually be used to fill the battery. They are different numbers
    and the floor needs the second one.

    Live 2026-07-28 10:45 BST: overflow_p90=12.28 but usable surplus was
    p10_pv-load = 10.85. R59a netted against overflow_p10 (=0.0), so the floor
    slammed to the full overnight target at 06:01 and sat there all day — which
    BLOCKED the morning drain on a day with 12.28 kWh of overflow risk, the exact
    inverse of R25 (headroom must be made before overflow, never after).
    """
    floor = compute_p10_recovery_floor(
        overnight_target_kwh=6.60,
        p10_pv_remaining_kwh=16.77,
        load_remaining_kwh=5.92,
    )
    assert floor == 0.0, f"10.85 kWh of surplus covers a 6.60 kWh target -> floor 0, got {floor}"

    cb = compute_charge_below(floor, soc_keep=0.0)
    # SOC 41% (7.34 kWh) must be ABOVE charge_below so the morning drain can run.
    assert 0.41 * 18.08 > cb, f"41% SOC must NOT be pinned by charge_below ({cb})"
    print(f"  test_charge_recovery_floor_nets_against_generation_not_overflow: PASSED (floor={floor}, charge_below={cb})")


def test_charge_recovery_floor_ramps_up_as_generation_runs_out():
    """The Schmitt band does the timing: the floor starts at 0 (Hold, keep
    headroom) and RISES through the afternoon as remaining P10 generation
    shrinks, crossing SOC and flipping Hold -> Solar Charge on its own.

    This is the 2026-07-27 case done correctly. R59a charged from dawn and threw
    away the afternoon headroom; here the bank happens late, which is what R25
    wants. Battery at 9% SOC = 1.63 kWh, overnight target 7.07 kWh.
    """
    soc_kwh = 0.09 * 18.08
    target = 7.07

    def floor_at(pv_remaining, load_remaining):
        return compute_p10_recovery_floor(
            overnight_target_kwh=target,
            p10_pv_remaining_kwh=pv_remaining,
            load_remaining_kwh=load_remaining,
        )

    morning = floor_at(16.77, 5.92)  # surplus 10.85
    midday = floor_at(11.00, 4.50)  # surplus  6.50
    afternoon = floor_at(6.00, 3.00)  # surplus  3.00
    dusk = floor_at(0.50, 1.50)  # surplus  0.00

    assert morning == 0.0, f"morning: plenty of PV ahead -> floor 0, got {morning}"
    assert morning < midday < afternoon < dusk, f"floor must ramp up: {morning}, {midday}, {afternoon}, {dusk}"
    # Dusk: PV 0.50 vs load 1.50 leaves a 1.0 kWh deficit still to serve, so the
    # floor lands at target + deficit, not merely at target.
    assert abs(dusk - (target + 1.0)) < 0.001, f"dusk: floor must cover target plus the 1.0 kWh deficit, got {dusk}"

    # Hold early (SOC above floor), Solar Charge later (floor crosses SOC).
    assert soc_kwh > compute_charge_below(morning, 0.0), "morning must Hold, preserving headroom"
    assert soc_kwh < compute_charge_below(afternoon, 0.0), "afternoon must flip to Solar Charge"
    print(f"  test_charge_recovery_floor_ramps_up_as_generation_runs_out: PASSED ({morning} -> {midday} -> {afternoon} -> {dusk})")


def test_charge_recovery_floor_overcast_day_charges():
    """Overcast: little P10 generation to come, so the floor stays high and
    Charge fires — the low-overflow day that RD17 was patching around.

    Load exceeds PV, so the battery DRAINS through the rest of the day and the
    floor must be raised ABOVE overnight_target by that deficit, matching the
    drain-side behaviour asserted in test_p10_recovery_floor_load_exceeds_pv.
    """
    floor = compute_p10_recovery_floor(overnight_target_kwh=7.07, p10_pv_remaining_kwh=3.00, load_remaining_kwh=5.92)
    # net = 3.00 - 5.92 = -2.92 deficit -> floor = 7.07 + 2.92 = 9.99
    assert abs(floor - 9.99) < 0.001, f"deficit must raise the floor above target, got {floor}"
    print(f"  test_charge_recovery_floor_overcast_day_charges: PASSED (floor={floor})")


def test_charge_recovery_floor_matches_drain_side_recovery():
    """R59b: charge_below and the R54 drain target now share one definition of
    'can P10 generation refill me?'. R59a's split existed only to justify the
    overflow netting; with that gone the two must not diverge."""
    kwargs = dict(overnight_target_kwh=7.07, p10_pv_remaining_kwh=16.77, load_remaining_kwh=5.92)
    charge_side = compute_p10_recovery_floor(**kwargs)
    drain_side = compute_p10_recovery_floor(**kwargs)
    assert charge_side == drain_side, f"charge {charge_side} != drain {drain_side}"

    # R25/R52 guard: a big overflow_floor must still win the R54 outer max, so
    # the drain target is not raised and headroom is not stranded.
    floor, source = compute_floor_with_source(reserve=0.54, p10_recovery=drain_side, overflow_floor=13.98, effective_keep=7.07)
    assert abs(floor - 13.98) < 0.001, f"Expected drain target 13.98, got {floor}"
    assert source == "Curtailment Buffer", f"Expected 'Curtailment Buffer', got {source}"
    print(f"  test_charge_recovery_floor_matches_drain_side_recovery: PASSED ({charge_side}, {floor}, {source})")


def test_no_surplus_hold_dawn_collapse():
    """2026-06-15 incident: dawn activation, target collapsed to 0.3, SOC 1.4.

    overnight_target legitimately shrinks toward sunrise, but PV wasn't yet
    covering load (pv_covering=False). Draining to 0.3 emptied the battery
    before PV relieved it → import. With no surplus, the drain target must be
    held at (at least) current SOC so no Drain fires.
    """
    held = apply_no_surplus_drain_hold(drain_target=0.3, soc_kw=1.4, pv_covering=False)
    assert abs(held - 1.4) < 0.001, f"Expected 1.4 (hold at SOC, no drain), got {held}"
    print(f"  test_no_surplus_hold_dawn_collapse: PASSED ({held})")


def test_no_surplus_hold_target_above_soc_unchanged():
    """No surplus but target already above SOC → unchanged (no spurious raise)."""
    held = apply_no_surplus_drain_hold(drain_target=6.0, soc_kw=1.4, pv_covering=False)
    assert abs(held - 6.0) < 0.001, f"Expected 6.0 (unchanged), got {held}"
    print(f"  test_no_surplus_hold_target_above_soc_unchanged: PASSED ({held})")


def test_no_surplus_hold_surplus_allows_drain():
    """PV covering load (genuine surplus) → drain to target allowed, unchanged.

    This is the case the dropping overnight target is FOR: real midday surplus,
    drain the battery to make curtailment room / export at 12p.
    """
    held = apply_no_surplus_drain_hold(drain_target=0.3, soc_kw=1.4, pv_covering=True)
    assert abs(held - 0.3) < 0.001, f"Expected 0.3 (drain allowed), got {held}"
    print(f"  test_no_surplus_hold_surplus_allows_drain: PASSED ({held})")


def test_huge_day_drain_budget_r61_r52():
    """R61 × R52 interaction: on a huge PV day the drain budget still closes.

    R61 blocks draining during the dawn gap (PV present but not covering
    load). The design relies on the OTHER two windows to reach the huge-day
    floor before overflow starts:
      A. Pre-dawn: R52 drains at full DNO to soc_keep + PRE_PV_BUFFER_PCT
         (separate path, not gated by R61).
      B. Ramp (pv_covering → overflow start): drain rate = DNO − (pv − load).

    Invariant pinned here: residual drain need after R52
    (r52_target − DEEP_DISCHARGE_FLOOR) must fit in the ramp window's
    capacity plus the reserved buffer (MAX_RESERVED_KWH), on a clear
    scale-8.1 July day at Middlemuir. Checked for BOTH the current 4.0 kW
    DNO and the post-swap 3.68 kW limit.

    Also pinned: the R52-didn't-fire risk case (low pre-dawn confidence,
    day turns out huge) starting from a typical overnight target — the ramp
    window alone must cover it within the buffer.

    Fails if: R61 is extended to block the ramp window, PRE_PV_BUFFER_PCT
    default rises, the buffer shrinks, or the DNO drop breaks the budget.
    """
    import math as _math
    from curtailment_plugin import PRE_PV_BUFFER_PCT_DEFAULT, MAX_RESERVED_KWH
    from curtailment_calc import DEEP_DISCHARGE_FLOOR_KWH

    lat, lon, doy, scale = 52.33, -1.32, 186, 8.1
    load_kw = 0.5
    soc_max, soc_keep = 18.08, 1.0
    pv_margin_kw = 0.5  # pv_covering threshold (PV_MARGIN_KW in calculate())

    def pv_at(t_utc):
        elev = solar_elevation(lat, lon, t_utc, doy)
        return scale * max(0.0, _math.sin(_math.radians(elev)))

    r52_target = soc_keep + (PRE_PV_BUFFER_PCT_DEFAULT / 100.0) * soc_max

    for dno in (4.0, 3.68):
        # Find dawn crossings by scanning the geometry curve
        t_cover = t_overflow = None
        t = 0.0
        while t < 14.0:
            p = pv_at(t)
            if t_cover is None and (p - load_kw) > pv_margin_kw:
                t_cover = t
            if t_overflow is None and (p - load_kw) > dno:
                t_overflow = t
                break
            t += 1.0 / 60
        assert t_cover is not None and t_overflow is not None and t_overflow > t_cover

        # Ramp window drain capacity: export budget left after PV surplus
        capacity = 0.0
        t = t_cover
        while t < t_overflow:
            capacity += max(0.0, dno - (pv_at(t) - load_kw)) * (1.0 / 60)
            t += 1.0 / 60

        # Case 1: R52 fired pre-dawn — residual from r52_target to floor
        residual = r52_target - DEEP_DISCHARGE_FLOOR_KWH
        assert residual <= capacity + MAX_RESERVED_KWH, f"dno={dno}: R52 residual {residual:.2f} kWh exceeds ramp capacity {capacity:.2f} + buffer {MAX_RESERVED_KWH}"

        # Case 2: R52 did NOT fire (low pre-dawn confidence, day turned huge).
        # Ramp window alone must get from a typical overnight target to the
        # floor, within the buffer.
        overnight_target = 6.0
        residual2 = overnight_target - DEEP_DISCHARGE_FLOOR_KWH
        assert residual2 <= capacity + MAX_RESERVED_KWH, f"dno={dno}: no-R52 residual {residual2:.2f} kWh exceeds ramp capacity {capacity:.2f} + buffer {MAX_RESERVED_KWH}"

        print(f"  test_huge_day_drain_budget_r61_r52: dno={dno} cover={t_cover:.2f}h overflow={t_overflow:.2f}h capacity={capacity:.2f}kWh residual={residual:.2f}/{residual2:.2f}kWh")

    print("  test_huge_day_drain_budget_r61_r52: PASSED")


def test_p10_recovery_floor_today_2026_05_08_cloudy():
    """Real input from 2026-05-08 cloudy morning that exposed the deficit bug.
    P10=7.97, load=10.46, target=7.42. Old (P10-only): floor = 9.91.
    """
    floor = compute_p10_recovery_floor(overnight_target_kwh=7.42, p10_pv_remaining_kwh=7.97, load_remaining_kwh=10.46)
    assert abs(floor - 9.91) < 0.001, f"Expected 9.91, got {floor}"
    print(f"  test_p10_recovery_floor_today_2026_05_08_cloudy: PASSED (floor={floor})")


def test_p10_recovery_floor_ignores_p50():
    """Design choice 2026-05-11: charge_below uses P10 only. P50 passed for
    backward compat is ignored — guarantees we hit overnight target even on
    a worse-than-median PV day.
    """
    floor = compute_p10_recovery_floor(
        overnight_target_kwh=9.4,
        p10_pv_remaining_kwh=2.0,
        p50_pv_remaining_kwh=10.0,
        load_remaining_kwh=5.0,
    )
    # Uses P10=2, ignores P50=10. net = 2-5 = -3, floor = max(0, 9.4+3) = 12.4
    assert abs(floor - 12.4) < 0.001, f"Expected 12.4 (P10-based, P50 ignored), got {floor}"
    print(f"  test_p10_recovery_floor_ignores_p50: PASSED (floor={floor})")


def test_p10_recovery_floor_calibration_ratio_ignored():
    """calibration_ratio is accepted but ignored — past 30-min tracking
    doesn't predict next 6 hours. Result identical to ratio=1.0.
    """
    f_ignored = compute_p10_recovery_floor(
        overnight_target_kwh=9.4,
        p10_pv_remaining_kwh=2.0,
        load_remaining_kwh=5.0,
        calibration_ratio=0.3,
    )
    f_default = compute_p10_recovery_floor(
        overnight_target_kwh=9.4,
        p10_pv_remaining_kwh=2.0,
        load_remaining_kwh=5.0,
    )
    assert abs(f_ignored - f_default) < 0.001, f"ratio should be ignored, got {f_ignored} vs {f_default}"
    print(f"  test_p10_recovery_floor_calibration_ratio_ignored: PASSED (floor={f_ignored})")


def test_p10_recovery_floor_late_afternoon_pessimistic():
    """Late afternoon real-world case: P10 PV remaining is small, load drains
    battery. Floor must be raised above overnight_target to cover the deficit.
    """
    floor = compute_p10_recovery_floor(
        overnight_target_kwh=7.45,
        p10_pv_remaining_kwh=2.72,
        load_remaining_kwh=4.26,
    )
    # net = 2.72 - 4.26 = -1.54, floor = max(0, 7.45 + 1.54) = 8.99
    assert abs(floor - 8.99) < 0.001, f"Expected 8.99, got {floor}"
    print(f"  test_p10_recovery_floor_late_afternoon_pessimistic: PASSED (floor={floor})")


def test_p10_recovery_floor_genuine_cloudy_day():
    """Real cloudy day: P10 PV low and load high → floor well above target
    to ensure overnight need is met.
    """
    floor = compute_p10_recovery_floor(
        overnight_target_kwh=7.42,
        p10_pv_remaining_kwh=4.0,
        load_remaining_kwh=10.0,
    )
    # net = 4-10 = -6, floor = max(0, 7.42 + 6) = 13.42
    assert abs(floor - 13.42) < 0.001, f"Expected 13.42, got {floor}"
    print(f"  test_p10_recovery_floor_genuine_cloudy_day: PASSED (floor={floor})")


def test_p10_recovery_floor_zero_target():
    """Edge case: overnight_target=0 → floor=0 (always)."""
    floor = compute_p10_recovery_floor(overnight_target_kwh=0.0, p10_pv_remaining_kwh=10.0, load_remaining_kwh=2.0)
    assert floor == 0.0, f"Expected 0.0, got {floor}"
    print(f"  test_p10_recovery_floor_zero_target: PASSED (floor={floor})")


def test_p10_recovery_floor_today_at_11_03():
    """Today's actual numbers (2026-05-06 11:03 BST): P10 remaining 16, load
    remaining ~7, overnight target 9.4 → floor ~0.4 kWh ≈ 2%.

    Reference: when sized like this, even P10 day still recovers to overnight
    target. Curtailment manager could have drained battery to ~2% this morning
    with no overnight risk.
    """
    floor = compute_p10_recovery_floor(overnight_target_kwh=9.4, p10_pv_remaining_kwh=16.0, load_remaining_kwh=7.0)
    # potential = 9, floor = 9.4 - 9 = 0.4
    assert abs(floor - 0.4) < 0.001, f"Today's case: expected 0.4 kWh, got {floor}"
    print(f"  test_p10_recovery_floor_today_at_11_03: PASSED (floor={floor:.2f} kWh = {floor/BATTERY_KWH*100:.1f}%)")


def test_p10_recovery_floor_combines_with_r54_min():
    """In R54, the new floor is an OUTER MAX term (lower bound), alongside reserve.

    target = max(reserve, p10_recovery, min(curt_floor, effective_keep))

    Verifies the combined formula picks the right answer in two regimes:
      - early day: p10_recovery≈0 → outer max = inner min (p10 inactive)
      - late day:  p10_recovery=overnight_target → outer max binds (p10 active)
    """
    reserve = 0.0
    curt_floor = 5.0
    effective_keep = 4.0  # R55 morning-gap-based

    # Early in day — lots of P10 PV ahead
    p10_early = compute_p10_recovery_floor(overnight_target_kwh=9.4, p10_pv_remaining_kwh=20.0, load_remaining_kwh=7.0)
    target_early = max(reserve, p10_early, min(curt_floor, effective_keep))
    assert target_early == 4.0, f"Early: expected min(5,4)=4, got {target_early}"

    # Late in day — no P10 PV remaining, load still depletes battery → floor raised by deficit
    p10_late = compute_p10_recovery_floor(overnight_target_kwh=9.4, p10_pv_remaining_kwh=0.0, load_remaining_kwh=2.0)
    target_late = max(reserve, p10_late, min(curt_floor, effective_keep))
    # p10_late = 9.4 - (-2) = 11.4 → outer max binds at 11.4
    assert abs(target_late - 11.4) < 0.001, f"Late: expected 11.4 (overnight_target + load deficit), got {target_late}"
    print(f"  test_p10_recovery_floor_combines_with_r54_min: PASSED (early={target_early}, late={target_late})")


# ============================================================================
# R60 — effective export cap (proposed addition for overflow integral)
#
# Realistic DNO for forecast overflow: median of recent voltage-throttle cap,
# falling back to yesterday's daytime mean (cold-start) or DNO (no history).
# Used in compute_solcast_overflow's dno_limit parameter to size overflow
# against what we can actually export, not the theoretical limit.
# ============================================================================


def test_effective_cap_no_history_returns_dno():
    """Cold start: no today samples, no yesterday avg → use DNO."""
    cap = compute_effective_export_cap(today_samples_kw=[], yesterday_avg_kw=None, dno_kw=4.0)
    assert cap == 4.0, f"Expected DNO=4.0, got {cap}"
    print(f"  test_effective_cap_no_history_returns_dno: PASSED ({cap})")


def test_effective_cap_today_data_wins():
    """≥ min_samples today samples → today's mean (yesterday ignored)."""
    today = [3.5] * 12  # 12 samples, all at 3.5 kW
    cap = compute_effective_export_cap(today_samples_kw=today, yesterday_avg_kw=2.5, dno_kw=4.0, min_samples=10)
    assert abs(cap - 3.5) < 0.01, f"Expected today's mean=3.5, got {cap}"
    print(f"  test_effective_cap_today_data_wins: PASSED ({cap})")


def test_effective_cap_few_samples_falls_back_to_yesterday():
    """Insufficient today samples → fall back to yesterday's avg."""
    today = [3.5, 3.0]  # only 2 samples, below min_samples=10
    cap = compute_effective_export_cap(today_samples_kw=today, yesterday_avg_kw=2.7, dno_kw=4.0, min_samples=10)
    assert abs(cap - 2.7) < 0.01, f"Expected yesterday=2.7, got {cap}"
    print(f"  test_effective_cap_few_samples_falls_back_to_yesterday: PASSED ({cap})")


def test_effective_cap_no_today_with_yesterday():
    """Pre-PV / first-cycle of day: only yesterday's avg available."""
    cap = compute_effective_export_cap(today_samples_kw=[], yesterday_avg_kw=3.1, dno_kw=4.0)
    assert abs(cap - 3.1) < 0.01, f"Expected yesterday=3.1, got {cap}"
    print(f"  test_effective_cap_no_today_with_yesterday: PASSED ({cap})")


def test_effective_cap_clamped_to_hard_floor():
    """Many low samples (V high all hour) — clamp to hard_floor to avoid disaster."""
    today = [0.5] * 20  # 20 samples at 0.5 kW
    cap = compute_effective_export_cap(today_samples_kw=today, yesterday_avg_kw=None, dno_kw=4.0, hard_floor_kw=2.0)
    assert cap == 2.0, f"Expected hard_floor=2.0, got {cap}"
    print(f"  test_effective_cap_clamped_to_hard_floor: PASSED ({cap})")


def test_effective_cap_clamped_to_dno_ceiling():
    """Defensive: yesterday avg above DNO (bad data) → clamp to DNO."""
    cap = compute_effective_export_cap(today_samples_kw=[], yesterday_avg_kw=5.5, dno_kw=4.0)
    assert cap == 4.0, f"Expected DNO=4.0 ceiling, got {cap}"
    print(f"  test_effective_cap_clamped_to_dno_ceiling: PASSED ({cap})")


def test_effective_cap_today_30min_typical_day():
    """Today's data, mixed throttling — gives realistic mean."""
    # 30 min @ 5s sampling = 360 samples typical, but assume aggregator returns 1/min
    # Mix: half hour with cap=4 (early morning), half hour throttled to 3
    today = [4.0] * 15 + [3.0] * 15
    cap = compute_effective_export_cap(today_samples_kw=today, yesterday_avg_kw=None, dno_kw=4.0, min_samples=10)
    assert abs(cap - 3.5) < 0.01, f"Expected mean of mixed=3.5, got {cap}"
    print(f"  test_effective_cap_today_30min_typical_day: PASSED ({cap})")


def test_effective_cap_today_actual_last_hour():
    """Reference: today's last-hour data (mean=2.92) — overflow integral should
    use ~2.92 not 4.0, sizing curtailment forecast against actual ceiling."""
    today = [2.92] * 60  # 60 samples at the observed mean
    cap = compute_effective_export_cap(today_samples_kw=today, yesterday_avg_kw=None, dno_kw=4.0)
    assert abs(cap - 2.92) < 0.01, f"Expected today's mean=2.92, got {cap}"
    print(f"  test_effective_cap_today_actual_last_hour: PASSED ({cap})")


# ============================================================================
# R4 defer-to-Predbat decision: only when GSHP heating is active.
# In summer (CH off), plugin handles morning drain — don't yield to Predbat.
# ============================================================================


def test_r4_defer_gshp_off_no_defer_even_when_low():
    """Summer (CH off): SOC below keep should NOT trigger defer — plugin manages drain."""
    assert should_defer_to_charge(gshp_ch_active=False, soc_kw=2.0, soc_keep=4.9, was_deferring=False) is False
    print("  test_r4_defer_gshp_off_no_defer_even_when_low: PASSED")


def test_r4_defer_gshp_on_low_soc_defers():
    """Winter (CH on): SOC below engage threshold → defer to Predbat charge."""
    # engage = 4.9 - 0.2 = 4.7. SOC=4.0 < 4.7 → defer
    assert should_defer_to_charge(gshp_ch_active=True, soc_kw=4.0, soc_keep=4.9, was_deferring=False) is True
    print("  test_r4_defer_gshp_on_low_soc_defers: PASSED")


def test_r4_defer_gshp_on_above_release_no_defer():
    """Winter, SOC above release threshold → no defer."""
    # release = 4.9 + 0.2 = 5.1. SOC=5.5 > 5.1 → no defer
    assert should_defer_to_charge(gshp_ch_active=True, soc_kw=5.5, soc_keep=4.9, was_deferring=True) is False
    print("  test_r4_defer_gshp_on_above_release_no_defer: PASSED")


def test_r4_defer_gshp_on_in_hysteresis_was_deferring():
    """Winter, SOC inside hysteresis band, was already deferring → keep deferring."""
    # SOC=4.8 between engage (4.7) and release (5.1). Already deferring → release threshold.
    assert should_defer_to_charge(gshp_ch_active=True, soc_kw=4.8, soc_keep=4.9, was_deferring=True) is True
    print("  test_r4_defer_gshp_on_in_hysteresis_was_deferring: PASSED")


def test_r4_defer_gshp_on_in_hysteresis_was_not_deferring():
    """Winter, SOC inside hysteresis band, was NOT deferring → don't start."""
    # SOC=4.8 between engage (4.7) and release (5.1). Not deferring → engage threshold.
    assert should_defer_to_charge(gshp_ch_active=True, soc_kw=4.8, soc_keep=4.9, was_deferring=False) is False
    print("  test_r4_defer_gshp_on_in_hysteresis_was_not_deferring: PASSED")


def test_r4_defer_gshp_off_high_soc_no_defer():
    """Summer + high SOC: definitely no defer."""
    assert should_defer_to_charge(gshp_ch_active=False, soc_kw=10.0, soc_keep=4.9, was_deferring=False) is False
    print("  test_r4_defer_gshp_off_high_soc_no_defer: PASSED")


# ============================================================================
# R54 with diagnostic source — which term of the max won?
#
# floor = max(reserve, p10_recovery, min(curt_floor, effective_keep))
# Returns (floor_kwh, source) so the publisher can label what's binding.
# ============================================================================


def test_floor_source_effective_keep_wins():
    """Typical mid-overflow day: effective_keep < curt_floor → effective_keep wins."""
    # v31: effective_keep is IGNORED — overflow_floor wins, not the old 4.0/Overnight Need.
    floor, source = compute_floor_with_source(reserve=0.0, p10_recovery=0.0, overflow_floor=10.0, effective_keep=4.0)
    assert floor == 10.0
    assert source == "Curtailment Buffer"
    print(f"  test_floor_source_effective_keep_wins: PASSED (effective_keep ignored → {floor}, {source})")


def test_floor_source_overflow_floor_wins():
    """Big-overflow day: overflow_floor < effective_keep → overflow_floor wins."""
    floor, source = compute_floor_with_source(reserve=0.0, p10_recovery=0.0, overflow_floor=2.0, effective_keep=5.0)
    assert floor == 2.0
    assert source == "Curtailment Buffer"
    print(f"  test_floor_source_overflow_floor_wins: PASSED ({floor}, {source})")


def test_floor_source_p10_recovery_binds():
    """Late in day: p10_recovery exceeds inner min → outer max binds on p10_recovery."""
    # v31: p10_recovery (evening reserve) binds when it exceeds overflow_floor.
    floor, source = compute_floor_with_source(reserve=0.0, p10_recovery=7.0, overflow_floor=5.0, effective_keep=4.0)
    assert floor == 7.0
    assert source == "P10 Recovery"
    print(f"  test_floor_source_p10_recovery_binds: PASSED ({floor}, {source})")


def test_floor_source_reserve_binds():
    """Pathological: everything below reserve → reserve wins."""
    floor, source = compute_floor_with_source(reserve=0.5, p10_recovery=0.0, overflow_floor=0.2, effective_keep=0.3)
    assert floor == 0.5
    assert source == "Reserve"
    print(f"  test_floor_source_reserve_binds: PASSED ({floor}, {source})")


def test_floor_source_tie_picks_inner_min_over_others():
    """v31: floor = max(reserve, p10_recovery, overflow_floor). On a tie between
    overflow_floor and p10_recovery, overflow_floor (the first-checked term) keeps
    the 'Curtailment Buffer' label."""
    floor, source = compute_floor_with_source(reserve=0.0, p10_recovery=10.0, overflow_floor=10.0, effective_keep=4.0)
    assert floor == 10.0
    assert source == "Curtailment Buffer", f"Got {source}"
    print(f"  test_floor_source_tie_picks_inner_min_over_others: PASSED ({floor}, {source})")


def test_floor_source_today_yesterday_morning():
    """v31: same inputs, but effective_keep no longer wins — the overnight reserve
    is Predbat's, so the floor follows overflow_floor (curtailment), leaving CM
    free to drain toward it instead of holding 41% for the evening.
    """
    floor, source = compute_floor_with_source(reserve=0.0, p10_recovery=0.4, overflow_floor=15.16, effective_keep=7.49)
    assert abs(floor - 15.16) < 0.01
    assert source == "Curtailment Buffer"
    print(f"  test_floor_source_today_yesterday_morning: PASSED ({floor}, {source})")


# ============================================================================
# Split-threshold proposed phase (shadow mode for upcoming HA refactor)
#
# Charge below charge_below (= p10_recovery), drain above drain_above
# (= curt_floor), Hold otherwise. Plugin still publishes legacy target_soc
# unchanged — this sensor is for monitoring before we cut the automation over.
# ============================================================================


def test_proposed_phase_hold_in_band():
    """SOC between thresholds → Hold (the common case)."""
    from curtailment_calc import compute_proposed_phase

    phase = compute_proposed_phase(soc_kwh=8.0, charge_below_kwh=2.0, drain_above_kwh=14.0)
    assert phase == "Hold", f"Expected Hold, got {phase}"
    print(f"  test_proposed_phase_hold_in_band: PASSED ({phase})")


def test_phase_to_policy_mapping():
    """RD9/RD3 (v30): curtailment phase → dispatch policy name for
    input_select.sig_dispatch_policy. Drain→Max Export, Hold→Hold Battery,
    Charge→Solar Charge Battery, Off→Predbat; unknown → Predbat (fail safe:
    hand back rather than mis-drive)."""
    from curtailment_calc import phase_to_policy

    assert phase_to_policy("Drain") == "Max Export"
    assert phase_to_policy("Hold") == "Hold Battery"
    assert phase_to_policy("Charge") == "Solar Charge Battery"
    assert phase_to_policy("Off") == "Predbat"
    assert phase_to_policy("wat") == "Predbat"
    print("  test_phase_to_policy_mapping: PASSED")


def _policy_calls(base):
    return [s[1]["option"] for s in base.services if s[0] == "input_select/select_option" and s[1].get("entity_id") == "input_select.sig_dispatch_policy"]


def _keep_floor_calls(base):
    return [s[1]["value"] for s in base.services if s[0] == "input_number/set_value" and s[1].get("entity_id") == "input_number.sig_keep_floor_pct"]


def test_dispatch_policy_gated_off_publishes_intended_only():
    """RD9 observe-only: gate off → NO input_select write, but the intended policy
    IS published to sensor.predbat_curtailment_intended_policy with acting=False."""
    base = MockBase()
    plugin = CurtailmentPlugin(base)
    plugin._charge_below, plugin._drain_above = 2.0, 14.0
    base.services.clear()
    plugin._publish_dispatch_policy(plugin_active=True, floor_kwh=8.0, soc_kwh=8.0, soc_max=18.08)
    assert not _policy_calls(base), f"gate off must not write policy, got {base.services}"
    pub = base.published.get("sensor.predbat_curtailment_intended_policy")
    assert pub is not None and pub["value"] == "Hold Battery", f"intended policy should publish Hold Battery, got {pub}"
    assert pub["attrs"]["acting"] is False, f"acting should be False when gate off, got {pub}"
    print("  test_dispatch_policy_gated_off_publishes_intended_only: PASSED")


def test_dispatch_policy_drives_hold_when_enabled():
    """Gate on + active + SOC in band → Hold Battery + keep floor set. v32.3: Hold
    is not a curtailment drain, so the sell floor is the overnight reserve
    (overnight_target), NOT the overflow_floor drain target."""
    base = MockBase()
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    plugin = CurtailmentPlugin(base)
    plugin._charge_below, plugin._drain_above = 2.0, 14.0
    plugin._overnight_target_kwh = 8.0  # ~44%
    base.services.clear()
    plugin._publish_dispatch_policy(True, floor_kwh=8.0, soc_kwh=8.0, soc_max=18.08)
    assert _policy_calls(base) == ["Hold Battery"], base.services
    kf = _keep_floor_calls(base)
    assert kf and abs(kf[-1] - 44) <= 1, f"Hold sell floor = overnight reserve ~44%, got {kf}"
    assert plugin._policy_driving is True
    print("  test_dispatch_policy_drives_hold_when_enabled: PASSED")


def test_dispatch_policy_max_export_high_soc():
    """SOC > drain_above → Max Export."""
    base = MockBase()
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    plugin = CurtailmentPlugin(base)
    plugin._charge_below, plugin._drain_above = 2.0, 14.0
    base.services.clear()
    plugin._publish_dispatch_policy(True, floor_kwh=14.0, soc_kwh=16.0, soc_max=18.08)
    assert _policy_calls(base) == ["Max Export"], base.services
    print("  test_dispatch_policy_max_export_high_soc: PASSED")


def test_overflow_tracking_published_and_below_p90():
    """RD31 end to end: the estimate reaches the phase sensor, and on a day that is
    UNDER-delivering it must sit below the p90 bound.

    Live 2026-08-07 13:52 is the case: bands p10 3.46 / p50 8.90 / p90 11.39 with
    confidence 0.82, so CM braced for ~11 kWh on a day tracking below p10.
    """
    pv = {m: 8.0 for m in range(0, 480, PLUGIN_STEP)}
    load = {m: 1.0 for m in range(0, 480, PLUGIN_STEP)}
    overrides = {"sensor.sigen_plant_pv_power": 8.0, "sensor.sigen_plant_consumed_power": 1.0}
    overrides.update(_make_p90_sensors(p90_peak_kw=10.0, solcast_remaining=45.0, all_bands=True))
    fc = overrides["sensor.solcast_pv_forecast_forecast_today"]["detailedForecast"]
    early = [{"period_start": "2025-07-12T{:02d}:{:02d}:00+00:00".format(9 + i // 2, 30 * (i % 2)), "pv_estimate10": 4.0, "pv_estimate": 6.0, "pv_estimate90": 8.0} for i in range(6)]
    overrides["sensor.solcast_pv_forecast_forecast_today"] = {"detailedForecast": early + fc}
    overrides["sensor.sigen_plant_daily_pv_energy"] = 12.0  # vs p50 expected 18.0 -> ratio 0.667
    base = MockBase(pv_step=pv, load_step=load, soc_kw=BATTERY_KWH * 0.5, minutes_now=720, best_soc_keep=4.0, sensor_overrides=overrides)
    plugin = CurtailmentPlugin(base)
    plugin._peak_pv, plugin._peak_pv_time = 9.0, 720
    plugin.on_update()
    attrs = base.published["sensor.predbat_curtailment_phase"]["attrs"]
    assert "overflow_tracking" in attrs, "estimate must reach the phase sensor"
    assert attrs["day_tracking_ratio"] is not None, "ratio must be computed with 18 kWh of expected energy behind us"
    assert abs(attrs["day_tracking_ratio"] - 0.667) < 0.01, attrs["day_tracking_ratio"]
    assert attrs["overflow_tracking"] <= attrs["overflow_p90"], "an under-delivering day cannot estimate ABOVE the p90 bound"
    # "<= p90" was the ONLY assertion here and it is far too weak: it passed
    # happily while overflow_tracking equalled overflow_p50 exactly, which is
    # what a no-op looks like. Live 2026-08-11 09:39: day_ratio 0.694,
    # overflow_p50 16.43, overflow_tracking 16.43 — RD31 had done nothing since
    # it shipped, because the ratio went into R58's 30-minute window knob.
    #
    # Overflow is non-linear in PV, so a 0.667 ratio must cut it SHARPLY, not
    # shave it. Assert against p50 (the band it rescales), with real headroom.
    assert attrs["overflow_tracking"] < attrs["overflow_p50"] * 0.75, "a 0.667 day must estimate WELL below p50 ({} vs p50 {}) — equal-to-p50 is the no-op signature".format(attrs["overflow_tracking"], attrs["overflow_p50"])
    print("  test_overflow_tracking_published_and_below_p90: PASSED (ratio {} -> {} vs p50 {} / p90 {})".format(attrs["day_tracking_ratio"], attrs["overflow_tracking"], attrs["overflow_p50"], attrs["overflow_p90"]))


def test_day_tracking_ratio():
    """RD31: how the day is ACTUALLY delivering, as a ratio of forecast-to-now."""
    assert abs(day_tracking_ratio(34.84, 39.85) - 0.874) < 0.001
    # Too early / no sun yet -> no opinion, not a divide-by-zero or a wild ratio.
    assert day_tracking_ratio(0.0, 0.0) is None
    assert day_tracking_ratio(0.5, 0.2) is None, "below the minimum expected energy the ratio is noise"
    # Clamped: a sensor glitch must not scale the forecast into fantasy.
    assert day_tracking_ratio(200.0, 20.0) == 2.0
    assert day_tracking_ratio(0.0, 20.0) == 0.1
    print("  test_day_tracking_ratio: PASSED")


def test_at_risk_kwh_is_raw_energy_not_padded_percent():
    """RD30: the human-facing shortfall is ENERGY, with no control padding.

    The card showed `headroom_short_pct` — overflow x 1.05 plus a 1.8 kWh
    reserve, expressed as a percentage of the pack. Live 2026-08-07 that printed
    "short 35% room" for a raw shortfall of 3.8 kWh. The safety factor and the
    reserve belong in the DECISION, not in the number a human acts on, and
    "% of pack" is not a unit anyone can act on at all.

    at_risk = max(0, overflow - headroom). Nothing else.
    """
    base = MockBase(soc_kw=18.08 * 0.435)  # live 2026-08-07 12:57
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    plugin = CurtailmentPlugin(base)
    plugin._overflow_p90 = 14.14
    plugin._charge_below, plugin._drain_above = 2.0, 14.0
    plugin._publish_dispatch_policy(True, floor_kwh=8.0, soc_kwh=18.08 * 0.435, soc_max=18.08)
    attrs = base.published["sensor.predbat_curtailment_intended_policy"]["attrs"]
    have = 18.08 - 18.08 * 0.435
    assert abs(attrs["pv_at_risk_kwh"] - round(14.14 - have, 2)) < 0.02, "at_risk = overflow - headroom = {:.2f}, got {}".format(14.14 - have, attrs["pv_at_risk_kwh"])
    assert attrs["pv_at_risk_kwh"] < attrs["headroom_short_kwh"], "at_risk must be SMALLER than the padded control figure — that padding is the bug being fixed"
    print("  test_at_risk_kwh_is_raw_energy_not_padded_percent: PASSED ({} kWh vs padded {})".format(attrs["pv_at_risk_kwh"], attrs["headroom_short_kwh"]))


def test_at_risk_is_zero_when_the_overflow_fits():
    """No overflow, or it fits, means nothing is at risk — not a negative number."""
    base = MockBase(soc_kw=1.0)
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    plugin = CurtailmentPlugin(base)
    plugin._overflow_p90 = 2.0
    plugin._charge_below, plugin._drain_above = 2.0, 14.0
    plugin._publish_dispatch_policy(True, floor_kwh=8.0, soc_kwh=1.0, soc_max=18.08)
    attrs = base.published["sensor.predbat_curtailment_intended_policy"]["attrs"]
    assert attrs["pv_at_risk_kwh"] == 0.0, "fits comfortably -> 0, got {}".format(attrs["pv_at_risk_kwh"])
    print("  test_at_risk_is_zero_when_the_overflow_fits: PASSED")


def test_risk_verdict_is_not_fooled_by_the_sun_merely_being_up():
    """RD33 — the verdict must key off the plugin's OWN fits determination.

    The first cut of this gated on `_peaked`, which reads `_peak_pv > 0.5`. That
    is a RUNNING MAXIMUM of PV seen today, so it means "the sun is up", NOT "we
    are past the peak". On 2026-08-08 PV crossed 0.5 kW at ~07:08 — the exact
    minute the card was complained about — so the gate would have flipped to
    "fits" at precisely the moment it was wrong. A near no-op shipped as a fix.

    `_overflow_fits_latched` is the real determination: it carries the safety
    factor, the R45 reserve and FITS_HYST_KWH, and is what the drain logic acts
    on. Here the sun is up (peak 0.66 kW, the live value) and the battery is
    empty so the raw subtraction says nothing is at risk — but the plugin has
    NOT concluded the overflow fits, so the honest answer is "too early".
    """
    base = MockBase(soc_kw=0.33)  # 1.8%, the live SOC
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    plugin = CurtailmentPlugin(base)
    plugin._overflow_p90 = 2.0  # headroom 17.75 >> 2.0, so at_risk == 0
    plugin._charge_below, plugin._drain_above = 0.5, 0.18
    # `_peaked` is what calculate() sets from `_peak_pv > 0.5`. Setting _peak_pv
    # alone does NOT exercise the gate — the first version of this test did that
    # and passed against the very code it was written to condemn.
    plugin._peak_pv = 0.66  # the live 07:10 value
    plugin._peaked = True  # sun IS up — the old gate would have said "fits" here
    plugin._overflow_fits_latched = False
    plugin._publish_dispatch_policy(True, floor_kwh=0.18, soc_kwh=0.33, soc_max=18.08)
    attrs = base.published["sensor.predbat_curtailment_intended_policy"]["attrs"]
    assert attrs["pv_at_risk_kwh"] == 0.0, "precondition: the raw subtraction must read zero here"
    # 2026-08-11: this asserted "too early". Once "tight" existed as its own
    # state that became the honest answer here — the sun IS up, so the verdict
    # is not premature, it is simply that the latch has not concluded "fits".
    # The defect being pinned is unchanged and is the important half: `_peaked`
    # must never by itself produce "fits".
    assert attrs["risk_verdict"] == "tight", "sun up without a fits determination is 'tight', got '{}'".format(attrs["risk_verdict"])
    assert attrs["risk_verdict"] != "fits", "the 07:08 defect: sun-up must never read as 'fits'"
    print("  test_risk_verdict_is_not_fooled_by_the_sun_merely_being_up: PASSED")


def _advice_attrs(overflow_kwh, tracking, soc_frac):
    """Publish the phase sensor and return the advice attributes."""
    base = MockBase(soc_kw=BATTERY_KWH * soc_frac)
    plugin = CurtailmentPlugin(base)
    plugin._remaining_overflow = overflow_kwh
    plugin._overflow_tracking = tracking
    plugin.publish("active", 0.0, dno_limit_kw=3.68)
    return base.published["sensor.predbat_curtailment_phase"]["attrs"]


def test_advice_uses_the_actual_day_not_p90():
    """The load-advice alert must be sized on how today is ACTUALLY delivering.

    Andrew, 2026-08-10: "this alert should be on the actual day not p90". The
    alert says "run the dishwasher, X kWh is going to waste" — advice, not
    control — so the p90 defence argument does not apply. p90 is what we DEFEND
    against; it is the wrong number to ask someone to act on, because on a day
    tracking below p50 it overstates the waste and cries wolf.

    Display-only: `overflow_kwh` is untouched and the drain still uses it.
    """
    attrs = _advice_attrs(overflow_kwh=9.8, tracking=6.1, soc_frac=0.56)
    assert attrs["advice_overflow_kwh"] == 6.1, "advice must use the tracking estimate, got {}".format(attrs["advice_overflow_kwh"])
    assert attrs["overflow_kwh"] == 9.8, "the CONTROL figure must be untouched, got {}".format(attrs["overflow_kwh"])
    print("  test_advice_uses_the_actual_day_not_p90: PASSED")


def test_advice_falls_back_to_the_forecast_before_the_day_has_data():
    """`overflow_tracking` is None until the day has delivered enough to judge
    (RD31/RD33). Before then the forecast is all we have, and advising on
    nothing is worse than advising on p90."""
    attrs = _advice_attrs(overflow_kwh=9.8, tracking=None, soc_frac=0.56)
    assert attrs["advice_overflow_kwh"] == 9.8, "no tracking yet -> fall back to the forecast, got {}".format(attrs["advice_overflow_kwh"])
    print("  test_advice_falls_back_to_the_forecast_before_the_day_has_data: PASSED")


def test_advice_shortfall_is_computed_once_not_three_times():
    """One number, three readers.

    The two trigger helpers (`binary_sensor.big_overflow_shortfall` and
    `..._high`) and the notification message each re-derived
    `overflow - (1 - soc/100) * 18.08`, with the pack size hardcoded in all
    three. That is the RD22 shape exactly: change one, the others drift. The
    plugin now publishes the finished figure and all three just read it.
    """
    # 85% SOC leaves 2.71 kWh of room against 6.1 kWh of overflow — a real
    # shortfall, which is the only case the alert exists for.
    attrs = _advice_attrs(overflow_kwh=9.8, tracking=6.1, soc_frac=0.85)
    room = BATTERY_KWH * (1 - 0.85)
    assert abs(attrs["advice_room_kwh"] - round(room, 2)) < 0.02, "room {} vs {}".format(attrs["advice_room_kwh"], room)
    assert abs(attrs["advice_shortfall_kwh"] - round(6.1 - room, 2)) < 0.02, "shortfall must be advice_overflow - room, got {}".format(attrs["advice_shortfall_kwh"])
    # And the p90 figure would have overstated it by exactly the gap.
    assert round(9.8 - room, 2) > attrs["advice_shortfall_kwh"], "p90 must overstate the waste — that is the defect being fixed"
    print("  test_advice_shortfall_is_computed_once_not_three_times: PASSED")


def test_advice_shortfall_never_goes_negative():
    """A comfortable day must read 0, not a negative "waste"."""
    attrs = _advice_attrs(overflow_kwh=1.0, tracking=0.4, soc_frac=0.10)
    assert attrs["advice_shortfall_kwh"] == 0.0, "plenty of room -> 0, got {}".format(attrs["advice_shortfall_kwh"])
    print("  test_advice_shortfall_never_goes_negative: PASSED")


def test_risk_verdict_is_absent_when_there_is_no_overflow_question():
    """No forecast overflow means there is no headroom question to answer, so
    there must be no verdict — matching `pv_at_risk_kwh`, which already
    publishes None here so the card omits the line.

    Found by reading the LIVE value after deploying, 2026-08-10 18:34: the
    sensor read `overflow_fits: True` and `pv_at_risk_kwh: None` next to
    `risk_verdict: "too early"`. The verdict is initialised to "too early" and
    only computed inside `if ovf > 0`, so with no overflow it kept the default
    and contradicted the attribute beside it. "Too early" also implies the
    answer is coming later, when there is nothing to know.
    """
    base = MockBase(soc_kw=14.4)
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    plugin = CurtailmentPlugin(base)
    plugin._overflow_p90 = 0.0  # end of day — the live 18:34 case
    plugin._charge_below, plugin._drain_above = 2.0, 14.0
    plugin._overflow_fits_latched = True
    plugin._publish_dispatch_policy(True, floor_kwh=8.0, soc_kwh=14.4, soc_max=18.08)
    attrs = base.published["sensor.predbat_curtailment_intended_policy"]["attrs"]
    assert attrs["pv_at_risk_kwh"] is None, "precondition: no overflow -> no at-risk figure"
    assert attrs["risk_verdict"] is None, "no overflow -> no verdict, got '{}'".format(attrs["risk_verdict"])
    print("  test_risk_verdict_is_absent_when_there_is_no_overflow_question: PASSED")


def test_publishes_the_tracking_margin_beside_the_p90_verdict():
    """The verdict is p90-based; the card also shows the day's tracking band.
    Adjacent, unexplained, they read as a contradiction.

    Live 2026-08-11 14:36: "sky tracking below p10" next to "tight". Both true —
    p90 8.29 needs 10.5 against 9.13 of headroom (short 1.37), while the day is
    actually tracking 5.74 which needs 7.83 (margin +1.3, comfortable). The card
    had no way to say "tight against the p90 defence, fine on today's actual".

    Publish the tracking-based margin so the card states the basis instead of
    the reader inferring a bug. Control is unchanged and stays on p90 — that is
    the whole point of defending it.
    """
    base = MockBase(soc_kw=18.08 - 9.13)
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    plugin = CurtailmentPlugin(base)
    plugin._overflow_p90 = 8.29
    plugin._overflow_tracking = 5.74
    plugin._charge_below, plugin._drain_above = 2.0, 14.0
    plugin._peaked = True
    plugin._overflow_fits_latched = False
    plugin._publish_dispatch_policy(True, floor_kwh=8.0, soc_kwh=18.08 - 9.13, soc_max=18.08)
    attrs = base.published["sensor.predbat_curtailment_intended_policy"]["attrs"]
    assert attrs["risk_verdict"] == "tight", "p90 verdict unchanged"
    margin = attrs["headroom_margin_tracking_kwh"]
    assert margin is not None, "must publish the tracking-based margin"
    assert abs(margin - 1.3) < 0.05, "have 9.13 - need_on_tracking 7.83 = +1.3, got {}".format(margin)
    assert margin > 0, "the point: comfortable on today's actual while tight on p90"
    print("  test_publishes_the_tracking_margin_beside_the_p90_verdict: PASSED (+{:.2f} kWh)".format(margin))


def test_risk_verdict_names_the_tight_case_instead_of_calling_it_early():
    """RD33 fix — "too early" was a catch-all hiding a real, common state.

    Live 2026-08-11 14:31: overflow_p90 8.29 against 9.13 kWh of headroom, so
    the raw energy FITS (pv_at_risk 0.0) — but the safety-factored requirement
    is 10.5 (1.05x + the R45 reserve), short by 1.37. The latch is therefore
    False while at_risk is 0, which fell through to the `else` and printed
    "too early to call" at half past two in the afternoon.

    That is not too early, it is TIGHT: fits on raw energy, no margin. It is
    also precisely the state CM armed on this morning (need 19.23 vs have
    17.32), so it is the common case, not an edge.

    "too early" now means only what it says: no basis to judge yet, i.e. the
    peak has not been seen.
    """
    base = MockBase(soc_kw=18.08 - 9.13)
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    plugin = CurtailmentPlugin(base)
    plugin._overflow_p90 = 8.29
    plugin._charge_below, plugin._drain_above = 2.0, 14.0
    plugin._peaked = True
    plugin._overflow_fits_latched = False
    plugin._publish_dispatch_policy(True, floor_kwh=8.0, soc_kwh=18.08 - 9.13, soc_max=18.08)
    attrs = base.published["sensor.predbat_curtailment_intended_policy"]["attrs"]
    assert attrs["pv_at_risk_kwh"] == 0.0, "precondition: raw energy must fit"
    assert attrs["risk_verdict"] == "tight", "raw fits but no margin is 'tight', not 'too early' — got '{}'".format(attrs["risk_verdict"])
    print("  test_risk_verdict_names_the_tight_case_instead_of_calling_it_early: PASSED")


def test_risk_verdict_says_fits_only_when_the_plugin_has_decided_it():
    """The one state that may print "fits": the real latch is set."""
    base = MockBase(soc_kw=1.0)
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    plugin = CurtailmentPlugin(base)
    plugin._overflow_p90 = 2.0
    plugin._charge_below, plugin._drain_above = 2.0, 14.0
    plugin._overflow_fits_latched = True
    plugin._publish_dispatch_policy(True, floor_kwh=8.0, soc_kwh=1.0, soc_max=18.08)
    attrs = base.published["sensor.predbat_curtailment_intended_policy"]["attrs"]
    assert attrs["risk_verdict"] == "fits", "latched fits must read 'fits', got '{}'".format(attrs["risk_verdict"])
    print("  test_risk_verdict_says_fits_only_when_the_plugin_has_decided_it: PASSED")


def test_risk_verdict_reports_risk_early_because_that_direction_is_safe():
    """ "at risk" IS trustworthy early, and must not be gagged.

    at_risk = remaining p90 overflow - headroom NOW. Early in the day headroom is
    at its maximum, so this is the SMALLEST the figure will ever be — it can only
    worsen as the battery fills. A shortfall visible at dawn is therefore real.
    Only the "fits" direction is unsound early, which is why the gate is
    asymmetric rather than a blanket "too early".
    """
    base = MockBase(soc_kw=18.08 * 0.435)
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    plugin = CurtailmentPlugin(base)
    plugin._overflow_p90 = 14.14
    plugin._charge_below, plugin._drain_above = 2.0, 14.0
    plugin._peak_pv = 0.0  # not even sunrise
    plugin._overflow_fits_latched = False
    plugin._publish_dispatch_policy(True, floor_kwh=8.0, soc_kwh=18.08 * 0.435, soc_max=18.08)
    attrs = base.published["sensor.predbat_curtailment_intended_policy"]["attrs"]
    assert attrs["pv_at_risk_kwh"] > 0
    assert attrs["risk_verdict"] == "at risk", "a real shortfall must be reported even pre-dawn, got '{}'".format(attrs["risk_verdict"])
    print("  test_risk_verdict_reports_risk_early_because_that_direction_is_safe: PASSED")


def test_forecast_energy_to_now_integrates_the_slots():
    """RD29: the tracking band must compare ENERGY, not a single peak.

    Solcast slots are kW means over 30 min, so energy-to-now is
    sum(kW x 0.5) over slots that have already started.
    """
    fc = [
        {"period_start": "2026-08-07T09:00:00+01:00", "pv_estimate10": 2.0, "pv_estimate": 4.0, "pv_estimate90": 6.0},
        {"period_start": "2026-08-07T09:30:00+01:00", "pv_estimate10": 3.0, "pv_estimate": 5.0, "pv_estimate90": 7.0},
        {"period_start": "2026-08-07T10:00:00+01:00", "pv_estimate10": 9.0, "pv_estimate": 9.0, "pv_estimate90": 9.0},
    ]
    # 10:00 local -> the first two slots have completed, the third has not started.
    p10, p50, p90 = forecast_energy_to_now(fc, minutes_now=600, local_offset_hours=1)
    assert abs(p10 - 2.5) < 1e-6, "p10 = (2.0+3.0)*0.5, got {}".format(p10)
    assert abs(p50 - 4.5) < 1e-6, "p50 = (4.0+5.0)*0.5, got {}".format(p50)
    assert abs(p90 - 6.5) < 1e-6, "p90 = (6.0+7.0)*0.5, got {}".format(p90)
    assert forecast_energy_to_now([], 600, 1) == (0.0, 0.0, 0.0)
    print("  test_forecast_energy_to_now_integrates_the_slots: PASSED")


def test_tracking_band_from_energy_not_peak():
    """The band classifier is band-agnostic — feed it kWh and it answers the
    question that actually matters: how is the day GOING.

    Live 2026-08-06: peak-based said "above p90, 100%" because one cloud-edge
    instant hit 10.14 kW against a 7.72 kW half-hourly p90 mean, on a day that
    delivered 38.56 kWh vs a 45.96 kWh p50 forecast — i.e. p10-p50 on energy.
    """
    # Yesterday's whole-day figures: actual 38.56 against p10 30.62 / p50 45.96 / p90 58.74.
    label, pct = classify_forecast_tracking(38.56, 30.62, 45.96, 58.74)
    assert label == "p10-p50", "38.56 kWh sits between p10 and p50, got {}".format(label)
    assert 10 < pct < 50, "percentile should land inside the p10-p50 span, got {}".format(pct)
    print("  test_tracking_band_from_energy_not_peak: PASSED ({} {:.0f}%)".format(label, pct))


def test_no_overflow_charge_target_is_the_overnight_need():
    """RD28: once no overflow is left, bank to TONIGHT'S NEED, then hold.

    While curtailment competes for the same kWh, `charge_below` (the P10 recovery
    floor) is the right threshold: it is a DEADLINE — "be at least this high now,
    or a P10 afternoon will not get you there" — and deferring preserves headroom.

    Once overflow_p90 is 0 nothing competes, so deferring only earns an export
    credit and risks buying the same energy back overnight at import rates.

    Live 2026-08-06 18:02: overflow_p90 0.0, SOC 5.66 kWh, overnight_target
    6.62 kWh, charge_below 5.15 kWh. SOC was ABOVE charge_below, so the Schmitt
    said Hold and CM exported the surplus while 0.96 kWh short of tonight's need.
    P10 margin was 0.51 kWh — about eight minutes of surplus. Andrew had to set a
    manual Solar Charge override to bank it.
    """
    t = compute_no_overflow_charge_target(overnight_target_kwh=6.62, soc_max=18.08, overflow_p90_kwh=0.0, max_reserved_kwh=1.8)
    assert abs(t - 6.62) < 0.01, "with no overflow the target is the overnight need, got {}".format(t)
    assert t > 5.15, "must be ABOVE the P10 recovery floor — that floor is a deadline, not a target"
    print("  test_no_overflow_charge_target_is_the_overnight_need: PASSED ({:.2f} kWh)".format(t))


def test_no_overflow_charge_target_never_eats_the_headroom():
    """The safeguard: never bank so much that a remaining overflow stops fitting.

    "No overflow left" is a forecast that can move. While p90 overflow is still
    non-zero the target is clamped to whatever leaves room for it — using the ONE
    definition of required headroom, never a second expression (Charter).
    """
    # Pass the safety factor EXPLICITLY to both, so the test cannot disagree with
    # the function about which constant applies.
    sf = 1.05
    ovf = 6.0
    need = required_headroom_kwh(ovf, 1.8, sf)
    t = compute_no_overflow_charge_target(overnight_target_kwh=16.0, soc_max=18.08, overflow_p90_kwh=ovf, max_reserved_kwh=1.8, safety_factor=sf)
    assert abs(t - (18.08 - need)) < 0.01, "target must clamp to soc_max - required_headroom ({:.2f}), got {:.2f}".format(18.08 - need, t)
    assert t < 16.0, "the clamp must actually bite when the overnight need would eat the headroom"
    assert compute_no_overflow_charge_target(6.62, 18.08, 40.0, 1.8) == 0.0, "must never go negative on a huge-overflow day"
    print("  test_no_overflow_charge_target_never_eats_the_headroom: PASSED (clamped to {:.2f})".format(t))


def test_no_drain_branch_charges_to_the_overnight_need():
    """RD28 end to end: in the no_drain state, the plugin must Charge whenever SOC
    is below tonight's need — NOT merely below the P10 recovery floor.

    Reproduces live 2026-08-06 18:02 exactly: SOC 5.66 kWh sits ABOVE charge_below
    5.15, so the old code said Hold and exported the surplus while 0.96 kWh short
    of the 6.62 kWh overnight target.
    """
    base = MockBase(soc_kw=5.66)
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    plugin = CurtailmentPlugin(base)
    plugin._policy_override = "no_drain"
    plugin._charge_below, plugin._drain_above = 5.15, 17.83
    plugin._overnight_target_kwh = 6.62
    plugin._overflow_p90 = 0.0
    base.services.clear()
    plugin._publish_dispatch_policy(True, floor_kwh=17.83, soc_kwh=5.66, soc_max=18.08)
    written = _policy_calls(base)
    assert written and written[-1] == "Solar Charge Battery", "SOC 5.66 < overnight need 6.62 with no overflow left must CHARGE, got {}".format(written)
    print("  test_no_drain_branch_charges_to_the_overnight_need: PASSED")


def test_no_drain_branch_holds_once_the_need_is_met():
    """...and stops there. Charge to the target, then Hold — not Charge to full."""
    base = MockBase(soc_kw=7.0)
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    plugin = CurtailmentPlugin(base)
    plugin._policy_override = "no_drain"
    plugin._charge_below, plugin._drain_above = 5.15, 17.83
    plugin._overnight_target_kwh = 6.62
    plugin._overflow_p90 = 0.0
    base.services.clear()
    plugin._publish_dispatch_policy(True, floor_kwh=17.83, soc_kwh=7.0, soc_max=18.08)
    written = _policy_calls(base)
    assert written and written[-1] == "Hold Battery", "SOC 7.0 above the 6.62 need must HOLD, not keep charging, got {}".format(written)
    print("  test_no_drain_branch_holds_once_the_need_is_met: PASSED")


def test_no_drain_still_never_drains():
    """RD17 unchanged: the no_drain override still clamps Drain to Hold."""
    base = MockBase(soc_kw=17.0)
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    plugin = CurtailmentPlugin(base)
    plugin._policy_override = "no_drain"
    plugin._charge_below, plugin._drain_above = 5.15, 2.0
    plugin._overnight_target_kwh = 6.62
    plugin._overflow_p90 = 0.0
    base.services.clear()
    plugin._publish_dispatch_policy(True, floor_kwh=2.0, soc_kwh=17.0, soc_max=18.08)
    written = _policy_calls(base)
    assert written and written[-1] == "Hold Battery", "no_drain must clamp Drain->Hold, got {}".format(written)
    print("  test_no_drain_still_never_drains: PASSED")


def test_card_publishes_the_threshold_actually_in_force():
    """The card must state the threshold the code USES, not a raw input.

    `compute_proposed_phase` charges below `min(charge_below, drain_above)` — on a
    cross-over day (deficit forecast, so charge_below > drain_above) the effective
    charge target is drain_above, because charging to the LOWER of the two
    preserves curtailment headroom.

    The sensor published `charge_below` raw. Live 2026-08-06 10:56 the card read
    "charge if below 2.8% (P10 recovery floor)" while the threshold in force was
    1.0% — a stated number that was not the one being used. Andrew spotted the
    inversion from the card and asked whether charge was being ignored; it was,
    correctly, but nothing on the card said so.
    """
    base = MockBase()
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    # The live drain floor, so the dawn arm cannot hold drain_above above
    # charge_below and quietly un-cross the very case under test.
    base._sensor_overrides["input_number.sig_drain_floor_pct"] = "1.0"
    plugin = CurtailmentPlugin(base)
    # Cross-over day: charge_below (2.8%) ABOVE drain_above (1.0%).
    plugin._p10_recovery_floor, plugin._overflow_floor_kwh = 0.5, 0.18
    plugin._effective_keep_kwh = 0.0
    plugin._dawn_released = True
    plugin.publish("active", 0.18, dno_limit_kw=4.0)
    attrs = base.published.get("sensor.predbat_curtailment_charge_below", {}).get("attrs", {})
    assert attrs.get("effective_charge_pct") is not None, "card needs the threshold in force, not just charge_below"
    expect = min(plugin._charge_below, plugin._drain_above) / 18.08 * 100.0
    assert abs(attrs["effective_charge_pct"] - round(expect, 1)) < 0.05, "effective target must be min(charge_below, drain_above) = {:.2f}%, got {}".format(expect, attrs["effective_charge_pct"])
    assert attrs["effective_charge_pct"] < attrs["soc_pct"], "on a cross-over day the effective target must be BELOW charge_below — that is the whole point"
    print("  test_card_publishes_the_threshold_actually_in_force: PASSED ({}% in force vs {}% charge_below)".format(attrs["effective_charge_pct"], attrs["soc_pct"]))


def test_low_soc_never_hands_back_mid_window():
    """RD27: while ACTIVE, CM never hands the wheel back on low SOC.

    The old RD4 'A' handover set the policy select to Predbat whenever SOC fell
    below the drain floor. It handed to NOBODY: `_release_to_predbat` is not
    called on this path, so `read_only` stayed on and the three Predbat mappers
    stayed disabled. Predbat therefore could not act, the heartbeat went inert in
    its Predbat branch, and the plant fell through to the SIG's own Maximum Self
    Consumption default.

    Live 2026-08-06: handed back at 04:50 and still handed back at 09:00 with the
    plant charging at 3.5 kW and ZERO export, on a day forecasting 19.89 kWh of
    overflow. Surplus was 3.54 kW against a 3.68 kW cap — every kWh of that could
    have been exported at full value; instead it filled the battery and consumed
    headroom that the overflow peak then had to curtail. ~2% of pack, unrecoverable.

    The handover only ever existed to escape CM's own drain-floor clamp. RD22 made
    that clamp sell-only, so there is nothing left to escape: at low SOC the Schmitt
    simply picks Charge (below charge_below) or Hold, both of which keep the battery
    off the grid and keep exporting surplus.
    """
    base = MockBase()
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    plugin = CurtailmentPlugin(base)
    plugin._charge_below, plugin._drain_above = 2.0, 14.0
    base.services.clear()
    # 0.4 kWh of 18.08 = 2.2%, below the 2.8% drain floor.
    plugin._publish_dispatch_policy(True, floor_kwh=8.0, soc_kwh=0.4, soc_max=18.08)
    written = _policy_calls(base)
    assert written and written[-1] != "Predbat", "low SOC must NOT hand back mid-window, got {}".format(written)
    # 0.4 kWh < charge_below 2.0 kWh, so the Schmitt wants to refill.
    assert written[-1] == "Solar Charge Battery", "below charge_below the Schmitt must Charge, got {}".format(written[-1])
    assert base.set_read_only is True, "CM still owns the wheel, so read_only must stay set"
    assert plugin._policy_driving is True
    print("  test_low_soc_never_hands_back_mid_window: PASSED ({})".format(written[-1]))


def test_low_soc_drain_stays_max_export_not_handback():
    """Above charge_below but below the drain floor: still Max Export, never a handback.

    Named for what it actually does. The plugin does NOT clamp Drain to Hold here,
    and it does not need to: the heartbeat's RD22 sell-clamp holds Max Export at PV
    below the floor, so the battery is not sold and the two are physically identical.
    The safety lives in the executor, not in a second rule up here.
    """
    base = MockBase()
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    plugin = CurtailmentPlugin(base)
    # charge_below below the SOC so the Schmitt does not want to charge.
    plugin._charge_below, plugin._drain_above = 0.1, 0.18
    base.services.clear()
    plugin._publish_dispatch_policy(True, floor_kwh=0.18, soc_kwh=0.4, soc_max=18.08)
    written = _policy_calls(base)
    assert written[-1] == "Max Export", "SOC above drain_above must stay Max Export (the sell-clamp makes it safe), got {}".format(written)
    assert base.set_read_only is True, "read_only must stay set — CM still owns the wheel"
    print("  test_low_soc_drain_stays_max_export_not_handback: PASSED")


def test_dispatch_policy_handback_once_on_deactivate():
    """RD10: active->off edge hands to Predbat once + resets keep floor to 38; no repeat."""
    base = MockBase()
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    plugin = CurtailmentPlugin(base)
    plugin._charge_below, plugin._drain_above = 2.0, 14.0
    plugin._cm_controlling = True  # we were controlling the window
    plugin._read_only_set = True
    base.services.clear()
    plugin._publish_dispatch_policy(False, floor_kwh=18.08, soc_kwh=10.0, soc_max=18.08)
    assert _policy_calls(base) == ["Predbat"], base.services
    kf = _keep_floor_calls(base)
    assert kf and abs(kf[-1] - 38) < 0.5, f"keep floor reset to 38, got {kf}"
    assert plugin._policy_driving is False
    assert plugin._cm_controlling is False
    base.services.clear()
    plugin._publish_dispatch_policy(False, 18.08, 10.0, 18.08)
    assert not _policy_calls(base), f"no repeat handback, got {base.services}"
    print("  test_dispatch_policy_handback_once_on_deactivate: PASSED")


def test_sell_floor_overnight_reserve_when_not_draining():
    """v32.3: the sell floor (keep_floor) must NOT track the rising overflow_floor
    while Holding — on a low-overflow morning that climbs to ~68% and reads as
    nonsense (we're not selling). When not curtailment-draining, publish the
    overnight reserve (~overnight_target), the level we actually preserve."""
    base = MockBase()
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    plugin = CurtailmentPlugin(base)
    plugin._charge_below, plugin._drain_above = 0.5, 12.3  # low-overflow: high drain_above
    plugin._policy_override = "no_drain"
    plugin._overnight_target_kwh = 7.0  # ~39%
    base.services.clear()
    # floor_kwh = overflow_floor 12.3 (68%) — the OLD (misleading) sell floor.
    plugin._publish_dispatch_policy(True, floor_kwh=12.3, soc_kwh=1.5, soc_max=18.08)
    kf = _keep_floor_calls(base)
    assert kf and 37 <= kf[-1] <= 40, f"sell floor should be overnight reserve ~39%, not overflow_floor 68%, got {kf}"
    print("  test_sell_floor_overnight_reserve_when_not_draining: PASSED")


def test_sell_floor_overflow_floor_during_curtailment_drain():
    """v32.3: during a genuine curtailment drain (Schmitt Drain, no override), the
    sell floor MUST stay = overflow_floor (drain target) so the big-overflow deep
    drain still works — unchanged from before."""
    base = MockBase()
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    plugin = CurtailmentPlugin(base)
    plugin._charge_below, plugin._drain_above = 0.5, 5.0  # big-overflow: low drain_above
    plugin._policy_override = None
    plugin._overnight_target_kwh = 7.0
    base.services.clear()
    # SOC above drain_above → Schmitt Drain. floor_kwh = overflow_floor 0.9 (5%).
    plugin._publish_dispatch_policy(True, floor_kwh=0.9, soc_kwh=10.0, soc_max=18.08)
    assert _policy_calls(base) == ["Max Export"], base.services
    kf = _keep_floor_calls(base)
    assert kf and abs(kf[-1] - 5) <= 1, f"curtailment drain: sell floor = overflow_floor ~5%, got {kf}"
    print("  test_sell_floor_overflow_floor_during_curtailment_drain: PASSED")


def test_sell_floor_session_dumps_to_overnight_reserve():
    """v32.3: a saving-session Max Export must dump down to the overnight reserve,
    NOT stop at the (high) overflow_floor — otherwise it under-sells the session."""
    base = MockBase()
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    plugin = CurtailmentPlugin(base)
    plugin._charge_below, plugin._drain_above = 0.5, 12.3  # low-overflow day
    plugin._policy_override = "max_export"  # session dump
    plugin._overnight_target_kwh = 7.0
    base.services.clear()
    plugin._publish_dispatch_policy(True, floor_kwh=12.3, soc_kwh=10.0, soc_max=18.08)
    assert _policy_calls(base) == ["Max Export"], base.services
    kf = _keep_floor_calls(base)
    assert kf and 37 <= kf[-1] <= 40, f"session dump sell floor = overnight reserve ~39%, not 68%, got {kf}"
    print("  test_sell_floor_session_dumps_to_overnight_reserve: PASSED")


def _automation_calls(base):
    return [(s[0], s[1].get("entity_id")) for s in base.services if s[0] in ("automation/turn_on", "automation/turn_off")]


def _ems_mode_calls(base):
    return [s[1].get("option") for s in base.services if s[0] == "select/select_option" and s[1].get("entity_id") == "select.sigen_plant_remote_ems_control_mode"]


def test_heartbeat_enabled_on_control():
    """Window start: entering CM control enables the heartbeat register-writer."""
    base = MockBase()
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    plugin = CurtailmentPlugin(base)
    plugin._charge_below, plugin._drain_above = 2.0, 14.0
    base.services.clear()
    plugin._publish_dispatch_policy(True, floor_kwh=8.0, soc_kwh=8.0, soc_max=18.08)
    assert ("automation/turn_on", "automation.sig_dispatch_heartbeat") in _automation_calls(base), base.services
    assert plugin._cm_controlling is True
    print("  test_heartbeat_enabled_on_control: PASSED")


def test_heartbeat_disabled_and_msc_on_handback():
    """Window end: handback disables the heartbeat AND parks EMS-MSC (never app mode)."""
    base = MockBase()
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    plugin = CurtailmentPlugin(base)
    plugin._charge_below, plugin._drain_above = 2.0, 14.0
    plugin._cm_controlling = True
    plugin._read_only_set = True
    base.services.clear()
    plugin._publish_dispatch_policy(False, floor_kwh=18.08, soc_kwh=10.0, soc_max=18.08)
    assert ("automation/turn_off", "automation.sig_dispatch_heartbeat") in _automation_calls(base), base.services
    assert _ems_mode_calls(base) == ["Maximum Self Consumption"], base.services
    print("  test_heartbeat_disabled_and_msc_on_handback: PASSED")


def test_exactly_one_writer_enabled_on_control():
    """Taking control: mapper OFF before heartbeat ON — never both enabled.

    Being disabled IS the mutex, so neither automation carries a condition of its
    own. Ordering matters: a gap with neither enabled is safe (inverter holds its
    last setpoint); an overlap is two writers fighting the same registers.
    """
    base = MockBase()
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    plugin = CurtailmentPlugin(base)
    plugin._charge_below, plugin._drain_above = 2.0, 14.0
    base.services.clear()
    plugin._publish_dispatch_policy(True, floor_kwh=8.0, soc_kwh=8.0, soc_max=18.08)

    calls = _automation_calls(base)
    assert ("automation/turn_off", "automation.predbat_requested_mode_action") in calls, base.services
    assert ("automation/turn_on", "automation.sig_dispatch_heartbeat") in calls, base.services
    off_idx = calls.index(("automation/turn_off", "automation.predbat_requested_mode_action"))
    on_idx = calls.index(("automation/turn_on", "automation.sig_dispatch_heartbeat"))
    assert off_idx < on_idx, f"mapper must be disabled BEFORE heartbeat enabled, got {calls}"

    # The WHOLE Predbat chain must be frozen, not just the mode mapper. The
    # discharging-limit mapper wrote ess_max_discharging_limit=0 at 04:01 on
    # 2026-07-28 and stayed enabled while CM drove, locking the battery for 4.5 h.
    for auto in ("automation.predbat_max_discharging_limit_action", "automation.predbat_max_charging_limit_action"):
        assert ("automation/turn_off", auto) in calls, f"{auto} must also be disabled — it writes plant registers"
    print("  test_exactly_one_writer_enabled_on_control: PASSED")


def test_predbat_neutralised_before_its_chain_is_frozen():
    """Predbat must undo its own register writes before we disable its mappers.

    The writer that changed a register should change it back. Enumerating Predbat's
    registers in CM is a losing game — ess_max_discharging_limit and
    grid_import_limitation were both missed, and a future mapper would be too.

    So set the SOURCE helpers back to neutral and let Predbat's own mappers unwind
    the registers. This must happen BEFORE the mappers are disabled — a disabled
    mapper cannot relay.
    """
    base = MockBase()
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    plugin = CurtailmentPlugin(base)
    plugin._charge_below, plugin._drain_above = 2.0, 14.0
    base.services.clear()
    plugin._publish_dispatch_policy(True, floor_kwh=8.0, soc_kwh=8.0, soc_max=18.08)

    names = [(s[0], s[1].get("entity_id")) for s in base.services]
    neutralised = [i for i, (svc, ent) in enumerate(names) if ent in ("input_select.predbat_requested_mode", "input_number.discharge_rate", "input_number.charge_rate")]
    assert len(neutralised) == 3, f"must neutralise mode + both rate helpers, got {names}"

    first_disable = next(i for i, (svc, ent) in enumerate(names) if svc == "automation/turn_off" and str(ent).startswith("automation.predbat_"))
    assert max(neutralised) < first_disable, f"neutralise must precede disabling the mappers, got {names}"
    print("  test_predbat_neutralised_before_its_chain_is_frozen: PASSED")


def test_exactly_one_writer_enabled_on_handback():
    """Handback: heartbeat OFF before mapper ON.

    Regression 2026-07-27: the mapper had been disabled since the 2026-07-15 swap
    and nothing re-enabled it, so Predbat had no control path at all — it asked for
    Discharging twice overnight on 07-26 and the EMS mode select never moved.
    """
    base = MockBase()
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    plugin = CurtailmentPlugin(base)
    plugin._charge_below, plugin._drain_above = 2.0, 14.0
    plugin._cm_controlling = True
    plugin._read_only_set = True
    base.services.clear()
    plugin._publish_dispatch_policy(False, floor_kwh=18.08, soc_kwh=10.0, soc_max=18.08)

    calls = _automation_calls(base)
    assert ("automation/turn_off", "automation.sig_dispatch_heartbeat") in calls, base.services
    assert ("automation/turn_on", "automation.predbat_requested_mode_action") in calls, base.services
    off_idx = calls.index(("automation/turn_off", "automation.sig_dispatch_heartbeat"))
    on_idx = calls.index(("automation/turn_on", "automation.predbat_requested_mode_action"))
    assert off_idx < on_idx, f"heartbeat must be disabled BEFORE mapper enabled, got {calls}"

    # The mapper must be live before read_only clears, or Predbat's first
    # requested_mode change lands with nothing listening.
    names = [s[0] for s in base.services]
    if "switch/turn_off" in names:
        assert names.index("automation/turn_on") < names.index("switch/turn_off"), f"mapper must be enabled before read_only clears, got {names}"
    print("  test_exactly_one_writer_enabled_on_handback: PASSED")


def test_first_run_reconciles_drifted_writers():
    """After a deploy/restart the writer enables must be reconciled, not assumed.

    Every deploy resets plugin state, and the take/release toggles are EDGE-triggered
    on _cm_controlling. First run adopts _cm_controlling from live read_only — so
    without an explicit reconcile, a drifted pair (both automations on, or both off)
    would persist silently for the whole window. Observed 2026-07-27: the mapper was
    manually enabled while CM was driving, giving two live writers.
    """
    base = MockBase()
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    base.set_read_only = True  # adopted: CM was driving before the restart
    plugin = CurtailmentPlugin(base)
    plugin._charge_below, plugin._drain_above = 2.0, 14.0
    assert plugin._cm_controlling is None, "fresh plugin must start with unknown control state"
    base.services.clear()
    plugin._publish_dispatch_policy(True, floor_kwh=8.0, soc_kwh=8.0, soc_max=18.08)

    calls = _automation_calls(base)
    assert ("automation/turn_off", "automation.predbat_requested_mode_action") in calls, f"first run must disable the mapper when adopting CM control, got {calls}"
    assert ("automation/turn_on", "automation.sig_dispatch_heartbeat") in calls, f"first run must enable the heartbeat, got {calls}"
    print("  test_first_run_reconciles_drifted_writers: PASSED")


def test_heartbeat_untouched_observe_only():
    """Observe-only (gate off): plugin never toggles the heartbeat or EMS mode."""
    base = MockBase()  # gate defaults off
    plugin = CurtailmentPlugin(base)
    plugin._charge_below, plugin._drain_above = 2.0, 14.0
    plugin._publish_dispatch_policy(True, floor_kwh=8.0, soc_kwh=8.0, soc_max=18.08)
    assert not _automation_calls(base), base.services
    assert not _ems_mode_calls(base), base.services
    print("  test_heartbeat_untouched_observe_only: PASSED")


def test_heartbeat_stays_on_through_low_soc():
    """The window spans low-SOC dips: heartbeat stays enabled (only turned on once,
    never off) AND CM keeps driving (RD27) when SOC drops below the drain floor."""
    base = MockBase()
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    plugin = CurtailmentPlugin(base)
    plugin._charge_below, plugin._drain_above = 2.0, 14.0
    # Enter control (SOC in band)
    plugin._publish_dispatch_policy(True, floor_kwh=8.0, soc_kwh=8.0, soc_max=18.08)
    base.services.clear()
    # SOC drops below the drain floor — RD27: still in the window AND still driving
    plugin._publish_dispatch_policy(True, floor_kwh=8.0, soc_kwh=0.4, soc_max=18.08)
    assert not any(s == "automation/turn_off" for s, _ in _automation_calls(base)), f"must NOT disable heartbeat on low-SOC, got {base.services}"
    assert plugin._cm_controlling is True
    written = _policy_calls(base)
    assert written and written[-1] != "Predbat", f"RD27: low SOC must not hand back mid-window, got {base.services}"
    print("  test_heartbeat_stays_on_through_low_soc: PASSED")


def test_read_only_set_when_cm_driving():
    """R3 mutex: gate on + active + SOC in band (CM driving a non-Predbat policy)
    → suppress Predbat via base.set_read_only=True."""
    base = MockBase()
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    plugin = CurtailmentPlugin(base)
    plugin._charge_below, plugin._drain_above = 2.0, 14.0
    assert base.set_read_only is False
    plugin._publish_dispatch_policy(True, floor_kwh=8.0, soc_kwh=8.0, soc_max=18.08)
    assert base.set_read_only is True, "CM driving must suppress Predbat (read_only True)"
    print("  test_read_only_set_when_cm_driving: PASSED")


def test_read_only_released_on_handback():
    """R3/RD6: once CM hands back (plugin inactive, e.g. safe_time) read_only clears
    so Predbat resumes owning the machine."""
    base = MockBase()
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    plugin = CurtailmentPlugin(base)
    plugin._charge_below, plugin._drain_above = 2.0, 14.0
    plugin._publish_dispatch_policy(True, floor_kwh=8.0, soc_kwh=8.0, soc_max=18.08)
    assert base.set_read_only is True
    plugin._publish_dispatch_policy(False, floor_kwh=18.08, soc_kwh=10.0, soc_max=18.08)
    assert base.set_read_only is False, "handback must release read_only"
    print("  test_read_only_released_on_handback: PASSED")


def test_read_only_held_through_low_soc():
    """RD27 (was RD4, retired 2026-08-06): low SOC must NOT release read_only.

    The old rule cleared read_only mid-window while leaving the three Predbat
    mappers DISABLED — un-muzzling Predbat with no write path. Nothing drove, and
    the plant fell through to the SIG's own self-consumption default. CM keeps both
    the wheel and read_only until the window ends."""
    base = MockBase()
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    plugin = CurtailmentPlugin(base)
    plugin._charge_below, plugin._drain_above = 2.0, 14.0
    plugin._publish_dispatch_policy(True, floor_kwh=8.0, soc_kwh=8.0, soc_max=18.08)
    assert base.set_read_only is True
    # 0.4 kWh of 18.08 = 2.2%, below the 2.8% drain floor — CM keeps driving.
    plugin._publish_dispatch_policy(True, floor_kwh=8.0, soc_kwh=0.4, soc_max=18.08)
    assert base.set_read_only is True, "read_only must be HELD through low SOC — releasing it without enabling the mappers hands to nobody"
    print("  test_read_only_held_through_low_soc: PASSED")


def test_read_only_untouched_observe_only():
    """Observe-only (gate off): CM never suppresses Predbat, even when active."""
    base = MockBase()  # gate defaults off
    plugin = CurtailmentPlugin(base)
    plugin._charge_below, plugin._drain_above = 2.0, 14.0
    plugin._publish_dispatch_policy(True, floor_kwh=8.0, soc_kwh=8.0, soc_max=18.08)
    assert base.set_read_only is False, "observe-only must not touch read_only"
    print("  test_read_only_untouched_observe_only: PASSED")


def test_manual_override_keeps_machine_live_skips_policy():
    """RD13 manual override: gate on + override on + active → keep the machine LIVE
    (heartbeat on, read_only True) but do NOT write the policy or keep floor. The user
    owns input_select.sig_dispatch_policy and it no longer gets overwritten each cycle."""
    base = MockBase()
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    base._sensor_overrides["input_select.sig_override"] = "Hold Battery"
    plugin = CurtailmentPlugin(base)
    plugin._charge_below, plugin._drain_above = 2.0, 14.0
    base.services.clear()
    plugin._publish_dispatch_policy(True, floor_kwh=8.0, soc_kwh=8.0, soc_max=18.08)
    assert not _policy_calls(base), f"manual override must NOT write policy, got {base.services}"
    assert not _keep_floor_calls(base), f"manual override must NOT write keep floor, got {base.services}"
    assert ("automation/turn_on", "automation.sig_dispatch_heartbeat") in _automation_calls(base), base.services
    assert base.set_read_only is True, "manual override still suppresses Predbat (single-writer stays live)"
    assert plugin._cm_controlling is True
    assert plugin._policy_driving is True
    print("  test_manual_override_keeps_machine_live_skips_policy: PASSED")


def test_manual_override_off_resumes_policy():
    """RD13: with the override off, automated policy control resumes normally."""
    base = MockBase()
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    base._sensor_overrides["input_select.sig_override"] = "Off"
    plugin = CurtailmentPlugin(base)
    plugin._charge_below, plugin._drain_above = 2.0, 14.0
    base.services.clear()
    plugin._publish_dispatch_policy(True, floor_kwh=8.0, soc_kwh=8.0, soc_max=18.08)
    assert _policy_calls(base) == ["Hold Battery"], base.services
    print("  test_manual_override_off_resumes_policy: PASSED")


def test_manual_override_grabs_control_even_when_inactive():
    """RD13 failsafe: override grabs the machine regardless of CM active state — it
    enables the heartbeat + read_only even when plugin_active is False, and never
    hands back to Predbat, so the user's manually-set policy keeps executing."""
    base = MockBase()
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    base._sensor_overrides["input_select.sig_override"] = "Hold Battery"
    plugin = CurtailmentPlugin(base)
    plugin._charge_below, plugin._drain_above = 2.0, 14.0
    base.services.clear()
    plugin._publish_dispatch_policy(False, floor_kwh=18.08, soc_kwh=10.0, soc_max=18.08)
    assert not _policy_calls(base), f"manual override must not hand back, got {base.services}"
    assert ("automation/turn_on", "automation.sig_dispatch_heartbeat") in _automation_calls(base), base.services
    assert ("automation/turn_off", "automation.sig_dispatch_heartbeat") not in _automation_calls(base), base.services
    assert base.set_read_only is True
    assert plugin._cm_controlling is True
    print("  test_manual_override_grabs_control_even_when_inactive: PASSED")


def test_manual_override_writer_follows_the_override_not_the_select():
    """RD13a SUPERSEDES the 2026-07-28 form of this test: the writer role follows
    the OVERRIDE, not the policy select.

    Originally this asserted that a `Predbat` in sig_dispatch_policy releases the
    writer even under manual override — protecting the 2026-07-28 case where
    sig_keep_floor_guard hit the reserve mid-drain, set policy -> Predbat, and the
    handover was left incomplete.

    Under RD13a the override select IS the policy (it has no Predbat option), so
    keying the writer role off sig_dispatch_policy reads a value the plugin itself
    may have written. Live failure 2026-07-29 08:56: override "Hold Battery",
    select "Predbat" left by the RD4 low-SOC handover -> heartbeat disabled ->
    nothing driving -> dispatch frozen at 2.89 kW while PV rose to 3.46, so the
    battery discharged 0.775 kW at 3% SOC.

    CONSEQUENCE, recorded deliberately: sig_keep_floor_guard can no longer hand
    back by writing sig_dispatch_policy while an override is held. The deep
    drain-floor clamp in the heartbeat (dispatch <= PV below sig_drain_floor_pct)
    still applies, but the higher keep-floor stop does not. The guard should write
    input_select.sig_override instead — tracked as follow-up.
    """
    base = MockBase()
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    base._sensor_overrides["input_select.sig_override"] = "Hold Battery"
    base._sensor_overrides["input_select.sig_dispatch_policy"] = "Predbat"
    plugin = CurtailmentPlugin(base)
    plugin._charge_below, plugin._drain_above = 2.0, 14.0
    plugin._cm_controlling = True  # CM held the wheel before the guard intervened
    plugin._read_only_set = True
    base.services.clear()
    plugin._publish_dispatch_policy(False, floor_kwh=18.08, soc_kwh=10.0, soc_max=18.08)

    calls = _automation_calls(base)
    assert ("automation/turn_off", "automation.sig_dispatch_heartbeat") not in calls, f"a held override must NOT release the executor, got {base.services}"
    assert ("automation/turn_on", "automation.predbat_requested_mode_action") not in calls, f"a held override must NOT hand the registers to Predbat, got {base.services}"
    assert plugin._cm_controlling is True, "CM must stay the writer while an override is held"
    assert plugin._read_only_set is True, "Predbat must stay suppressed while CM's executor drives"
    # Still must not write the policy select — that is the user's under override.
    assert not _policy_calls(base), f"manual override must never write the policy, got {base.services}"
    print("  test_manual_override_writer_follows_the_override_not_the_select: PASSED")


def test_proposed_phase_charge_below_floor():
    """SOC < charge_below → Charge (P10 recovery at risk)."""
    from curtailment_calc import compute_proposed_phase

    phase = compute_proposed_phase(soc_kwh=2.0, charge_below_kwh=4.0, drain_above_kwh=14.0)
    assert phase == "Charge", f"Expected Charge, got {phase}"
    print(f"  test_proposed_phase_charge_below_floor: PASSED ({phase})")


def test_proposed_phase_drain_above_ceiling():
    """SOC > drain_above → Drain (curtailment headroom exhausted)."""
    from curtailment_calc import compute_proposed_phase

    phase = compute_proposed_phase(soc_kwh=15.0, charge_below_kwh=2.0, drain_above_kwh=14.0)
    assert phase == "Drain", f"Expected Drain, got {phase}"
    print(f"  test_proposed_phase_drain_above_ceiling: PASSED ({phase})")


def test_proposed_phase_today_7am_actual():
    """Today 7 AM actual values: SOC=2.19, charge_below=2.09, drain_above=13.9 → Hold.

    User's observed state at 2026-05-07 07:00. Old single-target logic forced
    Charge (export=0). New split-threshold logic should report Hold so PV flows
    naturally to grid + battery.
    """
    from curtailment_calc import compute_proposed_phase

    phase = compute_proposed_phase(soc_kwh=2.19, charge_below_kwh=2.09, drain_above_kwh=13.9)
    assert phase == "Hold", f"Expected Hold for today's 7AM state, got {phase}"
    print(f"  test_proposed_phase_today_7am_actual: PASSED ({phase})")


def test_proposed_phase_off_when_plugin_inactive():
    """Plugin Off → phase Off regardless of SOC."""
    from curtailment_calc import compute_proposed_phase

    phase = compute_proposed_phase(soc_kwh=2.0, charge_below_kwh=4.0, drain_above_kwh=14.0, plugin_active=False)
    assert phase == "Off", f"Expected Off, got {phase}"
    print(f"  test_proposed_phase_off_when_plugin_inactive: PASSED ({phase})")


def test_proposed_phase_cross_over_charges_to_lower_threshold():
    """Cross-over day (cb > da): charge target is drain_above (the lower of the two).
    Drain still fires when SOC exceeds drain_above — curtailment defence wins.
    """
    from curtailment_calc import compute_proposed_phase

    # SOC below both thresholds → Charge
    assert compute_proposed_phase(soc_kwh=5.0, charge_below_kwh=10.0, drain_above_kwh=7.0) == "Charge"
    # SOC just above drain_above → Drain (back to threshold)
    assert compute_proposed_phase(soc_kwh=7.5, charge_below_kwh=10.0, drain_above_kwh=7.0) == "Drain"
    # SOC well above → Drain
    assert compute_proposed_phase(soc_kwh=15.0, charge_below_kwh=10.0, drain_above_kwh=7.0) == "Drain"
    # SOC exactly at drain_above (boundary) → Hold
    assert compute_proposed_phase(soc_kwh=7.0, charge_below_kwh=10.0, drain_above_kwh=7.0) == "Hold"
    print("  test_proposed_phase_cross_over_charges_to_lower_threshold: PASSED")


def test_proposed_phase_normal_day_unchanged():
    """Normal day (da > cb): charge target is charge_below (unchanged behaviour)."""
    from curtailment_calc import compute_proposed_phase

    # SOC below charge_below → Charge
    assert compute_proposed_phase(soc_kwh=1.0, charge_below_kwh=2.0, drain_above_kwh=14.0) == "Charge"
    # In between → Hold (wide band)
    assert compute_proposed_phase(soc_kwh=8.0, charge_below_kwh=2.0, drain_above_kwh=14.0) == "Hold"
    # Above drain_above → Drain (not suppressed on normal day)
    assert compute_proposed_phase(soc_kwh=15.0, charge_below_kwh=2.0, drain_above_kwh=14.0) == "Drain"
    print("  test_proposed_phase_normal_day_unchanged: PASSED")


def test_proposed_phase_thresholds_collapse_at_sunset():
    """Sunset: charge_below == drain_above (overflow=0, p10_recovery=overnight).
    SOC at threshold → Hold (boundary, not strictly < or >). Slight excursion
    triggers Charge or Drain accordingly.
    """
    from curtailment_calc import compute_proposed_phase

    # Exact boundary
    phase_eq = compute_proposed_phase(soc_kwh=10.0, charge_below_kwh=10.0, drain_above_kwh=10.0)
    assert phase_eq == "Hold", f"Boundary should Hold, got {phase_eq}"

    phase_below = compute_proposed_phase(soc_kwh=9.5, charge_below_kwh=10.0, drain_above_kwh=10.0)
    assert phase_below == "Charge", f"Below should Charge, got {phase_below}"

    phase_above = compute_proposed_phase(soc_kwh=10.5, charge_below_kwh=10.0, drain_above_kwh=10.0)
    assert phase_above == "Drain", f"Above should Drain, got {phase_above}"

    print("  test_proposed_phase_thresholds_collapse_at_sunset: PASSED")


# ============================================================================
# v10 floor tests
# ============================================================================


def test_floor_computation():
    """Floor = (soc_max * 0.9) - overflow * V10_SIM_SAFETY_FACTOR (R9 + R45)."""
    overflow = 10.0
    floor = BATTERY_KWH * 0.9 - overflow * V10_SIM_SAFETY_FACTOR
    expected = 18.08 * 0.9 - 10.0 * V10_SIM_SAFETY_FACTOR
    assert abs(floor - expected) < 0.01, f"Expected {expected}, got {floor}"
    print(f"  test_floor_computation: PASSED (floor={floor:.2f}kWh = {floor/BATTERY_KWH*100:.0f}%)")


def test_floor_above_soc_keep():
    """Floor never goes below soc_keep."""
    overflow = 20.0  # huge overflow → floor = 18.08 - 25 = -6.92
    soc_keep = 4.0
    floor = BATTERY_KWH - overflow * V10_SIM_SAFETY_FACTOR
    floor = max(floor, soc_keep)
    assert floor >= soc_keep, f"Floor should be >= soc_keep ({soc_keep}), got {floor}"
    print(f"  test_floor_above_soc_keep: PASSED (floor={floor:.2f}kWh)")


# ============================================================================
# v19 tapered-cap tests (R9/R45 rewritten)
# ============================================================================


def _taper_cap_formula(remaining_overflow):
    """Reference taper formula — buffer scales with remaining, capped at 10%."""
    buffer = min(MAX_RESERVED_KWH, max(0.0, remaining_overflow))
    return BATTERY_KWH - buffer, buffer


def test_cap_taper_at_peak_overflow():
    """Peak overflow: buffer clamps at 1.8 kWh, max_target stays at 90% (unchanged)."""
    max_target, buffer = _taper_cap_formula(remaining_overflow=10.0)
    assert abs(buffer - MAX_RESERVED_KWH) < 0.01, f"Peak-overflow buffer should clamp at {MAX_RESERVED_KWH}, got {buffer:.3f}"
    assert abs(max_target - BATTERY_KWH * 0.9) < 0.01, f"Peak-overflow max_target should be 90% soc_max, got {max_target:.3f}"
    print(f"  test_cap_taper_at_peak_overflow: PASSED (remaining=10 → max_target={max_target/BATTERY_KWH*100:.0f}%)")


def test_cap_taper_near_safe_time():
    """Tail of overflow: buffer = remaining (not clamped), max_target rises above 90%."""
    max_target, buffer = _taper_cap_formula(remaining_overflow=0.5)
    assert abs(buffer - 0.5) < 0.01, f"Tail buffer should equal remaining_overflow, got {buffer:.3f}"
    expected_target = BATTERY_KWH - 0.5
    assert abs(max_target - expected_target) < 0.01, f"Tail max_target should be {expected_target:.2f}, got {max_target:.3f}"
    assert max_target > BATTERY_KWH * 0.9, f"Tail max_target should be above 90% cap, got {max_target/BATTERY_KWH*100:.1f}%"
    print(f"  test_cap_taper_near_safe_time: PASSED (remaining=0.5 → max_target={max_target/BATTERY_KWH*100:.1f}%)")


def test_cap_at_safe_time_hits_100():
    """At safe_time: remaining=0 → buffer=0 → max_target=soc_max (100%)."""
    max_target, buffer = _taper_cap_formula(remaining_overflow=0.0)
    assert abs(buffer) < 0.01, f"Zero-remaining buffer should be 0, got {buffer:.3f}"
    assert abs(max_target - BATTERY_KWH) < 0.01, f"Zero-remaining max_target should be soc_max, got {max_target:.3f}"
    print(f"  test_cap_at_safe_time_hits_100: PASSED (remaining=0 → max_target=100%)")


def test_plugin_cap_taper_near_safe_time():
    """v31: R57's effective_keep cap is REMOVED (evening drain-to-reserve is
    Predbat's job now). So near safe_time with a nearly-full battery and tiny
    remaining overflow, the floor tapers UP toward soc_max (R45) — it is no
    longer pulled down to effective_keep. Battery fills to ~100%.
    """
    from datetime import datetime, timezone

    minutes_now = 1020  # 17:00 local
    pv = {m: 4.5 for m in range(0, 120, PLUGIN_STEP)}
    load = {m: 1.0 for m in range(0, 120, PLUGIN_STEP)}
    sensor_overrides = {
        "sensor.sigen_plant_pv_power": 4.5,
        "sensor.sigen_plant_consumed_power": 1.0,
    }
    sensor_overrides.update(_make_p90_sensors(p90_peak_kw=9.0, solcast_remaining=3.0))
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=BATTERY_KWH * 0.95,
        minutes_now=minutes_now,
        best_soc_keep=4.0,
        now_utc=datetime(2025, 7, 12, 16, 0, tzinfo=timezone.utc),
        sensor_overrides=sensor_overrides,
    )
    plugin = CurtailmentPlugin(base)
    floor, phase = plugin.calculate(dno_limit_kw=4.0)
    remaining = plugin._remaining_overflow
    assert remaining < MAX_RESERVED_KWH, f"Tiny remaining expected, got {remaining:.2f}"
    # v31: floor tapers UP toward soc_max (R45), no longer capped at effective_keep.
    assert floor > 4.0, f"v31: floor must taper up (not capped at effective_keep 4.0), got {floor:.2f}"
    print(f"  test_plugin_cap_taper_near_safe_time: PASSED (R45 taper — floor={floor:.2f})")


def test_cap_taper_ratchet_noise_immune():
    """Ratchet: if remaining oscillates up-down-up, floor never drops below its peak."""
    # Simulate three cycles with oscillating remaining overflow
    floors_seen = []
    ratchet = None
    for remaining in [1.0, 1.5, 1.0]:
        max_target, _ = _taper_cap_formula(remaining)
        overflow_floor = max(0.0, max_target - remaining * V10_SIM_SAFETY_FACTOR)
        if ratchet is not None:
            overflow_floor = max(overflow_floor, ratchet)
        ratchet = overflow_floor
        floors_seen.append(overflow_floor)

    # Each step must be >= previous (ratchet only rises)
    for i in range(1, len(floors_seen)):
        assert floors_seen[i] >= floors_seen[i - 1] - 1e-6, f"Ratchet violated: cycle {i} floor {floors_seen[i]:.3f} < cycle {i-1} floor {floors_seen[i-1]:.3f}"
    print(f"  test_cap_taper_ratchet_noise_immune: PASSED (floors={[f'{f:.2f}' for f in floors_seen]})")


def _build_cloudy_afternoon_base(cumulative_actual, cumulative_solcast, solcast_remaining):
    """Build a MockBase at 15:00 BST with given cumulative PV state.

    Sets SOLCAST_TODAY = cumulative_solcast + solcast_remaining (so so-far = cumulative_solcast)
    and SIG_DAILY_PV = cumulative_actual. Plugin reads these to compute the
    cumulative_ratio used by the dynamic buffer-reduction logic.
    """
    from datetime import datetime, timezone

    minutes_now = 900  # 15:00 BST
    pv = {m: 4.5 for m in range(0, 540, PLUGIN_STEP)}
    load = {m: 1.0 for m in range(0, 540, PLUGIN_STEP)}
    sensor_overrides = {
        "sensor.sigen_plant_pv_power": 4.5,
        "sensor.sigen_plant_consumed_power": 1.0,
        SIG_DAILY_PV: cumulative_actual,
    }
    # Inject Solcast: today=total, remaining→solcast_so_far derived
    sensor_overrides.update(_make_p90_sensors(p90_peak_kw=10.0, solcast_remaining=solcast_remaining))
    sensor_overrides[SOLCAST_TODAY] = {
        "state": cumulative_solcast + solcast_remaining,
        "detailedForecast": [{"period_start": "2025-07-12T11:00:00+00:00", "pv_estimate90": 10.0}],
    }
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=BATTERY_KWH * 0.55,
        minutes_now=minutes_now,
        now_utc=datetime(2025, 7, 12, 14, 0, tzinfo=timezone.utc),  # 15:00 BST
        sensor_overrides=sensor_overrides,
    )
    return base


def test_buffer_reduces_on_cloudy_afternoon():
    """v20 R49: confirmed-cloudy afternoon → effective_max_reserved drops to 0.7×.

    Tests the diagnostic (plugin._effective_max_reserved) directly because
    under v20 R54 the floor is min(overflow_floor, effective_keep) so the
    buffer reduction's effect on the live target is masked by effective_keep.
    R49 still runs internally and adjusts overflow_floor — we just verify
    the diagnostic.
    """
    base = _build_cloudy_afternoon_base(cumulative_actual=24.0, cumulative_solcast=30.0, solcast_remaining=15.0)
    plugin = CurtailmentPlugin(base)
    plugin._pv_history.append((840, 24.0, 19.0))  # recent_ratio ≈ 0.83 (<0.95)

    floor, phase = plugin.calculate(dno_limit_kw=4.0)
    assert phase == "active", f"Plugin must reach floor logic; got phase={phase}"
    assert plugin._buffer_reduced, "R49 should have fired on cloudy state"
    expected_max_reserved = max(0.5, MAX_RESERVED_KWH * 0.7)
    assert abs(plugin._effective_max_reserved - expected_max_reserved) < 0.01, f"Expected effective_max_reserved={expected_max_reserved:.2f}, got {plugin._effective_max_reserved:.2f}"
    print(f"  test_buffer_reduces_on_cloudy_afternoon: PASSED (effective_max_reserved={plugin._effective_max_reserved:.2f}, floor={floor:.2f})")


def test_buffer_unchanged_on_clear_afternoon():
    """v20 R49: clear afternoon (cumulative ratio ≥0.9) → no buffer reduction."""
    base = _build_cloudy_afternoon_base(cumulative_actual=28.0, cumulative_solcast=30.0, solcast_remaining=15.0)
    plugin = CurtailmentPlugin(base)
    plugin._pv_history.append((840, 24.0, 22.5))  # recent_ratio = 5.5/6 = 0.92

    floor, phase = plugin.calculate(dno_limit_kw=4.0)
    assert phase == "active", f"Plugin must reach floor logic; got phase={phase}"
    assert not plugin._buffer_reduced, "R49 should NOT fire on clear state"
    assert abs(plugin._effective_max_reserved - MAX_RESERVED_KWH) < 0.01, f"Expected effective_max_reserved={MAX_RESERVED_KWH:.2f}, got {plugin._effective_max_reserved:.2f}"
    print(f"  test_buffer_unchanged_on_clear_afternoon: PASSED (effective_max_reserved={plugin._effective_max_reserved:.2f})")


def test_buffer_unchanged_before_14_00():
    """v20: before 14:00 local, cumulative-ratio guard does NOT apply — full buffer reserved."""
    from datetime import datetime, timezone

    # 12:00 BST, cloudy state — would qualify after 14:00 but morning gate blocks it
    minutes_now = 720  # 12:00 BST
    pv = {m: 4.5 for m in range(0, 720, PLUGIN_STEP)}
    load = {m: 1.0 for m in range(0, 720, PLUGIN_STEP)}
    sensor_overrides = {
        "sensor.sigen_plant_pv_power": 4.5,
        "sensor.sigen_plant_consumed_power": 1.0,
        SIG_DAILY_PV: 8.0,
    }
    sensor_overrides.update(_make_p90_sensors(p90_peak_kw=10.0, solcast_remaining=30.0))
    sensor_overrides[SOLCAST_TODAY] = {
        "state": 40.0,
        "detailedForecast": [{"period_start": "2025-07-12T11:00:00+00:00", "pv_estimate90": 10.0}],
    }
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=BATTERY_KWH * 0.55,
        minutes_now=minutes_now,
        now_utc=datetime(2025, 7, 12, 11, 0, tzinfo=timezone.utc),
        sensor_overrides=sensor_overrides,
    )
    plugin = CurtailmentPlugin(base)
    plugin._pv_history.append((660, 6.0, 4.5))  # cumulative 8/10=0.8 (would qualify after 14:00)

    floor, phase = plugin.calculate(dno_limit_kw=4.0)
    assert phase == "active", f"Plugin must reach floor logic; got phase={phase}"
    remaining = plugin._remaining_overflow
    if remaining > 1.3:
        # Buffer should be the full MAX_RESERVED_KWH — no morning reduction
        expected_max_target = BATTERY_KWH - MAX_RESERVED_KWH
        expected_floor = max(0.0, expected_max_target - remaining * V10_SIM_SAFETY_FACTOR)
        assert abs(floor - expected_floor) < 0.05, f"Pre-14:00 floor should match un-reduced taper {expected_floor:.2f}, got {floor:.2f}"
    print(f"  test_buffer_unchanged_before_14_00: PASSED (remaining={remaining:.2f}, floor={floor:.2f})")


# ============================================================================
# R50 confidence-weighted overflow tests
# ============================================================================


def test_R50_p_scales_from_forecast():
    """p_scales_from_forecast returns three scales derived from p10/p50/p90 peaks."""
    # July noon at 55.86°N — sin(elev) ≈ 0.83
    forecast = [
        {"period_start": "2025-07-12T11:00:00+01:00", "pv_estimate10": 4.0, "pv_estimate": 8.0, "pv_estimate90": 10.0},
        {"period_start": "2025-07-12T11:30:00+01:00", "pv_estimate10": 5.0, "pv_estimate": 9.0, "pv_estimate90": 11.0},
        {"period_start": "2025-07-12T12:00:00+01:00", "pv_estimate10": 4.5, "pv_estimate": 8.5, "pv_estimate90": 10.5},
    ]
    p10, p50, p90 = p_scales_from_forecast(forecast, lat_deg=55.86, lon_deg=-3.2, day_of_year=193, local_offset_hours=1.0)

    assert p90 > p50 > p10, f"Expected p90 > p50 > p10, got p10={p10}, p50={p50}, p90={p90}"
    # All scales use the same peak slot (11:30 here, highest in each band).
    # sin(elev) at peak ~ 0.83 for July noon at 55.86°N.
    # p10 scale ≈ 5/0.83 ≈ 6.0; p50 ≈ 9/0.83 ≈ 10.8; p90 ≈ 11/0.83 ≈ 13.3
    assert 5.5 < p10 < 6.5, f"p10_scale out of range: {p10}"
    assert 10.0 < p50 < 11.5, f"p50_scale out of range: {p50}"
    assert 12.5 < p90 < 14.0, f"p90_scale out of range: {p90}"
    print(f"  test_R50_p_scales_from_forecast: PASSED (p10={p10:.2f}, p50={p50:.2f}, p90={p90:.2f})")


def test_R50_compute_expected_overflow_high_confidence():
    """High confidence (≥ HIGH threshold) → use overflow_p90 (current behaviour)."""
    expected = compute_expected_overflow(p10=0.0, p50=5.0, p90=14.0, confidence=0.9, low=0.6, high=0.85)
    assert abs(expected - 14.0) < 0.01, f"At c=0.9, expected p90 (14.0), got {expected}"
    print(f"  test_R50_compute_expected_overflow_high_confidence: PASSED (expected=14.0)")


def test_R50_compute_expected_overflow_mid_confidence():
    """Mid confidence (LOW..HIGH) → linear blend p50→p90."""
    # c=0.7, low=0.6, high=0.85 → t = (0.7-0.6)/0.25 = 0.4
    # expected = 0.6*p50 + 0.4*p90 = 0.6*5 + 0.4*14 = 3.0 + 5.6 = 8.6
    expected = compute_expected_overflow(p10=0.0, p50=5.0, p90=14.0, confidence=0.7, low=0.6, high=0.85)
    assert abs(expected - 8.6) < 0.01, f"At c=0.7, expected blend 8.6, got {expected}"
    print(f"  test_R50_compute_expected_overflow_mid_confidence: PASSED (expected=8.6)")


def test_R50_compute_expected_overflow_low_confidence():
    """Low confidence (< LOW threshold) → linear blend p10→p50."""
    # c=0.3, low=0.6 → t = 0.3/0.6 = 0.5
    # expected = 0.5*p10 + 0.5*p50 = 0.5*0 + 0.5*5 = 2.5
    expected = compute_expected_overflow(p10=0.0, p50=5.0, p90=14.0, confidence=0.3, low=0.6, high=0.85)
    assert abs(expected - 2.5) < 0.01, f"At c=0.3, expected blend 2.5, got {expected}"
    print(f"  test_R50_compute_expected_overflow_low_confidence: PASSED (expected=2.5)")


def test_R50_compute_expected_overflow_zero_confidence():
    """Zero confidence → pure p10 (most pessimistic)."""
    expected = compute_expected_overflow(p10=0.0, p50=5.0, p90=14.0, confidence=0.0, low=0.6, high=0.85)
    assert abs(expected - 0.0) < 0.01, f"At c=0.0, expected p10 (0.0), got {expected}"
    print(f"  test_R50_compute_expected_overflow_zero_confidence: PASSED (expected=0.0)")


def test_R50_compute_expected_overflow_apr_28_incident():
    """Apr 28 2026 incident: c=0.69, p50_overflow≈5, p90_overflow≈14 → expected ≈8.2 (drain to ~36%, not 8%)."""
    expected = compute_expected_overflow(p10=0.0, p50=5.0, p90=14.0, confidence=0.69, low=0.6, high=0.85)
    # c=0.69, low=0.6, high=0.85 → t = (0.69-0.6)/0.25 = 0.36
    # expected = 0.64*5 + 0.36*14 = 3.2 + 5.04 = 8.24
    assert 7.5 < expected < 9.0, f"Apr 28 case: expected ~8.2, got {expected}"
    # Verify floor calculation would produce reasonable target
    floor_kwh = BATTERY_KWH - MAX_RESERVED_KWH - expected * V10_SIM_SAFETY_FACTOR
    floor_pct = floor_kwh / BATTERY_KWH * 100
    assert floor_pct > 30, f"Apr 28 case: floor should be >30%, got {floor_pct:.1f}%"
    assert floor_pct < 50, f"Apr 28 case: floor should be <50% (still meaningful drain), got {floor_pct:.1f}%"
    print(f"  test_R50_compute_expected_overflow_apr_28_incident: PASSED (expected={expected:.2f}, floor={floor_pct:.1f}%)")


def test_R50_compute_expected_overflow_clamps_confidence():
    """Confidence outside [0, 1] is clamped — defensive against bad sensor values."""
    # confidence > 1 → treated as 1 (or HIGH) → use p90
    e_high = compute_expected_overflow(p10=0.0, p50=5.0, p90=14.0, confidence=1.5, low=0.6, high=0.85)
    assert abs(e_high - 14.0) < 0.01, f"Clamp >1: expected p90, got {e_high}"
    # confidence < 0 → treated as 0 → use p10
    e_low = compute_expected_overflow(p10=0.0, p50=5.0, p90=14.0, confidence=-0.5, low=0.6, high=0.85)
    assert abs(e_low - 0.0) < 0.01, f"Clamp <0: expected p10, got {e_low}"
    print(f"  test_R50_compute_expected_overflow_clamps_confidence: PASSED")


def test_R50_compute_expected_overflow_at_boundaries():
    """At c=LOW exactly → expected = p50; at c=HIGH exactly → expected = p90."""
    e_at_low = compute_expected_overflow(p10=0.0, p50=5.0, p90=14.0, confidence=0.6, low=0.6, high=0.85)
    assert abs(e_at_low - 5.0) < 0.01, f"At c=LOW exactly: expected p50, got {e_at_low}"
    e_at_high = compute_expected_overflow(p10=0.0, p50=5.0, p90=14.0, confidence=0.85, low=0.6, high=0.85)
    assert abs(e_at_high - 14.0) < 0.01, f"At c=HIGH exactly: expected p90, got {e_at_high}"
    print(f"  test_R50_compute_expected_overflow_at_boundaries: PASSED")


# ============================================================================
# R52 pre-PV drain tests
# ============================================================================


def test_R52_compute_pv_start_time_summer():
    """compute_pv_start_time finds sunrise crossing on a summer day."""
    # July 12, 55.86°N: sunrise ~04:20 UTC (BST 05:20). With scale=12 and threshold=0.5,
    # sin(elev) > 0.0417 needed → elev > 2.4°. Crosses ~10-15 min after geometric sunrise.
    # current=00:00 UTC → minutes_until > 0.
    minutes, crossing_utc = compute_pv_start_time(scale=12.0, lat_deg=55.86, lon_deg=-3.2, day_of_year=193, threshold_kw=0.5, current_utc_hours=0.0)
    assert crossing_utc is not None, "Should find PV start on a summer day with high scale"
    # Expect crossing roughly 03:30-04:45 UTC (BST 04:30-05:45)
    assert 3.0 < crossing_utc < 5.0, f"Crossing should be early morning UTC, got {crossing_utc:.2f}"
    assert minutes > 0, f"Minutes_until should be positive when called pre-dawn, got {minutes}"
    print(f"  test_R52_compute_pv_start_time_summer: PASSED (crossing at {crossing_utc:.2f} UTC, {minutes:.0f} min from 00:00)")


def test_R52_compute_pv_start_time_winter_low_scale():
    """compute_pv_start_time returns None if peak can't reach threshold (deep winter low scale)."""
    # Dec 21 at 55.86°N: solar noon elev ~10.6°, sin ≈ 0.184. With scale=2, peak PV = 0.37 kW.
    # threshold 0.5 kW — won't cross.
    minutes, crossing_utc = compute_pv_start_time(scale=2.0, lat_deg=55.86, lon_deg=-3.2, day_of_year=355, threshold_kw=0.5, current_utc_hours=0.0)
    assert crossing_utc is None, f"Should return None when peak won't reach threshold; got {crossing_utc}"
    assert minutes is None
    print(f"  test_R52_compute_pv_start_time_winter_low_scale: PASSED (no crossing, peak too weak)")


def test_R52_compute_pv_start_time_called_post_crossing():
    """If called after the morning crossing, returns negative minutes_until."""
    # July noon: sun is above threshold by midday. Calling at 12:00 UTC = post-crossing.
    minutes, crossing_utc = compute_pv_start_time(scale=12.0, lat_deg=55.86, lon_deg=-3.2, day_of_year=193, threshold_kw=0.5, current_utc_hours=12.0)
    assert crossing_utc is not None
    assert minutes < 0, f"Crossing was earlier today; minutes_until should be negative. Got {minutes}"
    print(f"  test_R52_compute_pv_start_time_called_post_crossing: PASSED (negative minutes={minutes:.0f})")


def test_R52_compute_pv_start_time_threshold_at_dno():
    """High threshold (e.g., DNO+load) gives later crossing than low threshold."""
    # Same day, two thresholds
    _, low_crossing = compute_pv_start_time(scale=12.0, lat_deg=55.86, lon_deg=-3.2, day_of_year=193, threshold_kw=0.5, current_utc_hours=0.0)
    _, high_crossing = compute_pv_start_time(scale=12.0, lat_deg=55.86, lon_deg=-3.2, day_of_year=193, threshold_kw=4.5, current_utc_hours=0.0)
    assert low_crossing < high_crossing, f"Higher threshold should cross later: low={low_crossing:.2f}, high={high_crossing:.2f}"
    print(f"  test_R52_compute_pv_start_time_threshold_at_dno: PASSED (low={low_crossing:.2f}, high={high_crossing:.2f})")


def _make_pre_pv_base(soc_pct=0.7, gshp_ch="off", buffer_pct=20, hour=2, p90_peak=8.58):
    """Build MockBase for pre-PV drain tests at given local hour (UTC=local for tz=0)."""
    from datetime import datetime, timezone

    minutes_now = int(hour * 60)
    pv = {m: 0 for m in range(0, 1440 - minutes_now, PLUGIN_STEP)}
    load = {m: 0.5 * (PLUGIN_STEP / 60) for m in range(0, 1440 - minutes_now, PLUGIN_STEP)}
    sensor_overrides = {
        "sensor.sigen_plant_pv_power": 0.0,
        "sensor.sigen_plant_consumed_power": 0.5,
        "input_boolean.gshp_ch_active": gshp_ch,
        "input_number.curtailment_pre_pv_buffer_pct": buffer_pct,
    }
    sensor_overrides.update(_make_p90_sensors(p90_peak_kw=p90_peak, solcast_remaining=40.0))
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=BATTERY_KWH * soc_pct,
        minutes_now=minutes_now,
        now_utc=datetime(2025, 7, 12, hour, 0, tzinfo=timezone.utc),
        best_soc_keep=1.5,
        sensor_overrides=sensor_overrides,
    )
    return base


def test_solcast_stale_date_rejected():
    """Unknown-unknowns item 4: a stale detailedForecast (slots dated yesterday)
    must be rejected, not consumed as today's. compute_solcast_overflow parses
    only HH:MM from period_start so date-blind consumption is silent."""
    pv, load = _make_overflow_pv(minutes_now=720)
    sensor_overrides = {
        "sensor.sigen_plant_pv_power": 8.0,
        "sensor.sigen_plant_consumed_power": 1.0,
        # Slot dated the day BEFORE MockBase's now_utc (2025-07-12)
        "sensor.solcast_pv_forecast_forecast_today": {"detailedForecast": [{"period_start": "2025-07-11T12:00:00+00:00", "pv_estimate90": 8.58}]},
        "sensor.solcast_pv_forecast_forecast_remaining_today": 25.0,
    }
    base = MockBase(pv_step=pv, load_step=load, soc_kw=BATTERY_KWH * 0.40, minutes_now=720, sensor_overrides=sensor_overrides)
    plugin = CurtailmentPlugin(base)
    assert plugin._get_solcast_detailed() == [], "Stale-dated forecast must be rejected"
    # With no cached scale and no usable forecast, plugin must go safe (off)
    floor, phase = plugin.calculate(dno_limit_kw=4.0)
    assert phase == "off", f"No trustworthy forecast should mean off, got {phase}"
    assert plugin._floor_source == "No Forecast", f"Expected No Forecast, got {plugin._floor_source}"
    print("  test_solcast_stale_date_rejected: PASSED")


def test_solcast_datacorrect_false_rejected():
    """Unknown-unknowns item 4: Solcast's own dataCorrect=False flag must gate
    the forecast (it was previously never read)."""
    pv, load = _make_overflow_pv(minutes_now=720)
    sensor_overrides = {
        "sensor.sigen_plant_pv_power": 8.0,
        "sensor.sigen_plant_consumed_power": 1.0,
        "sensor.solcast_pv_forecast_forecast_today": {"dataCorrect": False, "detailedForecast": [{"period_start": "2025-07-12T12:00:00+00:00", "pv_estimate90": 8.58}]},
        "sensor.solcast_pv_forecast_forecast_remaining_today": 25.0,
    }
    base = MockBase(pv_step=pv, load_step=load, soc_kw=BATTERY_KWH * 0.40, minutes_now=720, sensor_overrides=sensor_overrides)
    plugin = CurtailmentPlugin(base)
    assert plugin._get_solcast_detailed() == [], "dataCorrect=False forecast must be rejected"
    print("  test_solcast_datacorrect_false_rejected: PASSED")


def test_solcast_current_date_accepted():
    """Gate sanity: a correctly-dated forecast with dataCorrect=True passes."""
    sensor_overrides = {
        "sensor.solcast_pv_forecast_forecast_today": {"dataCorrect": True, "detailedForecast": [{"period_start": "2025-07-12T12:00:00+00:00", "pv_estimate90": 8.58}]},
    }
    base = MockBase(pv_step={}, load_step={}, soc_kw=5.0, minutes_now=720, sensor_overrides=sensor_overrides)
    plugin = CurtailmentPlugin(base)
    detailed = plugin._get_solcast_detailed()
    assert len(detailed) == 1, f"Valid forecast must pass the gates, got {detailed}"
    print("  test_solcast_current_date_accepted: PASSED")


def test_R52_pre_pv_drain_blocked_by_ch_active():
    """When GSHP CH is on, no pre-PV drain — protect overnight battery."""
    base = _make_pre_pv_base(soc_pct=0.7, gshp_ch="on", hour=2)  # 02:00 local, CH on
    plugin = CurtailmentPlugin(base)
    floor, phase = plugin.calculate(dno_limit_kw=4.0)
    assert phase == "off", f"CH-active should block pre-PV drain, got phase={phase}"
    assert floor == base.soc_max, f"Floor should be soc_max when off, got {floor}"
    print("  test_R52_pre_pv_drain_blocked_by_ch_active: PASSED")


def test_R52_pre_pv_drain_too_early():
    """Before drain_start_time, plugin stays off even with CH off + high SOC."""
    # 01:00 local, summer day, sunrise ~04:30 local (= same UTC since tz=0).
    # Even at 70% SOC drain_amount=12.78-(1.5+3.6)=7.7 kWh → drain_minutes=115.
    # drain_start = 04:30 - 1:55 = 02:35. At 01:00, too early.
    base = _make_pre_pv_base(soc_pct=0.7, gshp_ch="off", hour=1)
    plugin = CurtailmentPlugin(base)
    floor, phase = plugin.calculate(dno_limit_kw=4.0)
    assert phase == "off", f"Pre-drain-start should keep plugin off, got phase={phase}"
    print("  test_R52_pre_pv_drain_too_early: PASSED")


def test_R52_pre_pv_drain_active_at_drain_start():
    """At drain_start time with CH off + high SOC + big overflow forecast: Active.

    R62 (2026-07-07): on a big-overflow forecast the pre-PV target is
    forecast-driven (overflow_floor collapses toward the deep floor + dawn
    load), NOT the static soc_keep + buffer_pct. The legacy value acts only
    as a ceiling. This fixture's p90 forecast produces a large overflow, so
    the target must land well BELOW the legacy 5.12 kWh and at/above the
    deep-discharge floor.
    """
    base = _make_pre_pv_base(soc_pct=0.7, gshp_ch="off", hour=4)  # 04:00 local
    plugin = CurtailmentPlugin(base)
    floor, phase = plugin.calculate(dno_limit_kw=4.0)
    assert phase == "active", f"Pre-PV drain window should be Active, got phase={phase}"
    legacy_target = 1.5 + 0.20 * BATTERY_KWH  # 5.12
    assert floor < legacy_target - 1.0, f"R62: big-overflow pre-PV target should be well below legacy {legacy_target:.2f}, got {floor:.2f}"
    assert floor >= 0.5, f"Pre-PV target must respect deep-discharge floor, got {floor:.2f}"
    print(f"  test_R52_pre_pv_drain_active_at_drain_start: PASSED (target={floor:.2f}kWh < legacy {legacy_target:.2f})")


def test_R62_pre_pv_target_huge_confident_day():
    """R62 pure: huge confident overflow → target collapses to deep floor + dawn load."""
    from curtailment_calc import compute_pre_pv_target

    # Tomorrow-2026-07-08 shape: soc_keep 0, buffer 20%, overflow 18 kWh
    target = compute_pre_pv_target(
        soc_keep=0.0,
        soc_max=18.08,
        buffer_pct=20.0,
        reserve=0.0,
        expected_overflow_kwh=18.0,
        dawn_load_kwh=1.0,
        max_reserved_kwh=1.8,
        safety_factor=1.2,
    )
    # overflow_floor = (18.08-1.8) - 18*1.2 = negative → 0; floor_driven = 0.5+1.0 = 1.5
    assert abs(target - 1.5) < 0.01, f"Expected 1.5 (deep floor + dawn load), got {target}"
    print(f"  test_R62_pre_pv_target_huge_confident_day: PASSED ({target})")


def test_R62_pre_pv_target_moderate_day_legacy_ceiling():
    """R62 pure: moderate overflow → overflow_floor high → legacy ceiling binds (no behaviour change)."""
    from curtailment_calc import compute_pre_pv_target

    target = compute_pre_pv_target(
        soc_keep=1.5,
        soc_max=18.08,
        buffer_pct=20.0,
        reserve=0.0,
        expected_overflow_kwh=5.0,
        dawn_load_kwh=1.0,
        max_reserved_kwh=1.8,
        safety_factor=1.2,
    )
    # overflow_floor = (18.08-1.8) - 6.0 = 10.28 → floor_driven 10.28 → min(5.12, 10.28) = 5.12
    legacy = 1.5 + 0.20 * 18.08
    assert abs(target - legacy) < 0.01, f"Moderate day should keep legacy target {legacy:.2f}, got {target}"
    print(f"  test_R62_pre_pv_target_moderate_day_legacy_ceiling: PASSED ({target:.2f})")


def test_R62_pre_pv_target_low_confidence_stays_legacy():
    """R62 pure: low confidence blends overflow down → target stays at legacy (no over-drain on uncertain days)."""
    from curtailment_calc import compute_pre_pv_target, compute_expected_overflow

    # Confidence 0.4 with big p90 but zero p10 → blended overflow small
    blended = compute_expected_overflow(p10=0.0, p50=4.0, p90=16.0, confidence=0.4, low=0.6, high=0.85)
    target = compute_pre_pv_target(
        soc_keep=1.5,
        soc_max=18.08,
        buffer_pct=20.0,
        reserve=0.0,
        expected_overflow_kwh=blended,
        dawn_load_kwh=1.0,
        max_reserved_kwh=1.8,
        safety_factor=1.2,
    )
    legacy = 1.5 + 0.20 * 18.08
    assert abs(target - legacy) < 0.01, f"Low confidence should keep legacy target, got {target}"
    print(f"  test_R62_pre_pv_target_low_confidence_stays_legacy: PASSED (blend={blended:.2f}, target={target:.2f})")


def test_R62_pre_pv_target_reserve_wins():
    """R62 pure: hardware reserve is never violated even on extreme overflow."""
    from curtailment_calc import compute_pre_pv_target

    target = compute_pre_pv_target(
        soc_keep=0.0,
        soc_max=18.08,
        buffer_pct=20.0,
        reserve=2.5,
        expected_overflow_kwh=30.0,
        dawn_load_kwh=0.5,
        max_reserved_kwh=1.8,
        safety_factor=1.2,
    )
    assert abs(target - 2.5) < 0.01, f"Reserve should bind, got {target}"
    print(f"  test_R62_pre_pv_target_reserve_wins: PASSED ({target})")


def test_R62_pre_pv_publish_thresholds_not_stale():
    """R62: during pre-PV active, the published drain thresholds must reflect the
    pre-PV target — NOT yesterday evening's effective_keep/overflow_floor.

    Latent bug found 2026-07-07: the pre-PV branch returned target_kwh but left
    _effective_keep_kwh/_overflow_floor_kwh at their last main-path values (e.g.
    14.95 from the previous dusk after the R61 hold). publish() derives
    drain_above from those attrs, so the HA automation would refuse to drain
    below yesterday's level and pre-PV drain would do nothing.
    """
    from curtailment_calc import compute_drain_above

    base = _make_pre_pv_base(soc_pct=0.7, gshp_ch="off", hour=4)
    plugin = CurtailmentPlugin(base)
    # Simulate stale state from yesterday evening (R61 dusk hold stamped high keep)
    plugin._effective_keep_kwh = 14.95
    plugin._overflow_floor_kwh = 18.08
    plugin._p10_recovery_floor = 6.25
    floor, phase = plugin.calculate(dno_limit_kw=4.0)
    assert phase == "active"
    drain_above = compute_drain_above(0.0, plugin._overflow_floor_kwh, plugin._effective_keep_kwh)
    assert abs(drain_above - max(floor, 0.5)) < 0.05, f"Published drain_above must track pre-PV target {floor:.2f}, got {drain_above:.2f} (stale state leak)"
    assert plugin._p10_recovery_floor < 1.0, f"Stale p10_recovery must be cleared pre-dawn, got {plugin._p10_recovery_floor}"
    print(f"  test_R62_pre_pv_publish_thresholds_not_stale: PASSED (drain_above={drain_above:.2f} tracks target={floor:.2f})")


def test_R52_pre_pv_drain_already_below_target():
    """SOC already at/below the pre-PV target → no drain needed → off.

    R62 note: on this big-overflow fixture the forecast-driven target is
    ~0.7 kWh, so "below target" now means near the deep floor. 22% SOC on a
    huge day correctly KEEPS draining (covered by
    test_R52_pre_pv_drain_active_at_drain_start); only ≈empty is exempt.
    """
    base = _make_pre_pv_base(soc_pct=0.03, gshp_ch="off", hour=4)  # 0.54 kWh
    plugin = CurtailmentPlugin(base)
    floor, phase = plugin.calculate(dno_limit_kw=4.0)
    assert phase == "off", f"SOC at/below target should be off, got {phase}"
    print("  test_R52_pre_pv_drain_already_below_target: PASSED")


def test_R52_pre_pv_drain_low_overflow_forecast():
    """Small overflow forecast (winter day) → no pre-PV drain regardless of SOC."""
    # Low p90 peak so overflow_p90 < threshold
    base = _make_pre_pv_base(soc_pct=0.7, gshp_ch="off", hour=4, p90_peak=2.0)
    plugin = CurtailmentPlugin(base)
    floor, phase = plugin.calculate(dno_limit_kw=4.0)
    assert phase == "off", f"Low-overflow forecast should not trigger pre-PV drain, got {phase}"
    print("  test_R52_pre_pv_drain_low_overflow_forecast: PASSED")


def test_R52_pre_pv_drain_no_flap_once_started():
    """REGRESSION (2026-07-22): once the pre-PV drain starts, it must run to target
    WITHOUT the drain_start timing knife-edge flipping it back to Hold. As SOC
    drains at ~dno, drain_minutes shrinks at ~60min/h so drain_start_utc advances
    at the same rate as `now` — the `now < drain_start` ("too early") check hovers
    at equality and flips on noise, causing Max Export↔Hold flapping (observed
    04:27-05:47 BST). The start-latch must gate the START only, then drain to target.
    """
    base = _make_pre_pv_base(soc_pct=0.7, gshp_ch="off", hour=4)
    plugin = CurtailmentPlugin(base)
    floor1, phase1 = plugin.calculate(dno_limit_kw=4.0)
    assert phase1 == "active", f"cycle1: drain should be active, got {phase1}"
    assert plugin._policy_override is None, f"cycle1: draining → no override, got {plugin._policy_override}"
    assert plugin._pre_pv_drain_started, "start-latch must set once the drain begins"

    # Cycle 2: SOC has drained to just above target → drain_start_utc jumps toward
    # pv_start (in the future) → the un-latched timing check would return None
    # ('too early') → RD16 Hold. The latch must keep it draining (override None).
    base.soc_kw = floor1 + 0.5
    floor2, phase2 = plugin.calculate(dno_limit_kw=4.0)
    assert phase2 == "active", f"cycle2: must stay active (draining to target), got {phase2}"
    assert plugin._policy_override is None, f"cycle2: must keep draining, not flap to Hold, got override={plugin._policy_override}"
    print("  test_R52_pre_pv_drain_no_flap_once_started: PASSED")


# ============================================================================
# R9a: LoadML smoothing tests (v20)
# ============================================================================


def test_R9a_smooth_load_forecast_constant_input():
    """Constant input → constant output (smoothing identity)."""
    from curtailment_calc import smooth_load_forecast

    flat = [0.5] * 24  # 2 hours of 0.5 kW
    smoothed = smooth_load_forecast(flat, window_minutes=60, step_minutes=5)
    assert len(smoothed) == len(flat), f"Length mismatch: {len(smoothed)} vs {len(flat)}"
    for v in smoothed:
        assert abs(v - 0.5) < 1e-9, f"Constant 0.5 input should yield 0.5 out, got {v}"
    print("  test_R9a_smooth_load_forecast_constant_input: PASSED")


def test_R9a_smooth_load_forecast_attenuates_single_spike():
    """Single 5 kW transient in 0.5 kW background → attenuated by ~12× (60-min window)."""
    from curtailment_calc import smooth_load_forecast

    # 24 slots of 0.5 kW with a single spike of 5 kW at slot 12
    load = [0.5] * 24
    load[12] = 5.0  # single 5 kW transient
    smoothed = smooth_load_forecast(load, window_minutes=60, step_minutes=5)

    # At slot 12, smoothed value ≈ (0.5 × 12 + 5.0) / 13 ≈ 0.85 kW
    # (window = ±6 slots = 13 slots total, since centered)
    assert abs(smoothed[12] - 0.85) < 0.05, f"Spike at slot 12 should attenuate to ~0.85 kW, got {smoothed[12]:.3f}"
    # Far from spike (slot 0), value should be ≈ 0.5 kW (spike out of window)
    assert abs(smoothed[0] - 0.5) < 0.05, f"Slot 0 (out of spike window) should stay ~0.5 kW, got {smoothed[0]:.3f}"
    print("  test_R9a_smooth_load_forecast_attenuates_single_spike: PASSED")


def test_R9a_smoothed_integral_stable_against_load_noise():
    """Overflow integral with smoothed load resists single-slot LoadML noise (≤5% drift).

    This is the v5 failure mode test: a phantom 1 kW transient in LoadML must not
    swing the overflow integral by more than 5%. With unsmoothed load, the spike
    momentarily suppresses overflow at that slot. Smoothing distributes the spike
    so the integral barely moves.
    """
    from curtailment_calc import compute_solar_overflow, smooth_load_forecast

    # Simple noon-centered overflow window (6 hours, step 5 min, lat 52, summer)
    n_steps = 72  # 6 hours
    flat_load = [0.5] * n_steps  # baseline
    noisy_load = list(flat_load)
    noisy_load[36] = 1.5  # 1 kW transient mid-window

    common = dict(scale=10.0, lat_deg=52.0, lon_deg=-1.5, day_of_year=140, from_utc_hours=8.0, to_utc_hours=14.0, dno_limit=4.0, step_minutes=5)

    base_integral = compute_solar_overflow(load_forecast_kw=flat_load, **common)
    noisy_integral_unsmoothed = compute_solar_overflow(load_forecast_kw=noisy_load, **common)
    smoothed_load = smooth_load_forecast(noisy_load, window_minutes=60, step_minutes=5)
    noisy_integral_smoothed = compute_solar_overflow(load_forecast_kw=smoothed_load, **common)

    # Sanity: the unsmoothed noisy integral should differ from baseline (proves the spike has effect)
    assert abs(base_integral - noisy_integral_unsmoothed) > 0.0001, "Test setup invalid: spike should change unsmoothed integral"

    # Smoothed integral within 5% of baseline
    pct_drift = abs(noisy_integral_smoothed - base_integral) / max(0.001, base_integral)
    assert pct_drift < 0.05, f"Smoothed integral drifted {pct_drift*100:.1f}% from baseline " f"(base={base_integral:.3f}, smoothed_noisy={noisy_integral_smoothed:.3f}); " f"R9a requires ≤5%"
    print(f"  test_R9a_smoothed_integral_stable_against_load_noise: PASSED " f"(base={base_integral:.3f} kWh, smoothed drift={pct_drift*100:.2f}%)")


# ============================================================================
# R53: per-slot Solcast integral tests (v20)
# ============================================================================


def _make_solcast_slots(slot_specs, date="2026-05-02", local_offset="+01:00"):
    """Build a Solcast detailedForecast list from (hour, minute, pv50, pv10, pv90) tuples."""
    slots = []
    for hour, minute, pv50, pv10, pv90 in slot_specs:
        slots.append(
            {
                "period_start": f"{date}T{hour:02d}:{minute:02d}:00{local_offset}",
                "pv_estimate": pv50,
                "pv_estimate10": pv10,
                "pv_estimate90": pv90,
            }
        )
    return slots


def _steps(pairs, step=5):
    """{minute: kWh-per-step} for pv and load from (minute, pv_kw, load_kw) triples."""
    h = step / 60.0
    pv = {m: pv_kw * h for m, pv_kw, _ in pairs}
    load = {m: ld * h for m, _, ld in pairs}
    return pv, load


def test_pv_covers_load_minute_finds_the_real_crossing():
    """RD36 — the drain must end when PV COVERS LOAD, from the FORECAST.

    2026-08-12: the drain was timed to end at `pv_start_utc`, a clear-sky sine
    crossing of a fixed 0.5 kW, and then coasted on the dawn reserve until PV
    actually covered load. Measured: drain ended 05:45, PV covered load ~06:45 —
    an hour of coasting. The clear-sky prediction was itself ~35 min early
    (implied end 06:14 vs actual ~06:50).

    Predbat's own per-slot PV and load forecasts give the crossing directly and
    were accurate on the day (plan: PV 0.10 vs load 0.24 at 06:30, PV 0.43 vs
    0.23 at 07:00). Use those, not a sine.
    """
    from curtailment_calc import compute_pv_covers_load_minute

    pairs = [(m, 0.0, 0.4) for m in range(300, 400, 5)]  # 05:00-06:40 dark
    pairs += [(m, 0.6, 0.4) for m in range(400, 600, 5)]  # 06:40 on, PV over load
    pv, load = _steps(pairs)
    got = compute_pv_covers_load_minute(pv, load, start_minute=300, end_minute=600, values_are_kwh=True, sustained_minutes=30)
    assert got == 400, "crossing is 06:40 (minute 400), got {}".format(got)
    print("  test_pv_covers_load_minute_finds_the_real_crossing: PASSED")


def test_pv_covers_load_ignores_a_blip():
    """A single slot where load dips under PV is not dawn — same reasoning as
    RD35's sustained release, applied to the forecast rather than the meter."""
    from curtailment_calc import compute_pv_covers_load_minute

    pairs = [(m, 0.0, 0.4) for m in range(300, 350, 5)]
    pairs += [(350, 0.5, 0.4)]  # one-slot blip
    pairs += [(m, 0.0, 0.4) for m in range(355, 420, 5)]
    pairs += [(m, 0.6, 0.4) for m in range(420, 600, 5)]  # the real crossing
    pv, load = _steps(pairs)
    got = compute_pv_covers_load_minute(pv, load, start_minute=300, end_minute=600, values_are_kwh=True, sustained_minutes=30)
    assert got == 420, "a one-slot blip must not count as the crossing, got {}".format(got)
    print("  test_pv_covers_load_ignores_a_blip: PASSED")


def test_pv_covers_load_returns_none_when_it_never_crosses():
    """A dull winter day where PV never covers load: caller must fall back, not
    plan a drain that finishes at a crossing which does not exist."""
    from curtailment_calc import compute_pv_covers_load_minute

    pv, load = _steps([(m, 0.1, 0.5) for m in range(300, 600, 5)])
    assert compute_pv_covers_load_minute(pv, load, start_minute=300, end_minute=600, values_are_kwh=True) is None
    print("  test_pv_covers_load_returns_none_when_it_never_crosses: PASSED")


def test_R53_compute_solcast_overflow_empty_returns_zero():
    """Empty forecast → 0 kWh overflow."""
    from curtailment_calc import compute_solcast_overflow

    out = compute_solcast_overflow(
        detailed_forecast=[],
        from_utc_hours=8.0,
        to_utc_hours=14.0,
        dno_limit=4.0,
    )
    assert out == 0.0, f"Empty forecast should yield 0, got {out}"
    print("  test_R53_compute_solcast_overflow_empty_returns_zero: PASSED")


def test_R53_compute_solcast_overflow_uniform_sunny_slot():
    """One 30-min slot at 8 kW (4 kW above DNO) → ~1.75 kWh overflow."""
    from curtailment_calc import compute_solcast_overflow

    # Single slot 11:00-11:30 BST = 10:00-10:30 UTC, 8 kW
    slots = _make_solcast_slots([(11, 0, 8.0, 8.0, 8.0)])
    out = compute_solcast_overflow(
        detailed_forecast=slots,
        from_utc_hours=10.0,
        to_utc_hours=10.5,
        dno_limit=4.0,
        local_offset_hours=1.0,
        load_forecast_kw=[0.0] * 6,
    )
    # 30 min × (8 - 0.5 - 4) kW = 0.5h × 3.5 kW = 1.75 kWh
    assert abs(out - 1.75) < 0.01, f"Expected ~1.75 kWh, got {out:.3f}"
    print("  test_R53_compute_solcast_overflow_uniform_sunny_slot: PASSED")


def test_global_scale_applies_beyond_the_r58_calibration_window():
    """RD31 fix (2026-08-11). The whole-day ratio must scale the ENTIRE integral.

    The defect: `_update_tracking_estimate` passed the day ratio into
    `calibration_ratio`, which is R58's SHORT-WINDOW knob — it only multiplies
    pv_kw for the first `calibration_window_hours` (0.5 h). Over an ~8 h
    remaining window that is ~6% of the slots, so the ratio barely moved the
    answer and `overflow_tracking` came out equal to `overflow_p50`.

    Live 2026-08-11 09:39: day_ratio 0.694, overflow_p50 16.43,
    overflow_tracking 16.43 — identical. RD31 had been a no-op since it shipped,
    and yesterday's advice alert inherited it.

    This pins the distinction directly: ALL the overflow here sits AFTER the
    calibration window, so `calibration_ratio` provably cannot touch it and only
    a global scale can.
    """
    from curtailment_calc import compute_solcast_overflow

    # 10:00-10:30 UTC quiet (no overflow), 11:00-12:00 UTC sunny.
    slots = _make_solcast_slots([(11, 0, 4.0, 4.0, 4.0), (12, 0, 8.0, 8.0, 8.0), (12, 30, 8.0, 8.0, 8.0)])
    kwargs = dict(
        detailed_forecast=slots,
        from_utc_hours=10.0,
        to_utc_hours=11.5,
        dno_limit=4.0,
        local_offset_hours=1.0,
        load_forecast_kw=[0.0] * 20,
    )
    base = compute_solcast_overflow(**kwargs)
    r58_only = compute_solcast_overflow(calibration_ratio=0.694, **kwargs)
    scaled = compute_solcast_overflow(global_scale=0.694, **kwargs)

    assert base > 0, "precondition: the fixture must produce overflow"
    assert abs(r58_only - base) < 0.01, "precondition: overflow is all outside the R58 window, so calibration_ratio must NOT change it (got {:.3f} vs {:.3f}) — otherwise this test proves nothing".format(r58_only, base)
    assert scaled < base * 0.5, "a 0.694 global scale must cut the overflow SHARPLY (non-linear), got {:.3f} vs base {:.3f}".format(scaled, base)
    print("  test_global_scale_applies_beyond_the_r58_calibration_window: PASSED ({:.2f} -> {:.2f})".format(base, scaled))


def test_global_scale_is_non_linear_in_pv():
    """Overflow is `sum(max(0, pv - load - cap))`, so scaling PV down by 31% must
    cut overflow by far MORE than 31% — slots that were just over the cap drop
    under it entirely and stop contributing. That non-linearity is the whole
    reason the metric matters (12.6% shortfall cut it ~70% on 2026-08-07)."""
    from curtailment_calc import compute_solcast_overflow

    slots = _make_solcast_slots([(11, 0, 5.0, 5.0, 5.0), (11, 30, 5.0, 5.0, 5.0)])
    kwargs = dict(
        detailed_forecast=slots,
        from_utc_hours=10.0,
        to_utc_hours=11.0,
        dno_limit=4.0,
        local_offset_hours=1.0,
        load_forecast_kw=[0.0] * 12,
    )
    base = compute_solcast_overflow(**kwargs)
    scaled = compute_solcast_overflow(global_scale=0.694, **kwargs)
    assert base > 0
    # 5.0 -> 3.47 kW is BELOW cap+base_load, so the overflow vanishes entirely.
    assert scaled == 0.0, "PV scaled under the cap must yield zero overflow, got {:.3f}".format(scaled)
    print("  test_global_scale_is_non_linear_in_pv: PASSED ({:.2f} -> {:.2f})".format(base, scaled))


def test_r58_calibration_window_still_behaves():
    """Regression: the global scale must not disturb R58's short-window knob."""
    from curtailment_calc import compute_solcast_overflow

    slots = _make_solcast_slots([(11, 0, 8.0, 8.0, 8.0)])
    kwargs = dict(
        detailed_forecast=slots,
        from_utc_hours=10.0,
        to_utc_hours=10.5,
        dno_limit=4.0,
        local_offset_hours=1.0,
        load_forecast_kw=[0.0] * 6,
    )
    assert abs(compute_solcast_overflow(**kwargs) - 1.75) < 0.01
    # Inside the window, calibration still scales: 8 x 1.2 = 9.6 -> (9.6-0.5-4) x 0.5
    hot = compute_solcast_overflow(calibration_ratio=1.2, **kwargs)
    assert abs(hot - 2.55) < 0.02, "R58 in-window scaling must be unchanged, got {:.3f}".format(hot)
    print("  test_r58_calibration_window_still_behaves: PASSED")


def test_R53_compute_solcast_overflow_preserves_day_shape():
    """The 2026-05-02 failure case: clear morning + rainy afternoon → small overflow.

    Old compute_solar_overflow with scale=11 returns much more because it
    extrapolates the morning peak across the whole day (clear-sky model,
    ignores Solcast shape). New compute_solcast_overflow returns less
    because it sees the afternoon drop.
    """
    from curtailment_calc import compute_solar_overflow, compute_solcast_overflow

    # Slots 09:00 BST through 17:00 BST (8 hours = 16 slots of 30 min each)
    # Realistic 2026-05-02 shape: peak ~6.8 kW around 10:30 BST, then cloud,
    # then rain by 14:00. Models a "clear morning + bad afternoon" day.
    pv_curve = [
        (9, 0, 4.0),
        (9, 30, 5.5),
        (10, 0, 6.5),
        (10, 30, 6.8),
        (11, 0, 6.5),
        (11, 30, 5.0),
        (12, 0, 3.5),
        (12, 30, 2.5),
        (13, 0, 1.5),
        (13, 30, 1.0),
        (14, 0, 0.8),
        (14, 30, 0.5),
        (15, 0, 0.3),
        (15, 30, 0.3),
        (16, 0, 0.2),
        (16, 30, 0.2),
    ]
    slot_specs = [(h, m, pv, pv * 0.7, pv * 1.1) for h, m, pv in pv_curve]
    slots = _make_solcast_slots(slot_specs)

    n_steps = (16 - 8) * 12  # 96 five-min steps
    load = [0.5] * n_steps

    new_overflow = compute_solcast_overflow(
        detailed_forecast=slots,
        from_utc_hours=8.0,
        to_utc_hours=16.0,
        dno_limit=4.0,
        local_offset_hours=1.0,
        load_forecast_kw=load,
    )

    old_overflow = compute_solar_overflow(
        scale=11.0,
        lat_deg=52.0,
        lon_deg=-1.5,
        day_of_year=122,
        from_utc_hours=8.0,
        to_utc_hours=16.0,
        dno_limit=4.0,
        load_forecast_kw=load,
    )

    assert old_overflow > 10.0, f"Test sanity: clear-sky scale=11 should predict > 10 kWh, got {old_overflow:.2f}"
    # Shape preservation: Solcast must produce significantly less overflow
    # than the clear-sky model when afternoon goes cloudy/rainy.
    ratio = new_overflow / max(0.1, old_overflow)
    assert ratio < 0.4, f"Solcast slot integral should be < 40% of clear-sky integral on a variable day, " f"got {ratio*100:.0f}% (new={new_overflow:.2f} vs old={old_overflow:.2f})"
    assert new_overflow < 5.0, f"On this realistic shape, new overflow should be < 5 kWh, got {new_overflow:.2f}"
    print(f"  test_R53_compute_solcast_overflow_preserves_day_shape: PASSED " f"(new={new_overflow:.2f} kWh, old={old_overflow:.2f} kWh — {old_overflow / max(0.1, new_overflow):.1f}× clear-sky overestimate)")


def test_R53_compute_solcast_overflow_uses_load_forecast():
    """Load forecast reduces overflow per slot (PV partly absorbed by load)."""
    from curtailment_calc import compute_solcast_overflow

    slots = _make_solcast_slots([(11, 0, 8.0, 8.0, 8.0)])
    no_load = compute_solcast_overflow(
        detailed_forecast=slots,
        from_utc_hours=10.0,
        to_utc_hours=10.5,
        dno_limit=4.0,
        local_offset_hours=1.0,
        load_forecast_kw=[0.0] * 6,
    )
    with_load = compute_solcast_overflow(
        detailed_forecast=slots,
        from_utc_hours=10.0,
        to_utc_hours=10.5,
        dno_limit=4.0,
        local_offset_hours=1.0,
        load_forecast_kw=[2.0] * 6,
    )
    # No load: 30 min × (8 − 0.5 − 4) = 1.75 kWh
    # With 2 kW load: 30 min × (8 − 2 − 4) = 1.0 kWh
    assert abs(no_load - 1.75) < 0.01, f"no-load expected 1.75, got {no_load:.3f}"
    assert abs(with_load - 1.0) < 0.01, f"with-load expected 1.0, got {with_load:.3f}"
    print("  test_R53_compute_solcast_overflow_uses_load_forecast: PASSED")


def test_R53_real_2026_05_02_shape_preserved():
    """Real Solcast forecast for 2026-05-02 — the day that triggered v20.

    Live behaviour that day: plugin extracted scale=11 from observed peak
    (8.2 kW at noon BST), extrapolated 11 × sin(elev) across the whole
    day, predicted 16+ kWh of overflow. Reality: rain by 16:00 BST,
    actual overflow probably 0-2 kWh.

    With the real Solcast slots (this fixture), compute_solcast_overflow
    sees the afternoon drop encoded in pv_estimate per slot and produces
    a much smaller integral.
    """
    import json
    from curtailment_calc import compute_solar_overflow, compute_solcast_overflow

    fixture_path = os.path.join(CSV_DIR, "solcast_2026_05_02.json")
    if not os.path.exists(fixture_path):
        print("  test_R53_real_2026_05_02_shape_preserved: SKIPPED (fixture not found)")
        return
    with open(fixture_path) as f:
        slots = json.load(f)

    # Integration window: 11:00 UTC (12:00 BST, mid-day) through 19:00 UTC.
    n_steps = 8 * 12  # 8 hours of 5-min steps
    load = [0.5] * n_steps

    new_overflow = compute_solcast_overflow(
        detailed_forecast=slots,
        from_utc_hours=11.0,
        to_utc_hours=19.0,
        dno_limit=4.0,
        local_offset_hours=0.0,
        load_forecast_kw=load,
    )

    # Old clear-sky: scale from observed peak (matches plugin's R42 behaviour)
    peak_pv = 8.24
    peak_utc = 11.0
    elev_at_peak = solar_elevation(52.0, -1.5, peak_utc, 122)
    sin_peak = math.sin(math.radians(elev_at_peak))
    scale = peak_pv / sin_peak
    old_overflow = compute_solar_overflow(
        scale=scale,
        lat_deg=52.0,
        lon_deg=-1.5,
        day_of_year=122,
        from_utc_hours=11.0,
        to_utc_hours=19.0,
        dno_limit=4.0,
        load_forecast_kw=load,
    )

    assert old_overflow > 8.0, f"Test sanity: scale={scale:.1f} should predict > 8 kWh overflow, got {old_overflow:.2f}"
    assert new_overflow < old_overflow * 0.5, f"Real Solcast shape should be < 50% of clear-sky extrapolation. " f"new={new_overflow:.2f} kWh, old={old_overflow:.2f} kWh"
    print(f"  test_R53_real_2026_05_02_shape_preserved: PASSED " f"(scale={scale:.1f}, new={new_overflow:.2f} kWh, old={old_overflow:.2f} kWh, " f"clear-sky overestimate {old_overflow / max(0.1, new_overflow):.1f}x)")


def test_R53_real_last_10_days_no_crash_and_sane():
    """Run compute_solcast_overflow against 10 days of real Solcast forecasts.

    Asserts: function returns a finite non-negative number for each day, and
    matches a sensible band given Solcast's day total (overflow can't exceed
    Solcast remaining minus DNO export budget).
    """
    import glob
    import json
    from curtailment_calc import compute_solcast_overflow

    fixtures = sorted(glob.glob(os.path.join(CSV_DIR, "solcast_2026_*.json")))
    if not fixtures:
        print("  test_R53_real_last_10_days_no_crash_and_sane: SKIPPED (no fixtures)")
        return

    n_steps = 16 * 12  # 16-hour window 04:00-20:00 UTC
    load = [0.5] * n_steps
    summary = []
    for fp in fixtures:
        with open(fp) as f:
            slots = json.load(f)
        day = os.path.basename(fp).replace("solcast_", "").replace(".json", "").replace("_", "-")
        # P50 overflow integrating from 04:00 UTC to 20:00 UTC
        for band in ("pv_estimate10", "pv_estimate", "pv_estimate90"):
            ov = compute_solcast_overflow(
                detailed_forecast=slots,
                from_utc_hours=4.0,
                to_utc_hours=20.0,
                dno_limit=4.0,
                local_offset_hours=0.0,
                load_forecast_kw=load,
                band=band,
            )
            assert ov >= 0.0, f"{day} {band}: negative overflow {ov:.2f}"
            assert ov < 100.0, f"{day} {band}: implausible overflow {ov:.2f} kWh"
            day_total = sum(s.get(band, 0) for s in slots) * 0.5
            assert ov <= day_total, f"{day} {band}: overflow {ov:.2f} exceeds day total {day_total:.2f}"
        # Just collect the P50 result for the printed summary
        ov50 = compute_solcast_overflow(
            detailed_forecast=slots,
            from_utc_hours=4.0,
            to_utc_hours=20.0,
            dno_limit=4.0,
            local_offset_hours=0.0,
            load_forecast_kw=load,
            band="pv_estimate",
        )
        ov90 = compute_solcast_overflow(
            detailed_forecast=slots,
            from_utc_hours=4.0,
            to_utc_hours=20.0,
            dno_limit=4.0,
            local_offset_hours=0.0,
            load_forecast_kw=load,
            band="pv_estimate90",
        )
        day_total = sum(s.get("pv_estimate", 0) for s in slots) * 0.5
        summary.append((day, day_total, ov50, ov90))

    print(f"  test_R53_real_last_10_days_no_crash_and_sane: PASSED ({len(summary)} days)")
    for day, total, ov50, ov90 in summary:
        print(f"    {day}: day total P50={total:.1f} kWh, overflow P50={ov50:.2f} kWh, P90={ov90:.2f} kWh")


def test_R58_calibration_ratio_one_is_identity():
    """calibration_ratio=1.0 → output identical to no-calibration."""
    from curtailment_calc import compute_solcast_overflow

    slots = _make_solcast_slots([(11, 0, 7.0, 5.0, 9.0), (11, 30, 7.0, 5.0, 9.0)])
    common = dict(
        detailed_forecast=slots,
        from_utc_hours=10.0,
        to_utc_hours=11.0,
        dno_limit=4.0,
        local_offset_hours=1.0,
        load_forecast_kw=[0.0] * 12,
    )
    base = compute_solcast_overflow(**common)
    cal = compute_solcast_overflow(calibration_ratio=1.0, **common)
    assert abs(base - cal) < 1e-9, f"ratio=1.0 should be identity, base={base} cal={cal}"
    print("  test_R58_calibration_ratio_one_is_identity: PASSED")


def test_R58_calibration_ratio_only_affects_window():
    """ratio=1.5, window=0.5h → multiplies first 30 min only, rest unchanged."""
    from curtailment_calc import compute_solcast_overflow

    # Two slots, both 7 kW. Integration covers both. Calibration window = first slot.
    slots = _make_solcast_slots([(11, 0, 7.0, 5.0, 9.0), (11, 30, 7.0, 5.0, 9.0)])
    common = dict(
        detailed_forecast=slots,
        from_utc_hours=10.0,
        to_utc_hours=11.0,
        dno_limit=4.0,
        local_offset_hours=1.0,
        load_forecast_kw=[0.0] * 12,
    )
    base = compute_solcast_overflow(**common)
    boost = compute_solcast_overflow(calibration_ratio=1.5, calibration_window_hours=0.5, **common)
    # Slot 1 (calibrated): pv = 7 × 1.5 = 10.5; overflow = (10.5 − 0.5 − 4) × 0.5 = 3.0
    # Slot 2 (not calibrated): overflow = (7 − 0.5 − 4) × 0.5 = 1.25
    # boost = 4.25; base = 1.25 + 1.25 = 2.5
    assert abs(boost - 4.25) < 0.01, f"Expected boost=4.25, got {boost:.3f}"
    assert abs(base - 2.5) < 0.01, f"Expected base=2.5, got {base:.3f}"
    print(f"  test_R58_calibration_ratio_only_affects_window: PASSED (base={base:.2f}, boost={boost:.2f})")


def test_R58_calibration_ratio_capped_at_15x():
    """A wildly-high ratio (e.g. 5x) is capped at 1.5x to prevent runaway."""
    from curtailment_calc import compute_solcast_overflow

    slots = _make_solcast_slots([(11, 0, 7.0, 5.0, 9.0)])
    common = dict(
        detailed_forecast=slots,
        from_utc_hours=10.0,
        to_utc_hours=10.5,
        dno_limit=4.0,
        local_offset_hours=1.0,
        load_forecast_kw=[0.0] * 6,
    )
    cap = compute_solcast_overflow(calibration_ratio=1.5, calibration_window_hours=0.5, **common)
    runaway = compute_solcast_overflow(calibration_ratio=5.0, calibration_window_hours=0.5, **common)
    assert abs(cap - runaway) < 1e-9, f"Should cap at 1.5x, got cap={cap} runaway={runaway}"
    print(f"  test_R58_calibration_ratio_capped_at_15x: PASSED (cap={cap:.2f})")


def test_R53_plugin_uses_solcast_slots_when_available():
    """Plugin's _publish_forecast_overflow uses compute_solcast_overflow when
    Solcast detailedForecast has 4+ slots. Bands no longer collapse to a
    single value (R43 → R58 behaviour change).
    """
    import json
    from datetime import datetime, timezone

    fixture_path = os.path.join(CSV_DIR, "solcast_2026_05_02.json")
    if not os.path.exists(fixture_path):
        print("  test_R53_plugin_uses_solcast_slots_when_available: SKIPPED (fixture not found)")
        return
    with open(fixture_path) as f:
        slots = json.load(f)

    # MockBase at 11:00 UTC = 11:00 BST (we'll set timezone-naive UTC).
    # Use minutes_now = 720 (12:00 BST) so calibration window = noon onwards.
    base = MockBase(
        soc_kw=4.0,
        soc_max=18.08,
        minutes_now=720,
        now_utc=datetime(2026, 5, 2, 11, 0, tzinfo=timezone.utc),
        sensor_overrides={
            "sensor.solcast_pv_forecast_forecast_today": {
                "detailedForecast": slots,
                "state": 42.2,
            },
            "sensor.solcast_pv_forecast_forecast_remaining_today": 25.0,
        },
    )
    plugin = CurtailmentPlugin(base)
    plugin._publish_forecast_overflow(
        lat=52.0,
        lon=-1.5,
        doy=122,
        local_offset=1.0,
        utc_hours=11.0,
        dno_limit_kw=4.0,
    )

    p10 = plugin._overflow_p10
    p50 = plugin._overflow_p50
    p90 = plugin._overflow_p90
    assert p10 < p50 < p90, f"R50 spread should be preserved (p10<{p50}<p90), got p10={p10} p50={p50} p90={p90}"
    # On 2026-05-02 (clear morning + rain afternoon, integration from 11:00 UTC)
    # the plugin should report SMALL overflow — not the 16+ kWh that the old
    # clear-sky model produced from observed peak.
    assert p90 < 12.0, f"Solcast slot model should give P90 < 12 kWh on this variable day, got {p90}"
    assert p50 < 8.0, f"Solcast slot model should give P50 < 8 kWh on this variable day, got {p50}"
    print(f"  test_R53_plugin_uses_solcast_slots_when_available: PASSED (p10={p10:.2f} p50={p50:.2f} p90={p90:.2f} kWh)")


def test_R58_calibration_below_one_attenuates_window():
    """ratio<1.0 attenuates next-30-min PV (cloudier than forecast caught live)."""
    from curtailment_calc import compute_solcast_overflow

    slots = _make_solcast_slots([(11, 0, 7.0, 5.0, 9.0)])
    common = dict(
        detailed_forecast=slots,
        from_utc_hours=10.0,
        to_utc_hours=10.5,
        dno_limit=4.0,
        local_offset_hours=1.0,
        load_forecast_kw=[0.0] * 6,
    )
    base = compute_solcast_overflow(**common)
    attenuated = compute_solcast_overflow(calibration_ratio=0.5, calibration_window_hours=0.5, **common)
    # base: pv=7, overflow = (7 − 0.5 − 4) × 0.5 = 1.25
    # attenuated: pv=3.5, overflow = max(0, 3.5 − 0.5 − 4) × 0.5 = 0
    assert abs(base - 1.25) < 0.01, f"base expected 1.25, got {base:.3f}"
    assert attenuated == 0.0, f"attenuated expected 0 (pv below DNO), got {attenuated:.3f}"
    print(f"  test_R58_calibration_below_one_attenuates_window: PASSED (base={base:.2f}, attenuated={attenuated:.2f})")


def test_R53_compute_solcast_overflow_band_selection():
    """band='pv_estimate10' / 'pv_estimate90' read different fields (R53 enables R50)."""
    from curtailment_calc import compute_solcast_overflow

    slots = _make_solcast_slots([(11, 0, 7.0, 5.0, 9.0)])
    common = dict(
        detailed_forecast=slots,
        from_utc_hours=10.0,
        to_utc_hours=10.5,
        dno_limit=4.0,
        local_offset_hours=1.0,
        load_forecast_kw=[0.0] * 6,
    )
    p10 = compute_solcast_overflow(band="pv_estimate10", **common)
    p50 = compute_solcast_overflow(band="pv_estimate", **common)
    p90 = compute_solcast_overflow(band="pv_estimate90", **common)

    assert abs(p10 - 0.25) < 0.01, f"P10 expected 0.25 kWh, got {p10:.3f}"
    assert abs(p50 - 1.25) < 0.01, f"P50 expected 1.25 kWh, got {p50:.3f}"
    assert abs(p90 - 2.25) < 0.01, f"P90 expected 2.25 kWh, got {p90:.3f}"
    assert p10 < p50 < p90, "P10 < P50 < P90 spread should be preserved (R53 enables R50)"
    print(f"  test_R53_compute_solcast_overflow_band_selection: PASSED (p10={p10:.2f} p50={p50:.2f} p90={p90:.2f})")


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

from curtailment_plugin import (
    CurtailmentPlugin,
    PREDICT_STEP as PLUGIN_STEP,
    SIG_DAILY_PV,
    SOLCAST_TODAY,
    SIG_SAVING_SESSION as SIG_SAVING_SESSION_ENTITY,
    SIG_POLICY_SELECT,
    SIG_OVERRIDE_SELECT,
    SIG_BATTERY_SOC_PCT as SIG_PLANT_SOC_ENTITY,
    SIG_SAVING_SESSION_CALENDAR,
    DAWN_RELEASE_CONFIRM_CYCLES,
    MIN_BASE_LOAD_KW,
    SESSION_PROTECT_HORIZON_HOURS,
    PREDBAT_SOC_MIN_HELPER,
)


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
        # A0 fail-closed (2026-07-30): the plugin now HOLDS and changes nothing
        # when plant SOC is unreadable, rather than treating it as 0.0 ("battery
        # empty" — the 2026-07-29 night re-take). Rigs written before that guard
        # never supplied the sensor, so every one of them takes the fallback and
        # publishes nothing. Default it from soc_kw so the guard is exercised
        # only by tests that deliberately remove it (R37: fix the rig, never
        # weaken production to make a stale test pass).
        if SIG_PLANT_SOC_ENTITY not in self._sensor_overrides:
            self._sensor_overrides[SIG_PLANT_SOC_ENTITY] = round(soc_kw / soc_max * 100.0, 1) if soc_max else 0.0
        self.now_utc = now_utc or datetime(2025, 7, 12, 12, 0, tzinfo=timezone.utc)

    def log(self, msg, *args, **kwargs):
        self.logs.append(msg)

    def get_state_wrapper(self, entity, default=None, attribute=None):
        if entity in self._sensor_overrides:
            val = self._sensor_overrides[entity]
            if isinstance(val, dict):
                if attribute:
                    return val.get(attribute, default)
                # No attribute: return "state" key if present (mimics HA semantics
                # where states('x') returns state and state_attr('x','y') returns attr).
                # Falls back to default rather than the dict itself so a plugin's
                # float() of the result doesn't crash.
                return val.get("state", default)
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


def _make_p90_sensors(p90_peak_kw=8.58, solcast_remaining=25.0, all_bands=False):
    """Build sensor_overrides with Solcast p90 detailedForecast.

    MockBase default: now_utc=2025-07-12 12:00 UTC, minutes_now=720 → local_offset=0.
    Period_start at 12:00 local = 12:00 UTC. p90_scale_from_forecast will compute
    scale = p90_peak_kw / sin(elevation at 12:25 UTC) ≈ p90_peak_kw / 0.829.

    `all_bands` adds pv_estimate10 / pv_estimate so p_scales_from_forecast returns
    three real scales. Off by default: the p90-only shape is what production
    degrades to when Solcast is short, and most tests depend on that. Opt in when
    the test needs the band SPREAD rather than just the p90 floor.

    Returns a dict to merge into sensor_overrides.
    """
    slot = {"period_start": "2025-07-12T12:00:00+00:00", "pv_estimate90": p90_peak_kw}
    if all_bands:
        slot["pv_estimate"] = round(p90_peak_kw * 0.80, 3)
        slot["pv_estimate10"] = round(p90_peak_kw * 0.55, 3)
    return {
        "sensor.solcast_pv_forecast_forecast_today": {"detailedForecast": [slot]},
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


def test_r48_triggers_after_overnight_100pct():
    """R48 must trigger on a morning with a 100% overnight SOC.

    Live regression 2026-04-23: battery at 100% overnight latched
    _keep_recovered=True immediately at midnight rollover, because the naive
    'if soc_kw >= soc_keep: recovered=True' check always fired before the
    battery had a chance to drain. That defeated R48 entirely on big-overflow
    mornings — SOC stayed clamped at soc_keep_base instead of being drained
    to 0.5 kWh.

    Fix: require the battery to have been observed BELOW soc_keep this day
    before allowing the recovered latch to fire.
    """
    from datetime import datetime, timezone

    pv, load = _make_overflow_pv(minutes_now=360)  # 06:00 local — early morning
    sensor_overrides = {
        "sensor.sigen_plant_pv_power": 2.0,  # PV rising, pv_covering will be True
        "sensor.sigen_plant_consumed_power": 0.5,
    }
    sensor_overrides.update(_make_p90_sensors())
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=BATTERY_KWH * 1.0,  # 100% overnight — the crash case
        minutes_now=360,
        # Match minutes_now=360 (06:00 local). With UTC tz, local_offset=0 so
        # period_start "+00:00" is treated as UTC — peak slot at 12:00 UTC.
        now_utc=datetime(2025, 7, 12, 6, 0, tzinfo=timezone.utc),
        best_soc_keep=1.5,
        sensor_overrides=sensor_overrides,
    )
    plugin = CurtailmentPlugin(base)

    floor, phase = plugin.calculate(dno_limit_kw=4.0)

    # R48 must apply: big overflow + pv_covering → effective_keep = 0.5 kWh.
    # Floor must not be clamped at 1.5 (base keep) just because overnight SOC was high.
    assert floor < 1.5, f"R48 must trigger with 100%-overnight + big morning overflow, " f"but floor clamped to base keep ({floor:.2f} ≥ 1.5). " f"_keep_recovered latched incorrectly at rollover."
    print(f"  test_r48_triggers_after_overnight_100pct: PASSED (floor={floor:.2f} < base keep 1.5)")


def test_r48_latches_once_engaged():
    """R48 must latch once engaged for the day — don't toggle on flickering pv_covering.

    Live regression 2026-04-25: R48 fired/un-fired 5 times in 4 hours
    (06:11-09:58 BST) because actual_pv-actual_load oscillated around the
    PV_MARGIN_KW=0.5 threshold in cloudy morning. Each flicker re-evaluated
    pv_covering and toggled effective_keep between 0.5 and 1.5 kWh.

    Fix: once R48 has fired today, latch _r48_engaged_today=True and keep
    using RELAXED_KEEP_KWH until _keep_recovered fires (battery has
    completed its drain cycle and risen back to base keep).
    """
    from datetime import datetime, timezone

    pv, load = _make_overflow_pv(minutes_now=420)  # 07:00 local morning
    sensor_overrides = {
        "sensor.sigen_plant_pv_power": 1.0,  # >= 0.5+load: pv_covering
        "sensor.sigen_plant_consumed_power": 0.4,
    }
    # Big-PV-day fixture: solcast_remaining must exceed (soc_max − soc_kw + load) so
    # R5 will_fill gate passes (battery would overfill — R48 needed for headroom).
    sensor_overrides.update(_make_p90_sensors(solcast_remaining=40.0))
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=BATTERY_KWH * 0.05,  # below soc_keep — sets _keep_drained_today
        minutes_now=420,
        # Match minutes_now=420 (07:00 local) with UTC tz so local_offset=0
        now_utc=datetime(2025, 7, 12, 7, 0, tzinfo=timezone.utc),
        best_soc_keep=1.5,
        sensor_overrides=sensor_overrides,
    )
    plugin = CurtailmentPlugin(base)

    # Cycle 1: pv_covering True (1.0 - 0.4 = 0.6 > 0.5) → R48 fires
    floor1, _ = plugin.calculate(dno_limit_kw=4.0)
    assert plugin._keep_drained_today, "Should be drained_today (soc < keep)"
    assert plugin._r48_engaged_today, "R48 should have engaged"
    assert floor1 < 1.0, f"R48 active should give relaxed keep, got floor={floor1}"

    # Cycle 2: pv_covering becomes False (cloud passes, PV drops below load+0.5)
    base._sensor_overrides["sensor.sigen_plant_pv_power"] = 0.5  # 0.5 - 0.4 = 0.1, below margin
    floor2, _ = plugin.calculate(dno_limit_kw=4.0)
    # WITHOUT latch: floor would jump back to 1.5 (base keep)
    # WITH latch: floor stays at relaxed 0.5
    assert floor2 < 1.0, f"R48 latch must hold once engaged today: pv_covering flickered " f"to False, but floor jumped from {floor1:.2f} to {floor2:.2f}. " f"Latch broken — toggling again."
    print(f"  test_r48_latches_once_engaged: PASSED (floor1={floor1:.2f}, floor2={floor2:.2f}, latch held)")


def test_plugin_floor_not_clamped_by_soc_keep():
    """v31: on a big-overflow day the floor is PURE CURTAILMENT — it is NOT held
    up to soc_keep (R48/Bug-8 relax removed, R55 dropped). So even with a high
    best_soc_keep, the drain target follows overflow_floor low, giving maximum
    headroom. (Big overflow keeps the plugin active — no early handback.)"""
    pv, load = _make_overflow_pv(minutes_now=720)
    sensor_overrides = {
        "sensor.sigen_plant_pv_power": 8.0,
        "sensor.sigen_plant_consumed_power": 1.0,
    }
    sensor_overrides.update(_make_p90_sensors(p90_peak_kw=10.0, solcast_remaining=45.0))
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
    assert phase == "active", f"Expected active on a big overflow day, got {phase}"
    assert floor < 6.0, f"v31: floor must NOT be clamped up to soc_keep (6.0), got {floor:.1f}"
    print(f"  test_plugin_floor_not_clamped_by_soc_keep: PASSED (pure curtailment; floor={floor:.1f})")


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
    """On a big-overflow day with needs_room + pv_covering, R48 correctly
    relaxes keep to 0.5 kWh (not to soc_keep). Floor lower bound is RELAXED_KEEP.
    """
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
    # R48 relaxes keep on a big-overflow + pv_covering scenario — floor
    # clamps to RELAXED_KEEP_KWH (0.5), not soc_keep_base (8).
    assert floor >= 0.5, f"Floor {floor:.2f} should be >= RELAXED_KEEP (0.5)"
    assert floor < soc_keep, f"R48 must relax keep on big overflow: floor {floor:.2f} < soc_keep {soc_keep:.2f} expected"
    print(f"  test_floor_clamped_above_soc_keep: PASSED (floor={floor:.2f}kWh; R48 relaxed below keep={soc_keep:.2f})")


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
    assert plugin.last_phase == "active", "plugin should be active"

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
    assert plugin.last_phase == "off", "plugin should be off"
    print("  test_on_update_stays_off_low_pv: PASSED")


def test_holds_past_safe_time_until_sundown():
    """v32 (supersedes RD6 deactivate-at-safe_time): past safe_time with PV still
    flowing (>0.1), the plugin stays ACTIVE and Holds — it must NOT hand back to
    Predbat/MSC while PV flows (that round-trips the excess). It only deactivates
    at sundown (PV≈0).
    """
    from datetime import datetime, timezone

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
        best_soc_keep=4.0,
        now_utc=datetime(2025, 7, 12, 12, 0, tzinfo=timezone.utc),
        sensor_overrides=sensor_overrides,
    )
    plugin = CurtailmentPlugin(base)
    plugin.on_update()
    assert plugin.last_phase == "active", "Should activate at noon (mid-overflow)"

    # Past safe_time but PV still 0.3 kW (>0.1) → stay active + Hold, not off.
    base.minutes_now = 18 * 60 + 30
    base.now_utc = datetime(2025, 7, 12, 18, 30, tzinfo=timezone.utc)
    base._sensor_overrides["sensor.sigen_plant_pv_power"] = 0.3
    base.services.clear()
    plugin.base = base
    plugin.on_update()
    assert plugin.last_phase == "active", "v32: stay active past safe_time while PV flows"
    assert plugin._policy_override == "no_drain", f"v32.1: no_drain past safe_time, got {plugin._policy_override}"

    # Now PV falls to ≈0 AND the sun is genuinely down → deactivate.
    # 20:00 UTC on doy 193 = 4.9 deg elevation. (Was 19:00 UTC = 12.4 deg, which
    # O1 now correctly refuses to call sundown — at that height PV≈0 is a cloud.)
    base._sensor_overrides["sensor.sigen_plant_pv_power"] = 0.05
    base.minutes_now = 21 * 60
    base.now_utc = datetime(2025, 7, 12, 20, 0, tzinfo=timezone.utc)
    plugin.on_update()
    assert plugin.last_phase == "off", "v32: deactivate at sundown (PV≈0, sun down)"
    print("  test_holds_past_safe_time_until_sundown: PASSED")


def test_sundown_defers_while_a_saving_session_is_live():
    """Sundown must NOT hand back mid saving-session.

    Live 2026-08-03: a joined session ran 19:00-20:00. PV fell through the 0.1 kW
    sundown threshold at 19:37:40, so at 19:40:16 CM deactivated, disabled the
    heartbeat and handed back — killing the dump with 20 minutes of the paid
    window left. Export went 3.7 kW -> 0. (Compounded by the handback's
    `read_only -> False` write not taking, which left NO writer at all.)

    The heartbeat can only force Max Export while CM holds the wheel and the
    select is not `Predbat` (RD14c). So deactivating during a session does not
    merely change who reports — it stops the sell.

    Deliberately NOT fixed by pinning the select to Max Export from the plugin:
    that is exactly what RD14c removed, and it caused the 5 min 46 s over-run at
    session end. CM just stays active; the heartbeat's calendar rule keeps
    dispatching, and sundown lands normally on the first cycle after the session.
    """
    from datetime import datetime, timezone

    pv, load = _make_overflow_pv(minutes_now=720)
    sensor_overrides = {"sensor.sigen_plant_pv_power": 8.0, "sensor.sigen_plant_consumed_power": 1.0}
    sensor_overrides.update(_make_p90_sensors())
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=BATTERY_KWH * 0.40,
        minutes_now=720,
        best_soc_keep=4.0,
        now_utc=datetime(2025, 7, 12, 12, 0, tzinfo=timezone.utc),
        sensor_overrides=sensor_overrides,
    )
    plugin = CurtailmentPlugin(base)
    plugin.on_update()
    assert plugin.last_phase == "active"

    # Genuine dusk DURING a live session: PV below 0.1 kW *and* the sun below the
    # O1 elevation gate (20:00 UTC on doy 193 = 4.9 deg), so the session is the
    # only thing keeping CM active — not the gate.
    base._sensor_overrides["sensor.sigen_plant_pv_power"] = 0.03
    base._sensor_overrides[SIG_SAVING_SESSION_CALENDAR] = "on"
    base.minutes_now = 21 * 60
    base.now_utc = datetime(2025, 7, 12, 20, 0, tzinfo=timezone.utc)
    plugin.on_update()
    assert plugin.last_phase == "active", "must not hand back while a joined session is still dumping"

    # The plugin must NOT seize the select to force the dump — that is the
    # heartbeat's job off the calendar, and pinning it here re-creates the
    # RD14c end-of-session over-run.
    assert plugin._policy_override != "max_export", "dispatch stays with the heartbeat (RD14c)"

    # Session ends -> the very next cycle hands back normally.
    base._sensor_overrides[SIG_SAVING_SESSION_CALENDAR] = "off"
    base.minutes_now = 21 * 60 + 10
    base.now_utc = datetime(2025, 7, 12, 20, 10, tzinfo=timezone.utc)
    plugin.on_update()
    assert plugin.last_phase == "off", "once the session ends, sundown deactivates as before"
    print("  test_sundown_defers_while_a_saving_session_is_live: PASSED")


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
    assert plugin.last_phase == "off", "plugin should be off when deferring"
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
# R54 / R56 / R57: target = max(min(curt, keep), reserve), runs until PV=0,
# no 100% chase (v20)
# ============================================================================


def test_R54_target_uses_keep_when_lower_than_overflow_floor():
    """R54: target = min(overflow_floor, effective_keep). When overflow is
    small (overflow_floor high), target falls to effective_keep — replaces
    R45 100% chase.

    Scenario: a genuine but SMALL overflow window (peak just over the export
    threshold, so safe_time is still ahead → plugin active per RD6), with modest
    Solcast remaining → overflow_floor stays high → min() picks effective_keep.
    """
    pv = {m: 5.5 for m in range(0, 240, PLUGIN_STEP)}
    load = {m: 0.5 for m in range(0, 240, PLUGIN_STEP)}
    sensor_overrides = {
        "sensor.sigen_plant_pv_power": 5.5,
        "sensor.sigen_plant_consumed_power": 0.5,
    }
    sensor_overrides.update(_make_p90_sensors(p90_peak_kw=5.5, solcast_remaining=12.0))
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=10.0,
        minutes_now=720,
        best_soc_keep=4.0,  # 22% — well below overflow_floor
        sensor_overrides=sensor_overrides,
    )
    plugin = CurtailmentPlugin(base)
    floor, phase = plugin.calculate(dno_limit_kw=4.0)
    # v32: this small overflow already fits the battery headroom → the plugin stays
    # ACTIVE and Holds (battery flat, export surplus at cap), NOT deactivate to MSC
    # (the v31 early-handback round-tripped PV on 2026-07-20).
    assert phase == "active", f"v32: small overflow fits → active + Hold (not off), got {phase}"
    assert plugin._policy_override == "no_drain", f"v32.1: overflow-fits → no_drain override, got {plugin._policy_override}"
    print("  test_R54_target_uses_keep_when_lower_than_overflow_floor: PASSED (active + Hold)")


def test_R54_target_uses_overflow_when_lower_than_keep():
    """R54: when overflow_floor < effective_keep (big-overflow day), target
    follows overflow_floor (curtailment wins, with R48 latch relaxing keep).
    """
    pv = {m: 8.0 for m in range(0, 360, PLUGIN_STEP)}
    load = {m: 1.0 for m in range(0, 360, PLUGIN_STEP)}
    sensor_overrides = {
        "sensor.sigen_plant_pv_power": 8.0,
        "sensor.sigen_plant_consumed_power": 1.0,
    }
    sensor_overrides.update(_make_p90_sensors(p90_peak_kw=10.0, solcast_remaining=30.0))
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=2.0,  # below soc_keep so R48 latch can engage on this morning
        minutes_now=720,
        best_soc_keep=6.0,
        sensor_overrides=sensor_overrides,
    )
    plugin = CurtailmentPlugin(base)
    floor, phase = plugin.calculate(dno_limit_kw=4.0)
    assert phase == "active", f"Should be active, got {phase}"
    # R48 should engage (big overflow + pv covering load), relaxing keep to
    # 0.5 kWh. R54 then min(overflow_floor, 0.5) = ~0.5 because overflow
    # is huge → overflow_floor is very low.
    assert floor <= 0.5 + 0.01, f"R54+R48: target should be ~0.5 kWh on huge-overflow day, got {floor:.2f}"
    print(f"  test_R54_target_uses_overflow_when_lower_than_keep: PASSED (floor={floor:.2f})")


def test_R57_no_chase_to_soc_max_late_in_day():
    """R57: late in PV window with overflow nearly done, plugin no longer
    chases soc_max via the R45 taper. Floor stays at effective_keep.
    """
    from datetime import datetime, timezone

    minutes_now = 1080  # 18:00 BST = 17:00 UTC, near end of PV
    pv = {m: 1.5 for m in range(0, 90, PLUGIN_STEP)}
    load = {m: 0.5 for m in range(0, 90, PLUGIN_STEP)}
    sensor_overrides = {
        "sensor.sigen_plant_pv_power": 1.5,
        "sensor.sigen_plant_consumed_power": 0.5,
    }
    sensor_overrides.update(_make_p90_sensors(p90_peak_kw=8.0, solcast_remaining=2.0))
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=BATTERY_KWH * 0.92,
        minutes_now=minutes_now,
        best_soc_keep=4.0,
        now_utc=datetime(2025, 7, 12, 17, 0, tzinfo=timezone.utc),
        sensor_overrides=sensor_overrides,
    )
    plugin = CurtailmentPlugin(base)
    floor, phase = plugin.calculate(dno_limit_kw=4.0)
    assert phase == "active", f"plugin still active before safe_time, got {phase}"
    # v31: R57's cap is REMOVED — evening drain-to-reserve is Predbat's job, so the
    # R45 taper fills the battery near safe_time and the floor DOES approach soc_max.
    assert floor > BATTERY_KWH * 0.5, f"v31: floor tapers toward soc_max near safe_time, got {floor:.2f}"
    print(f"  test_R57_no_chase_to_soc_max_late_in_day: PASSED (R45 taper — floor={floor:.2f} kWh = {floor / BATTERY_KWH * 100:.0f}%)")


def test_off_at_sundown_backstop():
    """RD6 backstop: when peak PV was observed today AND actual PV is now ~0,
    plugin deactivates (sundown). This is the fallback for days where safe_time
    can't be computed; safe_time (R6/RD6) is the primary trigger. Hands back to
    Predbat for overnight.
    """
    from datetime import datetime, timezone

    pv = {m: 0.0 for m in range(0, 60, PLUGIN_STEP)}
    load = {m: 0.5 for m in range(0, 60, PLUGIN_STEP)}
    sensor_overrides = {
        "sensor.sigen_plant_pv_power": 0.0,  # PV is gone
        "sensor.sigen_plant_consumed_power": 0.5,
    }
    sensor_overrides.update(_make_p90_sensors(p90_peak_kw=8.0, solcast_remaining=0.0))
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=BATTERY_KWH * 0.40,
        minutes_now=1260,  # 21:00 BST
        best_soc_keep=4.0,
        now_utc=datetime(2025, 7, 12, 20, 0, tzinfo=timezone.utc),
        sensor_overrides=sensor_overrides,
    )
    plugin = CurtailmentPlugin(base)
    plugin._peak_pv = 7.5  # had PV earlier today
    floor, phase = plugin.calculate(dno_limit_kw=4.0)
    assert phase == "off", f"R56: sundown (peak observed, actual PV=0) → off, got {phase}"
    print("  test_off_at_sundown_backstop: PASSED (off, hands back to Predbat)")


def test_no_dusk_reactivation_after_peak_reset():
    """v32 REGRESSION: the observed peak PERSISTS through the evening (no evening
    reset), so once PV drops to ≈0 the plugin deactivates at sundown and stays off
    — it must not strand active overnight. v32 removed the past_safe deactivation,
    so this invariant now rests on peak persistence + the sundown (PV<0.1) trigger.
    """
    from datetime import datetime, timezone

    pv = {m: 0.0 for m in range(0, 60, PLUGIN_STEP)}
    load = {m: 0.5 for m in range(0, 60, PLUGIN_STEP)}
    sensor_overrides = {
        "sensor.sigen_plant_pv_power": 0.05,  # night: <0.1 → sundown
        "sensor.sigen_plant_consumed_power": 0.5,
    }
    sensor_overrides.update(_make_p90_sensors(p90_peak_kw=8.0, solcast_remaining=0.0))
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=BATTERY_KWH * 0.40,
        minutes_now=1210,  # 20:10 BST — past the old 1200 peak-reset threshold
        best_soc_keep=4.0,
        now_utc=datetime(2025, 7, 12, 20, 0, tzinfo=timezone.utc),
        sensor_overrides=sensor_overrides,
    )
    plugin = CurtailmentPlugin(base)
    plugin._peak_pv = 7.5  # real peak earlier today, persists into the evening
    floor, phase = plugin.calculate(dno_limit_kw=4.0)
    assert phase == "off", f"sundown must deactivate at night (peak persists), got {phase}"
    assert plugin._peak_pv == 7.5, "v32: peak must persist through the evening (no evening reset)"
    print("  test_no_dusk_reactivation_after_peak_reset: PASSED (off at sundown)")


# ============================================================================
# R55: overnight_target sensor tests (v20)
# ============================================================================


def test_R55_overnight_target_published_on_overflow_day():
    """on_before_plan publishes overnight_target with safety_pct + soc_keep attrs."""
    pv, load = _make_overflow_pv(minutes_now=720)
    base = MockBase(pv_step=pv, load_step=load, soc_kw=5.0, minutes_now=720)
    plugin = CurtailmentPlugin(base)
    plugin.on_before_plan({"best_soc_keep": 6.0})
    # _refresh_overnight_target (called from calculate) is the sole writer
    # of the overnight_target sensor. on_before_plan only affects soc_keep.
    try:
        plugin.calculate(dno_limit_kw=4.0)
    except Exception:
        pass

    entity = "sensor.predbat_curtailment_overnight_target"
    assert entity in base.published, f"R55 sensor must be published, got entities {list(base.published.keys())}"
    pub = base.published[entity]
    assert "morning_gap_kwh" in pub["attrs"]
    assert "safety_pct" in pub["attrs"], f"safety_pct attr missing: {pub['attrs']}"
    assert "soc_keep_kwh" in pub["attrs"], f"soc_keep_kwh attr missing: {pub['attrs']}"
    assert "soc_pct" in pub["attrs"]
    assert pub["attrs"]["safety_pct"] == 0.0, f"OVERNIGHT_SAFETY_PCT_DEFAULT should be 0, got {pub['attrs']['safety_pct']}"
    print(f"  test_R55_overnight_target_published_on_overflow_day: PASSED (value={pub['value']} kWh)")


def test_R55_overnight_target_formula_components():
    """R55 formula: target = morning_gap × (1 + safety_pct/100) + soc_keep.

    Realistic case: morning_gap≈2 kWh, soc_keep=6 kWh, safety_pct=0%.
    Expected: 2.0 × 1.0 + 6.0 = 8.0 kWh.
    """
    pv = {}
    load = {}
    for m in range(0, 1440, PLUGIN_STEP):
        pv[m] = 6.0 if m >= 240 else 0.0
        load[m] = 0.5
    base = MockBase(pv_step=pv, load_step=load, soc_kw=10.0, minutes_now=0, soc_max=18.08)
    plugin = CurtailmentPlugin(base)
    plugin.on_before_plan({"best_soc_keep": 6.0})
    # _refresh_overnight_target (called from calculate) is the sole writer
    # of the overnight_target sensor. on_before_plan only affects soc_keep.
    try:
        plugin.calculate(dno_limit_kw=4.0)
    except Exception:
        pass

    entity = "sensor.predbat_curtailment_overnight_target"
    pub = base.published[entity]
    morning_gap = pub["attrs"]["morning_gap_kwh"]
    safety_pct = pub["attrs"]["safety_pct"]
    soc_keep = pub["attrs"]["soc_keep_kwh"]
    expected = morning_gap * (1 + safety_pct / 100.0) + soc_keep
    assert abs(pub["value"] - expected) < 0.05, f"target should be morning_gap × (1 + {safety_pct}%) + soc_keep = {expected:.2f}, got {pub['value']:.2f}"
    print(f"  test_R55_overnight_target_formula_components: PASSED (gap={morning_gap:.2f} + keep={soc_keep:.2f} = {pub['value']:.2f} kWh)")


def test_R55_overnight_target_window_extended_to_tomorrow():
    """Window extended to forecast_minutes (24h) so morning_gap covers tonight + overnight + dawn."""
    # Set up a day where today_solar_end window misses overnight load:
    # - now = 18:00 BST (1080 min = 18h). today_solar_end = 23:00 = 5h forward.
    # - PV: 0 from now (sundown), forecast tomorrow 06:00 onwards.
    # - Load: 0.5 kW constant.
    # Old (window=today_solar_end): morning_gap ≈ 0.5 × 5h = 2.5 kWh
    # New (window=forecast_minutes): morning_gap ≈ 0.5 × 12h = 6.0 kWh (tonight 5h + midnight to dawn 7h)
    pv = {}
    load = {}
    # Forecast covers from "now" forward — minutes from now.
    for m in range(0, 1440, PLUGIN_STEP):
        # Tomorrow's PV starts at minute 720 (12h from now = 06:00 BST tomorrow)
        pv[m] = 3.0 if m >= 720 else 0.0
        load[m] = 0.5
    base = MockBase(pv_step=pv, load_step=load, soc_kw=10.0, minutes_now=1080, soc_max=18.08, forecast_minutes=1440)
    plugin = CurtailmentPlugin(base)
    try:
        plugin.calculate(dno_limit_kw=4.0)
    except Exception:
        pass

    pub = base.published["sensor.predbat_curtailment_overnight_target"]
    morning_gap = pub["attrs"]["morning_gap_kwh"]
    # 12 hours × 0.5 kW = 6 kWh deficit until tomorrow's PV catches up
    assert morning_gap >= 5.0, f"Window extension: morning_gap should cover overnight ≥5 kWh, got {morning_gap:.2f}"
    print(f"  test_R55_overnight_target_window_extended_to_tomorrow: PASSED (gap={morning_gap:.2f} kWh covers overnight)")


def test_R55_overnight_target_published_when_no_overflow():
    """Even on no-overflow days, overnight_target is published."""
    pv = {}
    load = {}
    for m in range(0, 1440, PLUGIN_STEP):
        pv[m] = 2.0
        load[m] = 3.0
    base = MockBase(pv_step=pv, load_step=load, soc_kw=10.0, minutes_now=0)
    plugin = CurtailmentPlugin(base)
    plugin.on_before_plan({"best_soc_keep": 6.0})
    # _refresh_overnight_target (called from calculate) is the sole writer
    # of the overnight_target sensor. on_before_plan only affects soc_keep.
    try:
        plugin.calculate(dno_limit_kw=4.0)
    except Exception:
        pass

    entity = "sensor.predbat_curtailment_overnight_target"
    assert entity in base.published
    pub = base.published[entity]
    assert "morning_gap_kwh" in pub["attrs"]
    print(f"  test_R55_overnight_target_published_when_no_overflow: PASSED (value={pub['value']} kWh)")


def test_R55_overnight_target_published_from_calculate_with_real_pv_step():
    """The live architecture: pv_forecast_minute_step is populated when
    calculate() runs (via on_update hook AFTER calculate_plan), but EMPTY
    when on_before_plan runs (BEFORE calculate_plan, predbat wipes pv_step
    at end of each update_pred cycle).

    Verifies: overnight_target sensor publishes morning_gap > 0 when
    calculate() runs with populated pv_step.
    """
    from datetime import datetime, timezone

    pv = {}
    load = {}
    # PV ramp down to 0, then 0 until tomorrow's morning ramp.
    for m in range(0, 1440, PLUGIN_STEP):
        if m < 300:
            pv[m] = 1.5
        elif m < 1080:
            pv[m] = 0.0
        else:
            pv[m] = 1.5
        load[m] = 0.5
    sensor_overrides = {"sensor.sigen_plant_pv_power": 1.5, "sensor.sigen_plant_consumed_power": 0.5}
    sensor_overrides.update(_make_p90_sensors(p90_peak_kw=2.0, solcast_remaining=4.0))
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=BATTERY_KWH * 0.50,
        minutes_now=720,
        best_soc_keep=4.0,
        forecast_minutes=1440,
        now_utc=datetime(2025, 7, 12, 12, 0, tzinfo=timezone.utc),
        sensor_overrides=sensor_overrides,
    )
    plugin = CurtailmentPlugin(base)
    plugin.calculate(dno_limit_kw=4.0)

    entity = "sensor.predbat_curtailment_overnight_target"
    assert entity in base.published, "R55 sensor must publish from calculate()"
    pub = base.published[entity]
    morning_gap = pub["attrs"]["morning_gap_kwh"]
    assert morning_gap > 0.5, f"R55 must compute morning_gap from calculate() with pv_step populated. Got morning_gap={morning_gap}"
    assert pub["attrs"]["source"] == "calculate", f"source should be 'calculate', got {pub['attrs']['source']}"
    print(f"  test_R55_overnight_target_published_from_calculate_with_real_pv_step: PASSED (morning_gap={morning_gap:.2f}, target={pub['value']:.2f})")


def test_R55_overnight_target_no_plan_yet_fallback():
    """Pre-startup case: no plan computed yet, pv_forecast_minute_step empty.
    Should still publish the sensor (with soc_keep value, source=no_plan_yet)
    so the dashboard isn't blank.
    """
    from datetime import datetime, timezone

    base = MockBase(
        pv_step={},  # empty — pre-startup
        load_step={},
        soc_kw=BATTERY_KWH * 0.50,
        minutes_now=720,
        best_soc_keep=4.0,
        forecast_minutes=1440,
        now_utc=datetime(2025, 7, 12, 12, 0, tzinfo=timezone.utc),
    )
    plugin = CurtailmentPlugin(base)
    # calculate() returns early on lat/lon=0 since no zone.home overrides — but
    # _refresh_overnight_target runs FIRST and should publish.
    try:
        plugin.calculate(dno_limit_kw=4.0)
    except Exception:
        pass

    entity = "sensor.predbat_curtailment_overnight_target"
    assert entity in base.published, "Sensor must publish even pre-startup (no_plan_yet fallback)"
    pub = base.published[entity]
    assert pub["attrs"]["source"] == "no_plan_yet", f"Expected no_plan_yet source, got {pub['attrs']['source']}"
    assert pub["attrs"]["morning_gap_kwh"] == 0.0
    print(f"  test_R55_overnight_target_no_plan_yet_fallback: PASSED (target={pub['value']:.2f}, source=no_plan_yet)")


def test_R55_on_before_plan_does_not_clobber_calculate_overnight_target():
    """Bug 2026-05-03 (live observation): on_before_plan's no_pv_forecast fallback
    overwrites _overnight_target_kwh and the published sensor with soc_keep when
    pv_step is empty, clobbering the correct value set by
    calculate()/_refresh_overnight_target.

    Predbat lifecycle: pv_step is wiped at end of update_pred. on_before_plan in
    the next cycle ALWAYS sees empty pv_step. The fallback must NOT corrupt the
    state from the previous calculate() — otherwise floor calc uses soc_keep
    (e.g. 0.5 kWh) as the overnight target, draining the battery to ~5%."""
    from datetime import datetime, timezone

    pv = {}
    load = {}
    for m in range(0, 1440, PLUGIN_STEP):
        if m < 300:
            pv[m] = 1.5
        elif m < 1080:
            pv[m] = 0.0
        else:
            pv[m] = 1.5
        load[m] = 0.5
    sensor_overrides = {"sensor.sigen_plant_pv_power": 1.5, "sensor.sigen_plant_consumed_power": 0.5}
    sensor_overrides.update(_make_p90_sensors(p90_peak_kw=2.0, solcast_remaining=4.0))

    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=BATTERY_KWH * 0.50,
        minutes_now=720,
        best_soc_keep=4.0,
        forecast_minutes=1440,
        now_utc=datetime(2025, 7, 12, 12, 0, tzinfo=timezone.utc),
        sensor_overrides=sensor_overrides,
    )
    plugin = CurtailmentPlugin(base)

    # Step 1: calculate() runs with populated pv_step → publishes correct value.
    plugin.calculate(dno_limit_kw=4.0)
    target_after_calculate = plugin._overnight_target_kwh
    sensor_after_calculate = base.published["sensor.predbat_curtailment_overnight_target"]
    assert sensor_after_calculate["attrs"]["source"] == "calculate", f"calculate() must publish source='calculate', " f"got {sensor_after_calculate['attrs']['source']}"
    assert target_after_calculate > 0.5, f"calculate() should set overnight_target > soc_keep_fallback (got {target_after_calculate})"

    # Step 2: simulate Predbat wiping pv_step at end of update_pred.
    base.pv_forecast_minute_step = {}

    # Step 3: next cycle's on_before_plan runs with empty pv_step.
    plugin.on_before_plan({"best_soc_keep": 4.0})

    # In-memory state from calculate() must be preserved.
    assert plugin._overnight_target_kwh == target_after_calculate, f"on_before_plan must not overwrite _overnight_target_kwh " f"(was {target_after_calculate}, now {plugin._overnight_target_kwh})"

    # Published sensor must still reflect calculate()'s value.
    pub = base.published["sensor.predbat_curtailment_overnight_target"]
    assert pub["attrs"]["source"] == "calculate", f"on_before_plan's no_pv_forecast fallback must not republish over " f"calculate()'s value (source now '{pub['attrs']['source']}')"
    print(f"  test_R55_on_before_plan_does_not_clobber_calculate_overnight_target: PASSED " f"(overnight_target preserved at {plugin._overnight_target_kwh:.2f} kWh)")


def test_R55_target_soc_uses_overnight_when_off():
    """When plugin is Off, target_soc sensor reports overnight_target instead
    of soc_max (the placeholder). Phase tile already says Off so no info lost."""
    from datetime import datetime, timezone

    base = MockBase(
        pv_step={},
        load_step={},
        soc_kw=BATTERY_KWH * 0.7,
        minutes_now=1260,  # 21:00 BST
        best_soc_keep=4.0,
        forecast_minutes=1440,
        now_utc=datetime(2025, 7, 12, 20, 0, tzinfo=timezone.utc),
    )
    plugin = CurtailmentPlugin(base)
    plugin._overnight_target_kwh = 7.0  # simulate cached overnight need
    plugin.publish("off", BATTERY_KWH, dno_limit_kw=4.0)

    pub = base.published["sensor.predbat_curtailment_target_soc"]
    expected_pct = 7.0 / BATTERY_KWH * 100
    assert abs(pub["value"] - round(expected_pct, 1)) < 0.05, f"Expected target_soc≈{expected_pct:.1f}% (overnight target), got {pub['value']}"
    assert abs(pub["attrs"]["target_kwh"] - 7.0) < 0.01, f"target_kwh attr should match overnight_target, got {pub['attrs']['target_kwh']}"
    print(f"  test_R55_target_soc_uses_overnight_when_off: PASSED (target_soc={pub['value']}% / {pub['attrs']['target_kwh']} kWh)")


def test_R55_target_soc_uses_floor_when_active():
    """When plugin is Active, target_soc sensor reports the live floor (the
    drain target for curtailment), not overnight_target."""
    from datetime import datetime, timezone

    base = MockBase(
        pv_step={},
        load_step={},
        soc_kw=BATTERY_KWH * 0.5,
        minutes_now=720,
        best_soc_keep=4.0,
        forecast_minutes=1440,
        now_utc=datetime(2025, 7, 12, 12, 0, tzinfo=timezone.utc),
    )
    plugin = CurtailmentPlugin(base)
    plugin._overnight_target_kwh = 7.0
    floor_kwh = 2.5  # active drain target (lower than overnight)
    plugin.publish("active", floor_kwh, dno_limit_kw=4.0)

    pub = base.published["sensor.predbat_curtailment_target_soc"]
    expected_pct = floor_kwh / BATTERY_KWH * 100
    assert abs(pub["value"] - round(expected_pct, 1)) < 0.05, f"Active: target_soc should equal floor%, got {pub['value']}"
    assert abs(pub["attrs"]["target_kwh"] - floor_kwh) < 0.01
    print(f"  test_R55_target_soc_uses_floor_when_active: PASSED (target_soc={pub['value']}% / {pub['attrs']['target_kwh']} kWh)")


def test_numeric_sensors_carry_state_class():
    """Every numeric curtailment sensor must carry state_class.

    WHY THIS EXISTS (2026-07-29): HA keeps 5-minute recorder history for only
    ~10 days, then retains hourly long-term statistics FOREVER — but *only*
    for sensors with a state_class. Without it a numeric diagnostic becomes
    unrecoverable once the recorder window passes.

    That bit us: reviewing the R9 overflow safety factor, the only surviving
    forecast-vs-actual evidence was three-month-old April fixtures captured by
    hand. Those turned out to be measured through the AC-coupled SMA, which
    clipped PV above the inverter ceiling and so understated actual overflow —
    making p90 look 56% conservative when the first DC-coupled day measured
    16%. A recommendation to trim the factor was built on that artefact.

    Retention is therefore a correctness property, not housekeeping.

    Categorical (string-valued) sensors cannot carry a state_class; they are
    listed explicitly so that adding one is a deliberate act rather than an
    accident. If you add a numeric sensor, give it a state_class — do not add
    it to this list.
    """
    from datetime import datetime, timezone

    categorical = {
        "sensor.predbat_curtailment_phase",
        "sensor.predbat_curtailment_floor_source",
        "sensor.predbat_curtailment_intended_policy",
        "sensor.predbat_curtailment_tomorrow",
    }

    base = MockBase(
        pv_step={},
        load_step={},
        soc_kw=BATTERY_KWH * 0.5,
        minutes_now=720,
        best_soc_keep=4.0,
        forecast_minutes=1440,
        now_utc=datetime(2025, 7, 12, 12, 0, tzinfo=timezone.utc),
    )
    plugin = CurtailmentPlugin(base)
    plugin._overnight_target_kwh = 7.0
    plugin.publish("active", 2.5, dno_limit_kw=4.0)
    # These two publish outside publish() — cover them in the same sweep.
    plugin._publish_offset(-1.5, {"reason": "state_class_audit"})
    plugin._publish_overnight_target(7.0, {})

    missing = []
    for entity, pub in base.published.items():
        if entity in categorical:
            continue
        if isinstance(pub["value"], bool) or not isinstance(pub["value"], (int, float)):
            continue
        if "state_class" not in (pub["attrs"] or {}):
            missing.append(entity)

    assert not missing, "numeric sensors published without state_class — HA will not keep long-term statistics for these, so they are lost after the recorder window:\n  " + "\n  ".join(sorted(missing))
    checked = sum(1 for e, p in base.published.items() if e not in categorical and isinstance(p["value"], (int, float)) and not isinstance(p["value"], bool))
    print(f"  test_numeric_sensors_carry_state_class: PASSED ({checked} numeric sensors all carry state_class)")


def test_floor_component_is_winner_matches_source_label():
    """The floor-component sensors' is_winner must match the real floor_source.

    WHY: compute_floor_with_source() returns a HUMAN-READABLE winner label
    ("Curtailment Buffer" / "P10 Recovery" / "Reserve"), not the variable name.
    A first cut of these sensors derived is_winner from the entity suffix
    ("overflow", "p10_recovery"), which could never match any real label — so
    is_winner would have read False forever, silently, on every sensor. A flag
    that is always False looks exactly like "this term never wins".
    """
    from datetime import datetime, timezone

    base = MockBase(
        pv_step={},
        load_step={},
        soc_kw=BATTERY_KWH * 0.5,
        minutes_now=720,
        best_soc_keep=4.0,
        forecast_minutes=1440,
        now_utc=datetime(2025, 7, 12, 12, 0, tzinfo=timezone.utc),
    )
    plugin = CurtailmentPlugin(base)
    plugin._overflow_floor_kwh = 6.0
    plugin._p10_recovery_floor = 2.0

    for source_label, winner_entity in (
        ("Curtailment Buffer", "sensor.predbat_curtailment_floor_overflow"),
        ("P10 Recovery", "sensor.predbat_curtailment_floor_p10_recovery"),
    ):
        plugin._floor_source = source_label
        plugin.publish("active", 6.0, dno_limit_kw=4.0)
        flags = {e: base.published[e]["attrs"]["is_winner"] for e in ("sensor.predbat_curtailment_floor_overflow", "sensor.predbat_curtailment_floor_p10_recovery")}
        assert flags[winner_entity] is True, f"floor_source={source_label!r} should mark {winner_entity} as winner, got {flags}"
        assert sum(1 for v in flags.values() if v) == 1, f"exactly one component may win, got {flags}"

    # And a source that is neither component (e.g. the hardware reserve) must
    # mark neither — not silently fall through to one of them.
    plugin._floor_source = "Reserve"
    plugin.publish("active", 6.0, dno_limit_kw=4.0)
    assert not base.published["sensor.predbat_curtailment_floor_overflow"]["attrs"]["is_winner"]
    assert not base.published["sensor.predbat_curtailment_floor_p10_recovery"]["attrs"]["is_winner"]
    print("  test_floor_component_is_winner_matches_source_label: PASSED (labels match compute_floor_with_source)")


def test_R9_overflow_safety_factor_is_1_05():
    """R9: OVERFLOW_SAFETY_FACTOR is 1.05, not 1.2.

    WHY (2026-07-30, user decision)
    ------------------------------
    The factor multiplies an ALREADY-CONSERVATIVE input. Overflow is fed from
    the p90 Solcast band, and overflow is an integral ABOVE a threshold, so
    forecast conservatism is amplified before the factor is applied at all.
    Measured leverage across the April fixture replay: a 13% generation
    over-forecast became a 36% overflow over-forecast (3.36x), and actual
    overflow never once exceeded the p90-derived estimate in 11 days.

    1.2 on top of that reserved roughly double the headroom actually needed,
    and over-reserving is NOT free -- it is paid for as a deeper pre-dawn drain
    and the overnight import that follows.

    CAVEAT ON THE EVIDENCE, recorded honestly: those April fixtures were
    measured through the AC-coupled SMA, which clipped PV above the inverter
    ceiling and therefore UNDERSTATED actual overflow -- flattering p90. The
    first DC-coupled day measured (19 Jul) showed only 16% margin against p90,
    versus 56% mean in April. So 1.05 is a deliberate step toward the truth
    with the meter now in place to check it, not a number the data has settled.

    HOW TO REFINE THIS -- do not re-derive from fixtures
    ---------------------------------------------------
    As of 2026-07-29 the actual overflow is metered natively and exactly:

        sensor.curtailment_overflow_power    template, max(0, pv - load - cap)
        sensor.curtailment_overflow_energy   Riemann integral of the above
        sensor.curtailment_overflow_daily    utility_meter, daily cycle

    The chain applies the clipping at native sensor resolution and only then
    integrates, so its daily total survives HA's hourly downsampling exactly --
    unlike reconstructing from 5-minute statistics, which understated a
    broken-cloud day by 63% and expires after the ~10-day recorder window.

    To retune: compare sensor.curtailment_overflow_daily (actual) against the
    daily max of sensor.predbat_curtailment_overflow_p90 (forecast) over a few
    weeks of DC-coupled days. The factor should cover the worst observed
    actual/p90 ratio with a little margin. If actual never approaches p90, cut
    it further; if any day exceeds p90, raise it.

    The number that ultimately matters is neither of those, though: it is
    whether we ever actually curtailed (SOC at max AND export at cap) versus
    how much we imported overnight. Forecast calibration is a proxy for that.
    """
    from curtailment_plugin import OVERFLOW_SAFETY_FACTOR

    assert abs(OVERFLOW_SAFETY_FACTOR - 1.05) < 1e-9, f"expected 1.05, got {OVERFLOW_SAFETY_FACTOR}"

    # 10 kWh overflow, 1.8 kWh reserve -> 1.05*10 + min(1.8, 10) = 12.3
    got = required_headroom_kwh(10.0, 1.8, OVERFLOW_SAFETY_FACTOR)
    assert abs(got - 12.3) < 1e-6, f"expected 12.3 kWh required headroom, got {got}"

    # Small-overflow day: the reserve term tapers with overflow (R45), so
    # 1.05*1.0 + min(1.8, 1.0) = 2.05 -- NOT 1.05 + 1.8.
    got = required_headroom_kwh(1.0, 1.8, OVERFLOW_SAFETY_FACTOR)
    assert abs(got - 2.05) < 1e-6, f"expected 2.05 kWh on a small-overflow day, got {got}"

    # The change must actually free headroom: at 10 kWh overflow, 1.5 kWh more
    # room than the old 1.2 -- i.e. 1.5 kWh less pre-dawn drain.
    freed = required_headroom_kwh(10.0, 1.8, 1.2) - required_headroom_kwh(10.0, 1.8, OVERFLOW_SAFETY_FACTOR)
    assert abs(freed - 1.5) < 1e-6, f"expected 1.5 kWh freed vs the old 1.2, got {freed}"
    print(f"  test_R9_overflow_safety_factor_is_1_05: PASSED (frees {freed:.2f} kWh of drain at 10 kWh overflow)")


def test_R55_safety_pct_helper_clamps_range():
    """HA helper input_number.curtailment_overnight_safety_pct clamped to [0, 200]."""
    pv = {}
    load = {}
    for m in range(0, 1440, PLUGIN_STEP):
        pv[m] = 0.0
        load[m] = 0.5
    for tested_pct, expected_pct in [(0.0, 0.0), (30.0, 30.0), (100.0, 100.0), (250.0, 200.0), (-10.0, 0.0)]:
        base = MockBase(
            pv_step=pv,
            load_step=load,
            soc_kw=10.0,
            minutes_now=0,
            sensor_overrides={"input_number.curtailment_overnight_safety_pct": tested_pct},
        )
        plugin = CurtailmentPlugin(base)
        try:
            plugin.calculate(dno_limit_kw=4.0)
        except Exception:
            pass
        pub = base.published["sensor.predbat_curtailment_overnight_target"]
        actual_pct = pub["attrs"]["safety_pct"]
        assert abs(actual_pct - expected_pct) < 0.01, f"safety_pct={tested_pct} should clamp to {expected_pct}, got {actual_pct}"
    print("  test_R55_safety_pct_helper_clamps_range: PASSED")


def test_R55_overnight_target_raises_effective_keep_in_calculate():
    """R55 (v20) integration: overnight_target acts as a floor on
    effective_keep in plugin.calculate().
    """
    from datetime import datetime, timezone

    # Realistic shape: PV during day, no PV at night, ~0.5 kW load.
    # Morning gap will accumulate from sundown (~8h from now if minutes_now=720)
    # through tomorrow's PV catching up.
    pv = {}
    load = {}
    for m in range(0, 1440, PLUGIN_STEP):
        # PV: 1.5 kW from now until ~5h forward, then nothing until tomorrow 6 AM
        # tomorrow morning = roughly minute 1080 onwards from minutes_now=720
        if m < 300:
            pv[m] = 1.5
        elif m < 1080:
            pv[m] = 0.0
        else:
            pv[m] = 1.5
        load[m] = 0.5
    sensor_overrides = {"sensor.sigen_plant_pv_power": 5.5, "sensor.sigen_plant_consumed_power": 0.5}
    sensor_overrides.update(_make_p90_sensors(p90_peak_kw=5.5, solcast_remaining=8.0))
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=BATTERY_KWH * 0.50,
        minutes_now=720,
        best_soc_keep=2.0,
        forecast_minutes=1440,
        now_utc=datetime(2025, 7, 12, 12, 0, tzinfo=timezone.utc),
        sensor_overrides=sensor_overrides,
    )
    plugin = CurtailmentPlugin(base)
    base.best_soc_keep = 2.0

    # _overnight_target_kwh is set by _refresh_overnight_target inside calculate()
    floor, phase = plugin.calculate(dno_limit_kw=4.0)
    cached_target = plugin._overnight_target_kwh
    # overnight_target is STILL computed and feeds the recovery floor
    # (compute_p10_recovery_floor, unit-tested separately). v32: this small overflow
    # fits the battery, so the plugin stays ACTIVE and Holds (was v31 early-handback
    # → off, which round-tripped PV via MSC).
    assert cached_target is not None, "calculate should still cache overnight_target (feeds recovery floor)"
    assert phase == "active", f"v32: small overflow fits → active + Hold, got {phase}"
    assert plugin._policy_override == "no_drain", f"v32.1: overflow-fits → no_drain override, got {plugin._policy_override}"
    print(f"  test_R55_...: PASSED (overnight_target cached={cached_target:.2f}, active+Hold)")


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
    # v20: tests overflow_floor sensitivity. With R54 the floor is
    # min(overflow_floor, effective_keep). Use a high best_soc_keep so the
    # min() is pinned by overflow_floor (i.e. effective_keep > overflow_floor)
    # and the test exercises the overflow_floor sensitivity directly.
    sensor1 = {"sensor.sigen_plant_pv_power": 8.0, "sensor.sigen_plant_consumed_power": 1.0}
    sensor1.update(_make_p90_sensors(p90_peak_kw=6.0, solcast_remaining=12.0))
    base1 = MockBase(pv_step=pv, load_step=load, soc_kw=10.0, minutes_now=720, best_soc_keep=15.0, sensor_overrides=sensor1)
    plugin1 = CurtailmentPlugin(base1)
    floor1, _ = plugin1.calculate(dno_limit_kw=4.0)

    sensor2 = {"sensor.sigen_plant_pv_power": 8.0, "sensor.sigen_plant_consumed_power": 1.0}
    sensor2.update(_make_p90_sensors(p90_peak_kw=10.0, solcast_remaining=20.0))
    base2 = MockBase(pv_step=pv, load_step=load, soc_kw=10.0, minutes_now=720, best_soc_keep=15.0, sensor_overrides=sensor2)
    plugin2 = CurtailmentPlugin(base2)
    floor2, _ = plugin2.calculate(dno_limit_kw=4.0)

    assert floor2 < floor1, f"Higher p90 should give lower floor: p90=10kW floor={floor2:.1f} vs p90=6kW floor={floor1:.1f}"
    print(f"  test_floor_lower_with_more_overflow: PASSED (p90=6kW→{floor1/BATTERY_KWH*100:.0f}%, p90=10kW→{floor2/BATTERY_KWH*100:.0f}%)")


def test_plugin_handles_local_tz_aware_now_utc():
    """Predbat's base.now_utc is named 'now_utc' but is actually local-tz-aware.
    Plugin must convert to real UTC via .astimezone(timezone.utc) — regression
    guard for Bug 11 (timezone fix)."""
    from datetime import datetime, timezone, timedelta

    # Simulate BST: 12:00 BST = 11:00 UTC
    bst_tz = timezone(timedelta(hours=1))
    # base.now_utc is "now in BST" — .hour returns 12 (BST), not 11 (UTC)
    fake_now_local_aware = datetime(2025, 7, 12, 12, 0, tzinfo=bst_tz)

    pv, load = _make_overflow_pv(minutes_now=720)
    sensor_overrides = {
        "sensor.sigen_plant_pv_power": 5.0,
        "sensor.sigen_plant_consumed_power": 0.5,
    }
    sensor_overrides.update(_make_p90_sensors())
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=5.0,
        minutes_now=720,  # 12:00 local BST
        now_utc=fake_now_local_aware,
        sensor_overrides=sensor_overrides,
    )
    plugin = CurtailmentPlugin(base)
    floor, phase = plugin.calculate(dno_limit_kw=4.0)

    # local_offset must compute +1 for BST (not 0 — that would mean we treated
    # local as UTC). safe_time should reflect correct geometry.
    # We test this indirectly: with correct TZ handling, the safe_time string
    # should be a reasonable late-afternoon time (17:00-20:00 BST range for July).
    assert plugin._safe_time_str != "none", "safe_time should be computed"
    hh = int(plugin._safe_time_str.split(":")[0])
    assert 17 <= hh <= 22, f"safe_time {plugin._safe_time_str} should be late-afternoon BST (17-22h) for July overflow day — got hour {hh}"
    print(f"  test_plugin_handles_local_tz_aware_now_utc: PASSED (safe_time={plugin._safe_time_str} BST)")


# ============================================================================
# Integration test — runs ACTUAL plugin.calculate() against CSV data
# Physics simulation is independent of algorithm code.
# This catches bugs that the v10 simulation misses (same-code problem).
# ============================================================================


def _integration_test_day(label, filename, watts, start_soc_pct=None, forecast_scale=1.0, forecast_scale_fn=None, min_sunset_soc=80, best_soc_keep=4.0):
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
            best_soc_keep=best_soc_keep,
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
                hold_cap = DNO_LIMIT  # v30: export always capped at DNO (hardware); plugin no longer sets export_target
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

    # v20 R57: plugin no longer chases 100% by sunset. Sunset SOC may end
    # near best_soc_keep on no-overflow days, well above on big-overflow
    # days, or below on cloudy evenings where PV stops while battery is
    # still serving load (sunset_soc is captured at the LAST PV slot, so
    # evening drain through to that slot is reflected). The old "≥80% /
    # ≥95%" checks are gone — they were artefacts of R45.
    # Safety properties enforced above (max_export ≤ DNO; curtailment
    # bounded) are what matters for v20.

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
            best_soc_keep=4.0,
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
                hold_cap = DNO_LIMIT  # v30: export always capped at DNO (hardware); plugin no longer sets export_target
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
    # v20 R57: no chase to 100%. Sunset SOC may be near best_soc_keep on
    # cloudy days (drained to overnight target) or higher on big-overflow
    # days where PV refilled the battery faster than evening drain. The
    # old hardcoded "≥75% / ≥95%" thresholds are gone.

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
    """Plugin is active mid-day when there is REAL overflow to manage (p90
    overflow does not yet fit the battery). v31: the early-handback would fire on
    a tiny-overflow day, so this asserts the genuine-overflow case stays active.
    """
    pv = {m: 8.0 for m in range(0, 480, PLUGIN_STEP)}
    load = {m: 1.0 for m in range(0, 480, PLUGIN_STEP)}
    soc_kw = BATTERY_KWH * 0.55  # less headroom, so the big p90 overflow does NOT fit
    sensor_overrides = {
        "sensor.sigen_plant_pv_power": 8.0,
        "sensor.sigen_plant_consumed_power": 1.0,
    }
    sensor_overrides.update(_make_p90_sensors(p90_peak_kw=10.0, solcast_remaining=45.0))
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=soc_kw,
        minutes_now=720,
        best_soc_keep=4.0,
        sensor_overrides=sensor_overrides,
    )
    plugin = CurtailmentPlugin(base)
    _, phase = plugin.calculate(dno_limit_kw=4.0)
    assert phase == "active", f"R56: plugin active during PV (was 'off' under pre-R56 will_fill gate), got {phase}"
    print("  test_activation_requires_will_fill: PASSED (R56 — active during PV regardless of will_fill)")


# ============================================================================
# State file persistence tests (atomic write, _pv_history)
# ============================================================================


def _state_test_base(config_root, **kwargs):
    """MockBase that uses a real temp dir as config_root so persistence is exercised."""
    base = MockBase(**kwargs)
    base.config_root = config_root
    return base


def test_save_state_atomic_against_partial_write():
    """Atomic guarantee: a failed write must NOT corrupt the prior state file.

    Simulates a crash mid-write by patching json.dump to raise after the file
    is opened. With the old non-atomic implementation, the main file is
    truncated and _load_state silently returns None, losing peak_pv/ratchet.
    With the atomic .tmp+rename pattern, the main file is untouched.
    """
    import tempfile
    import json as json_mod

    with tempfile.TemporaryDirectory() as tmp:
        base = _state_test_base(tmp, soc_kw=10.0)
        plugin = CurtailmentPlugin(base)
        # Save a known-good state
        plugin._peak_pv = 7.5
        plugin._floor_ratchet = 12.34
        plugin._save_state()

        # Verify file exists and contains the data
        path = os.path.join(tmp, "curtailment_state.json")
        assert os.path.exists(path), "Initial save did not produce a state file"
        with open(path) as f:
            saved_first = json_mod.load(f)
        assert saved_first["peak_pv_kw"] == 7.5

        # Simulate a crash mid-write by monkey-patching json.dump
        plugin._peak_pv = 99.9  # different value we DON'T want persisted

        import curtailment_plugin as plugin_mod

        original_dump = plugin_mod.json.dump

        def crashing_dump(*args, **kwargs):
            # Truncate the target file mid-write to simulate partial write,
            # then raise. With atomic implementation this hits the .tmp file
            # (and the .tmp gets discarded), not the main state file.
            args[1].write('{"date":"2099-01-01","peak_pv_kw":99.9')  # partial JSON
            raise OSError("simulated disk full")

        plugin_mod.json.dump = crashing_dump
        try:
            plugin._save_state()
        finally:
            plugin_mod.json.dump = original_dump

        # Main file must still contain the FIRST save's data, not partial garbage.
        with open(path) as f:
            content = f.read()
        try:
            saved_after_crash = json_mod.loads(content)
        except json_mod.JSONDecodeError:
            raise AssertionError(f"State file was corrupted by failed write — atomicity violated. Content: {content!r}")
        assert saved_after_crash["peak_pv_kw"] == 7.5, f"Atomic-write regression: failed write corrupted main file. Got peak_pv_kw={saved_after_crash.get('peak_pv_kw')}, expected 7.5"
    print("  test_save_state_atomic_against_partial_write: PASSED")


def test_save_state_round_trip_with_pv_history():
    """_pv_history must round-trip through save→load so R49 keeps working post-restart."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        base = _state_test_base(tmp, soc_kw=10.0)
        plugin = CurtailmentPlugin(base)

        # Force today's date so _load_state accepts it
        today = datetime.now().strftime("%Y-%m-%d")
        plugin._peak_pv = 6.2
        plugin._pv_history.append((780, 12.0, 10.5))
        plugin._pv_history.append((840, 18.0, 15.0))
        plugin._pv_history.append((900, 24.0, 19.5))
        plugin._save_state()

        # Fresh plugin should restore the deque
        base2 = _state_test_base(tmp, soc_kw=10.0)
        plugin2 = CurtailmentPlugin(base2)
        assert plugin2._peak_pv == 6.2, "peak_pv did not survive round-trip"
        assert len(plugin2._pv_history) == 3, f"_pv_history did not round-trip — got {len(plugin2._pv_history)} entries, expected 3"
        history_list = list(plugin2._pv_history)
        assert history_list[0] == (780, 12.0, 10.5), f"oldest entry wrong: {history_list[0]}"
        assert history_list[-1] == (900, 24.0, 19.5), f"newest entry wrong: {history_list[-1]}"
    print("  test_save_state_round_trip_with_pv_history: PASSED")


def test_load_state_logs_corruption():
    """A corrupted state file should log, not silently return — visibility matters."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        # Pre-write a corrupted file
        path = os.path.join(tmp, "curtailment_state.json")
        with open(path, "w") as f:
            f.write("{not valid json")

        base = _state_test_base(tmp, soc_kw=10.0)
        plugin = CurtailmentPlugin(base)
        # Plugin should NOT crash; should fall back to default state
        assert plugin._peak_pv == 0.0, "Default state should apply when file is corrupt"
        # And should have logged the corruption (visibility for ops)
        assert any("state" in msg.lower() and ("corrupt" in msg.lower() or "decode" in msg.lower() or "invalid" in msg.lower()) for msg in base.logs), f"Expected a log line about corrupted state file, got logs: {base.logs}"
    print("  test_load_state_logs_corruption: PASSED")


# ============================================================================
# v32 evening lifecycle (2026-07-20): overflow_fits → Hold (not deactivate),
# stay active to sundown, saving-session reserve + dump. Replaces v31 early-
# handback which round-tripped PV through the battery via MSC.
# ============================================================================


def _saving_session_sensors(active=False, current_mins=0, next_mins=0, current_start=None, next_start=None, current_end=None):
    """Octopus saving-session binary_sensor override (state + joined-event
    duration attributes read by _get_session_reserve_kwh / _is_saving_session_active).

    The *_start attributes feed the published `session_start` — the dashboard
    needs 'when', not just 'how much'."""
    return {
        SIG_SAVING_SESSION_ENTITY: {
            "state": "on" if active else "off",
            "current_joined_event_duration_in_minutes": current_mins,
            "next_joined_event_duration_in_minutes": next_mins,
            "current_joined_event_start": current_start,
            "current_joined_event_end": current_end,
            "next_joined_event_start": next_start,
        }
    }


def test_v32_no_deactivate_past_safe_time_holds():
    """v32: past safe_time with PV still flowing, the plugin STAYS ACTIVE and the
    policy override is Hold (battery flat, export surplus). v31 deactivated here →
    MSC → round-trip. Supersedes RD6 'deactivate at safe_time'."""
    from datetime import datetime, timezone

    pv = {m: 1.0 for m in range(0, 60, PLUGIN_STEP)}
    load = {m: 0.5 for m in range(0, 60, PLUGIN_STEP)}
    sensor_overrides = {"sensor.sigen_plant_pv_power": 1.0, "sensor.sigen_plant_consumed_power": 0.5}
    sensor_overrides.update(_make_p90_sensors(p90_peak_kw=8.0, solcast_remaining=1.0))
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=BATTERY_KWH * 0.85,
        minutes_now=1140,  # 19:00 BST — past safe_time
        best_soc_keep=4.0,
        now_utc=datetime(2025, 7, 12, 18, 0, tzinfo=timezone.utc),
        sensor_overrides=sensor_overrides,
    )
    plugin = CurtailmentPlugin(base)
    plugin._peak_pv = 7.5
    floor, phase = plugin.calculate(dno_limit_kw=4.0)
    assert phase == "active", f"v32: stay active past safe_time while PV>0.1, got {phase}"
    assert plugin._policy_override == "no_drain", f"v32.1: past safe_time → no_drain override, got {plugin._policy_override}"
    print("  test_v32_no_deactivate_past_safe_time_holds: PASSED")


def test_v32_overflow_fits_holds_not_off():
    """v32: mid-day, when the battery headroom can absorb all remaining p90
    overflow (+buffer), the plugin STAYS ACTIVE with a Hold override — it does NOT
    deactivate to Predbat/MSC (the v31 early-handback bug that round-tripped PV on
    2026-07-20)."""
    pv = {m: 2.0 for m in range(0, 480, PLUGIN_STEP)}
    load = {m: 0.5 for m in range(0, 480, PLUGIN_STEP)}
    sensor_overrides = {"sensor.sigen_plant_pv_power": 2.0, "sensor.sigen_plant_consumed_power": 0.5}
    # Modest remaining overflow (~4 kWh) vs 8 kWh headroom → fits with buffer,
    # while still BEFORE safe_time (15:51) so this exercises the fits path, not
    # the past_safe path.
    sensor_overrides.update(_make_p90_sensors(p90_peak_kw=6.0, solcast_remaining=3.0))
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=BATTERY_KWH * 0.55,  # ~8 kWh headroom ≫ remaining overflow + buffer
        minutes_now=840,  # 14:00 BST — before safe_time
        best_soc_keep=4.0,
        sensor_overrides=sensor_overrides,
    )
    plugin = CurtailmentPlugin(base)
    plugin._peak_pv = 7.5  # peak already seen today
    floor, phase = plugin.calculate(dno_limit_kw=4.0)
    assert phase == "active", f"v32: overflow-fits must NOT deactivate, got {phase}"
    assert plugin._policy_override == "no_drain", f"v32.1: overflow-fits → no_drain override, got {plugin._policy_override}"
    assert plugin._safe_time_str > "14:00", f"scenario must be before safe_time to test fits path, safe={plugin._safe_time_str}"
    print("  test_v32_overflow_fits_holds_not_off: PASSED")


def test_v32_overflow_does_not_fit_schmitt_drives():
    """v32: genuine mid-day overflow that does NOT fit the battery → no Hold
    override (None), the existing SOC-vs-band Schmitt makes room (drain)."""
    pv = {m: 8.0 for m in range(0, 480, PLUGIN_STEP)}
    load = {m: 1.0 for m in range(0, 480, PLUGIN_STEP)}
    sensor_overrides = {"sensor.sigen_plant_pv_power": 8.0, "sensor.sigen_plant_consumed_power": 1.0}
    sensor_overrides.update(_make_p90_sensors(p90_peak_kw=10.0, solcast_remaining=45.0))
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=BATTERY_KWH * 0.55,  # little headroom vs a big p90 overflow
        minutes_now=720,
        best_soc_keep=4.0,
        sensor_overrides=sensor_overrides,
    )
    plugin = CurtailmentPlugin(base)
    plugin._peak_pv = 9.0
    floor, phase = plugin.calculate(dno_limit_kw=4.0)
    assert phase == "active", f"expected active, got {phase}"
    assert plugin._policy_override is None, f"v32: overflow-doesn't-fit → no override (Schmitt drives), got {plugin._policy_override}"
    print("  test_v32_overflow_does_not_fit_schmitt_drives: PASSED")


def test_v32_sundown_still_deactivates():
    """v32: sundown (peak observed today AND actual PV ≈ 0) is still the sole
    deactivation trigger → phase off, no Hold override."""
    from datetime import datetime, timezone

    pv = {m: 0.0 for m in range(0, 60, PLUGIN_STEP)}
    load = {m: 0.5 for m in range(0, 60, PLUGIN_STEP)}
    sensor_overrides = {"sensor.sigen_plant_pv_power": 0.0, "sensor.sigen_plant_consumed_power": 0.5}
    sensor_overrides.update(_make_p90_sensors(p90_peak_kw=8.0, solcast_remaining=0.0))
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=BATTERY_KWH * 0.40,
        minutes_now=1260,  # 21:00 BST
        best_soc_keep=4.0,
        now_utc=datetime(2025, 7, 12, 20, 0, tzinfo=timezone.utc),
        sensor_overrides=sensor_overrides,
    )
    plugin = CurtailmentPlugin(base)
    plugin._peak_pv = 7.5
    floor, phase = plugin.calculate(dno_limit_kw=4.0)
    assert phase == "off", f"v32: sundown still deactivates, got {phase}"
    print("  test_v32_sundown_still_deactivates: PASSED")


def test_v32_saving_session_plugin_stays_active_but_delegates_dispatch():
    """RD14c (2026-07-28) SUPERSEDES v32(b): the plugin stays ACTIVE through a
    live session but no longer sets the dispatch override — the heartbeat drives
    Max Export natively off the Octoplus calendar.

    v32(b) asserted `_policy_override == "max_export"` here. That behaviour moved,
    it was not lost: see tests/test_yaml_heartbeat.py::test_rd14c_*. It had to
    move because the plugin PINNED the select to Max Export, so at session end the
    heartbeat saw `raw_policy = Max Export` and kept exporting until the plugin's
    next 5-minute cycle — measured at 5 min 46 s past the paid window on
    2026-07-28.

    What must still hold here: the plugin remains active (so its floors and the
    session reserve keep working) and does NOT claim dispatch.
    """
    pv = {m: 2.0 for m in range(0, 480, PLUGIN_STEP)}
    load = {m: 0.5 for m in range(0, 480, PLUGIN_STEP)}
    sensor_overrides = {"sensor.sigen_plant_pv_power": 2.0, "sensor.sigen_plant_consumed_power": 0.5}
    sensor_overrides.update(_make_p90_sensors(p90_peak_kw=8.0, solcast_remaining=2.0))
    sensor_overrides.update(_saving_session_sensors(active=True, current_mins=90))
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=BATTERY_KWH * 0.60,
        minutes_now=1050,  # ~17:30 BST, session hours
        best_soc_keep=4.0,
        sensor_overrides=sensor_overrides,
    )
    plugin = CurtailmentPlugin(base)
    plugin._peak_pv = 7.5
    floor, phase = plugin.calculate(dno_limit_kw=4.0)
    assert phase == "active", f"expected active during session, got {phase}"
    assert plugin._session_active, "plugin must still SEE the session (planning half depends on it)"
    assert plugin._policy_override != "max_export", f"RD14c: dispatch is the heartbeat's job now, plugin set {plugin._policy_override}"
    print("  test_v32_saving_session_active_forces_max_export: PASSED (RD14c: plugin active, dispatch delegated)")


def test_v32_upcoming_session_raises_drain_floor():
    """v32(a): an UPCOMING (not yet active) session raises drain_above so CM does
    not drain the reserve away before the session. Compare drain_above with vs
    without the scheduled session, same overflow day."""

    def _run(session):
        pv = {m: 8.0 for m in range(0, 480, PLUGIN_STEP)}
        load = {m: 1.0 for m in range(0, 480, PLUGIN_STEP)}
        sensor_overrides = {"sensor.sigen_plant_pv_power": 8.0, "sensor.sigen_plant_consumed_power": 1.0}
        sensor_overrides.update(_make_p90_sensors(p90_peak_kw=10.0, solcast_remaining=45.0))
        if session:
            # A duration with no start is not a state Octopus can produce, and
            # since 2026-08-11 the reserve is gated on WHEN the session is
            # (SESSION_PROTECT_HORIZON_HOURS). Rig clock is 12:00, so 19:00 the
            # same day is a realistic 7 h out.
            sensor_overrides.update(_saving_session_sensors(active=False, next_mins=120, next_start="2025-07-12T19:00:00+00:00"))
        base = MockBase(
            pv_step=pv,
            load_step=load,
            soc_kw=BATTERY_KWH * 0.55,
            minutes_now=720,
            best_soc_keep=4.0,
            sensor_overrides=sensor_overrides,
        )
        base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
        # RD37: dull P10 — this test is about the reserve RAISING the floor, not
        # about whether PV can refill it.
        base.pv_forecast_minute_step10 = {m: 0.05 * (PLUGIN_STEP / 60.0) for m in range(0, 24 * 60, PLUGIN_STEP)}
        plugin = CurtailmentPlugin(base)
        plugin._peak_pv = 9.0
        plugin._overnight_target_kwh = 6.0
        plugin.on_update()
        return base.published.get("sensor.predbat_curtailment_drain_above", {}).get("value")

    without = _run(session=False)
    with_session = _run(session=True)
    assert with_session > without + 1.0, f"v32(a): upcoming session must raise drain_above (protect reserve): with={with_session} without={without}"
    print(f"  test_v32_upcoming_session_raises_drain_floor: PASSED (drain_above {without}->{with_session})")


def test_two_floors_are_named_and_sourced_distinctly():
    """The two SOC floors pull in OPPOSITE directions and must be readable as
    such on the dashboard, or the plugin's mode is unexplainable:

      Overnight Floor (P10 generation) — stay ABOVE this to make it through
                                          tonight; below it -> Solar Charge
      Headroom Floor  (P90 overflow)   — drain DOWN to this so today's surplus
                                          fits; above it -> Max Export

    Guards against the pre-2026-07-28 naming ("Charge Below (P10 Recovery)" /
    "Drain Above (Curt Floor)"), which named the comparison operator rather than
    the thing being protected, and against a future edit re-pointing one floor at
    the other's driver — the R59a defect, where the overnight floor was driven by
    overflow and so blocked the morning drain.
    """
    pv = {m: 8.0 for m in range(0, 480, PLUGIN_STEP)}
    load = {m: 1.0 for m in range(0, 480, PLUGIN_STEP)}
    sensor_overrides = {"sensor.sigen_plant_pv_power": 8.0, "sensor.sigen_plant_consumed_power": 1.0}
    sensor_overrides.update(_make_p90_sensors(p90_peak_kw=10.0, solcast_remaining=45.0))
    base = MockBase(pv_step=pv, load_step=load, soc_kw=BATTERY_KWH * 0.55, minutes_now=720, best_soc_keep=4.0, sensor_overrides=sensor_overrides)
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    plugin = CurtailmentPlugin(base)
    plugin._peak_pv = 9.0
    plugin._overnight_target_kwh = 6.0
    plugin.on_update()

    overnight = base.published["sensor.predbat_curtailment_charge_below"]["attrs"]
    headroom = base.published["sensor.predbat_curtailment_drain_above"]["attrs"]

    assert "Overnight Floor" in overnight["friendly_name"], overnight["friendly_name"]
    assert "Headroom Floor" in headroom["friendly_name"], headroom["friendly_name"]
    assert overnight["friendly_name"] != headroom["friendly_name"]

    # Each floor must name its own driver, and must NOT claim the other's.
    assert "P10" in overnight["friendly_name"] and "P90" not in overnight["friendly_name"]
    assert "P90" in headroom["friendly_name"] and "P10" not in headroom["friendly_name"]

    # The driver values themselves must be published alongside each floor, so the
    # dashboard can show cause next to effect.
    for key in ("p10_pv_remaining_kwh", "load_remaining_kwh", "p10_surplus_kwh", "overnight_target_kwh"):
        assert key in overnight, f"Overnight Floor missing driver {key}"
    assert "overflow_p90_kwh" in headroom, "Headroom Floor missing its P90 overflow driver"

    # Both expose a percentage so they can be read against SOC%, which is how the
    # battery is displayed everywhere else.
    assert overnight["soc_pct"] is not None and headroom["soc_pct"] is not None
    assert "Solar Charge" in overnight["drives"] and "Max Export" in headroom["drives"]
    print(f"  test_two_floors_are_named_and_sourced_distinctly: PASSED ({overnight['soc_pct']}% / {headroom['soc_pct']}%)")


def test_drain_above_source_mirrors_compute_drain_above():
    """`compute_drain_above` is a 4-way max. The published `source` must name an
    arm whose value IS the answer — for every combination.

    Anti-drift guard: the source is derived FROM compute_drain_above, never
    re-implemented beside it. This is the `required_headroom_kwh` lesson (one
    quantity computed by three drifting expressions) applied before it can
    happen again.
    """
    cases = [
        # reserve, overflow_floor, session_protect, floor_kwh, expected winning arm
        (0.0, 3.39, 10.22, None, "session_protect"),  # live 2026-08-03
        (0.0, 3.39, 0.0, None, "overflow_floor"),  # ordinary overflow day
        (0.0, 12.0, 10.22, None, "overflow_floor"),  # session present but not binding
        (0.0, 0.2, 0.0, None, "deep_floor"),  # nothing else above the 0.5 kWh floor
        (5.0, 0.2, 0.0, None, "reserve"),  # hardware reserve dominates
        # The dawn arm must be nameable: a reader seeing a 1.81 kWh floor next to
        # overflow_floor 0.0 cannot otherwise tell what is holding it up.
        (0.0, 0.0, 0.0, 1.81, "dawn_reserve"),  # dawn gap, PV not yet covering load
        (0.0, 4.0, 0.0, 1.81, "overflow_floor"),  # dawn reserve present but not binding
        (0.0, 0.1, 0.0, DAWN_TEST_FLOOR_KWH, "deep_floor"),  # released: the 1% helper floor is the deep arm
    ]
    for reserve, overflow_floor, session_protect, floor_kwh, expected in cases:
        value = compute_drain_above(reserve, overflow_floor, None, session_protect, floor_kwh)
        source = compute_drain_above_source(reserve, overflow_floor, session_protect, floor_kwh)
        assert source == expected, f"reserve={reserve} overflow={overflow_floor} session={session_protect} floor={floor_kwh}: expected {expected}, got {source}"
        arm = {
            "session_protect": session_protect,
            "overflow_floor": overflow_floor,
            "dawn_reserve": floor_kwh,
            "deep_floor": DEEP_DISCHARGE_FLOOR_KWH if floor_kwh is None else floor_kwh,
            "reserve": reserve,
        }[source]
        assert abs(arm - value) < 1e-6, f"source '{source}' names {arm} but compute_drain_above returned {value}"
    print("  test_drain_above_source_mirrors_compute_drain_above: PASSED")


# The live drain-floor helper, deliberately NOT the rig default (2.8). At 2.8% the
# released floor is 0.506 kWh — within 0.006 of DEEP_DISCHARGE_FLOOR_KWH, so a test
# asserting it would pass just as well against the OLD behaviour. Charter: never let
# the failure mode into the allowed set.
DAWN_TEST_FLOOR_PCT = 1.0
DAWN_TEST_FLOOR_KWH = DAWN_TEST_FLOOR_PCT / 100.0 * BATTERY_KWH


def _make_dawn_base(pv_kw, load_kw, soc_pct=0.30, hour=6, floor_pct=DAWN_TEST_FLOOR_PCT):
    """MockBase for the dawn gap: PV has STARTED but does not yet cover load."""
    from datetime import datetime, timezone

    minutes_now = int(hour * 60)
    overrides = {
        "sensor.sigen_plant_pv_power": pv_kw,
        "sensor.sigen_plant_consumed_power": load_kw,
        "input_number.sig_drain_floor_pct": floor_pct,
    }
    return MockBase(
        soc_kw=BATTERY_KWH * soc_pct,
        minutes_now=minutes_now,
        now_utc=datetime(2025, 7, 12, hour, 0, tzinfo=timezone.utc),
        sensor_overrides=overrides,
    )


def _dawn_drain_above(plugin):
    """Publish on a maximum-overflow day (every other arm zero) and return the floor.

    Runs DAWN_RELEASE_CONFIRM_CYCLES crossing evaluations first: since RD35 the
    release needs a SUSTAINED crossing, so a rig that evaluates once would never
    release and these tests would pass for the wrong reason.
    """
    for _ in range(DAWN_RELEASE_CONFIRM_CYCLES):
        plugin._evaluate_dawn_crossing()
    plugin._overflow_floor_kwh = 0.0
    plugin._effective_keep_kwh = 0.0
    plugin._p10_recovery_floor = 0.0
    plugin._session_protect_kwh = 0.0
    plugin.publish("active", 0.0, dno_limit_kw=4.0)
    return plugin._drain_above


def test_dawn_margin_is_load_over_the_error_window():
    """RD38 — the reserve is now an ERROR MARGIN, and is sized as one.

    RD36 made the drain run TO the pv-covers-load crossing, so the reserve no
    longer bridges a designed gap — it only covers the case where the FORECAST
    crossing is earlier than the real one. That is a different quantity, and it
    was still being computed as the old PV-start-to-covers-load window load: a
    number that means something else and, in winter, could be hours.

    Sized as forecast house load over DAWN_ERROR_MARGIN_MINUTES following the
    predicted crossing — literally "what it costs if the crossing is that late".
    45 min because yesterday's clear-sky miss was 35 min and the per-slot
    forecast should beat it; ~0.3 kWh, about 1.7% of pack.
    """
    from curtailment_calc import compute_dawn_error_margin

    # 0.8 kW, deliberately ABOVE MIN_BASE_LOAD_KW: at 0.4 kW the base-load floor
    # (0.5 x 0.75 = 0.375) dominates and the integration under test is never
    # reached — the fixture would pass without exercising its own subject.
    load = {m: 0.8 * (5 / 60.0) for m in range(0, 720, 5)}
    got = compute_dawn_error_margin(load, crossing_minute=300, margin_minutes=45, step_minutes=5, values_are_kwh=True)
    assert abs(got - 0.6) < 0.02, "45 min at 0.8 kW = 0.60 kWh, got {:.3f}".format(got)
    print("  test_dawn_margin_is_load_over_the_error_window: PASSED ({:.2f} kWh)".format(got))


def test_dawn_margin_uses_the_load_after_the_crossing_not_before():
    """The risk is the crossing arriving LATE, so the margin must be the load
    that follows it. Taking the load before would size it on the wrong hour —
    and at dawn the two differ (DHW, heating recovery)."""
    from curtailment_calc import compute_dawn_error_margin

    load = {m: (3.0 if m < 300 else 0.8) * (5 / 60.0) for m in range(0, 720, 5)}
    got = compute_dawn_error_margin(load, crossing_minute=300, margin_minutes=45, step_minutes=5, values_are_kwh=True)
    assert abs(got - 0.6) < 0.02, "must use the 0.8 kW AFTER the crossing, not the 3.0 kW before it — got {:.3f}".format(got)
    print("  test_dawn_margin_uses_the_load_after_the_crossing_not_before: PASSED")


def test_dawn_margin_floors_at_base_load():
    """A forecast that claims near-zero overnight load must not produce a
    near-zero margin — the house always draws something."""
    from curtailment_calc import compute_dawn_error_margin

    load = {m: 0.0 for m in range(0, 720, 5)}
    got = compute_dawn_error_margin(load, crossing_minute=300, margin_minutes=45, step_minutes=5, values_are_kwh=True)
    assert got >= MIN_BASE_LOAD_KW * (45 / 60.0) - 0.01, "must floor at base load, got {:.3f}".format(got)
    print("  test_dawn_margin_floors_at_base_load: PASSED ({:.2f} kWh)".format(got))


def test_dawn_reserve_survives_pv_start():
    """The dawn reserve must hold until PV MEETS LOAD, not until PV starts.

    Live 2026-08-06, the defect this pins. `compute_pre_pv_target` reserves
    `DEEP_DISCHARGE_FLOOR_KWH + dawn_load_kwh` to carry the house to dawn, but
    that term lives ONLY in the pre-PV path. At PV start the phase ends, the
    normal Schmitt takes over with `compute_drain_above`, which had no dawn arm
    at all — so the reserve was spent 85 minutes before it was needed:

        04:00-05:30  pre-PV drain -> 5.5%   (reserve honoured)
        ~05:30       PV start -> reserve term vanishes
        05:30-06:00  Schmitt drains on to drain_above=2.8%, bottoms 2.5%
        06:00-06:55  coasts 2.5% -> 1.3% on house load, importing
        ~06:55       PV finally meets load

    PV start and PV-meets-load are ~85 min apart in August and hours apart in
    winter. Draining into that gap buys headroom hours before overflow needs it
    and pays for it with import.
    """
    plugin = CurtailmentPlugin(_make_dawn_base(pv_kw=0.31, load_kw=0.42))
    floor = _dawn_drain_above(plugin)
    # RD36 (2026-08-12): the fixed DAWN_RESERVE_FRACTION arm is gone — the drain
    # is now timed to END at the crossing, so the reserve is only the computed
    # gap need. The BEHAVIOUR under test is unchanged: the reserve must survive
    # PV-start rather than evaporating at the phase boundary.
    expected = round(DEEP_DISCHARGE_FLOOR_KWH + plugin._dawn_load_kwh, 2)
    assert abs(floor - expected) < 0.01, "PV 0.31 < load 0.42 is still the dawn gap: floor must be the {:.2f} kWh dawn reserve, got {:.2f}".format(expected, floor)
    print("  test_dawn_reserve_survives_pv_start: PASSED ({:.2f} kWh held)".format(floor))


def _cycle(plugin, pv_kw, load_kw):
    """One plan cycle at the given PV/load, then read the floor (which is read
    TWICE per real cycle — publish and R63 — so this catches double-counting)."""
    plugin.base._sensor_overrides["sensor.sigen_plant_pv_power"] = pv_kw
    plugin.base._sensor_overrides["sensor.sigen_plant_consumed_power"] = load_kw
    plugin._evaluate_dawn_crossing()
    plugin._dawn_floor_kwh(BATTERY_KWH)
    plugin._dawn_floor_kwh(BATTERY_KWH)
    return plugin._dawn_released


def test_single_crossing_sample_does_not_release_the_reserve():
    """RD35 — the release must be SUSTAINED, not a single lucky sample.

    Andrew: "Rather than pick the first moment pv exceeds load we should check
    it continues?" Right. The latch was one-way on ONE instantaneous reading, so
    a momentary load dip against rising PV (fridge cycling off at 0.10 kW while
    PV is 0.12) spends the dawn reserve for the whole day — and the house then
    runs onto the import meter for the rest of the gap.

    Asymmetric by design: releasing EARLY risks import at 12.4-25.3p; releasing
    LATE costs a few minutes of deferred export at a flat 12p, and there are
    hours of shed time before lockout. So bias late.
    """
    plugin = CurtailmentPlugin(_make_dawn_base(pv_kw=0.09, load_kw=0.37))
    assert _cycle(plugin, pv_kw=0.12, load_kw=0.10) is False, "one sample must not release the reserve"
    print("  test_single_crossing_sample_does_not_release_the_reserve: PASSED")


def test_sustained_crossing_releases():
    """Two consecutive cycles (~10 min) is a real dawn, not a blip."""
    plugin = CurtailmentPlugin(_make_dawn_base(pv_kw=0.09, load_kw=0.37))
    assert _cycle(plugin, pv_kw=0.60, load_kw=0.40) is False
    assert _cycle(plugin, pv_kw=0.70, load_kw=0.40) is True, "sustained crossing must release"
    print("  test_sustained_crossing_releases: PASSED")


def test_interrupted_crossing_resets_the_count():
    """A blip, a drop, then another blip is NOT sustained — the count resets, so
    two isolated samples must not add up to a release."""
    plugin = CurtailmentPlugin(_make_dawn_base(pv_kw=0.09, load_kw=0.37))
    assert _cycle(plugin, pv_kw=0.60, load_kw=0.40) is False
    assert _cycle(plugin, pv_kw=0.10, load_kw=0.40) is False, "PV fell back — must reset"
    assert _cycle(plugin, pv_kw=0.60, load_kw=0.40) is False, "count restarted, so still not released"
    assert _cycle(plugin, pv_kw=0.60, load_kw=0.40) is True
    print("  test_interrupted_crossing_resets_the_count: PASSED")


def test_crossing_counted_once_per_cycle_not_per_read():
    """`_dawn_floor_kwh` is read TWICE per cycle (publish + R63's floor). If the
    counting lived in it, one cycle would count as two and release immediately —
    which is the single-sample bug wearing a disguise."""
    plugin = CurtailmentPlugin(_make_dawn_base(pv_kw=0.09, load_kw=0.37))
    plugin.base._sensor_overrides["sensor.sigen_plant_pv_power"] = 0.60
    plugin.base._sensor_overrides["sensor.sigen_plant_consumed_power"] = 0.40
    plugin._evaluate_dawn_crossing()
    for _ in range(5):
        plugin._dawn_floor_kwh(BATTERY_KWH)
    assert plugin._dawn_released is False, "reading the floor must never advance the latch"
    print("  test_crossing_counted_once_per_cycle_not_per_read: PASSED")


def test_dawn_reserve_released_by_measured_crossover():
    """Once measured PV >= load the battery has no load duty left, so the whole
    reserve is released to headroom — down to POST_DAWN_FLOOR_KWH, not the old
    deep floor.

    Gated on MEASUREMENT, not forecast: over-reserving is then self-correcting,
    which is why 10% can be chosen without agonising over it. Releasing 10% ->
    1% is 1.63 kWh, ~27 min at the 3.68 kW cap, against ~3 h of slack before
    overflow starts — the headroom cost is nil.
    """
    plugin = CurtailmentPlugin(_make_dawn_base(pv_kw=0.59, load_kw=0.42))
    floor = _dawn_drain_above(plugin)
    assert abs(floor - DAWN_TEST_FLOOR_KWH) < 0.01, "PV 0.59 >= load 0.42: reserve must release to the {:.2f} kWh helper floor, got {:.2f}".format(DAWN_TEST_FLOOR_KWH, floor)
    assert plugin._dawn_released is True, "crossover must set the latch"
    print("  test_dawn_reserve_released_by_measured_crossover: PASSED ({:.2f} kWh)".format(floor))


def test_released_floor_comes_from_the_live_helper():
    """The released floor is the ONE drain-floor helper (RD15), not a second
    plugin constant beside it. Three coincident 5% floors were consolidated into
    that helper once already; a hardcoded twin would undo it silently — and a CM
    target below the helper is unreachable anyway, because the heartbeat's
    sell-clamp reads the same entity.
    """
    plugin = CurtailmentPlugin(_make_dawn_base(pv_kw=0.59, load_kw=0.42, floor_pct=4.0))
    floor = _dawn_drain_above(plugin)
    assert abs(floor - 4.0 / 100.0 * BATTERY_KWH) < 0.01, "helper at 4% must set the released floor to {:.2f}, got {:.2f}".format(4.0 / 100.0 * BATTERY_KWH, floor)
    print("  test_released_floor_comes_from_the_live_helper: PASSED ({:.2f} kWh)".format(floor))


def test_dawn_release_latches_against_midday_cloud():
    """Release is a ONE-WAY daily latch.

    Without it, a cloud at 11:00 drops PV under load, the floor jumps back to
    10% and the drain stops in the middle of the overflow window — the exact
    failure the elevation gate was built to avoid at dusk, arriving at dawn
    instead.
    """
    plugin = CurtailmentPlugin(_make_dawn_base(pv_kw=0.59, load_kw=0.42))
    _dawn_drain_above(plugin)
    assert plugin._dawn_released is True

    plugin.base._sensor_overrides["sensor.sigen_plant_pv_power"] = 0.10
    floor = _dawn_drain_above(plugin)
    assert abs(floor - DAWN_TEST_FLOOR_KWH) < 0.01, "a cloud after crossover must NOT re-arm the dawn reserve, got {:.2f}".format(floor)
    print("  test_dawn_release_latches_against_midday_cloud: PASSED")


def test_dawn_reserve_uses_forecast_dawn_load_when_larger():
    """10% is an August number. In winter the dawn gap is hours, so the
    forecast term must be able to win — `max(10%, DEEP + dawn_load)`.

    Keeps `compute_pre_pv_target`'s existing dawn_load computation load-bearing
    across the phase boundary instead of discarding it at PV start.
    """
    plugin = CurtailmentPlugin(_make_dawn_base(pv_kw=0.10, load_kw=0.80))
    plugin._dawn_load_kwh = 3.0  # a long, dark winter gap
    floor = _dawn_drain_above(plugin)
    assert abs(floor - (DEEP_DISCHARGE_FLOOR_KWH + 3.0)) < 0.01, "forecast dawn_load must win over the 10% floor when larger, got {:.2f}".format(floor)
    print("  test_dawn_reserve_uses_forecast_dawn_load_when_larger: PASSED ({:.2f} kWh)".format(floor))


def test_dawn_release_latch_persists_and_resets_daily():
    """The latch must survive a deploy (state is reloaded, not rebuilt) and must
    NOT survive midnight — a stale release would strand tomorrow's dawn."""
    plugin = CurtailmentPlugin(_make_dawn_base(pv_kw=0.59, load_kw=0.42))
    _dawn_drain_above(plugin)
    assert plugin._dawn_released is True

    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        plugin.base.config_root = tmpdir
        plugin._save_state()
        reloaded = CurtailmentPlugin(_make_dawn_base(pv_kw=0.10, load_kw=0.80))
        reloaded.base.config_root = tmpdir
        reloaded._load_state()
        assert reloaded._dawn_released is True, "latch must survive a deploy via curtailment_state.json"
        assert abs(_dawn_drain_above(reloaded) - DAWN_TEST_FLOOR_KWH) < 0.01, "a restored latch must keep the reserve released even though PV < load"

    plugin._reset_for_new_day()
    assert plugin._dawn_released is False, "midnight must re-arm the dawn reserve"
    print("  test_dawn_release_latch_persists_and_resets_daily: PASSED")


def test_r63_never_forces_max_export_through_a_session_reserve():
    """R63 RETIRED (2026-08-10). The deadline is a DIAGNOSTIC, not a lever.

    Audit finding: R63's condition is `needed > sheddable` and the ordinary
    Schmitt drains on `needed > 0`, with `sheddable >= 0` by construction. So
    every state where R63 fires is one where the Schmitt is ALREADY draining —
    it can never fire earlier, contrary to the requirement's central claim. It
    was written against the bare energy test, which was replaced by the
    safety-factored `required_headroom_kwh` in the SAME 2026-07-28 change set.

    Its documented purpose (outrank `no_drain`) is unreachable in both branches:
    `overflow_fits` needs fits_margin >= buffer while R63 needs needed > 0, and
    needed == -fits_margin, so they are mutually exclusive; `past_safe` is an
    evening state while R63 is hard-gated on lockout still being ahead.

    That left exactly one distinct behaviour, which this test pins: draining
    through a SAVING-SESSION reserve. SOC sits below `session_protect` (so the
    Schmitt correctly Holds) but above R63's own floor, and the day is short of
    headroom. The old code forced Max Export here — dumping premium-rate energy
    at the ordinary export rate to buy headroom, a trade nobody chose.
    """
    pv = {m: 8.0 for m in range(0, 480, PLUGIN_STEP)}
    load = {m: 1.0 for m in range(0, 480, PLUGIN_STEP)}
    overrides = {"sensor.sigen_plant_pv_power": 1.2, "sensor.sigen_plant_consumed_power": 0.6}
    overrides.update(_make_p90_sensors(p90_peak_kw=10.0, solcast_remaining=45.0))
    overrides.update(_saving_session_sensors(active=False, next_mins=60, next_start="2025-07-12T19:00:00+00:00"))
    # now_utc MUST match minutes_now — the solar geometry reads now_utc, so a
    # mismatched rig puts lockout in the past and R63 never engages, and the test
    # passes against the very code it condemns. First version of this test did
    # exactly that (2026-08-10), the same rig-fidelity trap as 2026-08-05.
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=BATTERY_KWH * 0.40,  # 7.23 kWh — BELOW session_protect 7.68, so the Schmitt Holds
        minutes_now=360,  # 06:00 — lockout still ahead, so R63 is live
        now_utc=_dt(2025, 7, 12, 6, 0, tzinfo=_tz.utc),
        best_soc_keep=4.0,
        sensor_overrides=overrides,
    )
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    plugin = CurtailmentPlugin(base)
    plugin._peak_pv = 1.2
    plugin._overnight_target_kwh = 6.0
    plugin.calculate(dno_limit_kw=3.68)

    assert plugin._policy_override != "max_export", "R63 must not force Max Export through the session reserve (override={})".format(plugin._policy_override)
    print("  test_r63_never_forces_max_export_through_a_session_reserve: PASSED")


def test_deadline_shortfall_is_published_as_a_diagnostic():
    """Removing the override must NOT throw away the measurement.

    "We cannot make enough room before the cap locks out" is real and useful —
    it is the honest input for the load-advice alert ("run the dishwasher"), and
    it is the one thing the Schmitt does not already tell us. It just is not an
    instruction, because when it is true we are already draining flat out.
    """
    base = MockBase(soc_kw=18.08 * 0.435)
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    plugin = CurtailmentPlugin(base)
    plugin._overflow_p90 = 14.14
    plugin._charge_below, plugin._drain_above = 2.0, 14.0
    plugin._r63_needed_kwh = 3.8
    plugin._publish_dispatch_policy(True, floor_kwh=8.0, soc_kwh=18.08 * 0.435, soc_max=18.08)
    attrs = base.published["sensor.predbat_curtailment_intended_policy"]["attrs"]
    assert "headroom_deadline_short_kwh" in attrs, "the deadline measurement must survive as a diagnostic: {}".format(sorted(attrs))
    print("  test_deadline_shortfall_is_published_as_a_diagnostic: PASSED")


def _soc_min_writes(base):
    return [c[1]["value"] for c in base.services if c[0] == "input_number/set_value" and c[1].get("entity_id") == PREDBAT_SOC_MIN_HELPER]


def _soc_min_run(pv_kw, load_kw, soc_pct=0.30, current=-1):
    """publish() with the dawn gap open or closed; `current` = helper's existing value."""
    base = _make_dawn_base(pv_kw=pv_kw, load_kw=load_kw, soc_pct=soc_pct)
    base._sensor_overrides[PREDBAT_SOC_MIN_HELPER] = current
    plugin = CurtailmentPlugin(base)
    for _ in range(DAWN_RELEASE_CONFIRM_CYCLES):
        plugin._evaluate_dawn_crossing()
    plugin._overflow_floor_kwh = 0.0
    plugin._effective_keep_kwh = 0.0
    plugin._p10_recovery_floor = 0.0
    plugin._session_protect_kwh = 0.0
    base.services.clear()
    plugin.publish("active", 0.0, dno_limit_kw=4.0)
    return plugin, base


def test_dawn_reserve_floors_predbat_export_plan():
    """RD34 — the dawn reserve must bind WHOEVER is draining.

    2026-08-11: Predbat exported to 3.4% overnight while CM's reserve sat at 10%.
    Nothing was broken — RD21/RD32 guard the dawn gap only while CM holds the
    wheel, and CM hands back every night, so on the nights that matter the
    reserve was inert.

    `best_soc_min` is the lever, proven live at 11:45: raised to 16%, every
    export target that had been 13% moved to exactly 16.0%, and NO charge window
    appeared. The two obvious alternatives are both wrong and were tried first:

      * `best_soc_keep` is a TARGET, not a floor — raising it makes Predbat
        IMPORT to reach it. Caught by test_before_plan_never_increases and
        stated outright in the R26 comment.
      * `set_reserve_min` is inert: SIG has `has_reserve_soc: False`, so Predbat
        never writes the reserve, and enabling it means patching config.py,
        which the charter forbids.
    """
    plugin, base = _soc_min_run(pv_kw=0.09, load_kw=0.37)
    writes = _soc_min_writes(base)
    expected = round(DEEP_DISCHARGE_FLOOR_KWH + plugin._dawn_load_kwh, 2)  # RD36: computed, not a fixed 10%
    assert writes, "in the dawn gap CM must floor Predbat's export plan"
    assert abs(writes[-1] - expected) < 0.01, "must write the dawn reserve {:.2f}, got {}".format(expected, writes[-1])
    print("  test_dawn_reserve_floors_predbat_export_plan: PASSED ({:.2f} kWh)".format(writes[-1]))


def test_export_floor_clears_when_pv_meets_load():
    """Released -> clear it, so the last stretch to the drain floor is available
    for headroom. Same one-way latch as the drain floor: one owner, both move
    together."""
    plugin, base = _soc_min_run(pv_kw=0.59, load_kw=0.42, current=1.81)
    writes = _soc_min_writes(base)
    assert plugin._dawn_released is True, "precondition: PV >= load must release"
    assert writes and writes[-1] == 0.0, "released reserve must clear the floor, got {}".format(writes)
    print("  test_export_floor_clears_when_pv_meets_load: PASSED")


def test_export_floor_not_rewritten_when_unchanged():
    """No redundant writes — the helper is read by the planner every cycle and a
    write storm is how `sig_keep_floor_pct` wedged on 2026-07-22."""
    plugin, base = _soc_min_run(pv_kw=0.09, load_kw=0.37, current=round(DEEP_DISCHARGE_FLOOR_KWH + MIN_BASE_LOAD_KW * 1.0, 2))
    assert not _soc_min_writes(base), "already correct -> must not rewrite, got {}".format(_soc_min_writes(base))
    print("  test_export_floor_not_rewritten_when_unchanged: PASSED")


def test_r63_floor_is_the_dawn_reserve_not_the_deep_floor():
    """RD32 — R63 must drain to the SAME floor the Schmitt does.

    Live 2026-08-08, the defect. RD21 armed correctly (drain_above 1.81 kWh =
    10.0%, source `dawn_reserve`) and the Schmitt stopped the drain there at
    05:25. Then R63 fired at 06:25 and ran the pack to 1.8% in the dark:

        06:25:27  R63 drain deadline — need 3.80 kWh headroom,
                  only 3.46 kWh sheddable before lockout 7.53Z -> Max Export

    R63 computed its OWN floor and never consulted `_dawn_floor_kwh`:

        r63_floor = max(reserve, DEEP_DISCHARGE_FLOOR_KWH,
                        soc_max * hard_floor_pct / 100.0)      # = 0.50 kWh

    so `drainable_kwh` read +0.80 instead of −0.51, the deadline "breached", and
    `_policy_override = "max_export"` outranks every floor below it. PV did not
    meet load until 06:52 — the reserve existed to cover exactly that gap.

    Cost that morning was zero only because load was 0.35 kW and PV arrived in
    time. On an overcast dawn, or with the DHW cycle an hour earlier, it is
    import at 25p to buy headroom the peak may not even need.
    """
    plugin = CurtailmentPlugin(_make_dawn_base(pv_kw=0.09, load_kw=0.37, soc_pct=0.072))
    expected = round(DEEP_DISCHARGE_FLOOR_KWH + plugin._dawn_load_kwh, 2)  # RD36
    floor = plugin._r63_floor_kwh(BATTERY_KWH)
    assert abs(floor - expected) < 0.01, "in the dawn gap R63's floor must be the {:.2f} kWh dawn reserve, got {:.2f}".format(expected, floor)
    print("  test_r63_floor_is_the_dawn_reserve_not_the_deep_floor: PASSED ({:.2f} kWh)".format(floor))


def test_r63_does_not_engage_below_the_dawn_reserve():
    """RD32, the consequence: the live 06:25 numbers must NOT force Max Export.

    Uses the real logged figures — need 3.80 kWh, 3.46 kWh sheddable, SOC 7.2%
    (1.30 kWh). The deadline IS breached on energy (3.80 > 3.46); the release is
    `drainable_kwh <= 0`, which only holds once R63's floor is the dawn reserve.
    """
    # RD36 shrank the reserve to the computed gap need, so the SOC that sits
    # below it is lower too: 4% (0.72 kWh) vs a 1.0 kWh floor.
    plugin = CurtailmentPlugin(_make_dawn_base(pv_kw=0.09, load_kw=0.37, soc_pct=0.040))
    soc_kwh = BATTERY_KWH * 0.040
    drainable = soc_kwh - plugin._r63_floor_kwh(BATTERY_KWH)
    assert drainable < 0, "SOC 4% is BELOW the dawn reserve, so there is nothing R63 may shed (got {:.2f} kWh)".format(drainable)
    assert not drain_deadline_breached(3.80, 3.46, engaged=False, drainable_kwh=drainable), "R63 must not force Max Export through the dawn reserve"
    print("  test_r63_does_not_engage_below_the_dawn_reserve: PASSED (drainable {:.2f} kWh)".format(drainable))


def test_r63_still_fires_above_the_dawn_reserve():
    """RD32 must not disarm R63 — it is the last-chance drain (R25).

    Same dawn gap, same unachievable deadline, but SOC at 60%: there is real
    headroom to make, so R63 must still engage. Without this the fix above would
    read as "R63 respects the reserve" while actually having switched it off.
    """
    plugin = CurtailmentPlugin(_make_dawn_base(pv_kw=0.09, load_kw=0.37, soc_pct=0.60))
    soc_kwh = BATTERY_KWH * 0.60
    drainable = soc_kwh - plugin._r63_floor_kwh(BATTERY_KWH)
    assert drainable > 0
    assert drain_deadline_breached(3.80, 3.46, engaged=False, drainable_kwh=drainable), "with 9 kWh above the reserve R63 must still force the drain"
    print("  test_r63_still_fires_above_the_dawn_reserve: PASSED (drainable {:.2f} kWh)".format(drainable))


def test_session_dump_still_sells_to_the_overnight_reserve():
    """RD20 must survive RD32: a SAVING-SESSION dump is not a curtailment drain,
    so it still stops at the overnight reserve.

    Same override value ("max_export"), different reason — `_r63_engaged` is what
    separates them. Without this test the RD32 fix would silently re-open the
    session under-sell RD20 was written to close.
    """
    base = MockBase()
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    plugin = CurtailmentPlugin(base)
    plugin._charge_below, plugin._drain_above = 0.5, 1.81
    plugin._overnight_target_kwh = 6.87  # 38%
    plugin._policy_override = "max_export"
    plugin._r63_engaged = False
    base.services.clear()
    plugin._publish_dispatch_policy(True, floor_kwh=0.0, soc_kwh=8.0, soc_max=18.08)
    kf = _keep_floor_calls(base)
    assert kf and abs(kf[-1] - 38.0) <= 1, "a session dump must still stop at the 38% overnight reserve, got {}".format(kf)
    print("  test_session_dump_still_sells_to_the_overnight_reserve: PASSED ({}%)".format(kf[-1]))


def _session_publish_run(session):
    """Shared rig: a MODERATE overflow day, with and without an upcoming session.

    Overflow is deliberately sized so `overflow_floor` (~6.1 kWh) is the winning
    arm on its own — a bigger overflow drives the floor under the 0.5 kWh deep
    floor and the contrast under test disappears. With the session,
    `session_protect` (overnight 6.0 + 60 min × 4.0 kW cap = 10.0) takes over.
    This is the live 2026-08-03 shape: a real overflow floor, overridden by a
    session the sensor never mentioned."""
    pv = {m: 8.0 for m in range(0, 480, PLUGIN_STEP)}
    load = {m: 1.0 for m in range(0, 480, PLUGIN_STEP)}
    sensor_overrides = {"sensor.sigen_plant_pv_power": 8.0, "sensor.sigen_plant_consumed_power": 1.0}
    sensor_overrides.update(_make_p90_sensors(p90_peak_kw=7.0, solcast_remaining=14.0))
    if session:
        # Start must be REAL relative to the rig clock (now_utc 2025-07-12 12:00).
        # This was "2026-08-03T19:00:00+01:00" — a session a YEAR ahead — which
        # only ever exercised the reserve because there was no horizon at all
        # (2026-08-11). A fixture that cannot happen tests nothing.
        sensor_overrides.update(_saving_session_sensors(active=False, next_mins=60, next_start="2025-07-12T19:00:00+00:00"))
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=BATTERY_KWH * 0.55,
        minutes_now=720,
        best_soc_keep=4.0,
        sensor_overrides=sensor_overrides,
    )
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    # RD37: dull P10 so REACHABILITY does not decide this test. The subject is
    # the session reserve raising the floor and naming itself as the arm; with a
    # sunny P10 the reserve correctly stands aside and there is nothing to see.
    base.pv_forecast_minute_step10 = {m: 0.05 * (PLUGIN_STEP / 60.0) for m in range(0, 24 * 60, PLUGIN_STEP)}
    plugin = CurtailmentPlugin(base)
    plugin._peak_pv = 6.0
    plugin._overnight_target_kwh = 6.0
    plugin.on_update()
    return base


def test_drain_above_publishes_its_source():
    """The Headroom Floor sensor must name the arm that set it.

    Live 2026-08-03: state 10.22 kWh, friendly_name "Headroom Floor (P90
    overflow)", attributes `overflow_p90_kwh: 12.28, overflow_floor_kwh: 3.39`.
    Every number on the sensor argued for 3.39 and nothing accounted for the
    other 6.83 kWh — which was the upcoming 19:00 saving session. The floor was
    correct; it was unauditable from the dashboard.
    """
    with_session = _session_publish_run(session=True).published["sensor.predbat_curtailment_drain_above"]
    without = _session_publish_run(session=False).published["sensor.predbat_curtailment_drain_above"]

    wa, wo = with_session["attrs"], without["attrs"]

    assert wa["source"] == "session_protect", f"session must be named as the driver, got {wa.get('source')}"
    assert wo["source"] == "overflow_floor", f"no session -> overflow drives the floor, got {wo.get('source')}"

    # The session terms must be present as numbers, not left to be inferred.
    assert wa["session_reserve_kwh"] > 0, "session reserve missing from the sensor that it set"
    assert abs(wa["session_protect_kwh"] - with_session["value"]) < 0.01, f"session_protect {wa['session_protect_kwh']} should equal the published floor {with_session['value']}"
    assert wa["session_start"], "session start time missing — 'when' is half the explanation"

    # A day with no session must stay clean: zeroes, not stale values.
    assert not wo["session_reserve_kwh"], f"no session -> reserve must be 0/None, got {wo['session_reserve_kwh']}"
    assert wo["session_start"] is None
    print(f"  test_drain_above_publishes_its_source: PASSED ({wo['source']} {without['value']} -> {wa['source']} {with_session['value']})")


def test_why_this_mode_reports_session_reserve():
    """The Why This Mode card REPORTS plugin attributes and must never re-derive
    (Charter). So a drain floor held up by a saving session has to reach
    `intended_policy` as attributes and be named in `reason` — otherwise the card
    shows a band whose upper edge it cannot explain.
    """
    base = _session_publish_run(session=True)
    attrs = base.published["sensor.predbat_curtailment_intended_policy"]["attrs"]

    assert attrs["drain_above_source"] == "session_protect", f"card needs the floor's driver, got {attrs.get('drain_above_source')}"
    assert attrs["session_reserve_kwh"] > 0
    assert attrs["session_reserve_pct"] > 0, "card is % SOC at a glance (A0) — kWh alone is not enough"
    assert attrs["session_start"]

    # The card must be able to name WHY the drain floor is where it is. That now
    # rides on the source label (rendered as "drain if above N% (saving session)")
    # rather than being restated in `reason` — see
    # test_reason_does_not_repeat_the_structured_attributes.
    assert attrs["drain_above_source_label"] == "saving session", attrs["drain_above_source_label"]

    # And a no-session day must not mention one.
    plain = _session_publish_run(session=False).published["sensor.predbat_curtailment_intended_policy"]["attrs"]
    assert "session" not in plain["reason"].lower(), f"no session -> reason must not mention one: {plain['reason']}"
    assert plain["drain_above_source"] == "overflow_floor"
    assert "session" not in plain["drain_above_source_label"]
    print(f"  test_why_this_mode_reports_session_reserve: PASSED ({attrs['reason']})")


def test_session_dump_is_published_as_the_effective_policy():
    """During a LIVE saving session the card must show what the heartbeat is
    actually dispatching, not the plugin's own Schmitt wish.

    Observed live 2026-08-03 19:13, mid-session: battery -3.84 kW, export
    3.68 kW at the cap (dispatching correctly), while Why This Mode read
    "→ Hold Battery / Hold · surplus fits". RD14c moved session DISPATCH to the
    heartbeat, which forces Max Export off the CALENDAR and never writes
    `sig_dispatch_policy` — so the select, and every consumer of it, keeps
    showing the pre-session policy.

    `curtailment_plugin.py` already documents the precedence as
    `override > session > select` (the RD13a comment) but only ever implemented
    the override layer. This is the same defect as 2026-07-29 08:44 / 3dca0d06:
    the sensor reporting the plugin's preference instead of the policy in force.
    """
    base = MockBase()
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    base._sensor_overrides[SIG_SAVING_SESSION_CALENDAR] = "on"
    plugin = CurtailmentPlugin(base)
    # A band that makes the plugin want Hold — exactly the live 19:13 shape.
    plugin._charge_below, plugin._drain_above = 6.2, 18.08
    plugin._policy_override = "no_drain"
    plugin._publish_dispatch_policy(True, floor_kwh=18.08, soc_kwh=9.4, soc_max=18.08)

    pub = base.published["sensor.predbat_curtailment_intended_policy"]
    assert pub["value"] == "Max Export", f"session dump is the policy in force, got {pub['value']}"
    assert pub["attrs"]["session_dispatch"] is True, "card needs to know the heartbeat (not the select) is driving"
    reason = pub["attrs"]["reason"].lower()
    assert "session" in reason, f"reason must name the session: {pub['attrs']['reason']}"
    assert "hold" in reason, f"the plugin's own wish must survive into the reason: {pub['attrs']['reason']}"
    print(f"  test_session_dump_is_published_as_the_effective_policy: PASSED ({pub['attrs']['reason']})")


def test_session_dump_respects_the_heartbeat_precedence_exactly():
    """The plugin's display must mirror the heartbeat's effective-policy rule,
    term for term — a display that disagrees with the dispatcher is worse than
    no display. The heartbeat (sig_dispatch_heartbeat.yaml) computes:

        policy = override        if override active
                 else 'Max Export' if (session_live and raw_policy != 'Predbat')
                 else raw_policy

    Two consequences pinned here:
      - a MANUAL override still outranks the session dump (a human can see
        something we cannot);
      - after handback (policy Predbat) a live session must NOT claim Max
        Export — Predbat owns the machine and the heartbeat deliberately stands
        down rather than putting two writers on the registers.
    """
    # 1. Manual override outranks the session.
    base = MockBase()
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    base._sensor_overrides[SIG_SAVING_SESSION_CALENDAR] = "on"
    base._sensor_overrides[SIG_OVERRIDE_SELECT] = "Hold Battery"
    plugin = CurtailmentPlugin(base)
    plugin._charge_below, plugin._drain_above = 6.2, 18.08
    plugin._publish_dispatch_policy(True, floor_kwh=18.08, soc_kwh=9.4, soc_max=18.08)
    pub = base.published["sensor.predbat_curtailment_intended_policy"]
    assert pub["value"] == "Hold Battery", f"manual override outranks the session, got {pub['value']}"
    assert "manual" in pub["attrs"]["reason"].lower()

    # 2. Handed back to Predbat -> the session must not claim the wheel.
    base2 = MockBase()
    base2._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    base2._sensor_overrides[SIG_SAVING_SESSION_CALENDAR] = "on"
    plugin2 = CurtailmentPlugin(base2)
    plugin2._publish_dispatch_policy(False, floor_kwh=0.0, soc_kwh=9.4, soc_max=18.08)
    pub2 = base2.published["sensor.predbat_curtailment_intended_policy"]
    assert pub2["value"] == "Predbat", f"after handback the session must not force Max Export, got {pub2['value']}"
    assert not pub2["attrs"]["session_dispatch"]
    print("  test_session_dump_respects_the_heartbeat_precedence_exactly: PASSED")


def test_session_end_soc_projection():
    """Projected SOC when the saving session ends.

    During the dump the battery supplies the export cap plus house load, less
    whatever PV is still coming in — the same quantity the heartbeat commands,
    so the projection matches the dispatch rather than a noisy instantaneous
    battery reading. Clamped at the sell floor because the keep-floor guard
    stops the discharge there.
    """
    # 30 min left, 3.68 kW cap + 0.5 kW load, no PV -> 2.09 kWh out of 9.0.
    end = estimate_session_end_kwh(soc_kwh=9.0, cap_kw=3.68, load_kw=0.5, pv_kw=0.0, minutes_remaining=30, floor_kwh=0.0)
    assert abs(end - (9.0 - (4.18 * 0.5))) < 0.01, end

    # PV offsets the draw.
    end_pv = estimate_session_end_kwh(soc_kwh=9.0, cap_kw=3.68, load_kw=0.5, pv_kw=1.0, minutes_remaining=30, floor_kwh=0.0)
    assert end_pv > end, "PV during the session must reduce the battery draw"

    # The guard stops the sell at the floor — never project through it.
    clamped = estimate_session_end_kwh(soc_kwh=7.0, cap_kw=3.68, load_kw=0.5, pv_kw=0.0, minutes_remaining=120, floor_kwh=6.46)
    assert abs(clamped - 6.46) < 1e-6, f"must clamp at the sell floor, got {clamped}"

    # No time left -> no change. Degenerate inputs must not explode.
    assert abs(estimate_session_end_kwh(9.0, 3.68, 0.5, 0.0, 0, 0.0) - 9.0) < 1e-6
    assert abs(estimate_session_end_kwh(9.0, 3.68, 0.5, 0.0, -5, 0.0) - 9.0) < 1e-6
    print("  test_session_end_soc_projection: PASSED")


def test_session_end_soc_is_published_during_the_dump():
    """The card should answer "where will this leave me?" while it is dumping —
    that is the number you actually want mid-session, and it is not derivable
    from anything else on the card."""
    base = MockBase()
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    base._sensor_overrides[SIG_SAVING_SESSION_CALENDAR] = "on"
    base._sensor_overrides.update(_saving_session_sensors(active=True, current_mins=60, current_end="2026-08-03T20:00:00+01:00"))
    plugin = CurtailmentPlugin(base)
    plugin._charge_below, plugin._drain_above = 6.2, 18.08
    plugin._publish_dispatch_policy(True, floor_kwh=18.08, soc_kwh=9.0, soc_max=18.08)
    attrs = base.published["sensor.predbat_curtailment_intended_policy"]["attrs"]
    assert attrs["session_end_soc_pct"] is not None, "mid-dump the card must project where the session leaves us"
    assert 0 <= attrs["session_end_soc_pct"] <= 100
    assert attrs["session_end_soc_pct"] < round(9.0 / 18.08 * 100, 1), "exporting at the cap must project DOWN"

    # No session -> nothing to project, and no stale number left lying around.
    plain = MockBase()
    plain._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    p2 = CurtailmentPlugin(plain)
    p2._charge_below, p2._drain_above = 6.2, 18.08
    p2._publish_dispatch_policy(True, floor_kwh=18.08, soc_kwh=9.0, soc_max=18.08)
    assert plain.published["sensor.predbat_curtailment_intended_policy"]["attrs"]["session_end_soc_pct"] is None
    print("  test_session_end_soc_is_published_during_the_dump: PASSED")


def test_no_overflow_left_is_not_reported_as_surplus_fits():
    """ "surplus fits" is a non-statement when the p90 forecast is zero — there is
    no surplus to fit. Observed on the card 2026-08-03 19:29 with
    overflow_p90 = 0.0: "↑ fits · 53% spare" and "· surplus fits", both of which
    describe a comparison against nothing.

    `no_drain` covers two different situations and they deserve different words:
    the surplus genuinely fits in the remaining headroom, or there is no surplus
    forecast left at all.
    """
    base = MockBase()
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    plugin = CurtailmentPlugin(base)
    plugin._charge_below, plugin._drain_above = 6.2, 18.08
    plugin._policy_override = "no_drain"
    plugin._overflow_p90 = 0.0
    plugin._publish_dispatch_policy(True, floor_kwh=18.08, soc_kwh=8.1, soc_max=18.08)
    attrs = base.published["sensor.predbat_curtailment_intended_policy"]["attrs"]
    assert attrs["override_label"] != "surplus fits", "with p90=0 there is no surplus to fit"
    assert "no overflow" in attrs["override_label"].lower(), attrs["override_label"]
    assert "surplus fits" not in attrs["reason"], attrs["reason"]
    # And the headroom comparison must be suppressed, not published as a number
    # the card will dutifully render.
    assert attrs["headroom_short_pct"] is None, "no forecast overflow -> no headroom verdict to show"

    # With a real forecast overflow the original wording stands.
    base2 = MockBase()
    base2._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    p2 = CurtailmentPlugin(base2)
    p2._charge_below, p2._drain_above = 6.2, 18.08
    p2._policy_override = "no_drain"
    p2._overflow_p90 = 4.0
    p2._publish_dispatch_policy(True, floor_kwh=18.08, soc_kwh=8.1, soc_max=18.08)
    a2 = base2.published["sensor.predbat_curtailment_intended_policy"]["attrs"]
    assert a2["override_label"] == "surplus fits", a2["override_label"]
    assert a2["headroom_short_pct"] is not None
    print("  test_no_overflow_left_is_not_reported_as_surplus_fits: PASSED")


def _dusk_rig(now_local, minutes_now, pv_kw, latched=False, session=False):
    """Plugin already through its PV day, now at dusk with `pv_kw` on the array."""
    from datetime import datetime, timezone

    pv, load = _make_overflow_pv(minutes_now=720)
    overrides = {"sensor.sigen_plant_pv_power": 8.0, "sensor.sigen_plant_consumed_power": 1.0}
    overrides.update(_make_p90_sensors())
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=BATTERY_KWH * 0.40,
        minutes_now=720,
        best_soc_keep=4.0,
        now_utc=datetime(2025, 7, 12, 12, 0, tzinfo=timezone.utc),
        sensor_overrides=overrides,
    )
    plugin = CurtailmentPlugin(base)
    plugin.on_update()
    assert plugin.last_phase == "active", "rig must start active (mid-overflow)"

    base._sensor_overrides["sensor.sigen_plant_pv_power"] = pv_kw
    if session:
        base._sensor_overrides[SIG_SAVING_SESSION_CALENDAR] = "on"
    base.minutes_now = minutes_now
    base.now_utc = now_local
    plugin._sundown_latched = latched
    plugin.on_update()
    return plugin


def test_sundown_gated_on_solar_elevation():
    """PV alone is too noisy to detect sunset — gate it on where the sun actually is.

    Ten nights of live phase transitions (2026-07-25 .. 2026-08-03) separate
    perfectly on solar elevation at the first "Off":

        flapped:  10.5, 12.7, 9.4 deg    (3 nights, 2-6 extra transitions each)
        clean:     5.4, 3.8, 2.9, 3.2, 4.4, 4.2, 2.5 deg   (7 nights, one handback each)

    No overlap, 4.0 deg of margin, at the REAL site latitude (52.31N). NOTE the
    rig below runs at MockBase's hardcoded 55.86N, so the elevations asserted here
    are the rig's, not the site's — what the test pins is the BEHAVIOUR either side
    of the gate, which is latitude-independent. The physics: at 11-15 deg the sun is still
    meaningfully up, so PV below 100 W is a CLOUD, not sunset — CM hands back,
    PV recovers, CM re-takes the wheel, and each toggle of read_only forces a
    full inverter reset (docs/customisation.md:38).

    Elevation is monotonic through dusk, so this cannot flap by construction —
    unlike a dwell timer, which only makes flapping slower.
    """
    from datetime import datetime, timezone

    # 11.1 deg — above the gate, reproducing a false sundown.
    high = _dusk_rig(datetime(2025, 7, 12, 19, 10, tzinfo=timezone.utc), 20 * 60 + 10, pv_kw=0.03)
    assert high.last_phase == "active", "sun still 11.1 deg up — PV≈0 is a cloud, not sunset"

    # 4.9 deg — below the gate, reproducing a genuine handback.
    low = _dusk_rig(datetime(2025, 7, 12, 20, 0, tzinfo=timezone.utc), 21 * 60, pv_kw=0.03)
    assert low.last_phase == "off", "sun down to 4.9 deg with PV≈0 — genuine sundown"
    print("  test_sundown_gated_on_solar_elevation: PASSED (11.1 deg stays active, 4.9 deg hands back)")


def test_sundown_latches_for_the_day():
    """Below the gate, a PV blip must not re-take the wheel.

    2026-08-03: CM handed back at 19:40 then re-activated at 19:41:49, 19:50:30
    and 20:05:09 as PV bounced 0.03 -> 0.22 -> 0.15 kW. The elevation gate stops
    the early false sundown; the latch covers residual noise BELOW the gate, and
    covers midwinter, when peak elevation at the real site (~14 deg at 52.31N)
    still clears 8.0 only briefly.
    """
    from datetime import datetime, timezone

    # Latched, sun low, and PV has bounced well above the 0.1 threshold.
    p = _dusk_rig(datetime(2025, 7, 12, 20, 0, tzinfo=timezone.utc), 21 * 60, pv_kw=0.9, latched=True)
    assert p.last_phase == "off", "latched below the gate — a PV blip must not re-activate"

    assert p._sundown_latched, "latch must stay armed"

    # It must survive a deploy, or an evening restart re-takes the wheel — which
    # is exactly what happened on 2026-08-03 (PHASE none -> active at 20:05).
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        p.base.config_root = tmp
        p._save_state()
        revived = CurtailmentPlugin(p.base)
        assert revived._sundown_latched, "sundown latch must persist across a restart"
    print("  test_sundown_latches_for_the_day: PASSED (latched + persisted)")


def test_sundown_latch_cannot_arm_while_the_sun_is_high():
    """The latch is only safe because the gate guards it.

    Without that, one heavy afternoon storm (PV -> 0 at high sun) would latch CM
    off for the rest of a big-overflow day — turning a display-level annoyance
    into lost generation. Arming is only permitted below the elevation gate.
    """
    from datetime import datetime, timezone

    # 14:00 BST, sun ~45 deg up, PV crushed to zero by a storm.
    p = _dusk_rig(datetime(2025, 7, 12, 13, 0, tzinfo=timezone.utc), 14 * 60, pv_kw=0.0)
    assert p.last_phase == "active", "a mid-afternoon PV collapse is not sundown"
    assert not p._sundown_latched, "the latch must not arm while the sun is high"
    print("  test_sundown_latch_cannot_arm_while_the_sun_is_high: PASSED")


def test_sundown_latch_still_defers_to_a_live_session():
    """RD14c-sundown must survive the latch: a joined session outranks both."""
    from datetime import datetime, timezone

    p = _dusk_rig(datetime(2025, 7, 12, 20, 0, tzinfo=timezone.utc), 21 * 60, pv_kw=0.03, latched=True, session=True)
    assert p.last_phase == "active", "a live session must keep CM driving even when latched"
    print("  test_sundown_latch_still_defers_to_a_live_session: PASSED")


def test_cm_owns_a_session_through_every_discretionary_handback():
    """While a joined session is live, CM OWNS it — no discretionary handback.

    The heartbeat can only force Max Export while CM holds the wheel AND the
    select is not `Predbat` (RD14c). So *any* path that hands back mid-session
    stops the sell, not just sundown. Guarding sundown alone left three other
    routes open:

      - "no PV yet" (peak never cleared 0.1 kW — a winter-evening session)
      - "p90_scale<0.5" (Solcast missing or stale)
      - R4 defer to a Predbat charge window (GSHP heating active)

    Each now holds position and keeps the wheel instead of handing back. Hold is
    the right neutral posture: the forecast machinery is unusable on these paths,
    so CM should not drive the Schmitt off it — but it must still own the plant so
    the heartbeat can dispatch.

    Genuine fail-closed paths (no soc_max, no location, no clock, unreadable SOC)
    are deliberately NOT guarded — those must still refuse to act.
    """
    from datetime import datetime, timezone

    def _run(session, pv_kw, solcast_ok=True):
        pv = {m: 0.0 for m in range(0, 480, PLUGIN_STEP)}
        load = {m: 0.5 for m in range(0, 480, PLUGIN_STEP)}
        overrides = {"sensor.sigen_plant_pv_power": pv_kw, "sensor.sigen_plant_consumed_power": 0.5}
        if solcast_ok:
            overrides.update(_make_p90_sensors())
        if session:
            overrides[SIG_SAVING_SESSION_CALENDAR] = "on"
        base = MockBase(
            pv_step=pv,
            load_step=load,
            soc_kw=BATTERY_KWH * 0.50,
            minutes_now=20 * 60,
            best_soc_keep=4.0,
            now_utc=datetime(2025, 7, 12, 19, 0, tzinfo=timezone.utc),
            sensor_overrides=overrides,
        )
        plugin = CurtailmentPlugin(base)
        _floor, phase = plugin.calculate(dno_limit_kw=4.0)
        return plugin, phase

    # "no PV yet" — peak never cleared 0.1 kW (winter evening session).
    _p, off_phase = _run(session=False, pv_kw=0.0)
    assert off_phase == "off", f"baseline: no PV and no session -> off, got {off_phase}"
    plugin, phase = _run(session=True, pv_kw=0.0)
    assert phase == "active", f"session live -> CM must own the plant even with no PV, got {phase}"
    assert plugin._policy_override == "hold", f"neutral posture, not a Schmitt drive: {plugin._policy_override}"

    # Solcast missing — cannot compute floors, but must not release the wheel.
    _p2, off2 = _run(session=False, pv_kw=3.0, solcast_ok=False)
    assert off2 == "off", f"baseline: no Solcast and no session -> off, got {off2}"
    p3, phase3 = _run(session=True, pv_kw=3.0, solcast_ok=False)
    assert phase3 == "active", f"session live -> keep the wheel despite no Solcast, got {phase3}"
    assert p3._policy_override == "hold"
    print("  test_cm_owns_a_session_through_every_discretionary_handback: PASSED")


def test_r4_defer_does_not_release_the_wheel_mid_session():
    """R4 hands the machine to a Predbat charge window when GSHP heating is on and
    SOC is below keep. Mid-session that would stop the dump, so the session wins."""
    pv = {m: 0.0 for m in range(0, 480, PLUGIN_STEP)}
    load = {m: 0.5 for m in range(0, 480, PLUGIN_STEP)}
    overrides = {"sensor.sigen_plant_pv_power": 3.0, "sensor.sigen_plant_consumed_power": 0.5}
    overrides.update(_make_p90_sensors())
    overrides[SIG_SAVING_SESSION_CALENDAR] = "on"
    base = MockBase(pv_step=pv, load_step=load, soc_kw=1.0, minutes_now=20 * 60, best_soc_keep=8.0, sensor_overrides=overrides)
    # Make the defer branch genuinely reachable: GSHP heating on (its default),
    # SOC well below keep, and a live Predbat charge window that is not a freeze.
    base.charge_window_best = [{"start": 0, "end": 1440, "average": 10.0}]
    base.charge_limit_best = [BATTERY_KWH]
    plugin = CurtailmentPlugin(base)
    plugin._r4_deferring = False
    assert plugin._is_gshp_ch_active(), "rig must have GSHP active or the branch is unreachable"
    plugin.on_update()
    assert plugin.last_phase != "off", "a live session outranks the R4 charge-window defer"
    assert not plugin._r4_deferring, "and must not leave the defer latch armed"

    # Without the session the defer stands.
    del base._sensor_overrides[SIG_SAVING_SESSION_CALENDAR]
    p2 = CurtailmentPlugin(base)
    p2._r4_deferring = False
    p2.on_update()
    assert p2.last_phase == "off", f"no session -> R4 defer still hands back, got {p2.last_phase}"
    print("  test_r4_defer_does_not_release_the_wheel_mid_session: PASSED")


def test_reason_does_not_repeat_the_structured_attributes():
    """`reason` is the one-line WHY. The numbers belong to the attributes, which
    the card renders as its own lines — so reason must not restate them.

    Observed on the card 2026-08-04, every fact printed twice:

        Hold · surplus fits · 9% · band 3-19%     <- reason
        9% · band 3-19%                           <- from soc_pct / *_pct attrs
        drain if above 19% (P90 overflow)         <- from drain_above_source_label
        fits · 10% spare                          <- from headroom_short_pct
        · surplus fits                            <- from override_label

    SOC, the band, the drain-floor source and the override label each appeared
    twice. Keep them in the attributes (where the card can format them and other
    consumers can read them as numbers), out of the prose.
    """
    base = MockBase()
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    plugin = CurtailmentPlugin(base)
    plugin._charge_below, plugin._drain_above = 0.5, 3.5
    plugin._policy_override = "no_drain"
    plugin._overflow_p90 = 4.0
    plugin._publish_dispatch_policy(True, floor_kwh=3.5, soc_kwh=1.6, soc_max=18.08)
    attrs = base.published["sensor.predbat_curtailment_intended_policy"]["attrs"]
    reason = attrs["reason"]

    assert "band" not in reason, f"band is published as charge_below_pct/drain_above_pct: {reason}"
    assert "%" not in reason, f"percentages belong in the attributes, not the prose: {reason}"
    # The WHY must survive.
    assert "Hold" in reason and attrs["override_label"] in reason, reason
    # And the attributes must still carry everything the card needs.
    for key in ("soc_pct", "charge_below_pct", "drain_above_pct", "override_label", "drain_above_source_label", "headroom_short_pct"):
        assert attrs[key] is not None, f"card needs {key}"
    print(f"  test_reason_does_not_repeat_the_structured_attributes: PASSED ({reason})")


def test_session_reserve_accounts_for_discharge_losses():
    """To DELIVER cap x duration through the meter, the battery must give up more.

    compute_session_reserve returned raw energy at the cap, so a 60 min session at
    3.68 kW reserved 3.68 kWh — but at Predbat's own discharge_efficiency of 0.947
    that only delivers ~3.49 kWh, and the dump runs short near the end of the paid
    hour. Spotted by Andrew 2026-08-05: published 20.4% of an 18.08 kWh battery
    where it should be ~21.5%.

    Same factor Predbat applies in its own predict trajectory, and the same one
    the R55 morning_gap already uses — battery_loss_discharge x inverter_loss.
    """
    # No efficiency given -> unchanged, so existing callers keep their behaviour.
    assert abs(compute_session_reserve(60, 3.68) - 3.68) < 1e-6

    # 60 min at the 3.68 kW cap, 0.947 efficiency.
    out = compute_session_reserve(60, 3.68, discharge_efficiency=0.947)
    assert abs(out - 3.68 / 0.947) < 1e-6, out
    assert out > 3.68, "reserving less than the deliverable energy under-sells the session"
    assert abs(out - 3.885) < 0.002, out

    # Half an hour scales linearly.
    assert abs(compute_session_reserve(30, 3.68, discharge_efficiency=0.947) - (3.68 / 0.947) / 2) < 1e-6

    # Degenerate efficiencies must not divide by zero or inflate absurdly.
    for bad in (0, -1, None, "x"):
        got = compute_session_reserve(60, 3.68, discharge_efficiency=bad)
        assert 3.68 <= got <= 3.68 / 0.5, f"efficiency {bad} must clamp, got {got}"
    print(f"  test_session_reserve_accounts_for_discharge_losses: PASSED (3.68 -> {out:.3f} kWh)")


def test_sundown_defers_for_an_IMMINENT_session_not_just_a_live_one():
    """A session starting minutes after sundown is the worst case.

    2026-08-05: session at 20:00, and the night before sundown fired at 20:00:53.
    If sundown lands first CM hands back, sets the select to Predbat and disables
    the heartbeat — and the heartbeat only forces Max Export while the select is
    NOT Predbat, so the dump never starts. CM would re-take on its next cycle
    (RD14-own), losing up to 5 minutes of a 60 minute paid window, and only if
    nothing else has grabbed the wheel meanwhile.

    Deferring for an imminent session removes the race entirely: CM simply does
    not hand back in the half hour before one.
    """
    from datetime import datetime, timedelta, timezone

    def _dusk(next_start_offset_min):
        pv, load = _make_overflow_pv(minutes_now=720)
        ov = {"sensor.sigen_plant_pv_power": 8.0, "sensor.sigen_plant_consumed_power": 1.0}
        ov.update(_make_p90_sensors())
        base = MockBase(pv_step=pv, load_step=load, soc_kw=BATTERY_KWH * 0.40, minutes_now=720, best_soc_keep=4.0, now_utc=datetime(2025, 7, 12, 12, 0, tzinfo=timezone.utc), sensor_overrides=ov)
        plugin = CurtailmentPlugin(base)
        plugin.on_update()
        assert plugin.last_phase == "active"
        # Genuine dusk: PV below 0.1 kW and sun below the gate (20:00 UTC, doy 193).
        now = datetime(2025, 7, 12, 20, 0, tzinfo=timezone.utc)
        base._sensor_overrides["sensor.sigen_plant_pv_power"] = 0.03
        base.minutes_now = 21 * 60
        base.now_utc = now
        if next_start_offset_min is not None:
            start = (now + timedelta(minutes=next_start_offset_min)).isoformat()
            base._sensor_overrides.update(_saving_session_sensors(active=False, next_mins=60, next_start=start))
        plugin.on_update()
        return plugin

    # No session -> normal sundown handback.
    assert _dusk(None).last_phase == "off", "without a session, dusk must still hand back"
    # Session 15 min away -> hold the wheel.
    assert _dusk(15).last_phase == "active", "must not hand back minutes before a session"
    # Session 6 hours away -> irrelevant, hand back normally.
    assert _dusk(360).last_phase == "off", "a distant session must not pin CM active all evening"
    print("  test_sundown_defers_for_an_IMMINENT_session_not_just_a_live_one: PASSED")


def test_forecast_bands_survive_a_datetime_period_start():
    """HA stores Solcast `period_start` as a datetime, not an ISO string.

    Both scale parsers slice it as a string (`ps[11:16]`), which raises
    TypeError on a datetime — and TypeError was NOT in the caught tuple
    (ValueError, IndexError, KeyError), so it escaped to the caller's broad
    `except Exception` and silently degraded to the (0, 0, cached_p90) fallback.

    Live 2026-08-05: p10_scale=0.0, p50_scale=0.0, p90_scale=10.16 — exactly the
    fallback signature, while the real forecast (p10 4.42 / p50 7.28 / p90 8.27
    kW peaks) should have given roughly 5.96 / 9.01 / 10.16. So R50's band spread
    has been collapsing to the p90 fallback in production, unnoticed, because the
    caller treats a missing band as "unusual but not fatal".

    str() first: "2026-08-05T11:00" and "2026-08-05 11:00" both slice to "11:00"
    at [11:16], so one normalisation covers both shapes.
    """
    from datetime import datetime, timezone, timedelta

    lat, lon, doy, off = 52.31, -1.41, 217, 1.0
    raw = [
        {"period_start": "2026-08-05T11:00:00+01:00", "pv_estimate10": 4.417, "pv_estimate": 6.801, "pv_estimate90": 7.4458},
        {"period_start": "2026-08-05T13:30:00+01:00", "pv_estimate10": 4.0927, "pv_estimate": 7.2777, "pv_estimate90": 8.2564},
    ]
    bst = timezone(timedelta(hours=1))
    as_dt = [dict(r, period_start=datetime.fromisoformat(r["period_start"]).astimezone(bst)) for r in raw]

    str_bands = p_scales_from_forecast(raw, lat, lon, doy, off)
    dt_bands = p_scales_from_forecast(as_dt, lat, lon, doy, off)
    assert all(b > 0 for b in str_bands), f"ISO strings must parse: {str_bands}"
    assert all(b > 0 for b in dt_bands), f"datetime period_start must parse too: {dt_bands}"
    for a, b in zip(str_bands, dt_bands):
        assert abs(a - b) < 0.01, f"both shapes must give the same scale: {str_bands} vs {dt_bands}"

    # And the p90-only parser, which has the identical slice.
    assert p90_scale_from_forecast(as_dt, lat, lon, doy, off)[0] > 0, "p90 parser must accept a datetime too"

    # The bands must stay ORDERED — a collapsed spread is what the fallback gives,
    # and it is indistinguishable from a genuinely narrow forecast unless checked.
    p10, p50, p90 = dt_bands
    assert p10 < p50 < p90, f"bands must remain ordered and distinct: {dt_bands}"
    print(f"  test_forecast_bands_survive_a_datetime_period_start: PASSED ({[round(x, 2) for x in dt_bands]})")


def test_forecast_tracking_band():
    """Which Solcast band is today's sky actually tracking?

    Without this, "was the forecast wrong or was the control wrong?" is
    unanswerable after the fact — the plugin computes three overflow integrals
    and blends them, but never said which one the day resembled.

    Real observations: 2026-08-04 ran actual_scale 11.78 against p90_scale 9.22,
    i.e. the day BEAT its own p90 — precisely the case that should read
    "above p90" rather than being silently clipped to it.
    """
    # Ordered bands, actual sitting in each region.
    assert classify_forecast_tracking(5.0, 7.0, 9.0, 11.0)[0] == "below p10"
    assert classify_forecast_tracking(8.0, 7.0, 9.0, 11.0)[0] == "p10-p50"
    assert classify_forecast_tracking(10.0, 7.0, 9.0, 11.0)[0] == "p50-p90"
    assert classify_forecast_tracking(11.78, 7.0, 9.22, 9.22)[0] == "above p90", "2026-08-04 beat its p90 — must not clip to it"

    # Percentile lands on the anchors.
    assert classify_forecast_tracking(7.0, 7.0, 9.0, 11.0)[1] == 10.0
    assert classify_forecast_tracking(9.0, 7.0, 9.0, 11.0)[1] == 50.0
    assert classify_forecast_tracking(11.0, 7.0, 9.0, 11.0)[1] == 90.0
    # Monotonic between them.
    assert classify_forecast_tracking(8.0, 7.0, 9.0, 11.0)[1] == 30.0

    # Degenerate inputs must say "unknown", never invent a band. A missing
    # Solcast band reads 0.0 (p_scales_from_forecast), which would otherwise
    # make every day look like it was beating the forecast.
    for bad in ((0, 7, 9, 11), (9, 0, 0, 0), ("x", 7, 9, 11), (9, 11, 9, 7)):
        assert classify_forecast_tracking(*bad) == ("unknown", None), bad
    print("  test_forecast_tracking_band: PASSED")


def test_tracking_band_is_too_early_before_enough_energy():
    """RD33 — at dawn there is no day to track yet, so the band must say so.

    Live 2026-08-08 07:10: actual 0.28 kWh against expected-to-now
    p10/p50/p90 = 0.57 / 0.60 / 0.63. The card read "sky tracking below p10
    (0%)" off 0.06 kWh of band spread — a confident verdict on the day, an hour
    after sunrise, on a day that went on to forecast 55.8 kWh p50 remaining.

    `day_tracking_ratio` already refuses this (min_expected_kwh); the classifier
    had no such gate. "too early" is a distinct state from "unknown": unknown
    means the bands are unusable, too early means they are fine but the day has
    not happened yet.
    """
    label, pct = classify_forecast_tracking(0.28, 0.57, 0.60, 0.63)
    assert label == "too early", "0.60 kWh expected so far cannot support a verdict, got '{}'".format(label)
    assert pct is None, "no percentile may be published before the band means anything, got {}".format(pct)
    print("  test_tracking_band_is_too_early_before_enough_energy: PASSED")


def test_tracking_band_reports_no_spread_when_bands_collapse():
    """RD33 — confident Solcast collapses the bands, and there is then nothing to
    interpolate on.

    Live 2026-08-07: whole-day peak bands 10.12 / 10.15 / 10.23. Any small miss
    saturates a 0.11 kWh span and prints "below p10 (0%)" mid-morning. Reported
    separately from "too early" because the cause and the cure differ — this one
    never improves as the day goes on.
    """
    label, pct = classify_forecast_tracking(9.80, 10.12, 10.15, 10.23)
    assert label == "no spread", "a 0.11 kWh p10-p90 span cannot discriminate, got '{}'".format(label)
    assert pct is None, "got {}".format(pct)
    print("  test_tracking_band_reports_no_spread_when_bands_collapse: PASSED")


def test_tracking_band_still_calls_a_real_miss():
    """RD33 must not gag the metric. A genuine shortfall on discriminating bands,
    late enough in the day to mean something, must still read "below p10"."""
    label, pct = classify_forecast_tracking(18.0, 24.0, 30.0, 36.0)
    assert label == "below p10", "a 6 kWh miss on a 12 kWh span is a real call, got '{}'".format(label)
    assert pct == 0.0, "extrapolated a full span below p10 clamps at 0, got {}".format(pct)
    print("  test_tracking_band_still_calls_a_real_miss: PASSED")


def test_tracking_band_published_for_debugging():
    """It has to reach the phase sensor or it is not a debug metric."""
    pv = {m: 8.0 for m in range(0, 480, PLUGIN_STEP)}
    load = {m: 1.0 for m in range(0, 480, PLUGIN_STEP)}
    overrides = {"sensor.sigen_plant_pv_power": 8.0, "sensor.sigen_plant_consumed_power": 1.0}
    overrides.update(_make_p90_sensors(p90_peak_kw=10.0, solcast_remaining=45.0, all_bands=True))
    # RD29: the band is now ENERGY-based, so the rig needs slots that have already
    # COMPLETED by minutes_now (the shared fixture's only slot starts exactly at
    # 12:00 = minutes_now, so it contributes nothing) plus a daily PV reading.
    # Two 30-min slots -> p10 2.0 / p50 4.0 / p90 6.0 kWh expected so far.
    fc = overrides["sensor.solcast_pv_forecast_forecast_today"]["detailedForecast"]
    early = [
        {"period_start": "2025-07-12T10:00:00+00:00", "pv_estimate10": 2.0, "pv_estimate": 4.0, "pv_estimate90": 6.0},
        {"period_start": "2025-07-12T11:00:00+00:00", "pv_estimate10": 2.0, "pv_estimate": 4.0, "pv_estimate90": 6.0},
    ]
    overrides["sensor.solcast_pv_forecast_forecast_today"] = {"detailedForecast": early + fc}
    overrides["sensor.sigen_plant_daily_pv_energy"] = 3.0  # between p10 2.0 and p50 4.0
    base = MockBase(pv_step=pv, load_step=load, soc_kw=BATTERY_KWH * 0.55, minutes_now=720, best_soc_keep=4.0, sensor_overrides=overrides)
    plugin = CurtailmentPlugin(base)
    plugin._peak_pv = 9.0
    plugin._peak_pv_time = 720
    plugin.on_update()
    attrs = base.published["sensor.predbat_curtailment_phase"]["attrs"]
    for key in ("p10_scale", "p50_scale", "p90_scale", "actual_scale", "tracking_band", "tracking_pct"):
        assert key in attrs, f"phase sensor missing {key}"
    # Assert the SPECIFIC band. The old form allowed "unknown" — the failure mode
    # itself — and passed on the exact defect it existed to catch, twice (Charter).
    assert attrs["tracking_band"] == "p10-p50", "3.0 kWh actual against p10 2.0 / p50 4.0 must be p10-p50, got {}".format(attrs["tracking_band"])
    assert abs(attrs["pv_actual_kwh"] - 3.0) < 0.01
    assert abs(attrs["pv_expected_p50_kwh"] - 4.0) < 0.01, attrs["pv_expected_p50_kwh"]

    # The band scales must be populated by the ACTIVE path, not just the pre-PV
    # ones. Instrumenting only the forecast-publish call sites left both at their
    # init 0.0 every cycle, so the band read "unknown" all day — correct-looking
    # code, no value (observed live 2026-08-05 13:02).
    assert attrs["p50_scale"] > 0, f"active path must populate p50_scale, got {attrs['p50_scale']}"
    assert attrs["p10_scale"] > 0, f"active path must populate p10_scale, got {attrs['p10_scale']}"
    assert attrs["tracking_band"] != "unknown", "populated scales must yield a real band"
    print(f"  test_tracking_band_published_for_debugging: PASSED ({attrs['tracking_band']} @ {attrs['tracking_pct']}%)")


def test_v32_drain_floor_drives_between_2_8_and_5pct():
    """v32: with the single drain floor (2.8%), the plugin keeps driving Max Export
    between 2.8% and the old 5% — SOC 0.7 kWh (3.9%) drives, SOC 0.4 kWh (2.2%)
    hands to MSC. (Old 5% handover would have handed to MSC at 3.9%.)"""
    base = MockBase()
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    plugin = CurtailmentPlugin(base)
    plugin._charge_below, plugin._drain_above = 0.5, 0.9
    plugin._policy_override = None
    base.services.clear()
    # 0.7 kWh / 18.08 = 3.9% > 2.8% → drive (SOC above drain_above 0.9? no, 0.7<0.9
    # → Charge). Point is it does NOT hand to MSC.
    plugin._publish_dispatch_policy(True, floor_kwh=0.9, soc_kwh=0.7, soc_max=18.08)
    assert _policy_calls(base) and _policy_calls(base)[-1] != "Predbat", f"3.9% > 2.8% floor must drive, not MSC: {base.services}"
    base.services.clear()
    # RD27: below the floor CM still drives — the drain floor governs SELLING (the
    # heartbeat clamp), never who holds the wheel.
    plugin._publish_dispatch_policy(True, floor_kwh=0.9, soc_kwh=0.4, soc_max=18.08)
    assert _policy_calls(base) and _policy_calls(base)[-1] != "Predbat", f"2.2% < floor must still DRIVE (RD27), got {base.services}"
    print("  test_v32_drain_floor_drives_between_2_8_and_5pct: PASSED")


def test_policy_is_reasserted_when_the_select_drifts():
    """The plugin must re-assert its intended policy whenever the LIVE select
    disagrees — not merely when its own decision changes.

    `_set_policy` compares against `get_state_wrapper(SIG_POLICY_SELECT)`, so
    anything that moves the select externally (a manual tap on the dashboard
    tile, the keep-floor guard) is corrected on the next cycle. This is what
    keeps RD13's invariant honest: either the plugin drives, or
    the override select says it doesn't. There is no third state where someone
    else quietly owns the inverter.

    Untested until 2026-07-28, when a single missing log line was misread as
    "the plugin only writes on its own change" and nearly became a redundant
    re-assert loop. It already re-asserts; this pins it.
    """
    base = MockBase()
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    plugin = CurtailmentPlugin(base)
    # SOC 5.0 kWh sits BELOW charge_below -> intent is Solar Charge, so a select
    # hand-set to Hold genuinely disagrees. (27.6% is clear of the 2.8% handover.)
    plugin._charge_below, plugin._drain_above = 8.0, 15.0
    plugin._policy_override = None

    base._sensor_overrides[SIG_POLICY_SELECT] = "Hold Battery"
    base.services.clear()
    plugin._publish_dispatch_policy(True, floor_kwh=15.0, soc_kwh=5.0, soc_max=18.08)
    drifted = _policy_calls(base)
    assert drifted, f"a drifted select must be re-asserted, got no write: {base.services}"
    assert drifted[-1] != "Hold Battery", f"must correct away from the hand-set value: {drifted}"

    # Select already matches intent -> no write, so we don't spam it every cycle.
    base._sensor_overrides[SIG_POLICY_SELECT] = drifted[-1]
    base.services.clear()
    plugin._publish_dispatch_policy(True, floor_kwh=15.0, soc_kwh=5.0, soc_max=18.08)
    assert not _policy_calls(base), f"matching select must not be rewritten: {base.services}"

    # The case that distinguishes a LIVE comparison from a cached one: the
    # plugin's own decision is UNCHANGED and only the select has drifted away
    # again. A cached "did I already write this?" check would skip here and
    # silently leave the hand-set value in force. Must re-assert.
    base._sensor_overrides[SIG_POLICY_SELECT] = "Hold Battery"
    base.services.clear()
    plugin._publish_dispatch_policy(True, floor_kwh=15.0, soc_kwh=5.0, soc_max=18.08)
    again = _policy_calls(base)
    assert again == [drifted[-1]], f"unchanged intent + drifted select must still re-assert: {base.services}"
    print(f"  test_policy_is_reasserted_when_the_select_drifts: PASSED (re-asserted {drifted[-1]})")


def test_manual_override_does_not_reassert_the_policy():
    """RD13: under manual override the user owns the POLICY SELECT, so a drifted
    select is left alone. This is the sanctioned way to hold a policy — and the
    reason re-assertion above is safe to be unconditional otherwise."""
    base = MockBase()
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    base._sensor_overrides[SIG_OVERRIDE_SELECT] = "Hold Battery"
    base._sensor_overrides[SIG_POLICY_SELECT] = "Hold Battery"
    plugin = CurtailmentPlugin(base)
    plugin._charge_below, plugin._drain_above = 0.5, 0.9
    plugin._policy_override = None
    base.services.clear()
    plugin._publish_dispatch_policy(True, floor_kwh=0.9, soc_kwh=0.7, soc_max=18.08)
    assert not _policy_calls(base), f"manual override must not write the policy select: {base.services}"
    print("  test_manual_override_does_not_reassert_the_policy: PASSED")


def test_v32_keep_floor_min_is_drain_floor_not_5():
    """v32: on a huge-overflow CURTAILMENT drain (Schmitt Drain, no override) the
    published keep-floor reaches the drain floor (2.8%), not the old hardcoded 5%.
    A floor_kwh of 0.5 kWh publishes ~2.8% (v32.3: curtailment drain uses floor_kwh)."""
    base = MockBase()
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    plugin = CurtailmentPlugin(base)
    plugin._charge_below, plugin._drain_above = 0.5, 0.5  # deep drain target
    # override None + SOC above drain_above → Schmitt Drain (curtailment drain)
    base.services.clear()
    plugin._publish_dispatch_policy(True, floor_kwh=0.5, soc_kwh=6.0, soc_max=18.08)
    assert _policy_calls(base) == ["Max Export"], base.services
    kf = _keep_floor_calls(base)
    assert kf and 2.5 <= kf[-1] <= 3.0, f"keep floor should clamp to ~2.8%, not 5%, got {kf}"
    print("  test_v32_keep_floor_min_is_drain_floor_not_5: PASSED")


def test_v32_drain_floor_helper_override():
    """v32/RD27: sig_drain_floor_pct is honoured as the SELL floor, not a handover.

    Set to 6% with SOC at 5%: the plugin keeps the wheel (RD27) and the published
    keep-floor is clamped to the helper — it is the heartbeat's sell-clamp that
    stops the battery being sold, not a change of driver."""
    base = MockBase()
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    base._sensor_overrides["input_number.sig_drain_floor_pct"] = "6"
    plugin = CurtailmentPlugin(base)
    plugin._charge_below, plugin._drain_above = 0.5, 0.9
    base.services.clear()
    # 0.9 kWh / 18.08 = 5.0% < 6% → MSC
    plugin._publish_dispatch_policy(True, floor_kwh=0.9, soc_kwh=0.9, soc_max=18.08)
    assert _policy_calls(base) and _policy_calls(base)[-1] != "Predbat", f"5% < 6% helper floor must still DRIVE (RD27), got {base.services}"
    keep = [v["value"] for svc_name, v in base.services if svc_name == "input_number/set_value" and v.get("entity_id") == "input_number.sig_keep_floor_pct"]
    assert keep and keep[-1] >= 6.0, f"keep floor must be clamped up to the 6% helper, got {keep}"
    print("  test_v32_drain_floor_helper_override: PASSED")


def test_v32_pre_pv_hold_no_dawn_flap():
    """v32 dawn-flap fix: once the pre-PV drain has fired, if PV hasn't arrived yet
    (actual_pv<0.1) and overflow is still forecast, the plugin HOLDS active instead
    of handing back to Predbat — no off↔active flap at the 0.1 kW dawn boundary."""
    pv = {m: 0.0 for m in range(0, 720, PLUGIN_STEP)}
    load = {m: 0.5 for m in range(0, 720, PLUGIN_STEP)}
    sensor_overrides = {"sensor.sigen_plant_pv_power": 0.05, "sensor.sigen_plant_consumed_power": 0.5}
    sensor_overrides.update(_make_p90_sensors(p90_peak_kw=10.0, solcast_remaining=45.0))
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=0.4,  # already drained below the pre-PV target → decision returns None
        minutes_now=240,  # 04:00 — pre-dawn, PV not yet
        best_soc_keep=4.0,
        sensor_overrides=sensor_overrides,
    )
    plugin = CurtailmentPlugin(base)
    plugin._pre_pv_engaged_today = True  # the pre-PV drain already fired this morning
    floor, phase = plugin.calculate(dno_limit_kw=4.0)
    assert phase == "active", f"v32: hold active after pre-PV drain done, got {phase}"
    assert plugin._policy_override == "hold", f"v32: dawn hold override, got {plugin._policy_override}"
    print("  test_v32_pre_pv_hold_no_dawn_flap: PASSED")


def test_v32_1_no_drain_allows_charge_for_evening():
    """v32.1: under the no_drain override (overflow fits / past safe), SOC below
    charge_below → Solar Charge Battery — bank PV for the evening reserve. This is
    the case v32's blanket Hold masked (overcast/low-overflow day)."""
    base = MockBase()
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    plugin = CurtailmentPlugin(base)
    # Late-day recovery need has lifted charge_below above the low SOC.
    plugin._charge_below, plugin._drain_above = 7.0, 14.0
    plugin._policy_override = "no_drain"
    base.services.clear()
    plugin._publish_dispatch_policy(True, floor_kwh=7.0, soc_kwh=1.4, soc_max=18.08)  # 7.7% < charge_below 7.0kWh(39%)
    assert _policy_calls(base) == ["Solar Charge Battery"], f"v32.1: no_drain + low SOC → Solar Charge (bank for evening), got {base.services}"
    print("  test_v32_1_no_drain_allows_charge_for_evening: PASSED")


def test_v32_1_no_drain_suppresses_drain():
    """v32.1: under no_drain, SOC above drain_above must NOT Max Export (that's the
    round-trip we're avoiding) — it clamps to Hold."""
    base = MockBase()
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    plugin = CurtailmentPlugin(base)
    plugin._charge_below, plugin._drain_above = 2.0, 8.0
    plugin._policy_override = "no_drain"
    base.services.clear()
    plugin._publish_dispatch_policy(True, floor_kwh=8.0, soc_kwh=16.0, soc_max=18.08)  # 88% > drain_above
    assert _policy_calls(base) == ["Hold Battery"], f"v32.1: no_drain must clamp Drain→Hold, got {base.services}"
    print("  test_v32_1_no_drain_suppresses_drain: PASSED")


def test_v32_1_no_drain_holds_in_band():
    """v32.1: under no_drain, SOC between the thresholds → Hold (unchanged)."""
    base = MockBase()
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    plugin = CurtailmentPlugin(base)
    plugin._charge_below, plugin._drain_above = 2.0, 14.0
    plugin._policy_override = "no_drain"
    base.services.clear()
    plugin._publish_dispatch_policy(True, floor_kwh=8.0, soc_kwh=8.0, soc_max=18.08)
    assert _policy_calls(base) == ["Hold Battery"], f"v32.1: no_drain in-band → Hold, got {base.services}"
    print("  test_v32_1_no_drain_holds_in_band: PASSED")


def test_v32_1_pure_hold_override_never_charges():
    """v32.1: the pure 'hold' override (pre-PV dawn wait) must NOT charge even at
    low SOC — we just drained for headroom and there's no surplus to bank."""
    base = MockBase()
    base._sensor_overrides["input_boolean.sig_plugin_policy_control"] = "on"
    plugin = CurtailmentPlugin(base)
    plugin._charge_below, plugin._drain_above = 7.0, 14.0
    plugin._policy_override = "hold"
    base.services.clear()
    plugin._publish_dispatch_policy(True, floor_kwh=7.0, soc_kwh=1.4, soc_max=18.08)
    assert _policy_calls(base) == ["Hold Battery"], f"v32.1: pure hold must stay Hold (no Charge), got {base.services}"
    print("  test_v32_1_pure_hold_override_never_charges: PASSED")


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
        test_morning_gap_cloudy_dawn,
        test_morning_gap_kwh_values,
        test_morning_gap_zero_zero_slots_do_not_terminate_walk,
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
        # R59 P10 recovery floor (proposed)
        test_p10_recovery_floor_huge_pv_runway,
        test_p10_recovery_floor_no_pv_remaining,
        test_p10_recovery_floor_partial_charging,
        test_p10_recovery_floor_load_exceeds_pv,
        test_drain_above_curtailment_buffer_only,
        test_drain_above_overflow_floor_wins_on_big_overflow,
        test_drain_above_reserve_floor,
        test_drain_above_deep_discharge_floor,
        test_charge_below_deep_discharge_floor,
        test_charge_below_soc_keep_wins,
        test_charge_below_p10_recovery_wins,
        test_R50a_floor_uses_p90_not_the_confidence_blend,
        test_R50a_incident_day_still_floored_by_r59b,
        test_override_keeps_cm_driving_even_when_the_select_says_predbat,
        test_intended_policy_reports_the_override_not_the_plugins_wish,
        test_override_is_the_select_alone_no_boolean,
        test_session_dispatch_belongs_to_the_heartbeat_not_the_plugin,
        test_session_reserve_is_not_advertised_when_it_is_not_held,
        test_session_reserve_is_advertised_once_actually_held,
        test_session_reserve_does_not_block_a_drain_pv_will_refill,
        test_session_reserve_still_binds_when_pv_cannot_refill,
        test_session_reserve_reachability_ignores_pv_after_the_session,
        test_session_reserve_does_not_arm_days_ahead,
        test_session_reserve_arms_in_time_for_the_morning_drain,
        test_session_reserve_horizon_boundary,
        test_session_reserve_still_protects_the_drain_floor,
        test_required_headroom_is_defined_once,
        test_no_drain_and_floor_agree_when_r49_reduces_the_buffer,
        test_recovery_floor_is_a_single_quantity,
        test_overflow_smoothing_rejects_a_single_spike,
        test_overflow_smoothing_tracks_the_real_trend,
        test_overflow_smoothing_lags_conservatively_not_optimistically,
        test_overflow_smoothing_degrades_safely_on_short_history,
        test_R11_removed_floor_follows_the_formula_down,
        test_no_drain_uses_the_same_safety_margin_as_the_headroom_floor,
        test_R16a_schmitt_hysteresis_stops_the_drain_flap,
        test_R63_does_not_force_drain_when_nothing_is_drainable,
        test_R63_shed_rate_inverts_once_pv_exceeds_the_cap,
        test_R63_max_sheddable_integrates_a_falling_rate,
        test_R63_deadline_breach_fires_only_when_drain_is_unachievable,
        test_R63_draining_clears_the_breach_it_must_not_latch,
        test_R63_hysteresis_band_stops_boundary_chatter,
        test_R63_fires_before_the_plain_energy_test_would,
        test_charge_recovery_floor_nets_against_generation_not_overflow,
        test_charge_recovery_floor_ramps_up_as_generation_runs_out,
        test_charge_recovery_floor_overcast_day_charges,
        test_charge_recovery_floor_matches_drain_side_recovery,
        test_no_surplus_hold_dawn_collapse,
        test_no_surplus_hold_target_above_soc_unchanged,
        test_no_surplus_hold_surplus_allows_drain,
        test_huge_day_drain_budget_r61_r52,
        test_p10_recovery_floor_today_2026_05_08_cloudy,
        test_p10_recovery_floor_ignores_p50,
        test_p10_recovery_floor_calibration_ratio_ignored,
        test_p10_recovery_floor_late_afternoon_pessimistic,
        test_p10_recovery_floor_genuine_cloudy_day,
        test_p10_recovery_floor_zero_target,
        test_p10_recovery_floor_today_at_11_03,
        test_p10_recovery_floor_combines_with_r54_min,
        # R60 effective export cap (proposed)
        test_effective_cap_no_history_returns_dno,
        test_effective_cap_today_data_wins,
        test_effective_cap_few_samples_falls_back_to_yesterday,
        test_effective_cap_no_today_with_yesterday,
        test_effective_cap_clamped_to_hard_floor,
        test_effective_cap_clamped_to_dno_ceiling,
        test_effective_cap_today_30min_typical_day,
        test_effective_cap_today_actual_last_hour,
        # R4 defer to Predbat — gshp gate
        test_r4_defer_gshp_off_no_defer_even_when_low,
        test_r4_defer_gshp_on_low_soc_defers,
        test_r4_defer_gshp_on_above_release_no_defer,
        test_r4_defer_gshp_on_in_hysteresis_was_deferring,
        test_r4_defer_gshp_on_in_hysteresis_was_not_deferring,
        test_r4_defer_gshp_off_high_soc_no_defer,
        # R54-with-source diagnostic
        test_floor_source_effective_keep_wins,
        test_floor_source_overflow_floor_wins,
        test_floor_source_p10_recovery_binds,
        test_floor_source_reserve_binds,
        test_floor_source_tie_picks_inner_min_over_others,
        test_floor_source_today_yesterday_morning,
        # Split-threshold proposed phase (shadow mode)
        test_proposed_phase_hold_in_band,
        test_phase_to_policy_mapping,
        test_dispatch_policy_gated_off_publishes_intended_only,
        test_dispatch_policy_drives_hold_when_enabled,
        test_dispatch_policy_max_export_high_soc,
        test_day_tracking_ratio,
        test_overflow_tracking_published_and_below_p90,
        test_at_risk_kwh_is_raw_energy_not_padded_percent,
        test_at_risk_is_zero_when_the_overflow_fits,
        test_risk_verdict_is_not_fooled_by_the_sun_merely_being_up,
        test_advice_uses_the_actual_day_not_p90,
        test_advice_falls_back_to_the_forecast_before_the_day_has_data,
        test_advice_shortfall_is_computed_once_not_three_times,
        test_advice_shortfall_never_goes_negative,
        test_risk_verdict_is_absent_when_there_is_no_overflow_question,
        test_publishes_the_tracking_margin_beside_the_p90_verdict,
        test_risk_verdict_names_the_tight_case_instead_of_calling_it_early,
        test_risk_verdict_says_fits_only_when_the_plugin_has_decided_it,
        test_risk_verdict_reports_risk_early_because_that_direction_is_safe,
        test_forecast_energy_to_now_integrates_the_slots,
        test_tracking_band_from_energy_not_peak,
        test_no_overflow_charge_target_is_the_overnight_need,
        test_no_overflow_charge_target_never_eats_the_headroom,
        test_no_drain_branch_charges_to_the_overnight_need,
        test_no_drain_branch_holds_once_the_need_is_met,
        test_no_drain_still_never_drains,
        test_card_publishes_the_threshold_actually_in_force,
        test_low_soc_never_hands_back_mid_window,
        test_low_soc_drain_stays_max_export_not_handback,
        test_dispatch_policy_handback_once_on_deactivate,
        test_sell_floor_overnight_reserve_when_not_draining,
        test_sell_floor_overflow_floor_during_curtailment_drain,
        test_sell_floor_session_dumps_to_overnight_reserve,
        test_read_only_set_when_cm_driving,
        test_read_only_released_on_handback,
        test_read_only_held_through_low_soc,
        test_read_only_untouched_observe_only,
        test_manual_override_keeps_machine_live_skips_policy,
        test_manual_override_off_resumes_policy,
        test_manual_override_grabs_control_even_when_inactive,
        test_manual_override_writer_follows_the_override_not_the_select,
        test_heartbeat_enabled_on_control,
        test_exactly_one_writer_enabled_on_control,
        test_predbat_neutralised_before_its_chain_is_frozen,
        test_exactly_one_writer_enabled_on_handback,
        test_first_run_reconciles_drifted_writers,
        test_heartbeat_disabled_and_msc_on_handback,
        test_heartbeat_untouched_observe_only,
        test_heartbeat_stays_on_through_low_soc,
        test_proposed_phase_charge_below_floor,
        test_proposed_phase_drain_above_ceiling,
        test_proposed_phase_today_7am_actual,
        test_proposed_phase_off_when_plugin_inactive,
        test_proposed_phase_cross_over_charges_to_lower_threshold,
        test_proposed_phase_normal_day_unchanged,
        test_proposed_phase_thresholds_collapse_at_sunset,
        test_floor_computation,
        test_floor_above_soc_keep,
        # v19 tapered-cap tests
        test_cap_taper_at_peak_overflow,
        test_cap_taper_near_safe_time,
        test_cap_at_safe_time_hits_100,
        test_plugin_cap_taper_near_safe_time,
        test_cap_taper_ratchet_noise_immune,
        # v20 dynamic buffer reduction
        test_buffer_reduces_on_cloudy_afternoon,
        test_buffer_unchanged_on_clear_afternoon,
        test_buffer_unchanged_before_14_00,
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
        test_plugin_floor_not_clamped_by_soc_keep,
        test_r48_triggers_after_overnight_100pct,
        test_r48_latches_once_engaged,
        test_plugin_active_high_soc,
        test_floor_clamped_above_soc_keep,
        test_floor_clamped_above_reserve,
        test_floor_lower_with_more_overflow,
        test_plugin_handles_local_tz_aware_now_utc,
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
        test_on_update_full_flow,
        test_on_update_stays_off_low_pv,
        test_holds_past_safe_time_until_sundown,
        test_sundown_defers_while_a_saving_session_is_live,
        test_sundown_gated_on_solar_elevation,
        test_sundown_latches_for_the_day,
        test_sundown_latch_cannot_arm_while_the_sun_is_high,
        test_sundown_latch_still_defers_to_a_live_session,
        test_cm_owns_a_session_through_every_discretionary_handback,
        test_r4_defer_does_not_release_the_wheel_mid_session,
        test_reason_does_not_repeat_the_structured_attributes,
        test_session_reserve_accounts_for_discharge_losses,
        test_sundown_defers_for_an_IMMINENT_session_not_just_a_live_one,
        test_forecast_bands_survive_a_datetime_period_start,
        test_forecast_tracking_band,
        test_tracking_band_is_too_early_before_enough_energy,
        test_tracking_band_reports_no_spread_when_bands_collapse,
        test_tracking_band_still_calls_a_real_miss,
        test_tracking_band_published_for_debugging,
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
        test_floor_soft_ratchet,
        test_floor_ratchet_allows_decrease,
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

    # State file persistence tests
    state_tests = [
        test_save_state_atomic_against_partial_write,
        test_save_state_round_trip_with_pv_history,
        test_load_state_logs_corruption,
    ]
    print("  --- state persistence tests ---")
    for test_fn in state_tests:
        try:
            test_fn()
        except Exception as e:
            print(f"  {test_fn.__name__}: FAILED — {e}")
            failed = True

    # R50 confidence-weighted overflow tests
    r50_tests = [
        test_R50_p_scales_from_forecast,
        test_R50_compute_expected_overflow_high_confidence,
        test_R50_compute_expected_overflow_mid_confidence,
        test_R50_compute_expected_overflow_low_confidence,
        test_R50_compute_expected_overflow_zero_confidence,
        test_R50_compute_expected_overflow_apr_28_incident,
        test_R50_compute_expected_overflow_clamps_confidence,
        test_R50_compute_expected_overflow_at_boundaries,
    ]
    print("  --- R50 confidence-weighted overflow tests ---")
    for test_fn in r50_tests:
        try:
            test_fn()
        except Exception as e:
            print(f"  {test_fn.__name__}: FAILED — {e}")
            failed = True

    # R9a load smoothing tests (v20)
    r9a_tests = [
        test_R9a_smooth_load_forecast_constant_input,
        test_R9a_smooth_load_forecast_attenuates_single_spike,
        test_R9a_smoothed_integral_stable_against_load_noise,
    ]
    print("  --- R9a load smoothing tests ---")
    for test_fn in r9a_tests:
        try:
            test_fn()
        except Exception as e:
            print(f"  {test_fn.__name__}: FAILED — {e}")
            failed = True

    # R54 / R56 / R57 plugin behaviour tests (v20)
    r54_56_57_tests = [
        test_R54_target_uses_keep_when_lower_than_overflow_floor,
        test_R54_target_uses_overflow_when_lower_than_keep,
        test_R57_no_chase_to_soc_max_late_in_day,
        test_off_at_sundown_backstop,
        test_no_dusk_reactivation_after_peak_reset,
    ]
    print("  --- R54/R56/R57 plugin behaviour tests ---")
    for test_fn in r54_56_57_tests:
        try:
            test_fn()
        except Exception as e:
            print(f"  {test_fn.__name__}: FAILED — {e}")
            failed = True

    # R55 overnight_target sensor tests (v20)
    r55_tests = [
        test_R55_overnight_target_published_on_overflow_day,
        test_R55_overnight_target_formula_components,
        test_R55_overnight_target_window_extended_to_tomorrow,
        test_R55_overnight_target_published_when_no_overflow,
        test_R55_overnight_target_published_from_calculate_with_real_pv_step,
        test_R55_overnight_target_no_plan_yet_fallback,
        test_R55_on_before_plan_does_not_clobber_calculate_overnight_target,
        test_R55_target_soc_uses_overnight_when_off,
        test_R55_target_soc_uses_floor_when_active,
        test_numeric_sensors_carry_state_class,
        test_R9_overflow_safety_factor_is_1_05,
        test_floor_component_is_winner_matches_source_label,
        test_R55_safety_pct_helper_clamps_range,
        test_R55_overnight_target_raises_effective_keep_in_calculate,
    ]
    print("  --- R55 overnight_target sensor tests ---")
    for test_fn in r55_tests:
        try:
            test_fn()
        except Exception as e:
            print(f"  {test_fn.__name__}: FAILED — {e}")
            failed = True

    # R53 per-slot Solcast integral tests (v20)
    r53_tests = [
        test_pv_covers_load_minute_finds_the_real_crossing,
        test_pv_covers_load_ignores_a_blip,
        test_pv_covers_load_returns_none_when_it_never_crosses,
        test_R53_compute_solcast_overflow_empty_returns_zero,
        test_R53_compute_solcast_overflow_uniform_sunny_slot,
        test_global_scale_applies_beyond_the_r58_calibration_window,
        test_global_scale_is_non_linear_in_pv,
        test_r58_calibration_window_still_behaves,
        test_R53_compute_solcast_overflow_preserves_day_shape,
        test_R53_compute_solcast_overflow_uses_load_forecast,
        test_R53_real_2026_05_02_shape_preserved,
        test_R53_real_last_10_days_no_crash_and_sane,
        test_R53_compute_solcast_overflow_band_selection,
        test_R58_calibration_ratio_one_is_identity,
        test_R58_calibration_ratio_only_affects_window,
        test_R58_calibration_ratio_capped_at_15x,
        test_R58_calibration_below_one_attenuates_window,
        test_R53_plugin_uses_solcast_slots_when_available,
    ]
    print("  --- R53 per-slot Solcast integral tests ---")
    for test_fn in r53_tests:
        try:
            test_fn()
        except Exception as e:
            print(f"  {test_fn.__name__}: FAILED — {e}")
            failed = True

    # R52 pre-PV drain tests
    r52_tests = [
        test_R52_compute_pv_start_time_summer,
        test_R52_compute_pv_start_time_winter_low_scale,
        test_R52_compute_pv_start_time_called_post_crossing,
        test_R52_compute_pv_start_time_threshold_at_dno,
        test_solcast_stale_date_rejected,
        test_solcast_datacorrect_false_rejected,
        test_solcast_current_date_accepted,
        test_R52_pre_pv_drain_blocked_by_ch_active,
        test_R52_pre_pv_drain_too_early,
        test_R52_pre_pv_drain_active_at_drain_start,
        test_R62_pre_pv_target_huge_confident_day,
        test_R62_pre_pv_target_moderate_day_legacy_ceiling,
        test_R62_pre_pv_target_low_confidence_stays_legacy,
        test_R62_pre_pv_target_reserve_wins,
        test_R62_pre_pv_publish_thresholds_not_stale,
        test_R52_pre_pv_drain_already_below_target,
        test_R52_pre_pv_drain_low_overflow_forecast,
        test_R52_pre_pv_drain_no_flap_once_started,
    ]
    print("  --- R52 pre-PV drain tests ---")
    for test_fn in r52_tests:
        try:
            test_fn()
        except Exception as e:
            print(f"  {test_fn.__name__}: FAILED — {e}")
            failed = True

    # v32 evening lifecycle (overflow_fits→Hold, sundown deactivate, sessions)
    v32_tests = [
        test_v32_no_deactivate_past_safe_time_holds,
        test_v32_overflow_fits_holds_not_off,
        test_v32_overflow_does_not_fit_schmitt_drives,
        test_v32_sundown_still_deactivates,
        test_v32_saving_session_plugin_stays_active_but_delegates_dispatch,
        test_v32_upcoming_session_raises_drain_floor,
        test_two_floors_are_named_and_sourced_distinctly,
        test_drain_above_source_mirrors_compute_drain_above,
        test_dawn_margin_is_load_over_the_error_window,
        test_dawn_margin_uses_the_load_after_the_crossing_not_before,
        test_dawn_margin_floors_at_base_load,
        test_dawn_reserve_survives_pv_start,
        test_single_crossing_sample_does_not_release_the_reserve,
        test_sustained_crossing_releases,
        test_interrupted_crossing_resets_the_count,
        test_crossing_counted_once_per_cycle_not_per_read,
        test_dawn_reserve_released_by_measured_crossover,
        test_released_floor_comes_from_the_live_helper,
        test_dawn_release_latches_against_midday_cloud,
        test_dawn_reserve_uses_forecast_dawn_load_when_larger,
        test_dawn_release_latch_persists_and_resets_daily,
        test_r63_never_forces_max_export_through_a_session_reserve,
        test_deadline_shortfall_is_published_as_a_diagnostic,
        test_dawn_reserve_floors_predbat_export_plan,
        test_export_floor_clears_when_pv_meets_load,
        test_export_floor_not_rewritten_when_unchanged,
        test_r63_floor_is_the_dawn_reserve_not_the_deep_floor,
        test_r63_does_not_engage_below_the_dawn_reserve,
        test_r63_still_fires_above_the_dawn_reserve,
        test_session_dump_still_sells_to_the_overnight_reserve,
        test_drain_above_publishes_its_source,
        test_session_dump_is_published_as_the_effective_policy,
        test_session_dump_respects_the_heartbeat_precedence_exactly,
        test_session_end_soc_projection,
        test_session_end_soc_is_published_during_the_dump,
        test_no_overflow_left_is_not_reported_as_surplus_fits,
        test_why_this_mode_reports_session_reserve,
        test_v32_drain_floor_drives_between_2_8_and_5pct,
        test_policy_is_reasserted_when_the_select_drifts,
        test_manual_override_does_not_reassert_the_policy,
        test_v32_keep_floor_min_is_drain_floor_not_5,
        test_v32_drain_floor_helper_override,
        test_v32_pre_pv_hold_no_dawn_flap,
        test_v32_1_no_drain_allows_charge_for_evening,
        test_v32_1_no_drain_suppresses_drain,
        test_v32_1_no_drain_holds_in_band,
        test_v32_1_pure_hold_override_never_charges,
    ]
    print("  --- v32 evening lifecycle tests ---")
    for test_fn in v32_tests:
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
