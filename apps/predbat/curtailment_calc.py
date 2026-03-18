# -----------------------------------------------------------------------------
# Predbat Home Battery System - Curtailment Calculator
# Pure algorithm functions for curtailment management (v5 iterative target SOC)
# No HA or Predbat dependencies — testable in isolation
# -----------------------------------------------------------------------------


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
