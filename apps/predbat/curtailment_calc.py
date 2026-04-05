# -----------------------------------------------------------------------------
# Predbat Home Battery System - Curtailment Calculator
# Pure algorithm functions for curtailment management
# No HA or Predbat dependencies — testable in isolation
# -----------------------------------------------------------------------------

import math

# Validated from Mar 28 real data (120 five-minute slots):
# Mean PV < 2.0kW: zero spikes above 4.5kW (safe to release)
# Mean PV 2-4kW: spikes to 6-10kW common (overflow risk)
SAFE_PV_THRESHOLD_KW = 2.0

MIN_BASE_LOAD_KW = 0.5


def compute_remaining_overflow(pv_forecast, load_forecast, dno_limit, start_minute=0, end_minute=1440, step_minutes=5, values_are_kwh=False):
    """
    Compute total remaining overflow (kWh) from start_minute to end_minute.

    Overflow = energy that would be curtailed if battery can't absorb it.
    For each step: overflow = max(0, excess_kw - dno_limit) * step_hours

    Args:
        pv_forecast: dict {minute: value} — PV forecast
        load_forecast: dict {minute: value} — load forecast
        dno_limit: float kW — maximum grid export allowed
        start_minute: int — first minute to consider (inclusive)
        end_minute: int — last minute (exclusive)
        step_minutes: int — forecast step size
        values_are_kwh: bool — if True, forecast values are kWh per step
                        (Predbat format); if False, values are kW (CSV/test format)

    Returns:
        float — total remaining overflow in kWh
    """
    step_hours = step_minutes / 60.0
    # Conversion factor: kWh-per-step to kW
    to_kw = (1.0 / step_hours) if values_are_kwh else 1.0

    total = 0.0
    for m in range(start_minute, end_minute, step_minutes):
        pv_kw = pv_forecast.get(m, 0.0) * to_kw
        load_kw = load_forecast.get(m, 0.0) * to_kw
        excess_kw = pv_kw - load_kw
        overflow_kwh = max(0.0, excess_kw - dno_limit) * step_hours
        total += overflow_kwh
    return total


def compute_morning_gap(pv_forecast, load_forecast, start_minute=0, end_minute=1440, step_minutes=5, values_are_kwh=False):
    """
    Compute energy deficit from now until PV consistently covers load.

    Walks forward through forecast slots, accumulating max(0, load - pv) per slot.
    Stops when PV exceeds load for 6 consecutive slots (30 min sustained solar),
    meaning solar has reliably taken over from battery.

    This is the energy the battery needs to bridge the morning gap before solar
    can sustain the house. Used to set best_soc_keep on sunny days.

    Args:
        pv_forecast: dict {minute: value} — PV forecast
        load_forecast: dict {minute: value} — load forecast
        start_minute: int — first minute to consider (inclusive)
        end_minute: int — last minute (exclusive)
        step_minutes: int — forecast step size
        values_are_kwh: bool — if True, values are kWh per step (Predbat format)

    Returns:
        float — morning energy gap in kWh
    """
    step_hours = step_minutes / 60.0
    to_kw = (1.0 / step_hours) if values_are_kwh else 1.0
    SUSTAINED_SLOTS = 6  # 30 min of PV > load = solar has taken over

    gap_kwh = 0.0
    consecutive_surplus = 0

    for m in range(start_minute, end_minute, step_minutes):
        pv_kw = pv_forecast.get(m, 0.0) * to_kw
        load_kw = load_forecast.get(m, 0.0) * to_kw

        if pv_kw >= load_kw:
            consecutive_surplus += 1
            if consecutive_surplus >= SUSTAINED_SLOTS:
                break
        else:
            consecutive_surplus = 0
            gap_kwh += (load_kw - pv_kw) * step_hours

    return gap_kwh


def simulate_soc_trajectory(pv_forecast, load_forecast, current_soc, soc_max, dno_limit, energy_ratio=1.0, load_ratio=1.0, start_minute=0, end_minute=1440, step_minutes=5, values_are_kwh=False, unmanaged=False):
    """
    Simulate battery SOC trajectory with curtailment active (export at DNO).

    Runs from start_minute until PV is exhausted (evening load irrelevant).
    PV is scaled by energy_ratio, load is scaled by load_ratio.

    Two modes:
      unmanaged=False (default): curtailment active, export at DNO
        - excess > DNO: export DNO, battery absorbs (excess - DNO)
        - 0 < excess <= DNO: export excess, battery unchanged
      unmanaged=True: MSC mode, battery absorbs ALL excess
        - excess > 0: battery absorbs all excess
        - Used for activation check: "will battery fill without intervention?"

    Args:
        pv_forecast: dict {minute: value}
        load_forecast: dict {minute: value}
        current_soc: float kWh — starting SOC
        soc_max: float kWh — battery capacity
        dno_limit: float kW — max grid export
        energy_ratio: float — PV scaling (1.0 = forecast, >1 = PV ahead)
        load_ratio: float — load scaling (1.0 = forecast, <1 = load lower than predicted)
        unmanaged: bool — if True, simulate MSC mode (battery absorbs all excess)
        start_minute: int — first minute (default 0)
        end_minute: int — last minute (default 1440)
        step_minutes: int — step size
        values_are_kwh: bool — if True, forecast values are kWh per step

    Returns:
        (peak_soc, net_battery_charge, last_danger_slot)
        - peak_soc: float kWh — highest SOC reached
        - net_battery_charge: float kWh — total energy battery absorbs minus deficits
        - last_danger_slot: int — last minute with PV > SAFE_PV_THRESHOLD_KW (0 if none)
    """
    step_hours = step_minutes / 60.0
    to_kw = (1.0 / step_hours) if values_are_kwh else 1.0

    soc = current_soc
    peak_soc = current_soc
    net_charge = 0.0
    last_danger = 0
    seen_pv = False

    for m in range(start_minute, end_minute, step_minutes):
        pv_kw = pv_forecast.get(m, 0.0) * to_kw * energy_ratio
        load_kw = load_forecast.get(m, 0.0) * to_kw * load_ratio

        if pv_kw > 0.1:
            seen_pv = True
            last_pv_minute = m
        elif seen_pv and m > last_pv_minute + 60:
            break  # PV done for the day, evening load irrelevant

        if pv_kw > SAFE_PV_THRESHOLD_KW:
            last_danger = m

        excess = pv_kw - load_kw

        if unmanaged:
            # MSC mode: battery absorbs ALL excess (for activation check)
            if excess > 0:
                charge = excess * step_hours
                soc += charge
                net_charge += charge
            elif excess < 0:
                soc += excess * step_hours
                net_charge += excess * step_hours
        else:
            # D-ESS mode: export at DNO, battery absorbs overflow only
            if excess > dno_limit:
                charge = (excess - dno_limit) * step_hours
                soc += charge
                net_charge += charge
            elif excess < 0:
                soc += excess * step_hours
                net_charge += excess * step_hours

        # Clamp SOC
        soc = max(0.0, min(soc_max, soc))
        if soc > peak_soc:
            peak_soc = soc

    return peak_soc, net_charge, last_danger


def compute_tomorrow_forecast(pv_forecast, load_forecast, soc_max, dno_limit, start_minute, end_minute, step_minutes=5, values_are_kwh=True):
    """
    Compute curtailment forecast for a future solar day (v10 logic).

    Uses overflow-vs-headroom activation (same as live calculate()) applied
    to a future window (typically tomorrow).

    Args:
        pv_forecast: dict {minute_from_now: value}
        load_forecast: dict {minute_from_now: value}
        soc_max: float kWh — battery capacity
        dno_limit: float kW — max grid export
        start_minute: int — start of tomorrow's solar window (minutes from now)
        end_minute: int — end of tomorrow's solar window (minutes from now)
        step_minutes: int — forecast step size
        values_are_kwh: bool — Predbat format (kWh per step)

    Returns:
        dict with: total_overflow_kwh, floor_pct, will_activate, morning_gap_kwh
    """
    total_overflow = compute_remaining_overflow(
        pv_forecast,
        load_forecast,
        dno_limit,
        start_minute=start_minute,
        end_minute=end_minute,
        step_minutes=step_minutes,
        values_are_kwh=values_are_kwh,
    )

    morning_gap = compute_morning_gap(
        pv_forecast,
        load_forecast,
        start_minute=start_minute,
        end_minute=end_minute,
        step_minutes=step_minutes,
        values_are_kwh=values_are_kwh,
    )

    # Estimated SOC when overflow starts: battery at keep level after bridging morning gap
    margin = 0.5
    estimated_start_soc = morning_gap + margin

    # Activation: overflow exceeds total headroom from estimated start
    will_activate = total_overflow > (soc_max - estimated_start_soc)

    floor = max(0.0, soc_max - total_overflow)
    floor_pct = floor / soc_max * 100 if soc_max > 0 else 100

    return {
        "total_overflow_kwh": round(total_overflow, 2),
        "floor_pct": round(floor_pct, 1),
        "will_activate": will_activate,
        "morning_gap_kwh": round(morning_gap, 2),
    }


def solar_elevation(lat_deg, lon_deg, utc_hours, day_of_year):
    """
    Solar elevation angle in degrees.

    Simplified solar position algorithm — accurate to ~1 degree.
    Uses Spencer (1971) declination and equation of time.

    Args:
        lat_deg: latitude in degrees (positive north)
        lon_deg: longitude in degrees (positive east)
        utc_hours: decimal UTC hours (e.g. 14.5 = 14:30 UTC)
        day_of_year: 1-366

    Returns:
        float — elevation angle in degrees (negative = below horizon)
    """
    lat = math.radians(lat_deg)
    B = math.radians((360.0 / 365.0) * (day_of_year - 81))
    decl = math.radians(23.45) * math.sin(B)
    B2 = math.radians((360.0 / 364.0) * (day_of_year - 81))
    eot = 9.87 * math.sin(2 * B2) - 7.53 * math.cos(B2) - 1.5 * math.sin(B2)
    solar_noon_utc = 12.0 - lon_deg / 15.0 - eot / 60.0
    hour_angle = math.radians(15.0 * (utc_hours - solar_noon_utc))
    sin_elev = math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(hour_angle)
    return math.degrees(math.asin(max(-1.0, min(1.0, sin_elev))))


def compute_release_time(scale, lat_deg, lon_deg, day_of_year, threshold_kw, current_utc_hours):
    """
    Compute minutes from now until PV drops below threshold (safe time).

    Models clear-sky PV as scale * sin(elevation). Finds when this drops
    below threshold_kw on the declining side of the solar curve.

    Args:
        scale: float kW — clear-sky scale (peak_pv / sin(elevation_at_peak))
        lat_deg, lon_deg: location
        day_of_year: 1-366
        threshold_kw: PV level below which it's safe (DNO + min_base_load)
        current_utc_hours: decimal UTC hours now

    Returns:
        (minutes_until_crossing, crossing_utc_hours) or (None, None) if
        cannot compute (scale too low, sun below horizon, etc.)
    """
    # Find solar noon
    B2 = math.radians((360.0 / 364.0) * (day_of_year - 81))
    eot = 9.87 * math.sin(2 * B2) - 7.53 * math.cos(B2) - 1.5 * math.sin(B2)
    solar_noon_utc = 12.0 - lon_deg / 15.0 - eot / 60.0

    # Check if peak PV (at solar noon) is below threshold
    noon_elev = solar_elevation(lat_deg, lon_deg, solar_noon_utc, day_of_year)
    peak_pv = scale * max(0.0, math.sin(math.radians(noon_elev)))
    if peak_pv < threshold_kw:
        return 0, current_utc_hours  # peak can't reach threshold — safe now

    # Start scanning from the later of (now, solar noon)
    scan_start = max(current_utc_hours, solar_noon_utc)

    # Scan forward in 1-minute steps
    crossing_utc = None
    for minute_offset in range(0, 720):  # up to 12 hours
        t = scan_start + minute_offset / 60.0
        elev = solar_elevation(lat_deg, lon_deg, t, day_of_year)
        predicted = scale * max(0.0, math.sin(math.radians(elev)))
        if predicted < threshold_kw:
            crossing_utc = t
            break

    if crossing_utc is None:
        return None, None

    minutes_until = (crossing_utc - current_utc_hours) * 60.0
    return minutes_until, crossing_utc
