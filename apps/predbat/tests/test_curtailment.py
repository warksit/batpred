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

from curtailment_calc import compute_remaining_overflow, compute_morning_gap, compute_target_soc, should_activate, compute_overflow_window, compute_post_overflow_energy

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


def _simulate_day_forecast_error(
    pv_actual,
    load_actual,
    pv_forecast,
    load_forecast=None,
    pv_scaling=False,
    strategy="v5",
    dno_limit=DNO_LIMIT,
    battery_kwh=BATTERY_KWH,
    max_charge_kw=MAX_CHARGE_KW,
    max_discharge_kw=MAX_DISCHARGE_KW,
    start_soc_pct=START_SOC_PCT,
    step_minutes=STEP_MINUTES,
    soc_floor_kwh=0.0,
    buffer_kwh=2.0,
):
    """
    Simulate a full day where forecast differs from reality.

    Algorithm decisions (target SOC, phase) use pv_forecast/load_forecast.
    Physical power flows use pv_actual/load_actual.
    PV scaling correction optionally adjusts forecast based on actual readings.

    Args:
        pv_actual: dict {minute: kW} — what the sun actually does
        load_actual: dict {minute: kW} — actual load
        pv_forecast: dict {minute: kW} — what algorithm believes
        load_forecast: dict {minute: kW} or None (uses load_actual)
        pv_scaling: bool — enable real-time PV scaling correction
        strategy: "v5" (current) or "v6" (time-aware)
        soc_floor_kwh: minimum SOC to maintain (covers unexpected loads)
        buffer_kwh: conservative buffer subtracted from target
    """
    if load_forecast is None:
        load_forecast = load_actual

    step_hours = step_minutes / 60.0
    soc = battery_kwh * start_soc_pct
    end_minute = 1440

    # Initial overflow from FORECAST (what the algorithm sees at dawn)
    initial_overflow_forecast = compute_remaining_overflow(pv_forecast, load_forecast, dno_limit, 0, end_minute, step_minutes)
    # Actual overflow (ground truth, for reporting)
    initial_overflow_actual = compute_remaining_overflow(pv_actual, load_actual, dno_limit, 0, end_minute, step_minutes)

    # For v6: compute overflow window from forecast
    overflow_start_f, overflow_end_f = None, None
    if strategy == "v6":
        overflow_start_f, overflow_end_f = compute_overflow_window(pv_forecast, load_forecast, dno_limit, 0, end_minute, step_minutes)

    results = []
    total_curtailed = 0.0
    total_export = 0.0
    max_export_kw = 0.0
    max_pv_scale = 1.0  # track peak PV scale factor
    was_overflowing = False
    pv_excess_history = []  # rolling actual excess history

    for m in range(0, end_minute, step_minutes):
        # === ACTUAL physics ===
        actual_pv = pv_actual.get(m, 0.0)
        actual_load = load_actual.get(m, 0.0)
        actual_excess = actual_pv - actual_load

        # Track actual PV trend for post-overflow detection
        if actual_excess > dno_limit:
            was_overflowing = True
        if actual_pv > 0.1:
            pv_excess_history.append(actual_excess)
        if len(pv_excess_history) > 3:
            pv_excess_history = pv_excess_history[-3:]

        # 15 min sustained actual excess below DNO after seeing overflow
        actual_overflow_ended = False
        if was_overflowing and len(pv_excess_history) >= 3:
            actual_overflow_ended = max(pv_excess_history) < dno_limit

        # === FORECAST for algorithm decisions ===
        forecast_pv = pv_forecast.get(m, 0.0)
        forecast_load = load_forecast.get(m, 0.0)

        # Remaining overflow from FORECAST (next step onward)
        remaining_forecast = compute_remaining_overflow(
            pv_forecast,
            load_forecast,
            dno_limit,
            start_minute=m + step_minutes,
            end_minute=end_minute,
            step_minutes=step_minutes,
        )

        # PV scaling: adjust forecast overflow based on actual PV
        pv_scale = 1.0
        if pv_scaling and forecast_pv > 0.5:
            pv_scale = actual_pv / forecast_pv
            max_pv_scale = max(max_pv_scale, pv_scale)

            if pv_scale > 1.1:
                # Actual > forecast: scale UP overflow (more drain needed)
                scaled_remaining = compute_remaining_overflow(
                    {k: v * pv_scale for k, v in pv_forecast.items()},
                    load_forecast,
                    dno_limit,
                    start_minute=m + step_minutes,
                    end_minute=end_minute,
                    step_minutes=step_minutes,
                )
                remaining_forecast = max(remaining_forecast, scaled_remaining)

        # Target SOC from algorithm
        target_soc = compute_target_soc(remaining_forecast, battery_kwh, buffer_kwh)
        target_soc = max(target_soc, soc_floor_kwh)

        # === PHASE DETERMINATION ===
        # Battery constraints
        remaining_cap = max(0, battery_kwh - soc)
        max_charge_slot = min(max_charge_kw, remaining_cap / step_hours) if remaining_cap > 0.01 else 0
        available_soc = max(0, soc)
        max_discharge_slot = min(max_discharge_kw, available_soc / step_hours) if available_soc > 0.01 else 0

        export = 0.0
        curtailed = 0.0
        charge = 0.0
        discharge = 0.0
        phase = "off"

        if strategy == "v6":
            # Time-aware strategy
            in_overflow = overflow_start_f is not None and overflow_end_f is not None and overflow_start_f <= m <= overflow_end_f

            # Dynamically update overflow window with PV scaling.
            # Key fix: no guard on overflow_start_f — scaling can CREATE a window
            # when the raw 60% forecast shows none but scaled forecast does.
            if pv_scaling and max_pv_scale > 1.1:
                scaled_start, scaled_end = compute_overflow_window(
                    {k: v * max_pv_scale for k, v in pv_forecast.items()},
                    load_forecast,
                    dno_limit,
                    0,
                    end_minute,
                    step_minutes,
                )
                if scaled_start is not None:
                    if overflow_start_f is None or scaled_start < overflow_start_f:
                        overflow_start_f = scaled_start
                    if scaled_end is not None and (overflow_end_f is None or scaled_end > overflow_end_f):
                        overflow_end_f = scaled_end
                    in_overflow = overflow_start_f <= m <= overflow_end_f

            # "Am I on track for 100%?" check
            post_energy = compute_post_overflow_energy(
                pv_forecast,
                load_forecast,
                after_minute=max(m + step_minutes, (overflow_end_f or m) + step_minutes),
                end_minute=end_minute,
                step_minutes=step_minutes,
            )
            if pv_scaling and pv_scale < 0.9 and forecast_pv > 0.5:
                post_energy *= pv_scale  # scale DOWN if actual < forecast

            energy_needed = battery_kwh - soc
            # Post-overflow from ACTUAL PV trend (ground truth, not forecast)
            post_overflow = actual_overflow_ended
            falling_behind = post_overflow and actual_pv > 0.1 and post_energy < energy_needed * 0.8

            pre_overflow = overflow_start_f is not None and m < overflow_start_f

            if actual_excess >= dno_limit:
                # HIGH PV (overflow happening right now regardless of phase)
                phase = "overflow"
                export = dno_limit
                overflow_kw = actual_excess - dno_limit
                charge = min(overflow_kw, max_charge_slot)
                curtailed = max(0, overflow_kw - charge)

            elif pre_overflow:
                # PRE-OVERFLOW
                if soc > target_soc + 0.5:
                    phase = "draining"
                    drain_wanted = min((soc - target_soc) / step_hours, max_discharge_kw)
                    discharge = min(drain_wanted, max_discharge_slot)
                    total_out = max(0, actual_excess) + discharge
                    export = min(total_out, dno_limit)
                    if actual_excess + discharge > dno_limit:
                        discharge = max(0, dno_limit - max(0, actual_excess))
                        export = min(max(0, actual_excess) + discharge, dno_limit)
                elif soc < soc_floor_kwh and actual_excess > 0:
                    phase = "floor_charge"
                    charge_wanted = min((soc_floor_kwh - soc) / step_hours, max_charge_kw)
                    charge = min(charge_wanted, actual_excess, max_charge_slot)
                    export = min(actual_excess - charge, dno_limit)
                else:
                    phase = "holding"
                    if actual_excess > 0:
                        export = min(actual_excess, dno_limit)
                    else:
                        discharge = min(-actual_excess, max_discharge_slot)

            elif in_overflow:
                # DURING OVERFLOW: always drain mode (export=DNO)
                # When PV dips, battery drains; when PV surges, battery charges
                phase = "drain_overflow"
                if actual_excess > 0:
                    export = min(dno_limit, actual_excess + max_discharge_slot)
                    if actual_excess >= dno_limit:
                        export = dno_limit
                        charge = min(actual_excess - dno_limit, max_charge_slot)
                    else:
                        # PV dip — drain battery to maintain export at DNO
                        drain_needed = dno_limit - actual_excess
                        discharge = min(drain_needed, max_discharge_slot)
                        export = actual_excess + discharge
                else:
                    discharge = min(-actual_excess, max_discharge_slot)

            elif (falling_behind or post_overflow) and actual_excess > 0 and soc < battery_kwh - 0.1:
                # POST-OVERFLOW: no risk (PV-load < DNO), greedily charge to 100%.
                # All PV excess goes to battery. Once full, excess exports safely.
                phase = "topping_up"
                charge = min(actual_excess, max_charge_slot)
                export = min(actual_excess - charge, dno_limit)

            elif post_overflow and actual_excess > 0 and soc >= battery_kwh - 0.1:
                # POST-OVERFLOW, FULL: battery at 100%, export excess (safe, < DNO)
                phase = "holding_full"
                export = min(actual_excess, dno_limit)

            elif remaining_forecast > 0 and actual_excess > 0:
                # Overflow expected but timing unclear (window not yet computed
                # or between bursts on a cloudy day) — hold, don't drain.
                phase = "holding"
                export = min(actual_excess, dno_limit)

            else:
                phase = "off"
                if actual_excess > 0:
                    export = min(actual_excess, dno_limit)
                elif actual_excess < 0:
                    discharge = min(-actual_excess, max_discharge_slot)

        else:
            # === CURRENT V5 STRATEGY (for comparison) ===
            if actual_excess >= dno_limit:
                export = dno_limit
                overflow_kw = actual_excess - dno_limit
                charge = min(overflow_kw, max_charge_slot)
                curtailed = max(0, overflow_kw - charge)

            elif actual_excess > 0:
                if soc > target_soc + 0.1:
                    drain_wanted = min((soc - target_soc) / step_hours, max_discharge_kw)
                    discharge = min(drain_wanted, max_discharge_slot)
                    total_out = actual_excess + discharge
                    export = min(total_out, dno_limit)
                    if actual_excess + discharge > dno_limit:
                        discharge = max(0, dno_limit - actual_excess)
                        export = dno_limit
                elif soc < target_soc - 0.1:
                    charge_wanted = min((target_soc - soc) / step_hours, max_charge_kw)
                    charge = min(charge_wanted, actual_excess, max_charge_slot)
                    export = min(actual_excess - charge, dno_limit)
                else:
                    export = min(actual_excess, dno_limit)
            else:
                discharge = min(-actual_excess, max_discharge_slot)

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
                "pv_actual": actual_pv,
                "pv_forecast": forecast_pv,
                "load": actual_load,
                "soc": soc,
                "soc_pct": soc / battery_kwh * 100,
                "target_soc": target_soc,
                "target_pct": target_soc / battery_kwh * 100,
                "export": export,
                "curtailed": curtailed,
                "charge": charge,
                "discharge": discharge,
                "phase": phase,
                "pv_scale": pv_scale,
            }
        )

    # Find SOC at sunset (last minute with PV > 0)
    sunset_soc = soc
    sunset_soc_pct = soc / battery_kwh * 100
    for r in reversed(results):
        if r["pv_actual"] > 0.05:
            sunset_soc = r["soc"]
            sunset_soc_pct = r["soc_pct"]
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
        "initial_overflow_forecast": initial_overflow_forecast,
        "initial_overflow_actual": initial_overflow_actual,
        "overflow_window": (overflow_start_f, overflow_end_f) if strategy == "v6" else None,
        "max_pv_scale": max_pv_scale,
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


def _run_csv_day_v6_test(label, filename, watts, forecast_scale=1.0, start_soc_pct=None):
    """Run a CSV day through the v6 split-forecast simulation.

    With forecast_scale=1.0, forecast matches reality (perfect forecast).
    With forecast_scale=0.6, forecast is 60% of actual (underforecast).
    """
    if start_soc_pct is None:
        start_soc_pct = START_SOC_PCT
    filepath = os.path.join(CSV_DIR, filename)
    if not os.path.exists(filepath):
        print(f"  {label}: SKIPPED (CSV not found)")
        return False

    pv_actual, load_actual = _load_csv_to_forecasts(filepath, watts=watts)
    pv_forecast = {m: v * forecast_scale for m, v in pv_actual.items()}

    sim = _simulate_day_forecast_error(
        pv_actual,
        load_actual,
        pv_forecast,
        strategy="v6",
        pv_scaling=(forecast_scale != 1.0),
        start_soc_pct=start_soc_pct,
        soc_floor_kwh=1.5,
    )

    errors = []
    max_exp = sim["max_export_kw"]
    curtailed = sim["total_curtailed"]
    sunset_soc = sim["sunset_soc_pct"]

    if max_exp > DNO_LIMIT + 0.01:
        errors.append(f"max_export={max_exp:.1f}kW > DNO {DNO_LIMIT}kW")
    # Allow small curtailment on extreme days (physics limit when overflow ≈ battery capacity)
    if curtailed > 1.0:
        errors.append(f"curtailment={curtailed:.2f}kWh (should be <1.0)")
    # Only check sunset SOC on overflow days — on non-overflow days the curtailment
    # manager is off and SOC depends on Predbat, not us
    has_overflow = sim["initial_overflow_actual"] > 0.5 or sim["initial_overflow_forecast"] > 0.5
    if has_overflow and sunset_soc < 80:
        errors.append(f"sunset_soc={sunset_soc:.0f}% (should be >80%)")

    scale_label = f" @ {forecast_scale:.0%} forecast" if forecast_scale != 1.0 else ""
    soc_label = f" start={start_soc_pct:.0%}" if start_soc_pct != START_SOC_PCT else ""
    tag = f"  v6 {label}{scale_label}{soc_label}"
    if errors:
        detail = "; ".join(errors)
        print(f"{tag}: FAILED — {detail}")
        return True

    print(f"{tag}: PASSED (curtailed={curtailed:.2f}kWh max_exp={max_exp:.1f}kW sunset_soc={sunset_soc:.0f}%)")
    return False


# ============================================================================
# Plugin integration tests (on_update deferral logic)
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
        buffer_min=1.0,
        buffer_pct=30,
        sensor_overrides=None,
    ):
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
        self.best_soc_keep = best_soc_keep
        self.reserve = reserve
        self._buffer_min = buffer_min
        self._buffer_pct = buffer_pct
        self.set_read_only = False
        self.config_index = {}
        self.prefix = "predbat"
        self.logs = []
        self.published = {}
        self.services = []
        self._sensor_overrides = sensor_overrides or {}

    def log(self, msg, *args, **kwargs):
        self.logs.append(msg)

    def get_state_wrapper(self, entity, default=None):
        if entity in self._sensor_overrides:
            return self._sensor_overrides[entity]
        if entity == "input_boolean.curtailment_manager_enable":
            return "on"
        if entity == "input_number.curtailment_manager_buffer":
            return self._buffer_min
        if entity == "input_number.curtailment_manager_buffer_percent":
            return self._buffer_pct
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
    assert phase_sensor.get("value") == "Off", f"Expected phase='Off' with no PV, got '{phase_sensor.get('value')}'"
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
    assert phase_sensor.get("value") != "off", f"Expected active phase with high PV, got '{phase_sensor.get('value')}'"
    assert base.set_read_only is True, "read_only should be True when plugin is active"
    print("  test_plugin_pv_above_threshold_allows_activation: PASSED")


def test_plugin_defers_to_charge_window():
    """Plugin should defer to Predbat during a planned grid charge window."""
    pv, load = _make_overflow_pv(minutes_now=300)
    # PV is generating (above threshold)
    pv[0] = 2.0
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=5.0,
        minutes_now=300,
        # Charge window active right now: 4am-7am (240-420)
        charge_window_best=[{"start": 240, "end": 420}],
        charge_limit_best=[10.0],  # 10kWh target — real charge, not freeze
    )
    plugin = CurtailmentPlugin(base)

    plugin.on_update()

    phase_sensor = base.published.get("sensor.predbat_curtailment_phase", {})
    assert phase_sensor.get("value") == "Off", f"Expected phase='Off' during charge window, got '{phase_sensor.get('value')}'"
    assert base.set_read_only is False, "read_only should be False when deferring to charge window"
    print("  test_plugin_defers_to_charge_window: PASSED")


def test_plugin_ignores_freeze_charge_window():
    """Plugin should NOT defer to a freeze charge window (freeze = hold at reserve)."""
    pv, load = _make_overflow_pv(minutes_now=720)
    # Freeze charge: limit = reserve SOC (4% of 18.08 = 0.7232 kWh)
    reserve_kwh = 18.08 * 4 / 100  # 0.7232
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=5.0,
        minutes_now=720,
        charge_window_best=[{"start": 700, "end": 800}],
        charge_limit_best=[reserve_kwh],
        reserve_percent=4,
    )
    plugin = CurtailmentPlugin(base)

    plugin.on_update()

    phase_sensor = base.published.get("sensor.predbat_curtailment_phase", {})
    assert phase_sensor.get("value") != "off", f"Expected active phase during freeze window, got '{phase_sensor.get('value')}'"
    print("  test_plugin_ignores_freeze_charge_window: PASSED")


def test_plugin_no_charge_window_activates_normally():
    """Plugin activates when no charge window is active."""
    pv, load = _make_overflow_pv(minutes_now=720)
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=5.0,
        minutes_now=720,
        # Charge window exists but not active right now (starts later)
        charge_window_best=[{"start": 1400, "end": 1440}],
        charge_limit_best=[10.0],
    )
    plugin = CurtailmentPlugin(base)

    plugin.on_update()

    phase_sensor = base.published.get("sensor.predbat_curtailment_phase", {})
    assert phase_sensor.get("value") != "off", f"Expected active phase outside charge window, got '{phase_sensor.get('value')}'"
    print("  test_plugin_no_charge_window_activates_normally: PASSED")


# ============================================================================
# Dynamic buffer and SOC floor tests
# ============================================================================


def test_dynamic_buffer_large_overflow():
    """10 kWh overflow at 30% → buffer = 3.0 kWh (percentage dominates)."""
    pv, load = _make_overflow_pv(minutes_now=720)
    base = MockBase(pv_step=pv, load_step=load, soc_kw=5.0, minutes_now=720, buffer_min=1.0, buffer_pct=30)
    plugin = CurtailmentPlugin(base)

    target, overflow, phase, export, dynamic_buffer = plugin.calculate(dno_limit_kw=4.0, buffer_min_kwh=1.0, buffer_pct=30.0)
    expected_buffer = max(1.0, overflow * 30.0 / 100.0)
    assert abs(dynamic_buffer - expected_buffer) < 0.01, f"Expected buffer={expected_buffer:.2f}, got {dynamic_buffer:.2f}"
    assert dynamic_buffer >= 1.0, f"Buffer {dynamic_buffer:.2f} should be >= floor 1.0"
    # With significant overflow, percentage should dominate
    assert dynamic_buffer > 1.0, f"Buffer {dynamic_buffer:.2f} should be > floor for large overflow"
    print("  test_dynamic_buffer_large_overflow: PASSED (buffer={:.2f}kWh for {:.1f}kWh overflow)".format(dynamic_buffer, overflow))


def test_dynamic_buffer_small_overflow():
    """Small overflow → buffer capped at overflow (not the floor)."""
    # Create PV/load with minimal overflow: PV=5kW, load=1kW → excess=4kW, barely above DNO
    pv = {}
    load = {}
    for m in range(0, 60, PLUGIN_STEP):  # Only 1 hour of slight overflow
        pv[m] = 4.5  # excess = 3.5kW, below DNO → no overflow
        load[m] = 1.0
    # Add a few slots with slight overflow
    for m in range(60, 90, PLUGIN_STEP):
        pv[m] = 5.5  # excess = 4.5kW, overflow = 0.5kW
        load[m] = 1.0
    base = MockBase(pv_step=pv, load_step=load, soc_kw=5.0, minutes_now=720, buffer_min=1.0, buffer_pct=30)
    plugin = CurtailmentPlugin(base)

    target, overflow, phase, export, dynamic_buffer = plugin.calculate(dno_limit_kw=4.0, buffer_min_kwh=1.0, buffer_pct=30.0)
    if overflow > 0:
        # Buffer should be capped at overflow, not the 1.0 kWh floor
        assert dynamic_buffer <= overflow + 0.01, "Buffer {:.2f} should be <= overflow {:.2f} (cap prevents buffer exceeding overflow)".format(dynamic_buffer, overflow)
        print("  test_dynamic_buffer_small_overflow: PASSED (buffer={:.2f}, capped at overflow={:.2f}kWh)".format(dynamic_buffer, overflow))
    else:
        print("  test_dynamic_buffer_small_overflow: PASSED (no overflow, buffer not applied)")


def test_target_soc_clamped_above_soc_keep():
    """Target SOC must never go below best_soc_keep."""
    pv, load = _make_overflow_pv(minutes_now=720)
    # Set best_soc_keep high — plugin should not drain below it
    soc_keep = 8.0  # kWh
    base = MockBase(pv_step=pv, load_step=load, soc_kw=10.0, minutes_now=720, best_soc_keep=soc_keep, reserve=0, buffer_min=1.0, buffer_pct=30)
    plugin = CurtailmentPlugin(base)

    target, overflow, phase, export, dynamic_buffer = plugin.calculate(dno_limit_kw=4.0, buffer_min_kwh=1.0, buffer_pct=30.0)
    # With large overflow, raw target would be near 0 — but clamped to soc_keep
    assert target >= soc_keep, f"Target SOC {target:.2f} kWh should be >= best_soc_keep {soc_keep:.2f} kWh"
    print("  test_target_soc_clamped_above_soc_keep: PASSED (target={:.2f}kWh >= soc_keep={:.2f}kWh)".format(target, soc_keep))


def test_target_soc_clamped_above_reserve():
    """Target SOC must never go below reserve."""
    pv, load = _make_overflow_pv(minutes_now=720)
    reserve = 5.0  # kWh
    base = MockBase(pv_step=pv, load_step=load, soc_kw=10.0, minutes_now=720, best_soc_keep=0, reserve=reserve, buffer_min=1.0, buffer_pct=30)
    plugin = CurtailmentPlugin(base)

    target, overflow, phase, export, dynamic_buffer = plugin.calculate(dno_limit_kw=4.0, buffer_min_kwh=1.0, buffer_pct=30.0)
    assert target >= reserve, f"Target SOC {target:.2f} kWh should be >= reserve {reserve:.2f} kWh"
    print("  test_target_soc_clamped_above_reserve: PASSED (target={:.2f}kWh >= reserve={:.2f}kWh)".format(target, reserve))


def test_buffer_kwh_in_phase_sensor():
    """buffer_kwh attribute should be published on the phase sensor."""
    pv, load = _make_overflow_pv(minutes_now=720)
    base = MockBase(pv_step=pv, load_step=load, soc_kw=5.0, minutes_now=720, buffer_min=1.0, buffer_pct=30)
    plugin = CurtailmentPlugin(base)

    plugin.on_update()

    phase_sensor = base.published.get("sensor.predbat_curtailment_phase", {})
    attrs = phase_sensor.get("attrs", {})
    assert "buffer_kwh" in attrs, f"Expected buffer_kwh attribute in phase sensor, got attrs: {attrs}"
    assert attrs["buffer_kwh"] > 0, f"Expected buffer_kwh > 0, got {attrs['buffer_kwh']}"
    print("  test_buffer_kwh_in_phase_sensor: PASSED (buffer_kwh={})".format(attrs["buffer_kwh"]))


# ============================================================================
# Morning gap tests
# ============================================================================


def test_morning_gap_pre_dawn():
    """Pre-dawn: load exceeds PV for hours, then solar takes over."""
    pv = {}
    load = {}
    # 5am start (minute 0), PV ramps up, load steady at 1kW
    for m in range(0, 480, 5):  # 8 hours
        hour = m / 60.0
        pv[m] = max(0, hour - 2) * 1.5  # ramps from 0 at hour 2 to 9kW at hour 8
        load[m] = 1.0

    gap = compute_morning_gap(pv, load, start_minute=0, end_minute=480, step_minutes=5)
    # Load > PV for first ~2.7 hours (until PV ramp reaches 1kW)
    # Gap should be roughly 2.7 kWh (1kW × 2.7h minus small PV ramp)
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
    assert gap == 0.0, f"Expected gap=0 when PV already covers load, got {gap:.2f}"
    print("  test_morning_gap_solar_already_covers: PASSED (gap={:.2f}kWh)".format(gap))


def test_morning_gap_cloudy_never_covers():
    """Cloudy day: PV never sustainably exceeds load."""
    pv = {}
    load = {}
    for m in range(0, 480, 5):
        pv[m] = 0.5  # low PV all day
        load[m] = 1.0
    gap = compute_morning_gap(pv, load, start_minute=0, end_minute=480, step_minutes=5)
    # 0.5kW deficit × 8 hours = 4 kWh
    assert 3.5 < gap < 4.5, f"Expected ~4kWh gap on cloudy day, got {gap:.2f}"
    print("  test_morning_gap_cloudy_never_covers: PASSED (gap={:.2f}kWh)".format(gap))


def test_morning_gap_kwh_values():
    """Morning gap with kWh-per-step values (Predbat format)."""
    pv = {}
    load = {}
    step_kwh = 5 / 60.0  # kW→kWh per 5-min step
    for m in range(0, 480, 5):
        hour = m / 60.0
        pv[m] = max(0, hour - 2) * 1.5 * step_kwh
        load[m] = 1.0 * step_kwh
    gap = compute_morning_gap(pv, load, start_minute=0, end_minute=480, step_minutes=5, values_are_kwh=True)
    assert 1.5 < gap < 4.0, f"Expected morning gap 1.5-4.0 kWh with kWh values, got {gap:.2f}"
    print("  test_morning_gap_kwh_values: PASSED (gap={:.2f}kWh)".format(gap))


# ============================================================================
# Buffer cap tests
# ============================================================================


def test_buffer_cap_small_overflow():
    """Buffer should be capped at overflow on marginal days."""
    pv, load = _make_overflow_pv(minutes_now=720)
    # Reduce PV so overflow is small (~0.3kWh)
    for m in pv:
        pv[m] = 4.3  # barely above DNO 4kW + 1kW load = 4.3kW excess, overflow = 0.3kW
    base = MockBase(pv_step=pv, load_step=load, soc_kw=17.0, minutes_now=720, buffer_min=2.0, buffer_pct=30)
    plugin = CurtailmentPlugin(base)
    target, overflow, phase, export, dynamic_buffer = plugin.calculate(dno_limit_kw=4.0, buffer_min_kwh=2.0, buffer_pct=30.0)
    # Buffer should be capped at overflow, not the 2.0 kWh floor
    assert dynamic_buffer <= overflow + 0.01, f"Buffer {dynamic_buffer:.2f} should be <= overflow {overflow:.2f}"
    print("  test_buffer_cap_small_overflow: PASSED (buffer={:.2f}, overflow={:.2f})".format(dynamic_buffer, overflow))


def test_buffer_cap_large_overflow():
    """Buffer floor should apply normally on high overflow days."""
    pv, load = _make_overflow_pv(minutes_now=720)
    base = MockBase(pv_step=pv, load_step=load, soc_kw=5.0, minutes_now=720, buffer_min=2.0, buffer_pct=30)
    plugin = CurtailmentPlugin(base)
    target, overflow, phase, export, dynamic_buffer = plugin.calculate(dno_limit_kw=4.0, buffer_min_kwh=2.0, buffer_pct=30.0)
    # High overflow: buffer should be max(floor, pct) and less than overflow
    assert dynamic_buffer >= 2.0, f"Buffer {dynamic_buffer:.2f} should be >= floor 2.0"
    assert dynamic_buffer <= overflow, f"Buffer {dynamic_buffer:.2f} should be <= overflow {overflow:.2f}"
    print("  test_buffer_cap_large_overflow: PASSED (buffer={:.2f}, overflow={:.2f})".format(dynamic_buffer, overflow))


# ============================================================================
# on_before_plan tests
# ============================================================================


def test_before_plan_reduces_keep_on_overflow_day():
    """on_before_plan should reduce best_soc_keep when overflow is forecast."""
    pv, load = _make_overflow_pv(minutes_now=720)
    base = MockBase(pv_step=pv, load_step=load, soc_kw=5.0, minutes_now=720)
    plugin = CurtailmentPlugin(base)

    context = {"best_soc_keep": 6.0}
    result = plugin.on_before_plan(context)
    assert result["best_soc_keep"] < 6.0, f"Expected soc_keep < 6.0, got {result['best_soc_keep']:.2f}"
    # With PV=8kW and load=1kW, morning gap should be 0 (solar already covers)
    # So adjusted keep should be near reserve + margin
    assert result["best_soc_keep"] <= 1.0, f"Expected keep <= 1.0 (margin only), got {result['best_soc_keep']:.2f}"
    print("  test_before_plan_reduces_keep_on_overflow_day: PASSED (keep={:.2f})".format(result["best_soc_keep"]))


def test_before_plan_no_change_without_overflow():
    """on_before_plan should not reduce best_soc_keep when no overflow."""
    pv = {}
    load = {}
    for m in range(0, 720, 5):
        pv[m] = 2.0  # moderate PV
        load[m] = 3.0  # load exceeds PV, no overflow
    step_kwh = 5 / 60.0
    base = MockBase(pv_step=pv, load_step=load, soc_kw=5.0, minutes_now=720)
    plugin = CurtailmentPlugin(base)

    context = {"best_soc_keep": 6.0}
    result = plugin.on_before_plan(context)
    assert result["best_soc_keep"] == 6.0, f"Expected soc_keep unchanged at 6.0, got {result['best_soc_keep']:.2f}"
    print("  test_before_plan_no_change_without_overflow: PASSED")


def test_before_plan_never_increases():
    """on_before_plan should only reduce, never increase best_soc_keep."""
    pv, load = _make_overflow_pv(minutes_now=720)
    base = MockBase(pv_step=pv, load_step=load, soc_kw=5.0, minutes_now=720)
    plugin = CurtailmentPlugin(base)

    context = {"best_soc_keep": 0.5}  # already very low
    result = plugin.on_before_plan(context)
    assert result["best_soc_keep"] == 0.5, f"Expected keep unchanged at 0.5 (not increased), got {result['best_soc_keep']:.2f}"
    print("  test_before_plan_never_increases: PASSED")


def test_realtime_pv_correction_raises_overflow():
    """When actual PV exceeds forecast, scaled overflow should drive aggressive early draining."""
    # Forecast: modest PV (4.5kW), barely below DNO with 1kW load → ~0 forecast overflow
    pv = {}
    load = {}
    for m in range(0, 660, PLUGIN_STEP):  # 11 hours of solar remaining
        pv[m] = 4.5
        load[m] = 1.0
    # Forecast excess = 4.5-1.0 = 3.5kW < DNO 4.0 → no forecast overflow
    # But actual PV = 7.8kW (1.73x forecast), so scaled PV ~ 7.8kW across all slots
    # Scaled excess ~ 7.8-1.0 = 6.8kW, overflow ~ 2.8kW per slot × 11 hours
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=16.0,
        soc_max=18.08,
        minutes_now=720,
        sensor_overrides={
            "sensor.sigen_plant_pv_power": 7.8,  # actual PV 73% above forecast
            "sensor.sigen_plant_consumed_power": 0.7,
        },
    )
    plugin = CurtailmentPlugin(base)
    target_soc, overflow, phase, export_target, buffer = plugin.calculate(4.0, buffer_min_kwh=2.0, buffer_pct=50)

    # Scaled overflow should be substantial — many kWh across remaining solar hours
    assert overflow >= 10.0, f"Expected scaled overflow >= 10.0 kWh, got {overflow:.2f}"
    # Target SOC should be very low — need maximum battery headroom
    target_pct = target_soc / 18.08 * 100
    assert target_pct < 50, f"Expected target < 50% with scaled PV correction, got {target_pct:.1f}%"
    # SOC 16.0 (88.5%) well above target → draining
    assert phase == "draining", f"Expected draining phase, got {phase}"
    assert any("PV scale" in log for log in base.logs), "Expected PV scale log message"
    print(f"  test_realtime_pv_correction_raises_overflow: PASSED (overflow={overflow:.2f}, target={target_pct:.0f}%)")


def test_realtime_pv_correction_no_effect_when_forecast_higher():
    """Real-time correction should not reduce overflow when forecast already accounts for it."""
    pv, load = _make_overflow_pv(minutes_now=720)  # 8kW PV, 1kW load → big forecast overflow
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=5.0,
        soc_max=18.08,
        minutes_now=720,
        sensor_overrides={
            "sensor.sigen_plant_pv_power": 5.0,  # actual PV lower than forecast
            "sensor.sigen_plant_consumed_power": 1.0,
        },
    )
    plugin = CurtailmentPlugin(base)
    target_soc, overflow, phase, export_target, buffer = plugin.calculate(4.0, buffer_min_kwh=2.0, buffer_pct=50)

    # Actual excess = 5-1 = 4kW, overflow rate = 0kW (at DNO limit) → no correction
    # Forecast overflow should dominate
    assert overflow > 5.0, f"Expected forecast overflow > 5.0, got {overflow:.2f}"
    assert not any("PV scale" in log for log in base.logs), "Should not log PV scale when forecast is higher"
    print(f"  test_realtime_pv_correction_no_effect_when_forecast_higher: PASSED (overflow={overflow:.2f})")


def test_before_plan_disabled():
    """on_before_plan should return context unchanged when disabled."""
    pv, load = _make_overflow_pv(minutes_now=720)
    base = MockBase(pv_step=pv, load_step=load, soc_kw=5.0, minutes_now=720)
    # Override enable to off
    base.get_state_wrapper = lambda entity, default=None: "off" if "enable" in entity else default
    plugin = CurtailmentPlugin(base)

    context = {"best_soc_keep": 6.0}
    result = plugin.on_before_plan(context)
    assert result["best_soc_keep"] == 6.0, f"Expected keep unchanged when disabled, got {result['best_soc_keep']:.2f}"
    print("  test_before_plan_disabled: PASSED")


# ============================================================================
# Forecast error tests — split forecast vs reality
# ============================================================================


def _make_sunny_day(peak_kw=8.0, load_kw=1.0, sunrise_hour=6, sunset_hour=18, step_minutes=STEP_MINUTES):
    """Generate a synthetic sunny day with bell-curve PV profile."""
    pv = {}
    load = {}
    mid = (sunrise_hour + sunset_hour) / 2.0
    half_width = (sunset_hour - sunrise_hour) / 2.0
    for m in range(0, 1440, step_minutes):
        hour = m / 60.0
        if sunrise_hour <= hour <= sunset_hour:
            # Bell curve peaking at mid-day
            x = (hour - mid) / half_width
            pv[m] = peak_kw * max(0, 1 - x * x)
        else:
            pv[m] = 0.0
        load[m] = load_kw
    return pv, load


def _print_sim_summary(label, sim, show_phases=False):
    """Print a simulation summary line."""
    print(
        f"    {label}: curtailed={sim['total_curtailed']:.2f}kWh "
        f"max_export={sim['max_export_kw']:.1f}kW "
        f"sunset_soc={sim['sunset_soc_pct']:.0f}% "
        f"overflow_forecast={sim['initial_overflow_forecast']:.1f}kWh "
        f"overflow_actual={sim['initial_overflow_actual']:.1f}kWh"
    )
    if show_phases:
        # Show SOC at key times
        for r in sim["results"]:
            m = r["minute"]
            if m in (0, 360, 480, 600, 720, 840, 960, 1020, 1080):
                h = m // 60
                print(f"      {h:02d}:00  SOC={r['soc_pct']:5.1f}%  " f"target={r['target_pct']:5.1f}%  " f"PV={r['pv_actual']:.1f}/{r['pv_forecast']:.1f}kW  " f"export={r['export']:.1f}kW  phase={r['phase']}")


def test_forecast_error_v5_fails_with_underforecast():
    """V5 strategy with 60% PV forecast should FAIL: export exceeds DNO when battery fills."""
    pv_actual, load_actual = _make_sunny_day(peak_kw=8.0, load_kw=0.7)
    pv_forecast = {m: v * 0.6 for m, v in pv_actual.items()}

    sim = _simulate_day_forecast_error(
        pv_actual,
        load_actual,
        pv_forecast,
        strategy="v5",
        start_soc_pct=0.4,
    )

    # V5 with bad forecast should produce export > DNO (the failure we saw yesterday)
    max_exp = sim["max_export_kw"]
    curtailed = sim["total_curtailed"]
    _print_sim_summary("v5 @ 60% forecast", sim, show_phases=True)

    # This test EXPECTS failure: either export > DNO or curtailment
    failed = max_exp > DNO_LIMIT + 0.01 or curtailed > 0.1
    assert failed, f"Expected v5 to fail with 60% forecast but it passed: " f"max_export={max_exp:.1f}kW curtailed={curtailed:.2f}kWh"
    print(f"  test_forecast_error_v5_fails_with_underforecast: PASSED (confirmed failure: max_export={max_exp:.1f}kW, curtailed={curtailed:.2f}kWh)")


def test_forecast_error_v6_handles_underforecast():
    """V6 time-aware strategy with 60% forecast + PV scaling should handle underforecast."""
    pv_actual, load_actual = _make_sunny_day(peak_kw=8.0, load_kw=0.7)
    pv_forecast = {m: v * 0.6 for m, v in pv_actual.items()}

    sim = _simulate_day_forecast_error(
        pv_actual,
        load_actual,
        pv_forecast,
        strategy="v6",
        pv_scaling=True,
        start_soc_pct=0.4,
        soc_floor_kwh=1.5,
    )

    max_exp = sim["max_export_kw"]
    curtailed = sim["total_curtailed"]
    sunset_soc = sim["sunset_soc_pct"]
    _print_sim_summary("v6 @ 60% forecast + scaling", sim, show_phases=True)

    assert max_exp <= DNO_LIMIT + 0.01, f"Export {max_exp:.1f}kW exceeded DNO {DNO_LIMIT}kW"
    # Small curtailment (~0.6 kWh) is unavoidable when overflow (17kWh) ≈ battery capacity (18kWh)
    # minus floor (1.5kWh) and buffer (2kWh). Physics limit, not algorithm failure.
    # Key metric: v5 curtails 5.70 kWh, v6 curtails <1 kWh — 10x improvement.
    assert curtailed < 1.0, f"Curtailment {curtailed:.2f}kWh should be <1.0 (physics limit ~0.6)"
    assert sunset_soc > 90, f"Sunset SOC {sunset_soc:.0f}% should be >90%"
    print(f"  test_forecast_error_v6_handles_underforecast: PASSED (max_export={max_exp:.1f}kW, curtailed={curtailed:.2f}kWh, sunset_soc={sunset_soc:.0f}%)")


def test_forecast_error_v6_pm_overforecast():
    """V6 should reach ~100% even when afternoon PV is 60% of forecast."""
    pv_actual, load_actual = _make_sunny_day(peak_kw=8.0, load_kw=0.7)
    # Morning forecast accurate, afternoon overforecast
    pv_forecast = {}
    for m, v in pv_actual.items():
        hour = m / 60.0
        if hour < 12:
            pv_forecast[m] = v  # accurate morning
        else:
            pv_forecast[m] = v * 1.4  # afternoon overforecast (actual will be lower)
    # Actual afternoon is just pv_actual (lower than forecast)

    sim = _simulate_day_forecast_error(
        pv_actual,
        load_actual,
        pv_forecast,
        strategy="v6",
        pv_scaling=True,
        start_soc_pct=0.4,
        soc_floor_kwh=1.5,
    )

    max_exp = sim["max_export_kw"]
    sunset_soc = sim["sunset_soc_pct"]
    _print_sim_summary("v6 PM overforecast", sim, show_phases=True)

    curtailed = sim["total_curtailed"]
    assert max_exp <= DNO_LIMIT + 0.01, f"Export {max_exp:.1f}kW exceeded DNO"
    assert curtailed < 1.0, f"Curtailment {curtailed:.2f}kWh should be <1.0"
    assert sunset_soc > 90, f"Sunset SOC {sunset_soc:.0f}% should be >90% (detect shortfall, top up early)"
    print(f"  test_forecast_error_v6_pm_overforecast: PASSED (max_export={max_exp:.1f}kW, curtailed={curtailed:.2f}kWh, sunset_soc={sunset_soc:.0f}%)")


def test_forecast_error_v6_overforecast_all_day():
    """V6 with 140% forecast overdrains morning but recovers via post-overflow top-up."""
    pv_actual, load_actual = _make_sunny_day(peak_kw=8.0, load_kw=0.7)
    pv_forecast = {m: v * 1.4 for m, v in pv_actual.items()}

    sim = _simulate_day_forecast_error(
        pv_actual,
        load_actual,
        pv_forecast,
        strategy="v6",
        pv_scaling=True,
        start_soc_pct=0.4,
    )

    max_exp = sim["max_export_kw"]
    sunset_soc = sim["sunset_soc_pct"]
    curtailed = sim["total_curtailed"]
    _print_sim_summary("v6 @ 140% forecast", sim, show_phases=True)

    assert max_exp <= DNO_LIMIT + 0.01, f"Export {max_exp:.1f}kW exceeded DNO"
    assert curtailed < 0.1, f"Curtailment {curtailed:.2f}kWh — overforecast shouldn't curtail"
    assert sunset_soc > 90, f"Sunset SOC {sunset_soc:.0f}% should recover via post-overflow top-up"
    print(f"  test_forecast_error_v6_overforecast_all_day: PASSED (max_export={max_exp:.1f}kW, sunset_soc={sunset_soc:.0f}%)")


def test_forecast_error_v6_perfect_forecast():
    """V6 with perfect forecast should work at least as well as v5."""
    pv_actual, load_actual = _make_sunny_day(peak_kw=8.0, load_kw=0.7)

    sim = _simulate_day_forecast_error(
        pv_actual,
        load_actual,
        pv_actual,
        load_forecast=load_actual,
        strategy="v6",
        pv_scaling=False,
        start_soc_pct=0.4,
    )

    max_exp = sim["max_export_kw"]
    curtailed = sim["total_curtailed"]
    sunset_soc = sim["sunset_soc_pct"]
    _print_sim_summary("v6 perfect forecast", sim, show_phases=True)

    assert max_exp <= DNO_LIMIT + 0.01, f"Export {max_exp:.1f}kW exceeded DNO"
    assert curtailed < 0.1, f"Curtailment {curtailed:.2f}kWh should be ~0"
    assert sunset_soc > 90, f"Sunset SOC {sunset_soc:.0f}% should be high"
    print(f"  test_forecast_error_v6_perfect_forecast: PASSED (max_export={max_exp:.1f}kW, sunset_soc={sunset_soc:.0f}%)")


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
        # Dynamic buffer and SOC floor tests
        test_dynamic_buffer_large_overflow,
        test_dynamic_buffer_small_overflow,
        test_target_soc_clamped_above_soc_keep,
        test_target_soc_clamped_above_reserve,
        test_buffer_kwh_in_phase_sensor,
        # Morning gap tests
        test_morning_gap_pre_dawn,
        test_morning_gap_solar_already_covers,
        test_morning_gap_cloudy_never_covers,
        test_morning_gap_kwh_values,
        # Buffer cap tests
        test_buffer_cap_small_overflow,
        test_buffer_cap_large_overflow,
        # Real-time PV correction tests
        test_realtime_pv_correction_raises_overflow,
        test_realtime_pv_correction_no_effect_when_forecast_higher,
        # on_before_plan tests
        test_before_plan_reduces_keep_on_overflow_day,
        test_before_plan_no_change_without_overflow,
        test_before_plan_never_increases,
        test_before_plan_disabled,
    ]

    # Forecast error tests (v5 vs v6 strategy comparison)
    forecast_error_tests = [
        test_forecast_error_v5_fails_with_underforecast,
        test_forecast_error_v6_handles_underforecast,
        test_forecast_error_v6_pm_overforecast,
        test_forecast_error_v6_overforecast_all_day,
        test_forecast_error_v6_perfect_forecast,
    ]

    for test_fn in tests:
        try:
            test_fn()
        except AssertionError as e:
            print(f"  {test_fn.__name__}: FAILED — {e}")
            failed = True

    # Forecast error tests
    print("  --- Forecast error tests ---")
    for test_fn in forecast_error_tests:
        try:
            test_fn()
        except AssertionError as e:
            print(f"  {test_fn.__name__}: FAILED — {e}")
            failed = True

    # CSV validation tests (real-world data) — v5 perfect forecast
    csv_available = os.path.exists(CSV_DIR)
    if csv_available:
        for label, filename, watts, expected in VALIDATION_DAYS:
            day_failed = _run_csv_day_test(label, filename, watts, expected)
            if day_failed:
                failed = True
    else:
        print(f"  CSV validation tests: SKIPPED (directory not found: {CSV_DIR})")

    # CSV validation — v6 strategy with perfect and underforecast scenarios
    if csv_available:
        print("  --- CSV v6 strategy tests ---")
        for label, filename, watts, expected in VALIDATION_DAYS:
            # Perfect forecast
            day_failed = _run_csv_day_v6_test(label, filename, watts, forecast_scale=1.0)
            if day_failed:
                failed = True
            # 60% underforecast (only on overflow days)
            if expected["overflow_approx"] > 1.0:
                day_failed = _run_csv_day_v6_test(label, filename, watts, forecast_scale=0.6)
                if day_failed:
                    failed = True
            # Low SOC start (realistic: Predbat reduces overnight charge on overflow days).
            # Big overflow (>8kWh): best_soc_keep drops to ~2kWh = 10% start.
            # Moderate overflow: best_soc_keep stays higher = 25% start.
            if expected["overflow_approx"] > 8.0:
                day_failed = _run_csv_day_v6_test(label, filename, watts, forecast_scale=1.0, start_soc_pct=0.10)
                if day_failed:
                    failed = True
            elif expected["overflow_approx"] > 1.0:
                day_failed = _run_csv_day_v6_test(label, filename, watts, forecast_scale=1.0, start_soc_pct=0.25)
                if day_failed:
                    failed = True

    if not failed:
        print("**** All curtailment tests PASSED ****")
    return failed


if __name__ == "__main__":
    sys.exit(1 if run_curtailment_tests() else 0)
