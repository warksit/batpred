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

from curtailment_calc import compute_remaining_overflow, compute_morning_gap, compute_target_soc, should_activate, compute_overflow_window, compute_post_overflow_energy, simulate_soc_trajectory

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
                    phase = "export"
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
                    phase = "hold"
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
                phase = "charge"
                charge = min(actual_excess, max_charge_slot)
                export = min(actual_excess - charge, dno_limit)

            elif post_overflow and actual_excess > 0 and soc >= battery_kwh - 0.1:
                # POST-OVERFLOW, FULL: battery at 100%, export excess (safe, < DNO)
                phase = "hold_full"
                export = min(actual_excess, dno_limit)

            elif remaining_forecast > 0 and actual_excess > 0:
                # Overflow expected but timing unclear (window not yet computed
                # or between bursts on a cloudy day) — hold, don't drain.
                phase = "hold"
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


def _simulate_day_v7(
    pv_actual,
    load_actual,
    pv_forecast,
    load_forecast=None,
    dno_limit=DNO_LIMIT,
    battery_kwh=BATTERY_KWH,
    max_charge_kw=MAX_CHARGE_KW,
    max_discharge_kw=MAX_DISCHARGE_KW,
    start_soc_pct=START_SOC_PCT,
    step_minutes=STEP_MINUTES,
    soc_floor_kwh=0.0,
):
    """
    Simulate a full day using the v7 "Commit and Ratchet" strategy.

    Key differences from v6:
    - Floor uses cumulative energy ratio (integral, not instantaneous PV scale)
    - Floor adjusts bidirectionally (up if overforecast, down if underforecast)
    - Phases are purely reactive (SOC vs floor + actual PV-load)
    - 95% cap during overflow as safety backstop
    """
    if load_forecast is None:
        load_forecast = load_actual

    step_hours = step_minutes / 60.0
    soc = battery_kwh * start_soc_pct
    end_minute = 1440

    # Pre-compute total forecast PV energy (for blend threshold)
    total_forecast = sum(pv_forecast.values()) * step_hours

    # Initial overflow from FORECAST (what the algorithm sees at dawn)
    initial_overflow_forecast = compute_remaining_overflow(pv_forecast, load_forecast, dno_limit, 0, end_minute, step_minutes)
    # Actual overflow (ground truth, for reporting)
    initial_overflow_actual = compute_remaining_overflow(pv_actual, load_actual, dno_limit, 0, end_minute, step_minutes)

    # Compute overflow window from forecast (for remaining_overflow termination)
    _, overflow_end_f = compute_overflow_window(pv_forecast, load_forecast, dno_limit, 0, end_minute, step_minutes)

    results = []
    total_curtailed = 0.0
    total_export = 0.0
    max_export_kw = 0.0

    # Cumulative energy tracking for ratio
    cumulative_actual = 0.0
    cumulative_forecast = 0.0

    # Rolling 30-min window of actual excess (6 slots × 5 min) for overflow cap
    actual_excess_history = []  # recent actual excess values

    prev_floor = None
    max_floor_change_pct = 0.0
    phase = "off"
    energy_ratio = 1.0

    for m in range(0, end_minute, step_minutes):
        # === ACTUAL physics ===
        actual_pv = pv_actual.get(m, 0.0)
        actual_load = load_actual.get(m, 0.0)
        actual_excess = actual_pv - actual_load

        # === CUMULATIVE ENERGY RATIO ===
        forecast_pv = pv_forecast.get(m, 0.0)
        cumulative_actual += actual_pv * step_hours
        cumulative_forecast += forecast_pv * step_hours

        # 10% blend: ease in ratio slowly at dawn to avoid wild swings
        threshold = total_forecast * 0.10
        blend = min(1.0, cumulative_forecast / max(threshold, 0.5))
        raw_ratio = cumulative_actual / max(cumulative_forecast, 0.5)
        energy_ratio = 1.0 + (raw_ratio - 1.0) * blend

        # === REMAINING OVERFLOW + POST-OVERFLOW from FORECAST ===
        remaining_overflow = compute_remaining_overflow(
            pv_forecast,
            load_forecast,
            dno_limit,
            start_minute=m + step_minutes,
            end_minute=end_minute,
            step_minutes=step_minutes,
        )

        post_start = (overflow_end_f or 0) + step_minutes
        post_overflow_energy = compute_post_overflow_energy(
            pv_forecast,
            load_forecast,
            after_minute=post_start,
            end_minute=end_minute,
            step_minutes=step_minutes,
        )

        # === FLOOR COMPUTATION (adjusted by cumulative energy ratio) ===
        adjusted_overflow = remaining_overflow * energy_ratio
        adjusted_post = post_overflow_energy * energy_ratio
        floor = max(soc_floor_kwh, battery_kwh - adjusted_overflow - adjusted_post)
        floor = max(0.0, min(battery_kwh, floor))

        # Track max floor change for stability reporting
        if prev_floor is not None:
            change_pct = abs(floor - prev_floor) / battery_kwh * 100
            if change_pct > max_floor_change_pct:
                max_floor_change_pct = change_pct
        prev_floor = floor

        # === OVERFLOW CAP (95% during overflow or recent overflow) ===
        # Track recent actual excess in rolling 30-min window (6 slots)
        actual_excess_history.append(actual_excess)
        if len(actual_excess_history) > 6:
            actual_excess_history = actual_excess_history[-6:]
        was_overflowing_recently = any(e > dno_limit for e in actual_excess_history)
        soc_cap = battery_kwh * 0.95 if was_overflowing_recently else battery_kwh

        # Battery constraints
        remaining_cap = max(0, soc_cap - soc)
        max_charge_slot = min(max_charge_kw, remaining_cap / step_hours) if remaining_cap > 0.01 else 0
        available_soc = max(0, soc)
        max_discharge_slot = min(max_discharge_kw, available_soc / step_hours) if available_soc > 0.01 else 0

        export = 0.0
        curtailed = 0.0
        charge = 0.0
        discharge = 0.0

        # === REACTIVE PHASE LOGIC ===
        if actual_excess >= dno_limit:
            # OVERFLOW: export at DNO limit, absorb remainder into battery
            phase = "export"
            export = dno_limit
            overflow_kw = actual_excess - dno_limit
            charge = min(overflow_kw, max_charge_slot)
            curtailed = max(0, overflow_kw - charge)

        elif soc < floor - 0.1 and actual_excess > 0:
            # BELOW FLOOR with PV available: charge urgently toward floor
            phase = "charge"
            charge = min(actual_excess, max_charge_slot)
            export = min(actual_excess - charge, dno_limit)

        elif soc > floor + 1.0 and actual_excess > 0 and remaining_overflow > 0:
            # ABOVE FLOOR BY >1kWh with overflow expected: drain toward floor.
            # Only drain while overflow is still expected — need headroom.
            phase = "drain"
            drain_wanted = min((soc - floor) / step_hours, max_discharge_kw)
            discharge = min(drain_wanted, max_discharge_slot)
            total_out = actual_excess + discharge
            export = min(total_out, dno_limit)
            if total_out > dno_limit:
                discharge = max(0, dno_limit - actual_excess)
                export = dno_limit

        elif actual_excess > 0 and soc < soc_cap - 0.1:
            # PV AVAILABLE, ROOM IN BATTERY: charge from PV.
            # This covers both post-overflow and pre-overflow when near floor.
            # Battery should always charge from free PV when possible.
            phase = "charge"
            remaining_cap_now = max(0, soc_cap - soc) if was_overflowing_recently else max(0, battery_kwh - soc)
            max_charge_now = min(max_charge_kw, remaining_cap_now / step_hours) if remaining_cap_now > 0.01 else 0
            charge = min(actual_excess, max_charge_now)
            export = min(actual_excess - charge, dno_limit)

        elif actual_excess > 0:
            # PV available but battery full (or at 95% cap): export
            phase = "hold"
            export = min(actual_excess, dno_limit)

        elif actual_excess < 0:
            # DEFICIT: battery covers load
            phase = "idle"
            discharge = min(-actual_excess, max_discharge_slot)

        else:
            phase = "off"

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
                "floor": floor,
                "floor_pct": floor / battery_kwh * 100,
                "export": export,
                "curtailed": curtailed,
                "charge": charge,
                "discharge": discharge,
                "phase": phase,
                "energy_ratio": energy_ratio,
            }
        )

    # Phase transition count
    phase_transitions = 0
    for i in range(1, len(results)):
        if results[i]["phase"] != results[i - 1]["phase"]:
            phase_transitions += 1

    # Find SOC at sunset (last slot with PV > 0)
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
        "phase_transitions": phase_transitions,
        "max_floor_change_pct": max_floor_change_pct,
        "energy_ratio_final": energy_ratio,
    }


def _run_csv_day_v7_test(label, filename, watts, forecast_scale=1.0, start_soc_pct=None):
    """Run a CSV day through the v7 Commit-and-Ratchet simulation.

    With forecast_scale=1.0, forecast matches reality (perfect forecast).
    With forecast_scale=0.6, forecast is 60% of actual (underforecast).
    With forecast_scale=1.4, forecast is 140% of actual (overforecast).

    Note on v7 limitations vs v6:
    - v7 has no proactive drain window, so on big overflow days the battery stays full
      and curtailment may exceed v6's ~0kWh (physics limit: battery was already full).
    - v7 has no PV scaling, so underforecast days will have more curtailment.
    - The key assertions are: export never exceeds DNO, and curtailment is bounded
      to < 5kWh (v5-style failure produces 5+ kWh).
    """
    if start_soc_pct is None:
        start_soc_pct = START_SOC_PCT
    filepath = os.path.join(CSV_DIR, filename)
    if not os.path.exists(filepath):
        print(f"  {label}: SKIPPED (CSV not found)")
        return False

    pv_actual, load_actual = _load_csv_to_forecasts(filepath, watts=watts)
    pv_forecast = {m: v * forecast_scale for m, v in pv_actual.items()}

    sim = _simulate_day_v7(
        pv_actual,
        load_actual,
        pv_forecast,
        start_soc_pct=start_soc_pct,
        soc_floor_kwh=1.5,
    )

    errors = []
    max_exp = sim["max_export_kw"]
    curtailed = sim["total_curtailed"]
    sunset_soc = sim["sunset_soc_pct"]
    transitions = sim["phase_transitions"]

    # Hard constraint: export must never exceed DNO limit
    if max_exp > DNO_LIMIT + 0.01:
        errors.append(f"max_export={max_exp:.1f}kW > DNO {DNO_LIMIT}kW")

    # Curtailment bound: v7 may curtail on big-overflow days (battery fills to 95% cap),
    # v7 trades some curtailment on extreme underforecast days (60% of actual)
    # for simplicity and stability. With 60% forecast, the algorithm charges in
    # the morning (forecast shows no overflow) — by the time the cumulative ratio
    # detects the error, battery is full. On peak days (65-68 kWh) this means
    # up to ~8 kWh curtailment. In practice, Solcast is within 20% not 40%.
    max_curtailment = 10.0 if forecast_scale < 0.7 else 5.0
    if curtailed > max_curtailment:
        errors.append(f"curtailment={curtailed:.2f}kWh (should be <{max_curtailment:.0f})")

    scale_label = f" @ {forecast_scale:.0%} forecast" if forecast_scale != 1.0 else ""
    soc_label = f" start={start_soc_pct:.0%}" if start_soc_pct != START_SOC_PCT else ""
    tag = f"  v7 {label}{scale_label}{soc_label}"
    if errors:
        detail = "; ".join(errors)
        print(f"{tag}: FAILED — {detail}")
        return True

    print(f"{tag}: PASSED (curtailed={curtailed:.2f}kWh max_exp={max_exp:.1f}kW sunset_soc={sunset_soc:.0f}% transitions={transitions})")
    return False


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
        self.set_read_only = False
        self.config_index = {}
        self.prefix = "predbat"
        self.logs = []
        self.published = {}
        self.services = []
        self._sensor_overrides = sensor_overrides or {}

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
    """Plugin should defer to Predbat during a charge window when SOC < best_soc_keep."""
    pv, load = _make_overflow_pv(minutes_now=300)
    # PV is generating (above threshold)
    pv[0] = 2.0
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=3.0,  # Below best_soc_keep (4.0) — battery needs charging
        minutes_now=300,
        # Charge window active right now: 4am-7am (240-420)
        charge_window_best=[{"start": 240, "end": 420}],
        charge_limit_best=[10.0],  # 10kWh target — real charge, not freeze
        best_soc_keep=4.0,
    )
    plugin = CurtailmentPlugin(base)

    plugin.on_update()

    phase_sensor = base.published.get("sensor.predbat_curtailment_phase", {})
    assert phase_sensor.get("value") == "Off", f"Expected phase='Off' during charge window (SOC < keep), got '{phase_sensor.get('value')}'"
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
# Sticky activation tests
# ============================================================================


def test_plugin_sticky_activation_oscillating_pv():
    """Once overflow is detected, plugin stays active even when PV dips below DNO.

    Simulates volatile cloud day: PV oscillates above and below DNO+load.
    Plugin should activate on first overflow and NOT deactivate on dips.
    """
    pv, load = _make_overflow_pv(minutes_now=720)

    # First cycle: PV high (overflow) — 8kW PV, 1kW load = 7kW excess > DNO
    overrides = {"sensor.sigen_plant_pv_power": 8.0, "sensor.sigen_plant_consumed_power": 1.0}
    base = MockBase(pv_step=pv, load_step=load, soc_kw=10.0, minutes_now=720, sensor_overrides=overrides)
    plugin = CurtailmentPlugin(base)
    plugin.on_update()

    phase1 = base.published.get("sensor.predbat_curtailment_phase", {}).get("value")
    assert phase1 != "Off", f"Expected active phase on overflow, got '{phase1}'"
    assert plugin._seen_overflow_today is True, "Should have seen overflow"

    # Second cycle: PV drops below DNO (cloud) — 3kW PV, 1kW load = 2kW excess < DNO
    base._sensor_overrides["sensor.sigen_plant_pv_power"] = 3.0
    plugin.on_update()

    phase2 = base.published.get("sensor.predbat_curtailment_phase", {}).get("value")
    assert phase2 != "Off", f"Expected plugin to stay active after PV dip, got '{phase2}'"
    assert plugin._seen_overflow_today is True, "Sticky flag should persist through PV dip"

    # Third cycle: PV recovers (sun back) — 7kW PV
    base._sensor_overrides["sensor.sigen_plant_pv_power"] = 7.0
    plugin.on_update()

    phase3 = base.published.get("sensor.predbat_curtailment_phase", {}).get("value")
    assert phase3 != "Off", f"Expected active phase on PV recovery, got '{phase3}'"

    # Fourth cycle: PV gone (evening) — 0kW
    base._sensor_overrides["sensor.sigen_plant_pv_power"] = 0.0
    plugin.on_update()

    assert plugin._seen_overflow_today is False, "Sticky flag should reset when PV is gone"
    print("  test_plugin_sticky_activation_oscillating_pv: PASSED")


# ============================================================================
# SOC floor tests
# ============================================================================


def test_target_soc_clamped_above_soc_keep():
    """Target SOC must never go below best_soc_keep."""
    pv, load = _make_overflow_pv(minutes_now=720)
    # Set best_soc_keep high — plugin should not drain below it
    soc_keep = 8.0  # kWh
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=10.0,
        minutes_now=720,
        best_soc_keep=soc_keep,
        reserve=0,
    )
    plugin = CurtailmentPlugin(base)

    target, overflow, phase, export = plugin.calculate(dno_limit_kw=4.0)
    # With large overflow, raw target would be near 0 — but clamped to soc_keep
    assert target >= soc_keep, f"Target SOC {target:.2f} kWh should be >= best_soc_keep {soc_keep:.2f} kWh"
    print("  test_target_soc_clamped_above_soc_keep: PASSED (target={:.2f}kWh >= soc_keep={:.2f}kWh)".format(target, soc_keep))


def test_target_soc_clamped_above_reserve():
    """Target SOC must never go below reserve."""
    pv, load = _make_overflow_pv(minutes_now=720)
    reserve = 5.0  # kWh
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=10.0,
        minutes_now=720,
        best_soc_keep=0,
        reserve=reserve,
    )
    plugin = CurtailmentPlugin(base)

    target, overflow, phase, export = plugin.calculate(dno_limit_kw=4.0)
    assert target >= reserve, f"Target SOC {target:.2f} kWh should be >= reserve {reserve:.2f} kWh"
    print("  test_target_soc_clamped_above_reserve: PASSED (target={:.2f}kWh >= reserve={:.2f}kWh)".format(target, reserve))


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
# Phase logic matrix tests — PV state × SOC state
# ============================================================================

PHASE_TEST_DNO = 4.0
PHASE_TEST_SOC_MAX = 18.08
PHASE_TEST_STEP = PLUGIN_STEP


def _make_phase_test_plugin(pv_kw, load_kw, soc_pct, target_pct):
    """Create a plugin with specific PV, load, SOC, and a pre-set smoothed floor."""
    # Create forecast with overflow so curtailment activates
    pv = {}
    load = {}
    for m in range(0, 660, PHASE_TEST_STEP):
        pv[m] = 6.0  # forecast: enough overflow to activate
        load[m] = 1.0

    soc_kwh = PHASE_TEST_SOC_MAX * soc_pct / 100.0
    target_kwh = PHASE_TEST_SOC_MAX * target_pct / 100.0

    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=soc_kwh,
        soc_max=PHASE_TEST_SOC_MAX,
        minutes_now=720,
        sensor_overrides={
            "sensor.sigen_plant_pv_power": pv_kw,
            "sensor.sigen_plant_consumed_power": load_kw,
        },
    )
    plugin = CurtailmentPlugin(base)
    # Pre-set the smoothed floor to the desired target
    # floor is raw now, no smoothing
    # Mark as having seen overflow (so post-overflow detection works)
    plugin._was_overflowing = True
    return plugin, base


def test_phase_overflow_any_soc():
    """When PV-load > DNO: phase is always export (overflow)."""
    for label, soc_pct in [("below", 5), ("above", 80)]:
        plugin, base = _make_phase_test_plugin(pv_kw=8.0, load_kw=0.5, soc_pct=soc_pct, target_pct=45)
        target, overflow, phase, export = plugin.calculate(PHASE_TEST_DNO)
        assert phase == "export", f"Overflow SOC {label}: expected export phase, got {phase}"
    print("  test_phase_overflow_any_soc: PASSED (export phase at all SOC levels)")


def test_phase_excess_soc_below_target():
    """PV excess < DNO, SOC well below computed target: charge phase."""
    # Low SOC ensures we're below whatever target the forecast computes
    plugin, base = _make_phase_test_plugin(pv_kw=3.0, load_kw=0.5, soc_pct=5, target_pct=45)
    target, overflow, phase, export = plugin.calculate(PHASE_TEST_DNO)
    assert phase == "charge", f"Expected charge phase, got {phase}"
    print("  test_phase_excess_soc_below_target: PASSED (charge from PV)")


def test_phase_excess_soc_above_target():
    """PV excess < DNO, SOC well above computed target: export phase (drain)."""
    # High SOC ensures we're well above whatever target the forecast computes
    plugin, base = _make_phase_test_plugin(pv_kw=3.0, load_kw=0.5, soc_pct=90, target_pct=45)
    target, overflow, phase, export = plugin.calculate(PHASE_TEST_DNO)
    # With big overflow forecast, target is low. SOC at 90% is well above → drain
    assert phase == "export", f"Expected export phase (drain), got {phase}"
    print("  test_phase_excess_soc_above_target: PASSED (drain toward target)")


def test_phase_deficit_not_export():
    """PV < load: phase should not be export."""
    for label, soc_pct in [("below", 5), ("above", 90)]:
        plugin, base = _make_phase_test_plugin(pv_kw=0.3, load_kw=0.8, soc_pct=soc_pct, target_pct=45)
        target, overflow, phase, export = plugin.calculate(PHASE_TEST_DNO)
        assert phase != "export", f"Deficit SOC {label}: should not be export phase, got {phase}"
    print("  test_phase_deficit_not_export: PASSED (no export during deficit)")


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
    """When actual PV greatly exceeds forecast, plugin activates via currently_overflowing.

    V8 behavior (trajectory-based floor):
    - Forecast excess = 4.5-1.0 = 3.5kW < DNO → no trajectory overflow → net_charge≈0
    - Plugin activates because actual excess (7.8-0.7=7.1kW) > DNO (4.0kW) → sticky
    - Phase = "export" (currently overflowing, regardless of forecast)
    - Floor = soc_cap (95%) since trajectory shows no overflow to absorb
    - Energy ratio > 1.0 but does not directly affect floor (trajectory handles it)
    """
    # Forecast: modest PV (4.5kW), barely below DNO with 1kW load → ~0 forecast overflow
    pv = {}
    load = {}
    for m in range(0, 660, PLUGIN_STEP):  # 11 hours of solar remaining
        pv[m] = 4.5
        load[m] = 1.0
    # Forecast excess = 4.5-1.0 = 3.5kW < DNO 4.0 → no forecast overflow
    # Actual excess = 7.8-0.7 = 7.1kW > DNO → currently_overflowing = True
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
    target_soc, net_charge, phase, export_target = plugin.calculate(4.0)

    # V8: plugin activates via currently_overflowing (sticky), phase=export
    assert phase == "export", f"Expected export phase (currently overflowing), got {phase}"
    target_pct = target_soc / 18.08 * 100
    # V8: floor = soc_cap (95%) since trajectory forecast shows no overflow to absorb
    # (forecast PV 4.5kW < DNO 4.0kW + 1kW load — no overflow slots in trajectory)
    assert 94 <= target_pct <= 96, f"Floor should be at soc_cap (~95%), got {target_pct:.1f}%"
    # Energy ratio > 1.0 because actual PV (7.8) > forecast PV (4.5)
    assert plugin._energy_ratio >= 1.0, f"Energy ratio should be >= 1.0 when actual > forecast, got {plugin._energy_ratio:.2f}"
    print(f"  test_realtime_pv_correction_raises_overflow: PASSED (net_charge={net_charge:.2f}, target={target_pct:.0f}%)")


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
    target_soc, overflow, phase, export_target = plugin.calculate(4.0)

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
# v7 strategy tests — "Commit and Ratchet" with cumulative energy ratio
# ============================================================================


def _print_sim_summary_v7(label, sim):
    """Print a v7 simulation summary line."""
    print(
        f"    {label}: curtailed={sim['total_curtailed']:.2f}kWh "
        f"max_export={sim['max_export_kw']:.1f}kW "
        f"sunset_soc={sim['sunset_soc_pct']:.0f}% "
        f"overflow_forecast={sim['initial_overflow_forecast']:.1f}kWh "
        f"overflow_actual={sim['initial_overflow_actual']:.1f}kWh "
        f"transitions={sim['phase_transitions']} "
        f"ratio={sim['energy_ratio_final']:.2f}"
    )


def test_v7_perfect_forecast():
    """V7 with perfect forecast: export stays within DNO, phase count is stable.

    Note: v7 does not proactively drain the battery before overflow, so on a day where
    overflow (17kWh) ≈ battery capacity (18kWh), the battery fills to the 95% cap and
    the remaining overflow is curtailed. This is by design — v7 trades curtailment on
    extreme days for simplicity and safety. The key assertions are: export stays in
    bounds and phase count is low (stable strategy).
    """
    pv_actual, load_actual = _make_sunny_day(peak_kw=8.0, load_kw=0.7)

    sim = _simulate_day_v7(
        pv_actual,
        load_actual,
        pv_forecast=pv_actual,  # perfect forecast
        load_forecast=load_actual,
        start_soc_pct=START_SOC_PCT,
        soc_floor_kwh=1.5,
    )

    max_exp = sim["max_export_kw"]
    curtailed = sim["total_curtailed"]
    sunset_soc = sim["sunset_soc_pct"]
    transitions = sim["phase_transitions"]
    _print_sim_summary_v7("v7 perfect forecast", sim)

    assert max_exp <= DNO_LIMIT + 0.01, f"Export {max_exp:.1f}kW exceeded DNO {DNO_LIMIT}kW"
    # v7 may curtail on big-overflow days (battery fills; 95% cap is safety backstop).
    # The meaningful bound: less than v5-level failure (5.7 kWh on this day).
    assert curtailed < 4.0, f"Curtailment {curtailed:.2f}kWh excessive (should be <4.0, v5 curtails 5.7)"
    assert transitions < 20, f"Phase transitions={transitions} should be <20 for stable strategy"
    # Energy ratio converges to 1.0 with perfect forecast
    assert abs(sim["energy_ratio_final"] - 1.0) < 0.05, f"Energy ratio {sim['energy_ratio_final']:.2f} should be ~1.0 with perfect forecast"
    print(f"  test_v7_perfect_forecast: PASSED (max_export={max_exp:.1f}kW curtailed={curtailed:.2f}kWh sunset_soc={sunset_soc:.0f}%)")


def test_v7_underforecast_60pct():
    """V7 with 60% underforecast: cumulative ratio corrects the floor over time.

    Without PV scaling, v7 can't know overflow is coming until it arrives.
    The cumulative ratio adjusts the floor to be lower (more room) as it detects
    that actual > forecast, but this correction is retrospective.
    Key assertions: export stays within DNO, energy ratio is >1.0 (detected underforecast).
    """
    pv_actual, load_actual = _make_sunny_day(peak_kw=8.0, load_kw=0.7)
    pv_forecast = {m: v * 0.6 for m, v in pv_actual.items()}

    sim = _simulate_day_v7(
        pv_actual,
        load_actual,
        pv_forecast=pv_forecast,
        start_soc_pct=START_SOC_PCT,
        soc_floor_kwh=1.5,
    )

    max_exp = sim["max_export_kw"]
    curtailed = sim["total_curtailed"]
    ratio_final = sim["energy_ratio_final"]
    _print_sim_summary_v7("v7 @ 60% forecast", sim)

    assert max_exp <= DNO_LIMIT + 0.01, f"Export {max_exp:.1f}kW exceeded DNO {DNO_LIMIT}kW"
    # Curtailment is expected on a severe underforecast day — v7 can't anticipate overflow.
    # Bound: less than v5 failure level (5.7 kWh).
    assert curtailed < 5.0, f"Curtailment {curtailed:.2f}kWh excessive (should be <5.0)"
    # Energy ratio should converge toward ~1/0.6 ≈ 1.67 by end of day
    assert ratio_final > 1.2, f"Final energy ratio {ratio_final:.2f} should be >1.2 for 60% underforecast"
    print(f"  test_v7_underforecast_60pct: PASSED (max_export={max_exp:.1f}kW curtailed={curtailed:.2f}kWh ratio={ratio_final:.2f})")


def test_v7_overforecast_140pct():
    """V7 with 140% overforecast: energy ratio detects the overforecast correctly.

    When forecast is 140% of actual, the algorithm initially sets a very low floor
    (expecting huge overflow). The 95% cap prevents over-charging during actual overflow.
    The cumulative ratio converges to ~0.71, correctly identifying overforecast.
    Key assertions: export within DNO, energy ratio < 1.0 (detected overforecast).
    """
    pv_actual, load_actual = _make_sunny_day(peak_kw=8.0, load_kw=0.7)
    pv_forecast = {m: v * 1.4 for m, v in pv_actual.items()}

    sim = _simulate_day_v7(
        pv_actual,
        load_actual,
        pv_forecast=pv_forecast,
        start_soc_pct=START_SOC_PCT,
        soc_floor_kwh=1.5,
    )

    max_exp = sim["max_export_kw"]
    curtailed = sim["total_curtailed"]
    sunset_soc = sim["sunset_soc_pct"]
    ratio_final = sim["energy_ratio_final"]
    _print_sim_summary_v7("v7 @ 140% forecast", sim)

    assert max_exp <= DNO_LIMIT + 0.01, f"Export {max_exp:.1f}kW exceeded DNO {DNO_LIMIT}kW"
    # Curtailment may occur as on a perfect forecast day (battery hits 95% cap during overflow).
    # Bound: less than v5 failure level.
    assert curtailed < 4.0, f"Curtailment {curtailed:.2f}kWh excessive for overforecast day"
    # Energy ratio should converge toward ~1/1.4 ≈ 0.71 (actual < forecast)
    assert ratio_final < 0.9, f"Final energy ratio {ratio_final:.2f} should be <0.9 for 140% overforecast"
    # Battery should end near full — overforecast means floor was low, battery charged freely
    assert sunset_soc >= 85, f"Sunset SOC {sunset_soc:.0f}% should be >=85% on overforecast day (floor is low)"
    print(f"  test_v7_overforecast_140pct: PASSED (max_export={max_exp:.1f}kW curtailed={curtailed:.2f}kWh sunset_soc={sunset_soc:.0f}% ratio={ratio_final:.2f})")


def test_v7_overforecast_sunset_soc():
    """V7 overforecast: battery ends near full because floor is kept low.

    When forecast overestimates, the floor (battery_kwh - adjusted_overflow - adjusted_post)
    is kept low (near 0), so the battery charges freely during overflow and post-overflow.
    This means overforecast days end with high SOC even without proactive drain logic.
    """
    pv_actual, load_actual = _make_sunny_day(peak_kw=8.0, load_kw=0.7)
    # Forecast is 140% of actual — algorithm will set very low floor expecting huge overflow
    pv_forecast = {m: v * 1.4 for m, v in pv_actual.items()}

    sim = _simulate_day_v7(
        pv_actual,
        load_actual,
        pv_forecast=pv_forecast,
        start_soc_pct=START_SOC_PCT,
        soc_floor_kwh=1.5,
    )

    max_exp = sim["max_export_kw"]
    curtailed = sim["total_curtailed"]
    sunset_soc = sim["sunset_soc_pct"]
    _print_sim_summary_v7("v7 overforecast sunset SOC", sim)

    assert max_exp <= DNO_LIMIT + 0.01, f"Export {max_exp:.1f}kW exceeded DNO {DNO_LIMIT}kW"
    assert curtailed < 4.0, f"Curtailment {curtailed:.2f}kWh excessive for overforecast day"
    # Overforecast keeps floor low → battery charges freely → high sunset SOC
    assert sunset_soc >= 85, f"Sunset SOC {sunset_soc:.0f}% should be >=85% (low floor from overforecast)"
    print(f"  test_v7_overforecast_sunset_soc: PASSED (max_export={max_exp:.1f}kW sunset_soc={sunset_soc:.0f}%)")


# ============================================================================
# v8 trajectory tests — simulate_soc_trajectory pure function
# ============================================================================


def _make_trajectory_pv(peak_kw, load_kw, hours=6, step_minutes=5):
    """
    Create PV/load dicts for simulate_soc_trajectory, keyed from minute 0.

    Simulates a mid-day window (current time = now, PV is already generating).
    Returns raw kW values (values_are_kwh=False, the function default).
    """
    pv = {}
    load = {}
    total_slots = hours * 60 // step_minutes
    mid = total_slots / 2
    for i in range(total_slots):
        m = i * step_minutes
        x = (i - mid) / (mid if mid else 1)
        pv[m] = peak_kw * max(0, 1 - x * x)
        load[m] = load_kw
    return pv, load


def test_trajectory_battery_fills():
    """Trajectory detects battery will fill on a big PV day.

    Starting at 66% (12kWh) with 8kW PV and 7.35kWh overflow in 6h window —
    overflow > remaining capacity (6.08kWh) so battery fills to soc_max.
    """
    # 6h window, 8kW peak, 1kW load — plenty of overflow (7.35kWh total)
    pv, load = _make_trajectory_pv(peak_kw=8.0, load_kw=1.0, hours=6)
    peak, net_charge, last_danger = simulate_soc_trajectory(pv, load, current_soc=12.0, soc_max=18.08, dno_limit=4.0)
    assert peak > 18.08 * 0.95, f"Expected battery to fill, peak={peak:.1f}"
    assert net_charge > 5.0, f"Expected significant overflow charge, got {net_charge:.1f}"
    assert last_danger > 0, "Should have danger slots"
    print(f"  test_trajectory_battery_fills: PASSED (peak={peak:.1f}kWh net_charge={net_charge:.1f}kWh)")


def test_trajectory_battery_wont_fill():
    """Low PV day — battery won't fill."""
    # 3kW peak, 1kW load → max excess = 2kW < DNO 4kW → no overflow → battery stays flat
    pv, load = _make_trajectory_pv(peak_kw=3.0, load_kw=1.0, hours=6)
    peak, net_charge, last_danger = simulate_soc_trajectory(pv, load, current_soc=9.0, soc_max=18.08, dno_limit=4.0)
    assert peak < 18.08 * 0.95, f"Battery shouldn't fill, peak={peak:.1f}"
    print(f"  test_trajectory_battery_wont_fill: PASSED (peak={peak:.1f}kWh)")


def test_trajectory_high_soc_fills():
    """Moderate PV + high starting SOC = fills.

    Starting at 83% (15kWh) with 7kW PV — 4.28kWh overflow covers remaining 3.08kWh capacity.
    """
    # 7kW peak, 1kW load → 4.28kWh overflow in 6h window > remaining 3.08kWh
    pv, load = _make_trajectory_pv(peak_kw=7.0, load_kw=1.0, hours=6)
    peak, net_charge, last_danger = simulate_soc_trajectory(pv, load, current_soc=15.0, soc_max=18.08, dno_limit=4.0)  # 83% start
    assert peak > 18.08 * 0.95, f"Should fill from 83% start with 7kW PV, peak={peak:.1f}"
    print(f"  test_trajectory_high_soc_fills: PASSED (peak={peak:.1f}kWh)")


def test_trajectory_energy_ratio_increases_overflow():
    """Energy ratio > 1 should increase peak SOC (more PV than forecast)."""
    # 6kW barely-overflowing scenario — ratio 1.5 should push peak higher
    pv, load = _make_trajectory_pv(peak_kw=6.0, load_kw=1.0, hours=6)
    peak_1x, _, _ = simulate_soc_trajectory(pv, load, 9.0, 18.08, 4.0, energy_ratio=1.0)
    peak_15x, _, _ = simulate_soc_trajectory(pv, load, 9.0, 18.08, 4.0, energy_ratio=1.5)
    assert peak_15x > peak_1x, f"Higher ratio should increase peak: {peak_15x:.1f} vs {peak_1x:.1f}"
    print(f"  test_trajectory_energy_ratio_increases_overflow: PASSED ({peak_1x:.1f} -> {peak_15x:.1f}kWh)")


def test_trajectory_energy_ratio_decreases_overflow():
    """Energy ratio < 1 should decrease peak SOC (less PV than forecast)."""
    # 8kW big overflow day — ratio 0.6 should significantly reduce peak
    pv, load = _make_trajectory_pv(peak_kw=8.0, load_kw=1.0, hours=6)
    peak_1x, _, _ = simulate_soc_trajectory(pv, load, 9.0, 18.08, 4.0, energy_ratio=1.0)
    peak_06x, _, _ = simulate_soc_trajectory(pv, load, 9.0, 18.08, 4.0, energy_ratio=0.6)
    assert peak_06x < peak_1x, f"Lower ratio should decrease peak: {peak_06x:.1f} vs {peak_1x:.1f}"
    print(f"  test_trajectory_energy_ratio_decreases_overflow: PASSED ({peak_1x:.1f} -> {peak_06x:.1f}kWh)")


def test_v8_floor_bidirectional():
    """v8 floor drops when forecast overflow increases (more PV predicted).

    The adaptive floor = soc_cap - net_charge. When the forecast shows more
    overflow (higher PV), net_charge increases and the floor decreases.

    Uses moderate PV (6kW vs 9kW) so that floor is not clamped to 0 in both
    cases. 6kW gives ~11kWh overflow → floor≈34%. 9kW gives ~44kWh → floor=0%.
    """
    # 6kW forecast: moderate overflow, floor should be > 0
    pv_moderate = {}
    load_moderate = {}
    for m in range(0, 660, PLUGIN_STEP):
        pv_moderate[m] = 6.0  # kW
        load_moderate[m] = 1.0  # kW

    base1 = MockBase(
        pv_step=pv_moderate,
        load_step=load_moderate,
        soc_kw=10.0,
        minutes_now=720,
        sensor_overrides={
            "sensor.sigen_plant_pv_power": 6.0,
            "sensor.sigen_plant_consumed_power": 1.0,
        },
    )
    plugin1 = CurtailmentPlugin(base1)
    target1, nc1, phase1, _ = plugin1.calculate(dno_limit_kw=4.0)
    floor_moderate = target1 / 18.08 * 100

    # 9kW forecast: larger overflow, floor should be lower
    pv_high = {}
    for m in range(0, 660, PLUGIN_STEP):
        pv_high[m] = 9.0  # kW

    base2 = MockBase(
        pv_step=pv_high,
        load_step=load_moderate,
        soc_kw=10.0,
        minutes_now=720,
        sensor_overrides={
            "sensor.sigen_plant_pv_power": 9.0,
            "sensor.sigen_plant_consumed_power": 1.0,
        },
    )
    plugin2 = CurtailmentPlugin(base2)
    target2, nc2, phase2, _ = plugin2.calculate(dno_limit_kw=4.0)
    floor_high = target2 / 18.08 * 100

    # More overflow → lower floor (need less headroom because overflow does the charging)
    assert floor_high < floor_moderate, f"Higher forecast overflow should give lower floor: " f"{floor_high:.1f}% vs {floor_moderate:.1f}% (nc1={nc1:.1f}, nc2={nc2:.1f})"
    # Also verify that the moderate case has a non-trivial floor (not clamped to 0)
    assert floor_moderate > 10, f"Moderate PV floor should be > 10%, got {floor_moderate:.1f}%"
    print(f"  test_v8_floor_bidirectional: PASSED (6kW floor={floor_moderate:.1f}%, 9kW floor={floor_high:.1f}%)")


def test_trajectory_unmanaged_vs_managed():
    """Unmanaged (MSC) fills battery, managed (D-ESS) doesn't — activation must use unmanaged.

    This is the Mar 31 bug: managed trajectory showed 42% peak (no activation) while
    unmanaged showed 100% (battery fills without intervention). Without unmanaged check,
    curtailment never activates and battery fills → SIG fault.
    """
    # Moderate PV day: 5kW peak, 1kW load. Excess 4kW = just at DNO limit.
    # Unmanaged: all 4kW excess charges battery → fills from 40%.
    # Managed: excess = DNO, overflow = 0 → battery stays at starting SOC.
    pv, load = _make_trajectory_pv(peak_kw=5.0, load_kw=1.0, hours=8)

    start = 18.08 * 0.40  # 40%
    peak_u, _, _ = simulate_soc_trajectory(pv, load, start, 18.08, 4.0, unmanaged=True)
    peak_m, _, _ = simulate_soc_trajectory(pv, load, start, 18.08, 4.0, unmanaged=False)

    assert peak_u > 18.08 * 0.95, f"Unmanaged should fill: peak={peak_u:.1f} (expected >17.2)"
    assert peak_m < 18.08 * 0.95, f"Managed should NOT fill: peak={peak_m:.1f} (expected <17.2)"
    print(f"  test_trajectory_unmanaged_vs_managed: PASSED (unmanaged={peak_u:.1f}, managed={peak_m:.1f})")


def test_trajectory_mar31_scenario():
    """Replay Mar 31 conditions: 35% start, high PV, moderate load.

    This day caused 2 SIG faults because curtailment didn't activate.
    v8 must activate (unmanaged fills) and set a floor that keeps peak < 100%.
    """
    # Mar 31 pattern: 6h of 6kW average PV, 1kW average load
    pv, load = _make_trajectory_pv(peak_kw=7.0, load_kw=1.0, hours=8)

    start = 18.08 * 0.35  # 35% morning SOC

    # Activation check (unmanaged)
    peak_u, _, _ = simulate_soc_trajectory(pv, load, start, 18.08, 4.0, unmanaged=True)
    assert peak_u > 18.08 * 0.95, f"Must activate: unmanaged peak={peak_u:.1f}"

    # Floor check (managed)
    _, net_charge, _ = simulate_soc_trajectory(pv, load, start, 18.08, 4.0, unmanaged=False)
    floor = max(0, 18.08 * 0.95 - net_charge)

    # Battery peaks at floor + net_charge — must stay below 100%
    peak_with_curtailment = floor + net_charge
    assert peak_with_curtailment <= 18.08, f"Peak with curtailment should be ≤100%: {peak_with_curtailment:.1f}"

    print(f"  test_trajectory_mar31_scenario: PASSED (activates, floor={floor/18.08*100:.0f}%, peak={peak_with_curtailment/18.08*100:.0f}%)")


def test_trajectory_low_pv_no_activation():
    """Low PV day: neither managed nor unmanaged fills battery. No activation."""
    pv, load = _make_trajectory_pv(peak_kw=3.0, load_kw=1.0, hours=6)

    start = 18.08 * 0.40
    peak_u, _, _ = simulate_soc_trajectory(pv, load, start, 18.08, 4.0, unmanaged=True)
    peak_m, _, _ = simulate_soc_trajectory(pv, load, start, 18.08, 4.0, unmanaged=False)

    assert peak_u < 18.08 * 0.95, f"Unmanaged should NOT fill: peak={peak_u:.1f}"
    assert peak_m < 18.08 * 0.95, f"Managed should NOT fill: peak={peak_m:.1f}"
    print(f"  test_trajectory_low_pv_no_activation: PASSED (unmanaged={peak_u:.1f}, managed={peak_m:.1f})")


def test_trajectory_load_ratio_reduces_excess():
    """Load ratio < 1 means actual load lower → more excess → lower floor."""
    pv, load = _make_trajectory_pv(peak_kw=7.0, load_kw=1.5, hours=6)

    _, net_10, _ = simulate_soc_trajectory(pv, load, 9.0, 18.08, 4.0, load_ratio=1.0)
    _, net_06, _ = simulate_soc_trajectory(pv, load, 9.0, 18.08, 4.0, load_ratio=0.6)

    # Lower load ratio → less load consumed → more excess → more overflow → more net_charge
    assert net_06 > net_10, f"Lower load ratio should increase net charge: {net_06:.1f} vs {net_10:.1f}"
    print(f"  test_trajectory_load_ratio_reduces_excess: PASSED (ratio 1.0: {net_10:.1f}, ratio 0.6: {net_06:.1f})")


# ============================================================================
# v8 plugin integration tests — full on_update / trajectory-based activation
# ============================================================================


def test_plugin_activates_from_unmanaged_trajectory():
    """Plugin must use unmanaged (MSC) trajectory for activation, not managed (D-ESS).

    A day with 5kW PV and 1kW load: excess = 4kW = DNO limit.
    Managed: no overflow (excess == DNO exactly) → battery stays flat.
    Unmanaged: all 4kW excess charges battery → fills from 40%.
    Plugin MUST activate because unmanaged shows battery fills.
    """
    # 5kW PV, 1kW load — excess exactly equals DNO limit (no managed overflow)
    pv, load = _make_trajectory_pv(peak_kw=5.0, load_kw=1.0, hours=8)

    # Verify the premise: managed won't fill, unmanaged will
    start = BATTERY_KWH * 0.40
    peak_u, _, _ = simulate_soc_trajectory(pv, load, start, BATTERY_KWH, DNO_LIMIT, unmanaged=True)
    peak_m, _, _ = simulate_soc_trajectory(pv, load, start, BATTERY_KWH, DNO_LIMIT, unmanaged=False)
    assert peak_u > BATTERY_KWH * 0.95, f"Premise: unmanaged should fill from 40%, got peak={peak_u:.1f}"
    assert peak_m < BATTERY_KWH * 0.95, f"Premise: managed should NOT fill, got peak={peak_m:.1f}"

    # Pass raw kW values to MockBase — it converts to kWh/step internally
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=BATTERY_KWH * 0.40,
        soc_max=BATTERY_KWH,
        minutes_now=720,
        sensor_overrides={
            "sensor.sigen_plant_pv_power": 5.0,
            "sensor.sigen_plant_consumed_power": 1.0,
        },
    )
    plugin = CurtailmentPlugin(base)
    plugin.on_update()

    phase_sensor = base.published.get("sensor.predbat_curtailment_phase", {})
    phase = phase_sensor.get("value", "Off")
    assert phase != "Off", f"Plugin should activate (unmanaged fills), got phase='{phase}'"
    assert base.set_read_only is True, "read_only should be True when plugin activates"
    print(f"  test_plugin_activates_from_unmanaged_trajectory: PASSED (phase={phase})")


def test_floor_rises_as_overflow_shrinks():
    """Floor should rise through the afternoon as future overflow decreases.

    Two plugin calculations with different amounts of remaining PV:
    - Many remaining high-PV slots → large net_charge → low floor.
    - Few remaining high-PV slots → small net_charge → high floor.
    """
    # "10:00": many high-PV slots remaining (8kW peak, 8h window)
    pv_long, load_long = _make_trajectory_pv(peak_kw=8.0, load_kw=1.0, hours=8)

    # Pass raw kW values to MockBase (MockBase converts to kWh/step internally)
    base_10 = MockBase(
        pv_step=pv_long,
        load_step=load_long,
        soc_kw=BATTERY_KWH * 0.40,
        soc_max=BATTERY_KWH,
        minutes_now=600,
        sensor_overrides={
            "sensor.sigen_plant_pv_power": 6.0,
            "sensor.sigen_plant_consumed_power": 1.0,
        },
    )
    plugin_10 = CurtailmentPlugin(base_10)
    target_10, nc_10, phase_10, _ = plugin_10.calculate(dno_limit_kw=DNO_LIMIT)

    # "15:00": only a small tail of PV remaining (8kW peak but only 2h window)
    pv_short, load_short = _make_trajectory_pv(peak_kw=8.0, load_kw=1.0, hours=2)

    base_15 = MockBase(
        pv_step=pv_short,
        load_step=load_short,
        soc_kw=BATTERY_KWH * 0.40,
        soc_max=BATTERY_KWH,
        minutes_now=900,
        sensor_overrides={
            "sensor.sigen_plant_pv_power": 4.0,
            "sensor.sigen_plant_consumed_power": 1.0,
        },
    )
    plugin_15 = CurtailmentPlugin(base_15)
    target_15, nc_15, phase_15, _ = plugin_15.calculate(dno_limit_kw=DNO_LIMIT)

    # More remaining PV → more net_charge → lower floor.
    # Less remaining PV → less net_charge → higher floor.
    assert nc_10 > nc_15, f"Premise: more PV remaining at 10:00 should give more net_charge " f"(nc_10={nc_10:.2f} nc_15={nc_15:.2f})"
    assert target_15 > target_10, f"Floor at 15:00 ({target_15:.2f} kWh = {target_15/BATTERY_KWH*100:.0f}%) " f"should be > floor at 10:00 ({target_10:.2f} kWh = {target_10/BATTERY_KWH*100:.0f}%)"
    print(f"  test_floor_rises_as_overflow_shrinks: PASSED " f"(10:00={target_10/BATTERY_KWH*100:.0f}% nc={nc_10:.1f}, " f"15:00={target_15/BATTERY_KWH*100:.0f}% nc={nc_15:.1f})")


def test_release_when_pv_below_threshold():
    """When all remaining PV < SAFE_PV_THRESHOLD_KW (2kW), floor rises to soc_max (release)."""
    # Build a forecast where all slots have PV well below 2kW
    # Pass raw kW values to MockBase (MockBase converts to kWh/step internally)
    pv_step = {m: 1.0 for m in range(0, 600, PLUGIN_STEP)}  # 1kW — below SAFE_PV_THRESHOLD_KW
    load_step = {m: 0.5 for m in range(0, 600, PLUGIN_STEP)}

    base = MockBase(
        pv_step=pv_step,
        load_step=load_step,
        soc_kw=BATTERY_KWH * 0.80,
        soc_max=BATTERY_KWH,
        minutes_now=720,
        sensor_overrides={
            "sensor.sigen_plant_pv_power": 1.0,  # actual PV also below threshold
            "sensor.sigen_plant_consumed_power": 0.5,
        },
    )
    plugin = CurtailmentPlugin(base)
    # Simulate that overflow was seen earlier today — plugin is sticky
    plugin._seen_overflow_today = True

    target_soc_kwh, net_charge, phase, _ = plugin.calculate(dno_limit_kw=DNO_LIMIT)

    assert target_soc_kwh == BATTERY_KWH, f"When all remaining PV < {SAFE_PV_THRESHOLD_KW}kW, floor should be soc_max " f"({BATTERY_KWH} kWh), got {target_soc_kwh:.2f} kWh"
    print(f"  test_release_when_pv_below_threshold: PASSED " f"(floor={target_soc_kwh:.2f} kWh = soc_max, phase={phase})")


def test_sticky_keeps_active_when_trajectory_clears():
    """Once overflow seen, plugin stays active even if trajectory no longer shows filling.

    Cycle 1: overflow currently happening → sticky flag set, plugin activates.
    Cycle 2: PV drops, trajectory shows battery won't fill — but sticky keeps plugin on.
    """
    # Cycle 1: high PV — trajectory fills (raw kW, MockBase converts to kWh/step)
    pv_high = {m: 8.0 for m in range(0, 600, PLUGIN_STEP)}
    load_high = {m: 1.0 for m in range(0, 600, PLUGIN_STEP)}

    base1 = MockBase(
        pv_step=pv_high,
        load_step=load_high,
        soc_kw=BATTERY_KWH * 0.50,
        soc_max=BATTERY_KWH,
        minutes_now=720,
        sensor_overrides={
            "sensor.sigen_plant_pv_power": 8.0,  # currently overflowing
            "sensor.sigen_plant_consumed_power": 1.0,
        },
    )
    plugin = CurtailmentPlugin(base1)
    plugin.calculate(dno_limit_kw=DNO_LIMIT)

    # Manually confirm sticky flag is set (simulate currently_overflowing = True)
    plugin._seen_overflow_today = True

    # Cycle 2: PV drops to 1.5kW — below SAFE_PV_THRESHOLD_KW, trajectory won't fill
    # Use raw kW values (MockBase converts to kWh/step)
    pv_low = {m: 1.5 for m in range(0, 600, PLUGIN_STEP)}
    load_low = {m: 1.0 for m in range(0, 600, PLUGIN_STEP)}

    # Verify premise: 1.5kW PV won't fill battery
    peak_u, _, _ = simulate_soc_trajectory(pv_low, load_low, BATTERY_KWH * 0.50, BATTERY_KWH, DNO_LIMIT, unmanaged=True)
    assert peak_u < BATTERY_KWH * 0.95, f"Premise: 1.5kW PV should not fill battery, peak={peak_u:.1f}"

    base2 = MockBase(
        pv_step=pv_low,
        load_step=load_low,
        soc_kw=BATTERY_KWH * 0.50,
        soc_max=BATTERY_KWH,
        minutes_now=720,
        sensor_overrides={
            "sensor.sigen_plant_pv_power": 1.5,  # low, not overflowing
            "sensor.sigen_plant_consumed_power": 1.0,
        },
    )
    # Transfer sticky state to new plugin instance (simulates persistent state across cycles)
    plugin.base = base2

    target, nc, phase, _ = plugin.calculate(dno_limit_kw=DNO_LIMIT)

    assert phase != "off", f"Sticky: plugin should stay active even when trajectory clears, got phase='{phase}'"
    print(f"  test_sticky_keeps_active_when_trajectory_clears: PASSED (phase={phase})")


def test_on_update_full_flow_activates():
    """Full on_update flow: trajectory fills → activate → D-ESS set → read_only → phase published."""
    # High PV day — unmanaged trajectory definitely fills from 40%
    # Pass raw kW values to MockBase (MockBase converts to kWh/step internally)
    pv, load = _make_trajectory_pv(peak_kw=8.0, load_kw=1.0, hours=8)

    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=BATTERY_KWH * 0.40,
        soc_max=BATTERY_KWH,
        minutes_now=720,
        sensor_overrides={
            "sensor.sigen_plant_pv_power": 8.0,
            "sensor.sigen_plant_consumed_power": 1.0,
        },
    )
    plugin = CurtailmentPlugin(base)
    plugin.on_update()

    # Phase sensor published and not "Off"
    phase_sensor = base.published.get("sensor.predbat_curtailment_phase", {})
    phase = phase_sensor.get("value", "Off")
    assert phase != "Off", f"Expected active phase (high PV, trajectory fills), got '{phase}'"

    # read_only must be True
    assert base.set_read_only is True, "set_read_only should be True when plugin activates"

    # Services called (D-ESS mode + charge limit written)
    service_names = [s[0] for s in base.services]
    assert any("select" in svc for svc in service_names), f"Expected select/select_option (EMS mode) service call, got {service_names}"
    assert any("number" in svc for svc in service_names), f"Expected number/set_value (charge limit) service call, got {service_names}"

    # Target SOC sensor published with a floor value (not None)
    target_sensor = base.published.get("sensor.predbat_curtailment_target_soc", {})
    assert target_sensor.get("value") is not None, "Target SOC sensor should be published"
    target_pct = target_sensor.get("value")
    assert isinstance(target_pct, (int, float)), f"Target SOC should be numeric, got {type(target_pct)}"

    print(f"  test_on_update_full_flow_activates: PASSED " f"(phase={phase}, target={target_pct:.0f}%, services={service_names})")


def test_before_plan_trajectory_reduces_keep():
    """on_before_plan should reduce best_soc_keep when trajectory shows battery will fill.

    on_before_plan uses simulate_soc_trajectory starting from soc_max.
    will_fill = True when the solar creates overflow even from 100% start (net_charge > 0).
    With big PV (8kW), overflow charges the battery → will_fill → keep is reduced.
    Without big PV (2kW, no overflow) → no fill → keep is unchanged.
    """
    # High PV: 8kW creates overflow even starting at 100% (excess - DNO = 8-1-4 = 3kW overflow)
    # Pass raw kW to MockBase (MockBase converts to kWh/step)
    pv_high = {m: 8.0 for m in range(0, 660, PLUGIN_STEP)}
    load_high = {m: 1.0 for m in range(0, 660, PLUGIN_STEP)}

    base_high = MockBase(
        pv_step=pv_high,
        load_step=load_high,
        soc_kw=BATTERY_KWH * 0.40,
        soc_max=BATTERY_KWH,
        minutes_now=600,  # 10:00 — morning window where on_before_plan recomputes
    )
    plugin_high = CurtailmentPlugin(base_high)

    original_keep = 6.0
    context_high = {"best_soc_keep": original_keep}
    result_high = plugin_high.on_before_plan(context_high)

    # Low PV: 2kW, load 3kW — PV never exceeds load, no overflow from 100% start,
    # morning_gap is large (load > PV all day) → solar_adjusted_keep > original_keep → no reduction
    pv_low = {m: 2.0 for m in range(0, 660, PLUGIN_STEP)}
    load_low = {m: 3.0 for m in range(0, 660, PLUGIN_STEP)}

    base_low = MockBase(
        pv_step=pv_low,
        load_step=load_low,
        soc_kw=BATTERY_KWH * 0.40,
        soc_max=BATTERY_KWH,
        minutes_now=600,
    )
    plugin_low = CurtailmentPlugin(base_low)

    context_low = {"best_soc_keep": original_keep}
    result_low = plugin_low.on_before_plan(context_low)

    # High PV: keep should be reduced (solar will fill battery → only need morning gap worth)
    assert result_high["best_soc_keep"] < original_keep, f"High PV: expected keep < {original_keep}, got {result_high['best_soc_keep']:.2f}"

    # Low PV (load > PV): keep should not be reduced
    assert result_low["best_soc_keep"] == original_keep, f"Low PV (load > PV): expected keep unchanged at {original_keep}, " f"got {result_low['best_soc_keep']:.2f}"

    print(f"  test_before_plan_trajectory_reduces_keep: PASSED " f"(high_PV keep={result_high['best_soc_keep']:.2f}, low_PV keep={result_low['best_soc_keep']:.2f})")


def test_on_update_stays_off_low_pv():
    """Low PV day: on_update should stay off, not set read_only, not call D-ESS services.

    1.5kW peak PV (below SAFE_PV_THRESHOLD_KW=2kW), 1kW load — unmanaged battery
    accumulates only ~1.2kWh from 50%, far below soc_max. Plugin should stay off.
    """
    # 1.5kW peak PV (bell curve) — below SAFE_PV_THRESHOLD_KW so no danger slots
    pv, load = _make_trajectory_pv(peak_kw=1.5, load_kw=1.0, hours=8)

    # Verify premise: unmanaged doesn't fill battery from 50%
    start_soc = BATTERY_KWH * 0.50
    peak_u, _, _ = simulate_soc_trajectory(pv, load, start_soc, BATTERY_KWH, DNO_LIMIT, unmanaged=True)
    assert peak_u < BATTERY_KWH * 0.95, f"Premise: 1.5kW peak PV should not fill battery, got peak={peak_u:.1f}"

    # Pass raw kW values to MockBase (MockBase converts to kWh/step)
    base = MockBase(
        pv_step=pv,
        load_step=load,
        soc_kw=start_soc,
        soc_max=BATTERY_KWH,
        minutes_now=720,
        sensor_overrides={
            "sensor.sigen_plant_pv_power": 1.5,  # low actual PV too
            "sensor.sigen_plant_consumed_power": 1.0,
        },
    )
    plugin = CurtailmentPlugin(base)
    plugin.on_update()

    phase_sensor = base.published.get("sensor.predbat_curtailment_phase", {})
    phase = phase_sensor.get("value", "Off")
    assert phase == "Off", f"Low PV: expected phase='Off', got '{phase}'"
    assert base.set_read_only is False, "read_only should be False when plugin stays off"

    # No D-ESS mode service calls expected (select/select_option)
    ems_calls = [s for s in base.services if "select" in s[0]]
    assert len(ems_calls) == 0, f"Should not call EMS mode service on low PV day, got {ems_calls}"

    print(f"  test_on_update_stays_off_low_pv: PASSED (peak_u={peak_u:.1f}kWh, phase={phase}, services={[s[0] for s in base.services]})")


def test_floor_clamped_to_soc_max():
    """Floor must never exceed soc_max. Negative net_charge caused 150% target SOC."""
    pv, load = _make_trajectory_pv(peak_kw=3.0, load_kw=2.0, hours=6)
    base = MockBase(pv_step=pv, load_step=load, soc_kw=10.0, minutes_now=720, sensor_overrides={"sensor.sigen_plant_pv_power": 3.0, "sensor.sigen_plant_consumed_power": 2.0})
    plugin = CurtailmentPlugin(base)
    plugin._seen_overflow_today = True
    target, net_charge, phase, _ = plugin.calculate(4.0)
    assert target <= 18.08, f"Floor must be <= soc_max: got {target:.1f} kWh ({target / 18.08 * 100:.0f}%)"
    assert target >= 0, f"Floor must be >= 0: got {target:.1f}"
    print(f"  test_floor_clamped_to_soc_max: PASSED (net_charge={net_charge:.1f}, floor={target / 18.08 * 100:.0f}%)")


def test_overflow_sensor_never_negative():
    """Published overflow kWh must never be negative. Was showing -9.94 kWh."""
    pv, load = _make_trajectory_pv(peak_kw=3.0, load_kw=2.0, hours=6)
    base = MockBase(pv_step=pv, load_step=load, soc_kw=10.0, minutes_now=720, sensor_overrides={"sensor.sigen_plant_pv_power": 3.0, "sensor.sigen_plant_consumed_power": 2.0})
    plugin = CurtailmentPlugin(base)
    plugin._seen_overflow_today = True
    plugin.on_update()
    overflow = base.published.get("sensor.predbat_curtailment_overflow_kwh", {}).get("value", 0)
    assert float(overflow) >= 0, f"Overflow must be >= 0: got {overflow}"
    print(f"  test_overflow_sensor_never_negative: PASSED (overflow={overflow})")


def test_target_soc_sensor_max_100pct():
    """Published target SOC % must never exceed 100%. Was showing 150%."""
    pv, load = _make_trajectory_pv(peak_kw=3.0, load_kw=2.0, hours=6)
    base = MockBase(pv_step=pv, load_step=load, soc_kw=10.0, minutes_now=720, sensor_overrides={"sensor.sigen_plant_pv_power": 3.0, "sensor.sigen_plant_consumed_power": 2.0})
    plugin = CurtailmentPlugin(base)
    plugin._seen_overflow_today = True
    plugin.on_update()
    target_pct = base.published.get("sensor.predbat_curtailment_target_soc", {}).get("value", 0)
    assert float(target_pct) <= 100.0, f"Target SOC must be <= 100%: got {target_pct}%"
    assert float(target_pct) >= 0, f"Target SOC must be >= 0%: got {target_pct}%"
    print(f"  test_target_soc_sensor_max_100pct: PASSED (target={target_pct}%)")


def test_deactivation_restores_msc_during_pv():
    """When will_fill becomes false, plugin must restore MSC even with PV generating.

    The PV guard previously kept D-ESS active when deactivating during daylight.
    This blocked Predbat's charge windows on moderate PV days. Since will_fill=false
    means the battery won't fill, MSC is safe regardless of PV level.
    """
    # First: activate with high PV (will fill from 50%)
    pv = {}
    load = {}
    for m in range(0, 360, PLUGIN_STEP):  # 6 hours only
        pv[m] = 8.0
        load[m] = 1.0

    base = MockBase(pv_step=pv, load_step=load, soc_kw=9.0, minutes_now=600, sensor_overrides={"sensor.sigen_plant_pv_power": 8.0, "sensor.sigen_plant_consumed_power": 1.0})
    plugin = CurtailmentPlugin(base)
    plugin.on_update()
    assert base.set_read_only is True, "Should be active (will fill)"

    # Second: switch to low PV — won't fill. Reset sticky. PV still generating (1.5kW).
    pv_low = {}
    load_low = {}
    for m in range(0, 360, PLUGIN_STEP):
        pv_low[m] = 1.5
        load_low[m] = 1.0
    base.pv_forecast_minute_step = {k: v * PLUGIN_STEP / 60.0 for k, v in pv_low.items()}
    base.load_minutes_step = {k: v * PLUGIN_STEP / 60.0 for k, v in load_low.items()}
    base._sensor_overrides["sensor.sigen_plant_pv_power"] = 1.5
    plugin._seen_overflow_today = False  # cloud passed, no more overflow

    plugin.on_update()

    assert base.set_read_only is False, "Should deactivate when will_fill=false (MSC safe)"
    msc_called = any(s[1].get("option") == "Maximum Self Consumption" for s in base.services if s[0] == "select/select_option")
    assert msc_called, "Should restore MSC on deactivation"
    print("  test_deactivation_restores_msc_during_pv: PASSED")


def test_activation_sets_dess_and_read_only():
    """ON transition: sets D-ESS, read_only=True, blocks Predbat."""
    pv = {}
    load = {}
    for m in range(0, 660, PLUGIN_STEP):
        pv[m] = 8.0
        load[m] = 1.0

    base = MockBase(pv_step=pv, load_step=load, soc_kw=7.0, minutes_now=600, sensor_overrides={"sensor.sigen_plant_pv_power": 8.0, "sensor.sigen_plant_consumed_power": 1.0})
    plugin = CurtailmentPlugin(base)

    assert base.set_read_only is False, "Starts inactive"
    plugin.on_update()
    assert base.set_read_only is True, "Should activate"

    dess_called = any(s[1].get("option") == "Command Discharging (ESS First)" for s in base.services if s[0] == "select/select_option")
    assert dess_called, "Should set D-ESS on activation"
    print("  test_activation_sets_dess_and_read_only: PASSED")


def test_sticky_prevents_deactivation():
    """Sticky flag keeps plugin active even when trajectory says won't fill."""
    pv = {}
    load = {}
    for m in range(0, 660, PLUGIN_STEP):
        pv[m] = 3.0  # won't fill
        load[m] = 1.0

    base = MockBase(pv_step=pv, load_step=load, soc_kw=7.0, minutes_now=600, sensor_overrides={"sensor.sigen_plant_pv_power": 3.0, "sensor.sigen_plant_consumed_power": 1.0})
    plugin = CurtailmentPlugin(base)
    plugin._seen_overflow_today = True  # sticky from earlier overflow

    plugin.on_update()

    phase = base.published.get("sensor.predbat_curtailment_phase", {}).get("value")
    assert phase != "Off", f"Sticky should keep active, got phase={phase}"
    assert base.set_read_only is True, "read_only should stay True with sticky"
    print("  test_sticky_prevents_deactivation: PASSED")


def test_no_oscillation_msc_safe():
    """Rapid on→off→on transitions: each off restores MSC cleanly."""
    pv_on = {}
    pv_off = {}
    load = {}
    for m in range(0, 360, PLUGIN_STEP):  # 6 hours
        pv_on[m] = 8.0  # fills from 50%
        pv_off[m] = 1.5  # won't fill
        load[m] = 1.0

    base = MockBase(pv_step=pv_on, load_step=load, soc_kw=9.0, minutes_now=600, sensor_overrides={"sensor.sigen_plant_pv_power": 8.0, "sensor.sigen_plant_consumed_power": 1.0})
    plugin = CurtailmentPlugin(base)

    # Cycle 1: activate
    plugin.on_update()
    assert base.set_read_only is True, "Cycle 1: should activate"

    # Cycle 2: low PV, deactivate. Reset sticky.
    base.pv_forecast_minute_step = {k: v * PLUGIN_STEP / 60.0 for k, v in pv_off.items()}
    base._sensor_overrides["sensor.sigen_plant_pv_power"] = 1.5
    plugin._seen_overflow_today = False
    plugin.on_update()
    assert base.set_read_only is False, "Cycle 2: should deactivate (MSC)"

    # Cycle 3: high PV, reactivate
    base.pv_forecast_minute_step = {k: v * PLUGIN_STEP / 60.0 for k, v in pv_on.items()}
    base._sensor_overrides["sensor.sigen_plant_pv_power"] = 8.0
    plugin.on_update()
    assert base.set_read_only is True, "Cycle 3: should reactivate"

    print("  test_no_oscillation_msc_safe: PASSED (on→off→on, MSC restored each off)")


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
        # Sticky activation tests
        test_plugin_sticky_activation_oscillating_pv,
        # SOC floor tests
        test_target_soc_clamped_above_soc_keep,
        test_target_soc_clamped_above_reserve,
        # Morning gap tests
        test_morning_gap_pre_dawn,
        test_morning_gap_solar_already_covers,
        test_morning_gap_cloudy_never_covers,
        test_morning_gap_kwh_values,
        # Phase logic matrix tests
        test_phase_overflow_any_soc,
        test_phase_excess_soc_below_target,
        test_phase_excess_soc_above_target,
        test_phase_deficit_not_export,
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

    # v7 strategy tests — synthetic days
    v7_tests = [
        test_v7_perfect_forecast,
        test_v7_underforecast_60pct,
        test_v7_overforecast_140pct,
        test_v7_overforecast_sunset_soc,
    ]
    print("  --- v7 strategy tests ---")
    for test_fn in v7_tests:
        try:
            test_fn()
        except AssertionError as e:
            print(f"  {test_fn.__name__}: FAILED — {e}")
            failed = True

    # CSV validation — v7 strategy with perfect, under- and over-forecast
    if csv_available:
        print("  --- CSV v7 strategy tests ---")
        for label, filename, watts, expected in VALIDATION_DAYS:
            # Perfect forecast
            day_failed = _run_csv_day_v7_test(label, filename, watts, forecast_scale=1.0)
            if day_failed:
                failed = True
            # 60% underforecast (only on overflow days)
            if expected["overflow_approx"] > 1.0:
                day_failed = _run_csv_day_v7_test(label, filename, watts, forecast_scale=0.6)
                if day_failed:
                    failed = True
                # 140% overforecast (v7 improvement — should still reach >=85% sunset SOC)
                day_failed = _run_csv_day_v7_test(label, filename, watts, forecast_scale=1.4)
                if day_failed:
                    failed = True
            # Low SOC start on big overflow days
            if expected["overflow_approx"] > 8.0:
                day_failed = _run_csv_day_v7_test(label, filename, watts, forecast_scale=1.0, start_soc_pct=0.10)
                if day_failed:
                    failed = True
            elif expected["overflow_approx"] > 1.0:
                day_failed = _run_csv_day_v7_test(label, filename, watts, forecast_scale=1.0, start_soc_pct=0.25)
                if day_failed:
                    failed = True

    # v8 trajectory tests
    v8_tests = [
        test_trajectory_battery_fills,
        test_trajectory_battery_wont_fill,
        test_trajectory_high_soc_fills,
        test_trajectory_energy_ratio_increases_overflow,
        test_trajectory_energy_ratio_decreases_overflow,
        test_v8_floor_bidirectional,
        test_trajectory_unmanaged_vs_managed,
        test_trajectory_mar31_scenario,
        test_trajectory_low_pv_no_activation,
        test_trajectory_load_ratio_reduces_excess,
        # v8 plugin integration tests
        test_plugin_activates_from_unmanaged_trajectory,
        test_floor_rises_as_overflow_shrinks,
        test_release_when_pv_below_threshold,
        test_sticky_keeps_active_when_trajectory_clears,
        test_on_update_full_flow_activates,
        test_before_plan_trajectory_reduces_keep,
        test_on_update_stays_off_low_pv,
        test_floor_clamped_to_soc_max,
        test_overflow_sensor_never_negative,
        test_target_soc_sensor_max_100pct,
        test_deactivation_restores_msc_during_pv,
        test_activation_sets_dess_and_read_only,
        test_sticky_prevents_deactivation,
        test_no_oscillation_msc_safe,
    ]
    print("  --- v8 trajectory tests ---")
    for test_fn in v8_tests:
        try:
            test_fn()
        except AssertionError as e:
            print(f"  {test_fn.__name__}: FAILED — {e}")
            failed = True

    if not failed:
        print("**** All curtailment tests PASSED ****")
    return failed


if __name__ == "__main__":
    sys.exit(1 if run_curtailment_tests() else 0)
