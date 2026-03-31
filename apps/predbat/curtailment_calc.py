# -----------------------------------------------------------------------------
# Predbat Home Battery System - Curtailment Calculator
# Pure algorithm functions for curtailment management
# No HA or Predbat dependencies — testable in isolation
# -----------------------------------------------------------------------------

# Validated from Mar 28 real data (120 five-minute slots):
# Mean PV < 2.0kW: zero spikes above 4.5kW (safe to release)
# Mean PV 2-4kW: spikes to 6-10kW common (overflow risk)
SAFE_PV_THRESHOLD_KW = 2.0


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


def compute_target_soc(remaining_overflow, battery_max_kwh, margin_kwh=0.0):
    """
    Compute target SOC: leave room in battery to absorb remaining overflow.

    target_soc = battery_max - remaining_overflow - margin, clamped to [0, battery_max]

    Args:
        remaining_overflow: float kWh — overflow still expected
        battery_max_kwh: float kWh — battery capacity
        margin_kwh: float kWh — extra buffer for forecast error

    Returns:
        float — target SOC in kWh
    """
    return max(0.0, min(battery_max_kwh, battery_max_kwh - remaining_overflow - margin_kwh))


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


def should_activate(remaining_overflow):
    """
    Whether curtailment management should be active.

    Active whenever any future slot has excess > DNO limit.

    Args:
        remaining_overflow: float kWh

    Returns:
        bool
    """
    return remaining_overflow > 0.0


def compute_overflow_window(pv_forecast, load_forecast, dno_limit, start_minute=0, end_minute=1440, step_minutes=5, values_are_kwh=False, sustained_slots=6):
    """
    Find the time window where PV excess exceeds the DNO limit.

    Returns the first and last minute of sustained overflow. Uses hysteresis
    (sustained_slots) to avoid brief cloud gaps splitting the window.

    Args:
        pv_forecast: dict {minute: value}
        load_forecast: dict {minute: value}
        dno_limit: float kW
        start_minute: int — first minute to consider
        end_minute: int — last minute (exclusive)
        step_minutes: int
        values_are_kwh: bool
        sustained_slots: int — consecutive sub-DNO slots to end the window

    Returns:
        (overflow_start, overflow_end) — minutes from start_minute.
        overflow_start = first minute where PV-load > DNO.
        overflow_end = last minute where PV-load > DNO, extended past brief dips.
        Returns (None, None) if no overflow found.
    """
    step_hours = step_minutes / 60.0
    to_kw = (1.0 / step_hours) if values_are_kwh else 1.0

    overflow_start = None
    overflow_end = None
    consecutive_below = 0

    for m in range(start_minute, end_minute, step_minutes):
        pv_kw = pv_forecast.get(m, 0.0) * to_kw
        load_kw = load_forecast.get(m, 0.0) * to_kw
        excess_kw = pv_kw - load_kw

        if excess_kw > dno_limit:
            if overflow_start is None:
                overflow_start = m
            overflow_end = m
            consecutive_below = 0
        elif overflow_start is not None:
            consecutive_below += 1
            if consecutive_below >= sustained_slots:
                break  # sustained dip — overflow window is closed

    return overflow_start, overflow_end


def compute_post_overflow_energy(pv_forecast, load_forecast, after_minute, end_minute=1440, step_minutes=5, dno_limit=None, values_are_kwh=False):
    """
    Compute energy available for battery charging after the overflow window.

    Sums min(max(0, PV-load), dno_limit) for each slot after after_minute.
    This is the solar excess that can charge the battery when overflow has ended
    (PV-load is positive but below DNO, so all excess can go to battery if
    export is set to 0).

    If dno_limit is None, no cap is applied (all PV-load excess counts).

    Args:
        pv_forecast: dict {minute: value}
        load_forecast: dict {minute: value}
        after_minute: int — first minute to consider (typically overflow_end + step)
        end_minute: int — last minute (exclusive)
        step_minutes: int
        dno_limit: float kW or None — cap per-slot charging rate
        values_are_kwh: bool

    Returns:
        float — total chargeable energy in kWh
    """
    step_hours = step_minutes / 60.0
    to_kw = (1.0 / step_hours) if values_are_kwh else 1.0

    total = 0.0
    for m in range(after_minute, end_minute, step_minutes):
        pv_kw = pv_forecast.get(m, 0.0) * to_kw
        load_kw = load_forecast.get(m, 0.0) * to_kw
        excess_kw = max(0.0, pv_kw - load_kw)
        if dno_limit is not None:
            excess_kw = min(excess_kw, dno_limit)
        total += excess_kw * step_hours
    return total


def simulate_soc_trajectory(pv_forecast, load_forecast, current_soc, soc_max, dno_limit, energy_ratio=1.0, start_minute=0, end_minute=1440, step_minutes=5, values_are_kwh=False):
    """
    Simulate battery SOC trajectory with curtailment active (export at DNO).

    Runs from start_minute until PV is exhausted (evening load irrelevant).
    PV is scaled by energy_ratio (actual/forecast cumulative tracking).

    For each slot:
      - excess = PV*ratio - load
      - If excess > DNO: export DNO, battery absorbs (excess - DNO)
      - If 0 < excess <= DNO: export excess, battery unchanged
      - If excess < 0: battery covers deficit

    Args:
        pv_forecast: dict {minute: value}
        load_forecast: dict {minute: value}
        current_soc: float kWh — starting SOC
        soc_max: float kWh — battery capacity
        dno_limit: float kW — max grid export
        energy_ratio: float — PV scaling factor (1.0 = forecast, >1 = ahead)
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
    last_pv_slot = 0

    for m in range(start_minute, end_minute, step_minutes):
        pv_kw = pv_forecast.get(m, 0.0) * to_kw * energy_ratio
        load_kw = load_forecast.get(m, 0.0) * to_kw

        if pv_kw > 0.1:
            last_pv_slot = m
        elif m > last_pv_slot + 60:
            break  # PV done for the day, evening load irrelevant

        if pv_kw > SAFE_PV_THRESHOLD_KW:
            last_danger = m

        excess = pv_kw - load_kw

        if excess > dno_limit:
            # Overflow: export at DNO, battery absorbs the rest
            charge = (excess - dno_limit) * step_hours
            soc += charge
            net_charge += charge
        elif excess < 0:
            # Deficit: battery covers load
            drain = excess * step_hours  # negative
            soc += drain
            net_charge += drain

        # Clamp SOC
        soc = max(0.0, min(soc_max, soc))
        if soc > peak_soc:
            peak_soc = soc

    return peak_soc, net_charge, last_danger
