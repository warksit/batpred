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


def compute_release_offset(pv_forecast, load_forecast, dno_limit=4.0, start_minute=0, end_minute=1440, step_minutes=5, values_are_kwh=False):
    """Find the release point: one slot after the last slot where PV-load > DNO.

    Scans the forecast for the last slot where overflow (PV-load > DNO) occurs.
    Returns the minute of the slot immediately following that, relative to start_minute.

    Using the DNO threshold directly (not a SOC-adjusted proxy) means load spikes
    can never create a false early release — a spike reduces PV-load, making it
    less likely to qualify as overflow, not more.

    Args:
        pv_forecast: dict {minute: value}
        load_forecast: dict {minute: value}
        dno_limit: float kW — grid export limit (overflow = PV-load > dno_limit)
        start_minute, end_minute: search window
        step_minutes: forecast step size
        values_are_kwh: if True, values are kWh per step (predbat format)

    Returns:
        release_offset: minutes from start_minute to the slot after last overflow.
        None if no overflow found (PV-load never exceeds DNO — release now).
    """
    step_hours = step_minutes / 60.0
    to_kw = (1.0 / step_hours) if values_are_kwh else 1.0

    last_overflow_minute = None
    for m in range(start_minute, end_minute, step_minutes):
        pv_kw = pv_forecast.get(m, 0) * to_kw
        load_kw = load_forecast.get(m, 0) * to_kw
        if pv_kw - load_kw > dno_limit:
            last_overflow_minute = m

    if last_overflow_minute is None:
        return None  # No overflow — no release delay needed

    return last_overflow_minute + step_minutes - start_minute


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


CALIBRATION_RATIO_MAX = 1.5


def compute_solcast_overflow(
    detailed_forecast,
    from_utc_hours,
    to_utc_hours,
    dno_limit,
    load_forecast_kw=None,
    local_offset_hours=0.0,
    base_load=MIN_BASE_LOAD_KW,
    step_minutes=5,
    band="pv_estimate",
    calibration_ratio=1.0,
    calibration_window_hours=0.5,
):
    """
    Integrate overflow energy from Solcast per-slot forecast (R53, v20).

    Replaces compute_solar_overflow's clear-sky `scale × sin(elev)` model.
    Preserves Solcast's day-shape (cloud, rain, ramp) by reading per-slot
    pv_estimate directly instead of fitting a single scalar to peak.

    For each integration step, looks up the Solcast 30-min slot containing
    that UTC time, takes the band's kW value, then:
        overflow_step = max(0, pv_kw - effective_load_kw - dno_limit) × step_h

    R58 live calibration: pv_kw is multiplied by min(CALIBRATION_RATIO_MAX,
    calibration_ratio) for the first calibration_window_hours of integration
    only. Lets the plugin apply a "we're tracking 1.2× ahead of Solcast right
    now" multiplier to the next 30 min only — beyond that, raw shape is
    preserved (no global override of remaining-day Solcast). Replaces R43's
    `max(p_scale, actual_scale)` global collapse.

    Args:
        detailed_forecast: list of dicts with 'period_start' (ISO local-time),
                           and the band field (default 'pv_estimate' kW averaged
                           over 30 min). Bands: 'pv_estimate10' / 'pv_estimate' /
                           'pv_estimate90' for R50 confidence percentiles.
        from_utc_hours: integration start (decimal UTC hours)
        to_utc_hours: integration end (decimal UTC hours)
        dno_limit: float kW
        load_forecast_kw: optional list of kW per step. Pre-smooth via R9a.
        local_offset_hours: local time offset from UTC (e.g. 1.0 for BST)
        base_load: floor for effective load (R9a)
        step_minutes: integration step (default 5)
        band: which Solcast field to read
        calibration_ratio: live actual_pv / forecast_pv ratio over the recent
                           past (R58). 1.0 = no calibration. Capped at 1.5.
        calibration_window_hours: how far forward the calibration applies.
                                  Default 0.5 (= one Solcast slot).

    Returns:
        float kWh — total overflow over the window
    """
    if not detailed_forecast or to_utc_hours <= from_utc_hours:
        return 0.0

    # Parse slots into (utc_start, utc_end, pv_kw) triples
    slots = []
    for s in detailed_forecast:
        try:
            ps = s["period_start"]
            time_part = ps[11:16]
            h, m = int(time_part[:2]), int(time_part[3:5])
            local_h = h + m / 60.0
            utc_h = local_h - local_offset_hours
            pv = float(s.get(band, 0.0) or 0.0)
            slots.append((utc_h, utc_h + 0.5, pv))
        except (ValueError, IndexError, KeyError, TypeError):
            continue
    if not slots:
        return 0.0
    slots.sort(key=lambda x: x[0])

    step_hours = step_minutes / 60.0
    capped_calibration = min(CALIBRATION_RATIO_MAX, max(0.0, calibration_ratio))
    total = 0.0
    t = from_utc_hours
    i = 0
    slot_idx = 0
    while t < to_utc_hours:
        # Advance slot pointer to the slot containing t
        while slot_idx < len(slots) and slots[slot_idx][1] <= t:
            slot_idx += 1
        if slot_idx >= len(slots):
            break
        s_start, s_end, s_pv = slots[slot_idx]
        if t < s_start:
            # Gap before next slot — assume 0 PV (no overflow)
            pv_kw = 0.0
        else:
            pv_kw = s_pv
        # R58 live calibration: scale only within the calibration window
        if (t - from_utc_hours) < calibration_window_hours and capped_calibration != 1.0:
            pv_kw = pv_kw * capped_calibration
        forecast_load = load_forecast_kw[i] if load_forecast_kw and i < len(load_forecast_kw) else 0.0
        load_kw = max(base_load, forecast_load)
        total += max(0.0, pv_kw - load_kw - dno_limit) * step_hours
        t += step_hours
        i += 1
    return total


def smooth_load_forecast(load_kw_list, window_minutes=60, step_minutes=5):
    """
    Centered rolling-mean smoothing of a load forecast list (R9a).

    LoadML produces per-slot forecasts that can spike on phantom predictions
    (the v5 failure mode that broke per-slot integration). Smoothing with a
    60-min window distributes single-slot spikes across neighbours so the
    overflow integral isn't dominated by noise. v20 (R53) reintroduces
    per-slot Solcast integration, which only works safely with smoothed load.

    Args:
        load_kw_list: list of kW values per step
        window_minutes: smoothing window size in minutes (default 60)
        step_minutes: step size of the list (default 5)

    Returns:
        list of smoothed kW values, same length as input
    """
    if not load_kw_list:
        return []
    half_window = max(1, window_minutes // (2 * step_minutes))
    n = len(load_kw_list)
    smoothed = []
    for i in range(n):
        lo = max(0, i - half_window)
        hi = min(n, i + half_window + 1)
        window = load_kw_list[lo:hi]
        smoothed.append(sum(window) / len(window))
    return smoothed


def compute_solar_overflow(scale, lat_deg, lon_deg, day_of_year, from_utc_hours, to_utc_hours, dno_limit, base_load=MIN_BASE_LOAD_KW, step_minutes=5, load_forecast_kw=None):
    """
    Integrate overflow energy from the solar geometry curve between two UTC times.

    overflow(t) = max(0, scale × sin(elev(t)) - effective_load(t) - dno_limit)
    Returns ∫ overflow(t) dt in kWh.

    effective_load(t) = max(base_load, load_forecast_kw[i]) at step i, or base_load if no forecast.
    Using LoadML's forecast lets the integral account for real daytime load (DHW, EV, etc.)
    which absorbs PV and reduces the overflow needing export headroom.

    Args:
        scale: float kW — clear-sky scale (peak_pv / sin(elevation_at_peak))
        lat_deg, lon_deg: location
        day_of_year: 1-366
        from_utc_hours: integration start (decimal UTC hours, i.e. now)
        to_utc_hours: integration end (decimal UTC hours, i.e. safe_time)
        dno_limit: float kW — max grid export
        base_load: float kW — minimum household load offsetting PV (floor)
        step_minutes: integration step size in minutes
        load_forecast_kw: optional list of kW per step starting at from_utc_hours.
            Each entry is the forecast load for that step. If None or shorter than
            the integration, remaining steps use base_load.

    Returns:
        float kWh — total overflow energy over the window
    """
    if scale <= 0 or to_utc_hours <= from_utc_hours:
        return 0.0
    step_hours = step_minutes / 60.0
    total = 0.0
    t = from_utc_hours
    i = 0
    while t < to_utc_hours:
        elev = solar_elevation(lat_deg, lon_deg, t, day_of_year)
        pv_kw = scale * max(0.0, math.sin(math.radians(elev)))
        forecast_load = load_forecast_kw[i] if load_forecast_kw and i < len(load_forecast_kw) else 0.0
        load_kw = max(base_load, forecast_load)
        total += max(0.0, pv_kw - load_kw - dno_limit) * step_hours
        t += step_hours
        i += 1
    return total


def p90_scale_from_forecast(detailed_forecast, lat_deg, lon_deg, day_of_year, local_offset_hours=0):
    """
    Derive clear-sky scale from Solcast p90 (near-worst-case) forecast.

    Finds the 30-min slot with highest pv_estimate90, converts to UTC time,
    computes solar elevation at that time, returns scale = p90_peak / sin(elev).

    Args:
        detailed_forecast: list of dicts with 'period_start' (ISO string BST/local)
                           and 'pv_estimate90' (kW average over 30-min period)
        lat_deg, lon_deg: location
        day_of_year: 1-366
        local_offset_hours: local time offset from UTC (e.g. 1.0 for BST)

    Returns:
        (scale, p90_peak_kw, p90_peak_utc_hours) or (0, 0, 0) if unavailable
    """
    if not detailed_forecast:
        return 0.0, 0.0, 0.0

    best_kw = 0.0
    best_utc = 0.0
    for slot in detailed_forecast:
        kw = slot.get("pv_estimate90", 0.0)
        if kw <= best_kw:
            continue
        # Parse period_start hour — assume local time, convert to UTC
        try:
            ps = slot["period_start"]
            # Format: "2026-04-19T13:00:00+01:00" or similar
            time_part = ps[11:16]  # "HH:MM"
            h, m = int(time_part[:2]), int(time_part[3:5])
            local_h = h + m / 60.0 + 0.25  # mid-point of 30-min slot
            utc_h = local_h - local_offset_hours
        except (ValueError, IndexError, KeyError):
            continue
        best_kw = kw
        best_utc = utc_h

    if best_kw < 0.5:
        return 0.0, 0.0, 0.0

    elev = solar_elevation(lat_deg, lon_deg, best_utc, day_of_year)
    sin_elev = math.sin(math.radians(elev))
    if sin_elev < 0.05:
        return 0.0, 0.0, 0.0

    scale = best_kw / sin_elev
    return scale, best_kw, best_utc


def p_scales_from_forecast(detailed_forecast, lat_deg, lon_deg, day_of_year, local_offset_hours=0):
    """R50: derive (p10, p50, p90) clear-sky scales from each forecast band's peak.

    Solcast publishes pv_estimate10 / pv_estimate (P50) / pv_estimate90 per slot.
    Each band's peak gives a different worst-/best-case scale. The plugin uses
    these three scales to compute three overflow integrals which are then
    blended by confidence (compute_expected_overflow).

    Args:
        detailed_forecast: list of dicts with 'period_start',
                           'pv_estimate10', 'pv_estimate', 'pv_estimate90' (kW)
        lat_deg, lon_deg, day_of_year, local_offset_hours: as p90_scale_from_forecast

    Returns:
        (p10_scale, p50_scale, p90_scale). Any band whose peak < 0.5 kW or
        elevation too low yields 0.0 for that band.
    """
    if not detailed_forecast:
        return 0.0, 0.0, 0.0

    def best_peak(key):
        best_kw = 0.0
        best_utc = 0.0
        for slot in detailed_forecast:
            kw = slot.get(key, 0.0) or 0.0
            if kw <= best_kw:
                continue
            try:
                ps = slot["period_start"]
                time_part = ps[11:16]
                h, m = int(time_part[:2]), int(time_part[3:5])
                local_h = h + m / 60.0 + 0.25
                utc_h = local_h - local_offset_hours
            except (ValueError, IndexError, KeyError):
                continue
            best_kw = kw
            best_utc = utc_h
        return best_kw, best_utc

    def scale_from_peak(peak_kw, peak_utc):
        if peak_kw < 0.5:
            return 0.0
        elev = solar_elevation(lat_deg, lon_deg, peak_utc, day_of_year)
        sin_elev = math.sin(math.radians(elev))
        if sin_elev < 0.05:
            return 0.0
        return peak_kw / sin_elev

    p10_peak, p10_utc = best_peak("pv_estimate10")
    p50_peak, p50_utc = best_peak("pv_estimate")
    p90_peak, p90_utc = best_peak("pv_estimate90")

    return (
        scale_from_peak(p10_peak, p10_utc),
        scale_from_peak(p50_peak, p50_utc),
        scale_from_peak(p90_peak, p90_utc),
    )


def compute_expected_overflow(p10, p50, p90, confidence, low, high):
    """R50: blend three overflow integrals by Solcast confidence.

    Confidence (0..1) selects between bands:
      c >= high: pure p90 (current pre-R50 behaviour)
      low <= c < high: linear blend p50 → p90
      c < low: linear blend p10 → p50
      c <= 0: pure p10 (most pessimistic)

    Args:
        p10, p50, p90: kWh overflow integrals at each scale
        confidence: Solcast analysis.confidence (clamped to [0, 1])
        low, high: tunable thresholds, must satisfy 0 <= low < high <= 1

    Returns:
        Expected overflow in kWh. Always in [min(p10..p90), max(p10..p90)].
    """
    c = max(0.0, min(1.0, confidence))
    # Defensive: degenerate threshold pair → fall back to p50
    if high <= low:
        return p50

    if c >= high:
        return p90
    if c >= low:
        t = (c - low) / (high - low)
        return (1.0 - t) * p50 + t * p90
    # c < low
    if low <= 0:
        return p10
    t = c / low
    return (1.0 - t) * p10 + t * p50


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


def compute_pv_start_time(scale, lat_deg, lon_deg, day_of_year, threshold_kw, current_utc_hours):
    """R52: find when PV first crosses ABOVE threshold today (rising side).

    Mirror of compute_release_time but on the ascending side of the solar curve.
    Models clear-sky PV as scale × sin(elevation). Finds the earliest UTC hour
    today where this exceeds threshold_kw.

    Args:
        scale: float kW — clear-sky scale
        lat_deg, lon_deg: location
        day_of_year: 1-366
        threshold_kw: PV level we consider "PV started" (e.g., 0.5 kW)
        current_utc_hours: decimal UTC hours now

    Returns:
        (minutes_until_crossing, crossing_utc_hours) or (None, None) if:
            - peak PV today won't reach threshold (won't cross)
            - it's already past today's PV start window (we're post-noon and
              still above threshold means PV started earlier; but the function
              still returns the crossing for early-morning callers).
        If we're already past the morning crossing (current > crossing time on
        the rising side), the function returns the crossing time as past with
        negative minutes — caller should handle.
    """
    # Find solar noon
    B2 = math.radians((360.0 / 364.0) * (day_of_year - 81))
    eot = 9.87 * math.sin(2 * B2) - 7.53 * math.cos(B2) - 1.5 * math.sin(B2)
    solar_noon_utc = 12.0 - lon_deg / 15.0 - eot / 60.0

    # If peak is below threshold, never crosses
    noon_elev = solar_elevation(lat_deg, lon_deg, solar_noon_utc, day_of_year)
    peak_pv = scale * max(0.0, math.sin(math.radians(noon_elev)))
    if peak_pv < threshold_kw:
        return None, None

    # Scan from sunrise (or earlier) up to solar noon — rising side only.
    # Start from 0:00 to find today's first crossing regardless of current_utc_hours.
    crossing_utc = None
    for minute_offset in range(0, int(solar_noon_utc * 60) + 60):
        t = minute_offset / 60.0
        if t > solar_noon_utc:
            break
        elev = solar_elevation(lat_deg, lon_deg, t, day_of_year)
        predicted = scale * max(0.0, math.sin(math.radians(elev)))
        if predicted >= threshold_kw:
            crossing_utc = t
            break

    if crossing_utc is None:
        return None, None

    minutes_until = (crossing_utc - current_utc_hours) * 60.0
    return minutes_until, crossing_utc
