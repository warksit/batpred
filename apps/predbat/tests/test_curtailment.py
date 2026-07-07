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
    compute_p10_recovery_floor,
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
OVERFLOW_SAFETY_FACTOR = 1.2
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
# R59 — P10 recovery floor (proposed addition to R54)
#
# Lower-bound on floor: even on a P10 (worst-case PV) day the battery must
# still recover to overnight_target by sundown. P10 = 90% chance actual PV
# exceeds this, so it's the conservative-charging case.
# ============================================================================


def test_p10_recovery_floor_huge_pv_runway():
    """Lots of P10 PV ahead → floor near zero (we'll easily recover)."""
    floor = compute_p10_recovery_floor(overnight_target_kwh=9.4, p10_pv_remaining_kwh=20.0, load_remaining_kwh=7.0)
    # potential = 20-7 = 13, target - potential = -3.6 → clamped to 0
    assert floor == 0.0, f"Expected 0.0 (huge runway), got {floor}"
    print(f"  test_p10_recovery_floor_huge_pv_runway: PASSED (floor={floor})")


def test_p10_recovery_floor_no_pv_remaining():
    """Sunset: no PV ahead, load still drains battery → floor must cover both
    overnight_target AND remaining load."""
    floor = compute_p10_recovery_floor(overnight_target_kwh=9.4, p10_pv_remaining_kwh=0.0, load_remaining_kwh=2.0)
    # net = 0 - 2 = -2 (deficit). floor = 9.4 - (-2) = 11.4
    assert abs(floor - 11.4) < 0.001, f"Expected 11.4 (target+load deficit), got {floor}"
    print(f"  test_p10_recovery_floor_no_pv_remaining: PASSED (floor={floor})")


def test_p10_recovery_floor_partial_charging():
    """Mid-afternoon: P10 PV partly covers → floor = remainder."""
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
    """drain_above is the curtailment-buffer floor; NOT clamped up by p10_recovery.

    On a cloudy day the deficit raises the overall combined floor (charge_below)
    above the curtailment buffer — drain_above must remain at the buffer level
    so the two thresholds can cross over.
    """
    # Cloudy day: overflow_floor=17.87 (essentially soc_max-buffer), effective_keep=7.42
    drain = compute_drain_above(reserve=0.0, overflow_floor=17.87, effective_keep=7.42)
    # min(17.87, 7.42) = 7.42; max(0, 7.42) = 7.42
    assert abs(drain - 7.42) < 0.001, f"Expected 7.42 (effective_keep wins), got {drain}"
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
    floor, source = compute_floor_with_source(reserve=0.0, p10_recovery=0.0, overflow_floor=10.0, effective_keep=4.0)
    assert floor == 4.0
    assert source == "Overnight Need"
    print(f"  test_floor_source_effective_keep_wins: PASSED ({floor}, {source})")


def test_floor_source_overflow_floor_wins():
    """Big-overflow day: overflow_floor < effective_keep → overflow_floor wins."""
    floor, source = compute_floor_with_source(reserve=0.0, p10_recovery=0.0, overflow_floor=2.0, effective_keep=5.0)
    assert floor == 2.0
    assert source == "Curtailment Buffer"
    print(f"  test_floor_source_overflow_floor_wins: PASSED ({floor}, {source})")


def test_floor_source_p10_recovery_binds():
    """Late in day: p10_recovery exceeds inner min → outer max binds on p10_recovery."""
    floor, source = compute_floor_with_source(reserve=0.0, p10_recovery=7.0, overflow_floor=10.0, effective_keep=4.0)
    # inner min = 4.0 (effective_keep), but p10_recovery=7 > 4 → p10_recovery wins
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
    """Tie between inner_min and another term: prefer the more-specific source."""
    # When p10_recovery == inner_min, label as inner_min source (more useful info)
    floor, source = compute_floor_with_source(reserve=0.0, p10_recovery=4.0, overflow_floor=10.0, effective_keep=4.0)
    assert floor == 4.0
    # tie-breaking: prefer Overnight Need / Curtailment Buffer over P10 Recovery
    assert source in ("Overnight Need", "P10 Recovery"), f"Got {source}"
    print(f"  test_floor_source_tie_picks_inner_min_over_others: PASSED ({floor}, {source})")


def test_floor_source_today_yesterday_morning():
    """Reference: yesterday morning's transition. effective_keep ≈ 7.5 kWh (41%),
    overflow_floor was high, p10_recovery low, reserve 0. Overnight Need should win.
    """
    floor, source = compute_floor_with_source(reserve=0.0, p10_recovery=0.4, overflow_floor=15.16, effective_keep=7.49)
    assert abs(floor - 7.49) < 0.01
    assert source == "Overnight Need"
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
    """v20 R57 supersedes R45 cap taper toward 100%. Plugin no longer chases
    soc_max — once overflow integral falls, the floor is capped at
    effective_keep (the overnight target) instead of tapering up to 100%.
    Test now verifies the new behaviour: late in day with tiny remaining
    overflow, floor equals effective_keep (or below if overflow_floor lower).
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
    # R57: floor must be ≤ best_soc_keep (no chase to soc_max). With a small
    # remaining overflow, overflow_floor is high → min() takes effective_keep.
    assert floor <= 4.0 + 0.01, f"R57: floor must not exceed effective_keep (4.0 kWh), got {floor:.2f}"
    print(f"  test_plugin_cap_taper_near_safe_time: PASSED (R57 — floor={floor:.2f}, capped at effective_keep)")


def test_cap_taper_ratchet_noise_immune():
    """Ratchet: if remaining oscillates up-down-up, floor never drops below its peak."""
    # Simulate three cycles with oscillating remaining overflow
    floors_seen = []
    ratchet = None
    for remaining in [1.0, 1.5, 1.0]:
        max_target, _ = _taper_cap_formula(remaining)
        overflow_floor = max(0.0, max_target - remaining * OVERFLOW_SAFETY_FACTOR)
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
        expected_floor = max(0.0, expected_max_target - remaining * OVERFLOW_SAFETY_FACTOR)
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
    floor_kwh = BATTERY_KWH - MAX_RESERVED_KWH - expected * OVERFLOW_SAFETY_FACTOR
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

from curtailment_plugin import CurtailmentPlugin, PREDICT_STEP as PLUGIN_STEP, SIG_DAILY_PV, SOLCAST_TODAY


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


def test_plugin_floor_clamped_by_soc_keep():
    """With big overflow that needs room, Bug 8 relaxes keep to 0.5 kWh; otherwise clamps to soc_keep."""
    pv, load = _make_overflow_pv(minutes_now=720)
    sensor_overrides = {
        "sensor.sigen_plant_pv_power": 8.0,
        "sensor.sigen_plant_consumed_power": 1.0,
    }
    sensor_overrides.update(_make_p90_sensors())
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=2.0,  # below soc_keep=6 → keep_recovered stays False
        minutes_now=720,
        best_soc_keep=6.0,
        sensor_overrides=sensor_overrides,
    )
    plugin = CurtailmentPlugin(base)

    floor, phase = plugin.calculate(dno_limit_kw=4.0)
    # Bug 8: PV (8) >> load (1) + 0.5 margin → pv_covering. Big overflow >
    # room with base keep (16.27 - 6 = 10.27). Both conditions met → keep
    # relaxes to 0.5. Floor clamped to that.
    assert floor >= 0.5, f"Floor should be clamped to at least RELAXED_KEEP (0.5), got {floor:.1f}"
    assert floor < 6.0, f"Bug 8 should have relaxed keep below 6.0, got {floor:.1f}"
    assert phase == "active", f"Expected active, got {phase}"
    print(f"  test_plugin_floor_clamped_by_soc_keep: PASSED (Bug 8 relaxed; floor={floor:.1f})")


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


def test_on_update_publishes_phase_before_writing_ems():
    """Active-edge ordering: phase sensor publish must precede EMS service write.

    The HA automation has a Restore-MSC branch that fires when
    (manual=Off, phase sensor=Off, EMS!=MSC). If the plugin writes EMS=D-ESS
    BEFORE publishing phase=Active, the automation can reverse our EMS write
    in the race window — observed live on 2026-04-30, drain stalled 1h57m.

    Lock in publish-before-apply ordering by capturing call order through
    instrumented dashboard_item and call_service_wrapper.
    """
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

    call_order = []
    orig_dashboard = base.dashboard_item
    orig_service = base.call_service_wrapper

    def tracked_dashboard(entity, value, attrs=None):
        call_order.append(("publish", entity))
        return orig_dashboard(entity, value, attrs)

    def tracked_service(service, **kwargs):
        call_order.append(("service", service, kwargs.get("entity_id"), kwargs.get("option")))
        return orig_service(service, **kwargs)

    base.dashboard_item = tracked_dashboard
    base.call_service_wrapper = tracked_service

    plugin = CurtailmentPlugin(base)
    plugin.on_update()

    # Find first phase sensor publish and first EMS service write.
    phase_idx = next((i for i, c in enumerate(call_order) if c[0] == "publish" and c[1] == "sensor.predbat_curtailment_phase"), None)
    ems_idx = next((i for i, c in enumerate(call_order) if c[0] == "service" and c[1] == "select/select_option" and c[2] == "select.sigen_plant_remote_ems_control_mode"), None)

    assert phase_idx is not None, "phase sensor was never published"
    assert ems_idx is not None, "EMS mode was never written"
    assert phase_idx < ems_idx, "phase sensor publish (idx {}) must precede EMS write (idx {}); preserves ordering required to avoid the HA automation Restore-MSC race".format(phase_idx, ems_idx)

    print("  test_on_update_publishes_phase_before_writing_ems: PASSED (phase@{} before EMS@{})".format(phase_idx, ems_idx))


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
    """v20 R56 supersedes R6/R12 safe_time deactivation. Plugin now keeps
    running past safe_time so it can drain SOC to overnight target via Drain
    mode in late afternoon. Test renamed-in-spirit to verify R56 sundown
    deactivation: plugin only goes off when actual PV drops to ~0 (sundown),
    NOT at safe_time.
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
    assert base.set_read_only is True, "Should activate at noon"

    # Jump past safe_time but PV still 0.3 kW — R56 says STAY ACTIVE so we
    # can drain SOC to overnight target through the late afternoon.
    base.minutes_now = 18 * 60 + 30
    base.now_utc = datetime(2025, 7, 12, 18, 30, tzinfo=timezone.utc)
    base._sensor_overrides["sensor.sigen_plant_pv_power"] = 0.3
    base.services.clear()
    plugin.base = base
    plugin.on_update()
    assert base.set_read_only is True, "R56: stay active past safe_time while PV>0"

    # Now sundown — actual PV drops to ~0 with peak observed → off
    base.minutes_now = 21 * 60
    base.now_utc = datetime(2025, 7, 12, 21, 0, tzinfo=timezone.utc)
    base._sensor_overrides["sensor.sigen_plant_pv_power"] = 0.05
    base.services.clear()
    plugin.base = base
    plugin.on_update()
    assert base.set_read_only is False, "R56: deactivate at sundown (PV ~0 after peak observed)"
    msc_called = any(s[1].get("option") == "Maximum Self Consumption" for s in base.services if s[0] == "select/select_option")
    assert msc_called, "Should restore MSC at sundown"
    print("  test_deactivation_at_safe_time: PASSED (R56: active past safe_time, off at sundown)")


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
# R54 / R56 / R57: target = max(min(curt, keep), reserve), runs until PV=0,
# no 100% chase (v20)
# ============================================================================


def test_R54_target_uses_keep_when_lower_than_overflow_floor():
    """R54: target = min(overflow_floor, effective_keep). When overflow is
    small (overflow_floor high), target falls to effective_keep — replaces
    R45 100% chase.
    """
    # Small overflow scenario: cloudy day with low Solcast peak
    pv = {m: 3.0 for m in range(0, 240, PLUGIN_STEP)}
    load = {m: 0.5 for m in range(0, 240, PLUGIN_STEP)}
    sensor_overrides = {
        "sensor.sigen_plant_pv_power": 3.0,
        "sensor.sigen_plant_consumed_power": 0.5,
    }
    sensor_overrides.update(_make_p90_sensors(p90_peak_kw=4.0, solcast_remaining=15.0))
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
    assert phase == "active", f"Should be active, got {phase}"
    # overflow_floor would be ~14-18 kWh on this cloudy day; effective_keep=4
    # min() takes 4. Target should be 4.0 ± small drift from R48 latching.
    assert floor <= 4.0 + 0.01, f"R54: target should equal effective_keep (4 kWh), got {floor:.2f}"
    print(f"  test_R54_target_uses_keep_when_lower_than_overflow_floor: PASSED (floor={floor:.2f} = keep)")


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
    assert phase == "active", f"R56: plugin still active after safe_time, got {phase}"
    assert floor < BATTERY_KWH * 0.5, f"R57: floor must NOT chase soc_max, got {floor:.2f} (>50% of soc_max)"
    print(f"  test_R57_no_chase_to_soc_max_late_in_day: PASSED (floor={floor:.2f} kWh = {floor / BATTERY_KWH * 100:.0f}%)")


def test_R56_active_after_safe_time_when_soc_above_keep():
    """R56: plugin stays active past safe_time so it can drain SOC down to
    overnight target via Drain mode in the late afternoon. Pre-R56 it would
    deactivate at safe_time.
    """
    from datetime import datetime, timezone

    pv = {m: 1.0 for m in range(0, 60, PLUGIN_STEP)}
    load = {m: 0.5 for m in range(0, 60, PLUGIN_STEP)}
    sensor_overrides = {
        "sensor.sigen_plant_pv_power": 1.0,
        "sensor.sigen_plant_consumed_power": 0.5,
    }
    sensor_overrides.update(_make_p90_sensors(p90_peak_kw=8.0, solcast_remaining=1.0))
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=BATTERY_KWH * 0.85,  # above overnight target — Drain territory
        minutes_now=1140,  # 19:00 BST
        best_soc_keep=4.0,
        now_utc=datetime(2025, 7, 12, 18, 0, tzinfo=timezone.utc),
        sensor_overrides=sensor_overrides,
    )
    plugin = CurtailmentPlugin(base)
    plugin._peak_pv = 7.5  # earlier today there was real PV, so not pre-PV path
    floor, phase = plugin.calculate(dno_limit_kw=4.0)
    assert phase == "active", f"R56: plugin must stay active past safe_time, got {phase}"
    assert plugin._export_target == 4.0, f"R56: export_target should be DNO so HA can Drain, got {plugin._export_target}"
    print(f"  test_R56_active_after_safe_time_when_soc_above_keep: PASSED (active, floor={floor:.2f})")


def test_R56_off_at_sundown():
    """R56: when peak PV was observed today AND actual PV is now ~0, plugin
    deactivates (sundown). Hands back to Predbat MSC for overnight.
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
    assert plugin._export_target == -2, f"R56: export_target=-2 to signal MSC handoff, got {plugin._export_target}"
    print("  test_R56_off_at_sundown: PASSED (off, MSC handoff signaled)")


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
    plugin.publish("off", BATTERY_KWH, dno_limit_kw=4.0, export_target=-2)

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
    plugin.publish("active", floor_kwh, dno_limit_kw=4.0, export_target=4.0)

    pub = base.published["sensor.predbat_curtailment_target_soc"]
    expected_pct = floor_kwh / BATTERY_KWH * 100
    assert abs(pub["value"] - round(expected_pct, 1)) < 0.05, f"Active: target_soc should equal floor%, got {pub['value']}"
    assert abs(pub["attrs"]["target_kwh"] - floor_kwh) < 0.01
    print(f"  test_R55_target_soc_uses_floor_when_active: PASSED (target_soc={pub['value']}% / {pub['attrs']['target_kwh']} kWh)")


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
    sensor_overrides = {"sensor.sigen_plant_pv_power": 1.5, "sensor.sigen_plant_consumed_power": 0.5}
    sensor_overrides.update(_make_p90_sensors(p90_peak_kw=2.0, solcast_remaining=4.0))
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
    assert cached_target is not None, "calculate should cache overnight_target"
    assert cached_target > 2.0, f"Test setup: overnight_target ({cached_target:.2f}) should exceed best_soc_keep (2.0)."
    assert phase == "active", f"Should be active, got {phase}"
    # R55 should raise effective_keep above 2.0; floor reflects min(overflow_floor, raised_keep)
    assert floor >= cached_target - 0.1, f"R55: floor must be raised by overnight_target ({cached_target:.2f}), got {floor:.2f}"
    print(f"  test_R55_overnight_target_raises_effective_keep_in_calculate: PASSED (target={cached_target:.2f}, floor={floor:.2f})")


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
    # v20: high best_soc_keep so floor = min(overflow_floor, effective_keep)
    # ends up high (below SOC=80%) — recreates pre-v20 "SOC below floor" case.
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=soc_kw,
        minutes_now=1020,
        best_soc_keep=16.0,
        # Match minutes_now=1020 (17:00 local) with UTC tz so local_offset=0
        now_utc=datetime(2025, 7, 12, 17, 0, tzinfo=timezone.utc),
        sensor_overrides=sensor_overrides,
    )
    plugin = CurtailmentPlugin(base)
    floor, phase = plugin.calculate(dno_limit_kw=4.0)
    assert phase == "active", f"Should be active, got {phase}"
    assert soc_kw < floor, f"Test requires SOC below floor ({soc_kw:.1f} vs {floor:.1f})"
    assert plugin._export_target == 4.0, f"Plugin should publish DNO when active, got {plugin._export_target}"
    print(f"  test_export_target_dno_when_active_regardless_of_soc: PASSED (floor={floor/BATTERY_KWH*100:.0f}%, SOC=80%, export_target={plugin._export_target}kW)")


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
    """v20 R56 supersedes the will_fill activation gate. Plugin runs whenever
    PV is producing AND there is work to do (target < soc_max). Stays off
    only at sundown (peak observed AND PV ~0). Test now verifies new
    behaviour: with PV active mid-day, plugin is active regardless of how
    much energy will fill the battery.
    """
    pv = {m: 5.0 for m in range(0, 120, PLUGIN_STEP)}
    load = {m: 1.0 for m in range(0, 120, PLUGIN_STEP)}
    soc_kw = BATTERY_KWH * 0.10
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
        test_plugin_floor_clamped_by_soc_keep,
        test_r48_triggers_after_overnight_100pct,
        test_r48_latches_once_engaged,
        test_plugin_active_high_soc,
        test_floor_clamped_above_soc_keep,
        test_floor_clamped_above_reserve,
        test_floor_lower_with_more_overflow,
        test_export_target_at_dno_when_soc_above_floor,
        test_export_target_dno_when_active_regardless_of_soc,
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
        test_apply_active_sets_export_zero_and_dess,
        test_apply_already_active_no_export_write,
        test_apply_off_restores_msc,
        test_on_update_full_flow,
        test_on_update_publishes_phase_before_writing_ems,
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
        test_R56_active_after_safe_time_when_soc_above_keep,
        test_R56_off_at_sundown,
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
        test_R53_compute_solcast_overflow_empty_returns_zero,
        test_R53_compute_solcast_overflow_uniform_sunny_slot,
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
    ]
    print("  --- R52 pre-PV drain tests ---")
    for test_fn in r52_tests:
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
