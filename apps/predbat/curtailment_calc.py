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

# RD33 tracking-band gates. Below MIN_TRACKING_ENERGY_KWH of EXPECTED energy so
# far there is not enough day to judge (dawn); below MIN_TRACKING_SPREAD_KWH of
# p10-p90 span there is nothing to interpolate on (a confident Solcast collapses
# the bands). 3.0 kWh lands mid-morning in summer, which is the earliest the
# answer is worth printing; 1.0 kWh of span is roughly the point at which a
# normal forecast error stops saturating the scale.
MIN_TRACKING_ENERGY_KWH = 3.0
MIN_TRACKING_SPREAD_KWH = 1.0

# R63 drain-deadline release band (kWh). Draining CLEARS the breach, so this is
# hysteresis on a closed loop, not a latch — see drain_deadline_breached().
R63_HYST_KWH = 0.5

# Deep-discharge floor (~2.8% of an 18 kWh battery). Applies symmetrically to
# both charge_below and drain_above — the system must never report a target
# below this, so charge_target = min(charge_below, drain_above) and the YAML
# phase logic both stay anchored above the deep-discharge SOC. Prevents the
# inner min collapsing to 0 on extreme-overflow days (drain_above) AND prevents
# Hold/export while SOC is empty on sunny-tomorrow days where soc_keep relaxes
# toward 0 (charge_below).
DEEP_DISCHARGE_FLOOR_KWH = 0.5

# R16a Schmitt deadband for entering Drain (~1% of an 18 kWh battery). Sized to
# be robust to Sigen's 0.1% SOC quantisation (0.018 kWh) so sensor noise alone
# cannot start a drain. Entry needs drain_above + this; once draining we run all
# the way TO drain_above, so the drain never stops short of target.
OUTER_THRESHOLD_KWH = 0.18


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


def _scan_sustained(pv_forecast, start_minute, end_minute, step_minutes, threshold_kw, sustained_minutes, values_are_kwh, want_below):
    """Walk pv_forecast and find the first minute starting a sustained run.

    `want_below=True`: find first slot starting a `sustained_minutes` run of
    pv < threshold_kw (sunset).
    `want_below=False`: find first slot starting a run of pv >= threshold_kw
    (sunrise).

    Returns the START minute of the sustained run, or None if no such run
    exists in the window.
    """
    step_hours = step_minutes / 60.0
    to_kw = (1.0 / step_hours) if values_are_kwh else 1.0
    sustained_slots = max(1, sustained_minutes // step_minutes)

    consecutive = 0
    run_start = None
    for m in range(start_minute, end_minute, step_minutes):
        pv_kw = pv_forecast.get(m, 0.0) * to_kw
        match = (pv_kw < threshold_kw) if want_below else (pv_kw >= threshold_kw)
        if match:
            if run_start is None:
                run_start = m
            consecutive += 1
            if consecutive >= sustained_slots:
                return run_start
        else:
            consecutive = 0
            run_start = None
    return None


def compute_morning_gap(
    pv_forecast,
    load_forecast,
    start_minute=0,
    end_minute=1440,
    step_minutes=5,
    values_are_kwh=False,
    skip_initial_surplus=None,
    pv_threshold_kw=0.3,
    sustained_minutes=30,
):
    """Energy deficit between today's effective sunset and tomorrow's sunrise.

    Boundary detection:
      - sunset = first minute starting a `sustained_minutes` run of pv < threshold
      - sunrise = first minute after sunset starting a `sustained_minutes` run
                  of pv >= threshold (or end_minute if no sunrise in window)

    Returns sum of max(0, load - pv) for minutes in [sunset, sunrise).
    Returns 0 if no sunset is detected in the window (PV stays on throughout —
    e.g. mid-summer at high latitude, or test fixtures with continuous PV).

    Args:
        pv_forecast: dict {minute: value} — PV forecast (kW or kWh-per-step)
        load_forecast: dict {minute: value} — load forecast
        start_minute, end_minute: scan window (inclusive, exclusive)
        step_minutes: forecast step size
        values_are_kwh: if True, values are kWh per step (Predbat format)
        skip_initial_surplus: deprecated, ignored.
        pv_threshold_kw: PV at or above this counts as "sun reliably on".
        sustained_minutes: how long PV must be on/off to count as a transition.

    Returns:
        float — overnight (sun-off) energy gap in kWh.
    """
    sunset = _scan_sustained(pv_forecast, start_minute, end_minute, step_minutes, pv_threshold_kw, sustained_minutes, values_are_kwh, want_below=True)
    if sunset is None:
        return 0.0

    sunrise = _scan_sustained(pv_forecast, sunset, end_minute, step_minutes, pv_threshold_kw, sustained_minutes, values_are_kwh, want_below=False)
    if sunrise is None:
        sunrise = end_minute

    step_hours = step_minutes / 60.0
    to_kw = (1.0 / step_hours) if values_are_kwh else 1.0
    gap_kwh = 0.0
    for m in range(sunset, sunrise, step_minutes):
        pv_kw = pv_forecast.get(m, 0.0) * to_kw
        load_kw = load_forecast.get(m, 0.0) * to_kw
        gap_kwh += max(0.0, load_kw - pv_kw) * step_hours
    return gap_kwh


def compute_pv_covers_load_minute(
    pv_forecast,
    load_forecast,
    start_minute=0,
    end_minute=1440,
    step_minutes=5,
    values_are_kwh=False,
    sustained_minutes=30,
):
    """RD36: first minute at/after `start_minute` where forecast PV >= forecast
    LOAD and STAYS there for `sustained_minutes`. None if it never does.

    This is the moment the battery stops having to carry the house — the honest
    end-point for the pre-PV drain.

    Why it exists (2026-08-12): the drain was timed to end at
    `compute_pv_start_time`, a CLEAR-SKY sine crossing of a fixed 0.5 kW, and
    then coasted on the dawn reserve until PV actually covered load. Measured
    that morning: drain ended 05:45, PV covered load ~06:45 — an hour of
    coasting, on top of the sine prediction itself being ~35 min optimistic
    (implied end 06:14 vs actual ~06:50).

    Predbat's per-slot PV and load forecasts already carry this and were accurate
    on the day (06:30 PV 0.10 vs load 0.24; 07:00 PV 0.43 vs 0.23), so use them
    instead of a sine. Compare against the FORECAST LOAD rather than a fixed
    threshold — the house is not a constant.

    Sustained for the same reason RD35 requires a sustained measured crossing: a
    single slot where load dips under PV is not dawn.

    Args:
        pv_forecast, load_forecast: dict {minute: value}, Predbat step maps.
        start_minute, end_minute: scan window (inclusive, exclusive).
        step_minutes: forecast step size.
        values_are_kwh: True when values are kWh-per-step (Predbat format).
        sustained_minutes: how long the crossing must hold.

    Returns:
        int minute-of-day, or None if PV never sustainedly covers load.
    """
    step_hours = step_minutes / 60.0
    to_kw = (1.0 / step_hours) if values_are_kwh else 1.0
    for m in range(start_minute, end_minute, step_minutes):
        held = True
        for probe in range(m, min(m + sustained_minutes, end_minute), step_minutes):
            pv_kw = pv_forecast.get(probe, 0.0) * to_kw
            load_kw = load_forecast.get(probe, 0.0) * to_kw
            if pv_kw < load_kw:
                held = False
                break
        if held:
            # Guard the degenerate all-zero window (no forecast loaded): 0 >= 0
            # would otherwise report a crossing at midnight.
            if (pv_forecast.get(m, 0.0) * to_kw) <= 0:
                continue
            return m
    return None


def session_reserve_is_reachable(
    pv_forecast,
    load_forecast,
    minutes_to_session,
    deficit_kwh,
    step_minutes=5,
    values_are_kwh=False,
    margin=1.2,
):
    """RD37: can forecast PV refill `deficit_kwh` before the session starts?

    When it can, the session reserve must NOT hold the curtailment drain back —
    it would be protecting energy the sun is about to deliver, at the cost of the
    headroom the day actually needs.

    Live 2026-08-12 06:59: SOC 1.16 kWh with `drain_above` pinned to 15.29 kWh
    (84.6%) for an 18:00 session, suppressing the morning drain, while 60.6 kWh
    of PV was forecast and overflow_p90 was 16.0 kWh. The reserve was protecting
    energy that did not exist against a refill that was never in doubt.

    Caller passes the deficit measured from the DRAIN FLOOR, not from current
    SOC: the question is "if I drain to the floor, can PV still refill in time",
    which is the decision being taken.

    Pass the P10 band as `pv_forecast` — a paid session should not be staked on
    optimistic sun. `margin` requires headroom beyond break-even for the same
    reason.

    Args:
        pv_forecast, load_forecast: dict {minute-from-now: value}.
        minutes_to_session: how long until the session starts.
        deficit_kwh: kWh needed between the drain floor and the reserve target.
        step_minutes: forecast step.
        values_are_kwh: True for Predbat's kWh-per-step maps.
        margin: required surplus multiple (1.2 = 20% clear).

    Returns:
        bool — True when PV comfortably covers the refill, so the reserve should
        stand aside.
    """
    if deficit_kwh <= 0:
        return True
    if minutes_to_session <= 0:
        return False
    step_hours = step_minutes / 60.0
    to_kwh = 1.0 if values_are_kwh else step_hours
    net = 0.0
    for m in range(0, int(minutes_to_session), step_minutes):
        pv = pv_forecast.get(m, 0.0) * to_kwh
        load = load_forecast.get(m, 0.0) * to_kwh
        net += max(0.0, pv - load)
    return net >= deficit_kwh * margin


# RD38: how late the pv-covers-load crossing may plausibly be. The dawn reserve
# now covers ONLY this error — RD36 made the drain run to the crossing, so a
# correct forecast means the reserve is never touched. 45 min because the
# clear-sky miss on 2026-08-12 was 35 min and the per-slot forecast should beat
# it; at ~0.4 kW that is ~0.3 kWh, about 1.7% of pack.
DAWN_ERROR_MARGIN_MINUTES = 45


def compute_dawn_error_margin(load_forecast, crossing_minute, margin_minutes=DAWN_ERROR_MARGIN_MINUTES, step_minutes=5, values_are_kwh=False, base_load_kw=MIN_BASE_LOAD_KW):
    """RD38: kWh the battery must hold in case the pv-covers-load crossing is late.

    Literally "what it costs if the crossing arrives `margin_minutes` after the
    forecast said it would" — the forecast house load over the window FOLLOWING
    the predicted crossing.

    Replaces `dawn_load_kwh`, which measured the PV-start-to-covers-load window.
    That was the right quantity while the drain deliberately stopped at PV-start
    and coasted (pre-RD36); once the drain runs TO the crossing it is a number
    that no longer means what its name says, and in winter it could be hours.

    The load AFTER the crossing is the relevant one — the risk is the crossing
    being late, so it is the following hour the battery must cover. At dawn the
    before/after loads genuinely differ (DHW, heating recovery).

    Floored at `base_load_kw` over the window: a forecast claiming near-zero
    overnight load must not produce a near-zero margin.
    """
    step_hours = step_minutes / 60.0
    to_kwh = 1.0 if values_are_kwh else step_hours
    total = 0.0
    for m in range(int(crossing_minute), int(crossing_minute + margin_minutes), step_minutes):
        total += load_forecast.get(m, 0.0) * to_kwh
    # Floor the TOTAL, not each step: a per-step floor would override a
    # legitimately lower forecast (base_load 0.5 kW vs a measured ~0.4), which is
    # not "don't trust zero", it is "ignore the forecast".
    return max(total, base_load_kw * (margin_minutes / 60.0))


def should_defer_to_charge(gshp_ch_active, soc_kw, soc_keep, was_deferring):
    """R4 (gated): defer to Predbat charge window only when GSHP heating is active.

    R4's purpose was to let Predbat fill the battery from cheap-rate grid for
    overnight heating load. In summer (CH off), there's no overnight heating
    load to cover — plugin should manage its own morning drain to maximize
    curtailment headroom, not yield to Predbat.

    Hysteresis ±0.2 kWh (was_deferring): once deferring, hold until SOC
    rises above keep+0.2 (prevents 5-min flicker at the boundary).

    Args:
        gshp_ch_active: bool — whether central heating is currently active.
        soc_kw: kWh — current battery SOC.
        soc_keep: kWh — Predbat's effective keep target.
        was_deferring: bool — was the plugin already in defer state last cycle?

    Returns:
        bool — True if plugin should yield to Predbat charge window.
    """
    if not gshp_ch_active:
        return False
    engage = soc_keep - 0.2
    release = soc_keep + 0.2
    threshold = release if was_deferring else engage
    return soc_kw < threshold


def compute_floor_with_source(reserve, p10_recovery, overflow_floor, effective_keep=None):
    """R54 with diagnostic source tracking.

    Same formula as R54:
        floor = max(reserve, p10_recovery, min(overflow_floor, effective_keep))

    But also returns which term won, so the dashboard can display the binding
    constraint. Without this, the published target_soc is opaque — the user
    can't tell if they're seeing the overnight target, R59 P10 recovery,
    overflow headroom, or the absolute reserve.

    Args:
        reserve: kWh — absolute physical floor (battery/inverter limit)
        p10_recovery: kWh — R59 P10 recovery floor
        overflow_floor: kWh — R9 overflow-headroom floor (soc_max - overflow*1.2)
        effective_keep: kWh — R55 morning_gap-derived effective_keep

    Returns:
        (floor_kwh, source) where source ∈ {'effective_keep', 'overflow_floor',
        'p10_recovery', 'reserve'}.

    Tie-breaking: when multiple terms produce the same floor, the source
    label prefers the one with the most diagnostic value (the *inner-min*
    term that was already determining the active control). This matters
    most for the typical case where effective_keep == p10_recovery numerically.
    """
    # v31 (2026-07-19): pure-curtailment floor + recovery floor. effective_keep
    # (R55 overnight target) is no longer a drain target — it's dropped. The
    # floor is the higher of the curtailment headroom need (overflow_floor) and
    # the evening-reserve recovery floor (p10_recovery = overnight load + saving
    # session, netted against remaining PV), never below the hardware reserve.
    floor = overflow_floor
    source = "Curtailment Buffer"
    if p10_recovery > floor:
        floor = p10_recovery
        source = "P10 Recovery"
    if reserve > floor:
        floor = reserve
        source = "Reserve"
    return floor, source


def compute_effective_export_cap(today_samples_kw, yesterday_avg_kw, dno_kw=4.0, min_samples=10, hard_floor_kw=2.0):
    """R60: realistic export ceiling for the overflow integral.

    The overflow integral asks "how much PV will exceed our export ability?".
    Theoretical DNO (4.0) over-estimates the ceiling whenever the voltage
    throttle is active — actual ceiling is whatever the throttle allows.

    Falls back across three regimes:
      1. ≥ min_samples of today's during-PV cap readings → today's mean
      2. else yesterday's daytime mean (persisted across days) → yesterday's
      3. else DNO (cold start, never had data)

    Result is clamped to [hard_floor_kw, dno_kw]:
      - Floor prevents a single bad hour predicting "no export at all" tomorrow
      - Ceiling defensively guards against bad data above DNO

    Args:
        today_samples_kw: list of recent voltage_throttle_filtered_cap readings
                          (kW). Caller maintains the rolling window — typically
                          30 min of samples taken during PV > load conditions.
        yesterday_avg_kw: yesterday's daytime mean effective cap (kW). None on
                          first day or when state file unavailable.
        dno_kw: theoretical DNO limit (kW). Used as ceiling and final fallback.
        min_samples: today_samples must have ≥ this many to be trusted.
        hard_floor_kw: minimum estimate (kW). Defensive against "throttle active
                       all hour" → effective_dno=0 → integral predicts disaster.

    Returns:
        kW — effective DNO estimate for the overflow integral.
    """
    if today_samples_kw and len(today_samples_kw) >= min_samples:
        estimate = sum(today_samples_kw) / len(today_samples_kw)
    elif yesterday_avg_kw is not None and yesterday_avg_kw > 0:
        estimate = yesterday_avg_kw
    else:
        estimate = dno_kw
    return max(hard_floor_kw, min(dno_kw, estimate))


def compute_p10_recovery_floor(overnight_target_kwh, p10_pv_remaining_kwh, load_remaining_kwh, p50_pv_remaining_kwh=None, calibration_ratio=1.0):
    """Recovery floor based on Solcast's P10 (pessimistic) remaining estimate —
    minimum SOC needed now to land on overnight_target by sundown even on a
    worse-than-median PV day.

    Formula:
        floor = max(0, overnight_target - (p10_pv_remaining - load_remaining))

    Design choice (2026-05-11): use P10, not P50. The cost of being defensive
    is a small extra round-trip when the day beats P10 (~10% efficiency loss
    on the slice we over-charged). The cost of being optimistic is missing
    overnight_target → forced grid-fill at evening rates (which can be more
    expensive than mid-day import, especially on flat 19p legacy tariffs).
    Once we cross overnight_target the Hold/Drain logic exports the excess,
    so over-charging by a kWh or two costs at most the round-trip; under-
    charging costs the import bill plus comfort risk.

    Args:
        overnight_target_kwh: required SOC by end of day.
        p10_pv_remaining_kwh: Solcast P10 PV remaining (kWh) — used.
        load_remaining_kwh: forecast load remaining today (kWh).
        p50_pv_remaining_kwh: accepted for backward compat; ignored.
        calibration_ratio: accepted for backward compat; ignored.

    Returns:
        kWh — minimum SOC needed now to land on overnight target.

    Used in R54's outer max as a lower-bound term (alongside `reserve`):
        target = max(reserve, p10_recovery, min(curt_floor, effective_keep))
    """
    del calibration_ratio, p50_pv_remaining_kwh  # accepted but not used
    net_charging = p10_pv_remaining_kwh - load_remaining_kwh  # signed
    return max(0.0, overnight_target_kwh - net_charging)


def compute_session_reserve(duration_minutes, cap_kw, discharge_efficiency=1.0):
    """Battery energy (kWh) to reserve for a saving-session export: run at the
    export cap for the session duration. Feeds the recovery-floor target on top
    of the overnight LOAD need (they don't overlap — load is consumption, the
    session is discretionary export). Zero if no session.

        session_reserve = (duration_minutes / 60) * cap_kw / discharge_efficiency

    The efficiency term is what makes this the BATTERY drawdown rather than the
    energy delivered. To push cap x duration through the meter the battery must
    give up more, and reserving only the delivered figure runs the dump short at
    the end of the paid window (2026-08-05: 3.68 kWh reserved for a 60 min
    session at 3.68 kW would deliver ~3.49 kWh at 0.947). Same factor Predbat
    applies in its own predict trajectory, and the same one the R55 morning_gap
    uses — battery_loss_discharge x inverter_loss.

    Clamped at 0.5 like the R55 path: a bad or missing efficiency must not
    inflate the reserve without bound.
    """
    try:
        mins = float(duration_minutes)
    except (TypeError, ValueError):
        return 0.0
    if mins <= 0:
        return 0.0
    try:
        eff = float(discharge_efficiency)
    except (TypeError, ValueError):
        eff = 1.0
    if eff <= 0:
        eff = 1.0
    eff = max(0.5, min(1.0, eff))
    return (mins / 60.0) * cap_kw / eff


def compute_drain_above(reserve, overflow_floor, effective_keep=None, session_protect_kwh=0.0, floor_kwh=None):
    """Drain target: the SOC above which CM drains (Max Export) — PURE
    CURTAILMENT (v31, 2026-07-19). Drain only to make room for forecast
    overflow; NEVER to an overnight/evening reserve (that's Predbat's job, and
    the recovery floor in charge_below is what keeps us from handing back too
    low). So effective_keep (R55) is no longer a drain target — the param is
    kept optional/ignored for back-compat.

    Returns: max(reserve, DEEP_DISCHARGE_FLOOR_KWH, overflow_floor,
                 session_protect_kwh)

    Outer max keeps us off the hardware reserve and the deep-discharge buffer.
    On a big-overflow day overflow_floor is low → drain hard for headroom. On a
    small/no-overflow day overflow_floor ≈ soc_max → drain_above high → CM Holds
    (doesn't drain to a reserve); compute_charge_below / p10_recovery is the
    "don't hand off below the evening need" backstop.

    v32(a): session_protect_kwh (overnight target + saving-session reserve) is a
    lower bound while a session is UPCOMING — CM won't drain the reserve away
    before it. 0 when no session is scheduled, so days without a session keep the
    pure-curtailment drain target unchanged. The session is dumped live via a
    Max Export override, not via this floor.

    v33 (2026-08-06): `floor_kwh` replaces the fixed DEEP_DISCHARGE_FLOOR_KWH arm
    so the caller can raise it to the DAWN RESERVE (carry the house until PV meets
    load) or lower it to POST_DAWN_FLOOR_KWH once that duty is discharged. Defaults
    to DEEP_DISCHARGE_FLOOR_KWH, so callers that do not care are unchanged.

    Why it has to live HERE and not only in compute_pre_pv_target: the pre-PV path
    ends at PV START, but the battery is not free of load duty until PV MEETS LOAD
    — ~85 min later in August, hours in winter. The reserve used to evaporate at
    that phase boundary and the Schmitt drained straight through the gap (live
    2026-08-06: 5.5% -> 2.5% -> coasted to 1.3% importing).
    """
    if floor_kwh is None:
        floor_kwh = DEEP_DISCHARGE_FLOOR_KWH
    return max(reserve, floor_kwh, overflow_floor, session_protect_kwh)


def compute_drain_above_source(reserve, overflow_floor, session_protect_kwh=0.0, floor_kwh=None):
    """Which arm of `compute_drain_above` set the floor.

    Returns one of: "session_protect", "overflow_floor", "dawn_reserve",
    "deep_floor", "reserve".

    "dawn_reserve" is reported when the caller-supplied `floor_kwh` is ABOVE the
    deep floor, i.e. we are holding charge back to carry the house until PV meets
    load. Without a distinct label a reader sees a 1.81 kWh floor sitting next to
    `overflow_floor_kwh: 0.0` with nothing to explain it — the same complaint that
    produced this function for `session_protect`.

    The sensor is called "Headroom Floor (P90 overflow)", but on a saving-session
    day the value comes from `session_protect` instead — observed live
    2026-08-03, floor 10.22 kWh published next to `overflow_floor_kwh: 3.39`
    with nothing to explain the difference. The dashboard must be able to name
    the term that won.

    Derived FROM compute_drain_above rather than re-implementing the max, so the
    two can never disagree (the `required_headroom_kwh` drift lesson). On a tie
    the more surprising arm is reported first: a floor held up by a session is
    the thing a reader cannot infer from the other attributes.
    """
    value = compute_drain_above(reserve, overflow_floor, None, session_protect_kwh, floor_kwh)
    hard_floor = DEEP_DISCHARGE_FLOOR_KWH if floor_kwh is None else floor_kwh
    hard_name = "dawn_reserve" if hard_floor > DEEP_DISCHARGE_FLOOR_KWH else "deep_floor"
    for name, term in (
        ("session_protect", session_protect_kwh),
        ("overflow_floor", overflow_floor),
        (hard_name, hard_floor),
        ("reserve", reserve),
    ):
        try:
            if float(term) >= value - 1e-9:
                return name
        except (TypeError, ValueError):
            continue
    return "overflow_floor"


def estimate_session_export_left_kwh(cap_kw, minutes_remaining):
    """Grid-side energy still to be sold before the saving session ends.

        export_left = cap_kw x hours_remaining

    The heartbeat holds export at the DNO cap for the whole paid window, so this
    is simply the cap times the time left. GRID-side on purpose: it is what the
    meter records and what the session pays on, so it carries no discharge
    divide — that belongs to the pack side of the same sum, in
    `estimate_session_end_kwh`.

    This is the number the card leads with. "How much is there still to sell"
    is the question a human asks mid-session, and it was not on the card at all.
    """
    try:
        mins = float(minutes_remaining)
    except (TypeError, ValueError):
        return 0.0
    if mins <= 0:
        return 0.0
    return max(0.0, float(cap_kw)) * (mins / 60.0)


def estimate_session_end_kwh(soc_kwh, cap_kw, load_kw, pv_kw, minutes_remaining, discharge_efficiency=1.0):
    """Projected battery energy (kWh) when a live saving session ends.

    Built from the energy still to sell rather than from a power extrapolation:

        end = soc - (export_left / discharge_efficiency + (load - pv) x hours)

    The pack must give up MORE than the meter will see, so the grid-side
    `export_left` carries the discharge divide — the same conversion, in the
    same direction, as `compute_session_reserve`. `load` and `pv` do not: the
    site load sensor is a balance residual and is already pack-side (see RD39).

    Zero or negative time remaining returns the current SOC unchanged.

    RD42 (2026-08-14) — why there is no floor clamp. It used to end
    `return max(floor_kwh, projected)`, justified as "the keep-floor guard stops
    the sell there". The guard DEFERS to a live session (RD14-own), so the dump
    runs straight through the floor and the clamp promised a stop that does not
    happen. Live 12 Aug 2026 19:00: published 39.3% — exactly the overnight
    target, i.e. the clamp itself — with SOC 57.2% and an hour left to run. The
    session ended at 31.2%. The projection must be free to go below the
    overnight reserve, because that is precisely the warning worth having.
    """
    try:
        mins = float(minutes_remaining)
    except (TypeError, ValueError):
        return soc_kwh
    if mins <= 0:
        return soc_kwh
    hours = mins / 60.0
    eff = float(discharge_efficiency) if discharge_efficiency else 1.0
    export_left = estimate_session_export_left_kwh(cap_kw, mins)
    pack_draw = export_left / max(0.5, eff) + (float(load_kw) - float(pv_kw)) * hours
    return float(soc_kwh) - max(0.0, pack_draw)


def forecast_energy_to_now(detailed_forecast, minutes_now, local_offset_hours=0):
    """RD29: forecast energy (kWh) generated SO FAR today, per band.

    Solcast slots are kW **means over 30 minutes**, so energy = kW x 0.5 summed
    over every slot that has already started.

    **Why this replaces the peak for tracking.** `actual_scale` is a running max
    of INSTANTANEOUS PV samples, while the forecast peak is a half-hourly MEAN —
    not like for like, and biased high by construction. Cloud-edge enhancement
    can push an instant above clear-sky, so the peak reads most optimistic on
    exactly the broken-cloud days that under-deliver. Live 2026-08-06: one
    10.14 kW instant against a 7.72 kW p90 slot mean reported "above p90, 100%"
    on a day that returned 38.56 kWh against a 45.96 kWh p50 forecast.

    An integral is the right statistic for "how is the day going": robust to
    single spikes, meaningful from mid-morning, and directly comparable to
    `sensor.sigen_plant_daily_pv_energy`.

    `actual_scale` is deliberately NOT changed — it feeds safe_time (R21/R43),
    where the optimistic peak is arguably correct because that decision asks
    "could we still overflow?"

    Args:
        detailed_forecast: Solcast slots with 'period_start' and
            'pv_estimate10' / 'pv_estimate' / 'pv_estimate90' (kW).
        minutes_now: minutes past LOCAL midnight.
        local_offset_hours: unused — slot timestamps are already local and
            `minutes_now` is local, so no conversion is needed. Kept only so the
            signature matches the scale helpers, which DO convert to UTC.

    Returns:
        (p10_kwh, p50_kwh, p90_kwh) generated so far. (0,0,0) if unusable.
    """
    if not detailed_forecast:
        return 0.0, 0.0, 0.0
    totals = {"pv_estimate10": 0.0, "pv_estimate": 0.0, "pv_estimate90": 0.0}
    for slot in detailed_forecast:
        try:
            # period_start arrives as a datetime from HA — str() first, as in
            # p_scales_from_forecast, where a bare [11:16] silently degraded
            # every band (2026-08-05).
            ps = str(slot["period_start"])
            h, m = int(ps[11:13]), int(ps[14:16])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if h * 60 + m >= minutes_now:
            continue
        for key in totals:
            try:
                totals[key] += float(slot.get(key, 0.0) or 0.0) * 0.5
            except (TypeError, ValueError):
                continue
    return totals["pv_estimate10"], totals["pv_estimate"], totals["pv_estimate90"]


def day_tracking_ratio(actual_kwh, expected_p50_kwh, min_expected_kwh=1.0, lo=0.1, hi=2.0):
    """RD31: how today is ACTUALLY delivering, as a fraction of the p50 forecast
    for the same period. `None` when there is not yet enough sun to mean anything.

    Distinct from the R58 `calibration_ratio`, which is a rolling **30-minute**
    window — that tracks the cloud overhead right now. This is the whole day so
    far, which is what says "this day is a p10 day, not a p90 day".

    **Why it matters more than it looks.** Overflow is NON-LINEAR in PV: it is
    `sum(max(0, pv - load - cap))`, so slots that were just over the cap fall under
    it entirely and stop contributing. Live 2026-08-07: a 12.6% shortfall in
    generation (ratio 0.874) took the overflow estimate from 8.90 kWh to ~2.6 —
    a 70% cut. "We're 12% down" sounds minor and changes the answer completely.

    Clamped to [lo, hi] so a sensor glitch cannot scale the forecast into fantasy,
    and gated on `min_expected_kwh` so dawn — when expected is ~0 and any actual
    reading gives a huge ratio — returns None rather than noise.

    Args:
        actual_kwh: generation so far today (the daily PV meter).
        expected_p50_kwh: p50 forecast energy over the same period
            (`forecast_energy_to_now`).

    Returns:
        float ratio, or None when it would not be meaningful.
    """
    try:
        actual = float(actual_kwh)
        expected = float(expected_p50_kwh)
    except (TypeError, ValueError):
        return None
    if expected < min_expected_kwh:
        return None
    return max(lo, min(hi, actual / expected))


def classify_forecast_tracking(actual_kwh, p10_kwh, p50_kwh, p90_kwh, min_expected_kwh=MIN_TRACKING_ENERGY_KWH, min_spread_kwh=MIN_TRACKING_SPREAD_KWH):
    """Which Solcast band is today's delivered ENERGY actually tracking?

    The plugin derives THREE overflow integrals from three clear-sky scales
    (p_scales_from_forecast) and blends them. When the day goes wrong it matters
    which of the three was right — but nothing said so, which left "was the
    forecast bad, or was the control bad?" unanswerable after the fact.

    **Units: kWh of generation SO FAR TODAY, against the p10/p50/p90 forecast
    energy for the same period** (`forecast_energy_to_now`). The caller passes
    energies — `curtailment_plugin` calls this with `(actual_e, p10_e, p50_e,
    p90_e)`. Both gates below are therefore in kWh and only make sense that way.

    Do NOT pass the clear-sky *scales* (`actual_scale`, `p10_scale`, ... as
    published on the phase sensor). Those are a different quantity — peak-derived
    shape factors, typically 6-10 — and they sail past `min_expected_kwh`, so the
    "too early" gate silently stops working and a dawn reading prints a confident
    band. Live 2026-08-12 08:20 the two disagreed outright: the energies
    (1.86 / 2.33 / 2.52 / 2.55) gave "too early", the scales
    (6.19 / 7.52 / 9.72 / 9.72) gave "below p10". Naming fixed 2026-08-12 after
    the old `*_scale` parameter names and this docstring claimed the opposite of
    what the code does, and misled a reader into working the gate by hand
    against the wrong numbers.

    Returns:

        (label, percentile)

    label      "below p10" | "p10-p50" | "p50-p90" | "above p90" | "unknown"
    percentile piecewise-linear position on the 10/50/90 anchors, or None when
               the bands are unusable. Extrapolated beyond the anchors and
               clamped to 0-100, so "above p90" reads 90-100 rather than
               pretending to precision the bands cannot support.

    Energy-based, so it answers "which band is today's ENERGY tracking", not
    "which band is today's SKY tracking". Cloud timing can put those two apart.
    """
    try:
        a = float(actual_kwh)
        p10 = float(p10_kwh)
        p50 = float(p50_kwh)
        p90 = float(p90_kwh)
    except (TypeError, ValueError):
        return "unknown", None
    # Bands must be present and correctly ordered to interpolate on.
    if a <= 0 or p10 <= 0 or p50 <= 0 or p90 <= 0:
        return "unknown", None
    if not (p10 <= p50 <= p90):
        return "unknown", None

    # RD33 — two ways the bands are USABLE but the answer is still meaningless.
    # Both are distinct from "unknown", which means the bands are broken; these
    # mean the bands are fine and the question is premature or unanswerable.
    #
    # "too early": live 2026-08-08 07:10, actual 0.28 kWh against expected
    # p10/p50/p90 of 0.57/0.60/0.63. The card read "sky tracking below p10 (0%)"
    # an hour after sunrise, on a day forecast at 55.8 kWh p50 remaining — a
    # confident verdict on a day that had not happened yet. `day_tracking_ratio`
    # already refuses this via min_expected_kwh; the classifier had no such gate.
    #
    # "no spread": live 2026-08-07, whole-day bands 10.12/10.15/10.23. When
    # Solcast is confident the three collapse, any small miss saturates a 0.11 kWh
    # span and prints "below p10". Unlike "too early" this never improves as the
    # day goes on, so it is reported separately — the cause and the cure differ.
    if p50 < min_expected_kwh:
        return "too early", None
    if (p90 - p10) < min_spread_kwh:
        return "no spread", None

    if a < p10:
        label = "below p10"
        span = p50 - p10
        pct = 10.0 - 40.0 * ((p10 - a) / span) if span > 0 else 0.0
    elif a <= p50:
        label = "p10-p50"
        span = p50 - p10
        pct = 10.0 + 40.0 * ((a - p10) / span) if span > 0 else 30.0
    elif a <= p90:
        label = "p50-p90"
        span = p90 - p50
        pct = 50.0 + 40.0 * ((a - p50) / span) if span > 0 else 70.0
    else:
        label = "above p90"
        span = p90 - p50
        pct = 90.0 + 40.0 * ((a - p90) / span) if span > 0 else 100.0
    return label, round(max(0.0, min(100.0, pct)), 1)


def compute_charge_below(p10_recovery_floor, soc_keep, session_charge_target=0.0):
    """Charge target: SOC level below which the system must not be exporting.

    Returns: max(p10_recovery_floor, soc_keep, DEEP_DISCHARGE_FLOOR_KWH,
                 session_charge_target)

    Four terms in descending order of priority:
      - p10_recovery_floor: SOC needed now to land on overnight target even on
        a worst-case (P10) PV day (R59).
      - soc_keep: overnight need / safety margin — the "soc_keep floor" clamp
        documented in REQUIREMENTS (2026-05-08).
      - DEEP_DISCHARGE_FLOOR_KWH: symmetric with compute_drain_above. The
        soc_keep clamp evaporates when on_before_plan relaxes soc_keep toward 0
        on sunny-tomorrow days (charge_below = max(0, 0) = 0). The deep-
        discharge floor keeps charge_target = min(charge_below, drain_above)
        above 0.5 kWh, so the YAML never reports Hold/exporting while SOC sits
        at empty. Observed 2026-06-04 with battery at 0% during PV surplus +
        kettle transient → grid import.
      - session_charge_target (RD41): the saving-session reserve, already
        clamped by the caller to the headroom the forecast still needs. 0 when
        no session is armed, so ordinary days are untouched.

    RD41 (2026-08-14) — why the session term has to be HERE and not only in
    compute_drain_above. It was a floor and never a target: on 12 Aug 2026 the
    reserve held `drain_above` at 14.86 kWh while `charge_below` sat at 0.50,
    leaving a 14 kWh band in which CM neither drained nor charged. In Hold the
    pack receives only the above-cap overflow, so SOC stalled at 12.82 kWh from
    16:50 and the 18:00 session opened 2.08 kWh short of the reserve CM had
    itself computed. "Do not fall below X" does not get you to X.

    The caller passes `min(session_protect_kwh, overflow_floor)` — the p90
    defence keeps priority, and the clamp lifts on its own as the day's
    remaining overflow decays, so the reserve is banked as soon as it is
    affordable rather than at a deadline that has already passed.
    """
    return max(p10_recovery_floor, soc_keep, DEEP_DISCHARGE_FLOOR_KWH, session_charge_target)


def compute_pre_pv_target(soc_max, reserve, expected_overflow_kwh, dawn_load_kwh, max_reserved_kwh=1.8, safety_factor=1.05):
    """R62 (2026-07-07): forecast-driven pre-PV drain target.

    The drain goes exactly as deep as the forecast headroom requirement, and no
    deeper:

        overflow_floor = max(0, (soc_max − min(max_reserved, overflow))
                                − overflow × safety_factor)      # same shape as R54/R9
        target         = max(reserve,
                             DEEP_DISCHARGE_FLOOR_KWH + dawn_load,  # carry the house
                                                                    # through the R61 dawn gap
                             overflow_floor)

    A small forecast overflow leaves `overflow_floor` near `soc_max`, so the
    target lands at or above current SOC and the caller drains nothing. That is
    the intended answer on a day that does not need headroom.

    Motivation (2026-07-07): 63 kWh forecast at 0.92 confidence needed ~17-20
    kWh of battery room, but R52 stopped draining at soc_keep + 20% = 3.6 kWh,
    stranding 3 kWh of headroom on exactly the day it mattered. The plugin is
    supposed to be autonomous — no helper-tweaking the night before.

    RD43 (2026-08-17) — why the legacy term is GONE. R62 originally returned
    `min(legacy, floor_driven)` with `legacy = soc_keep + buffer_pct% × soc_max`,
    described as a ceiling that let the forecast be "MORE aggressive, never
    less". A `min()` in that position is one-way: the forecast could only ever
    deepen the drain. Whenever the forecast said *no headroom needed*
    (`floor_driven` → `soc_max`), `legacy` bound instead and drained anyway.

    On 17 Aug 2026 that dumped 5.1 kWh pre-dawn and refilled 3.5 kWh from PV
    three hours later. Overnight `best_soc_keep` was 0 and the buffer helper
    10%, so `legacy` = 1.81 kWh — the battery was driven to 10% SOC on a day
    whose realised requirement was 4.08 kWh of headroom, which the 51.5% SOC it
    started at already covered twice over. `drain_above` published exactly 1.81
    (= 0 + 0.10 × 18.08), which is how we know `legacy` was the binding term and
    `floor_driven` wanted little or no drain.

    The static term cannot be rescued by tuning: with `soc_keep` at 0 overnight
    it is a bare percentage of pack size, carrying no information about the day.
    Over-drain protection is `overflow_floor` itself — it is the quantity that
    knows how much room the day needs.

    Args:
        soc_max: kWh — battery capacity.
        reserve: kWh — hardware reserve floor.
        expected_overflow_kwh: kWh — forecast overflow against the R60 effective
            export cap (≥ 0). A throttled grid (smaller effective cap) raises it
            and so automatically deepens the drain.
        dawn_load_kwh: kWh — forecast house load from PV-start until PV covers
            load (the R61 window where the battery still carries the house).
        max_reserved_kwh: kWh — R45 buffer cap (MAX_RESERVED_KWH).
        safety_factor: R9 overflow safety multiplier (OVERFLOW_SAFETY_FACTOR).

    Returns:
        kWh — pre-PV drain target.
    """
    overflow = max(0.0, expected_overflow_kwh)
    buffer_kwh = min(max_reserved_kwh, overflow)
    overflow_floor = max(0.0, (soc_max - buffer_kwh) - overflow * safety_factor)
    return max(reserve, DEEP_DISCHARGE_FLOOR_KWH + max(0.0, dawn_load_kwh), overflow_floor)


def apply_no_surplus_drain_hold(drain_target, soc_kw, pv_covering):
    """Option A (2026-06-15): never request draining below current SOC while PV
    is not yet covering load (no genuine surplus).

    The morning_gap-derived overnight target legitimately shrinks toward sunrise
    (less battery needed as morning nears) — that part is correct. The flaw is
    that compute_morning_gap calls "sunrise" the moment PV crosses ~0.3 kW, well
    before PV actually exceeds load. Draining to that shrinking target empties
    the battery before PV relieves it. 2026-06-15: plugin activated 05:11 BST
    with target 0.3 kWh, drained 7.6% → 2.7% to grid, then imported once empty;
    PV did not actually exceed load until ~08:44.

    Draining exists to make room for *surplus* PV. With no surplus there is
    nothing to make room for, so hold at (at least) current SOC — no Drain
    fires, the battery still covers load naturally in Hold (export = 0). Once PV
    covers load, normal drain-to-target resumes, and by then the target has
    correctly risen again (post-sunrise rollover).

    R52 pre-PV drain (intentional pre-sunrise drain on confirmed-overflow days)
    uses a separate, separately-gated path and does not flow through here.

    Args:
        drain_target: kWh — computed drain floor (effective_keep) before the guard.
        soc_kw: kWh — current battery SOC.
        pv_covering: bool — True when actual PV exceeds load by the surplus margin.

    Returns:
        kWh — drain_target unchanged when there is surplus; otherwise raised to
        at least soc_kw so no drain below the current level is requested.
    """
    if pv_covering:
        return drain_target
    return max(drain_target, soc_kw)


def compute_proposed_phase(soc_kwh, charge_below_kwh, drain_above_kwh, plugin_active=True, was_draining=False, outer_threshold_kwh=OUTER_THRESHOLD_KWH):
    """Split-threshold phase decision (shadow + automation logic).

    Charge target = min(charge_below, drain_above). On a normal day this is
    charge_below (the lower threshold); on a cross-over day (cb > da, deficit
    forecast) this is drain_above. Charging to the lower of the two preserves
    curtailment headroom — if the deficit forecast proves wrong and surplus PV
    arrives, we have room to absorb without curtailing.

    Drain fires when SOC > drain_above (curtailment buffer breached).
    Curtailment defence wins over deficit insurance: if SOC drifts above
    drain_above (e.g., during Hold while battery passively absorbs PV),
    Drain pulls it back. Predbat handles overnight grid-charge if the
    deficit forecast holds and we end the day below overnight target.

    R16a hysteresis (restored 2026-07-29). Entering Drain requires SOC to exceed
    drain_above by `outer_threshold_kwh`; once draining (`was_draining`) it runs
    all the way TO drain_above. R16a dates from v19 but its implementation lived
    in the 5-second HA automation that v30 retired, leaving a bare
    `soc > drain_above` with no deadband.

    Live consequence 2026-07-29 07:50-08:33: the policy flapped
    Max Export <-> Predbat eight times in 45 minutes while SOC oscillated
    2.8-3.1% around a drain_above of 0.54 kWh (3.0%). Sigen quantises SOC to
    0.1% (0.018 kWh) and the RD4 low-SOC handover sits at 2.8%, so each micro
    drain tripped the handover, MSC charged it back, and it drained again.

    Args:
        soc_kwh: current battery SOC in kWh
        charge_below_kwh: P10 recovery floor (must charge to this if no deficit forecast)
        drain_above_kwh: curtailment-buffer floor (drain to this on overflow days)
        plugin_active: when False, plugin is Off — automation idles regardless.

    Returns:
        "Off", "Charge", "Drain", or "Hold".
    """
    if not plugin_active:
        return "Off"
    charge_target = min(charge_below_kwh, drain_above_kwh)
    if soc_kwh < charge_target:
        return "Charge"
    # Run-to-target once engaged; a deadband only on the way IN (R16a).
    drain_entry = drain_above_kwh if was_draining else drain_above_kwh + outer_threshold_kwh
    if soc_kwh > drain_entry:
        return "Drain"
    return "Hold"


# v30 (DC-coupled) dispatch policy names — input_select.sig_dispatch_policy (RD3).
POLICY_PREDBAT = "Predbat"
POLICY_MAX_EXPORT = "Max Export"
POLICY_HOLD_BATTERY = "Hold Battery"
POLICY_SOLAR_CHARGE = "Solar Charge Battery"

_PHASE_TO_POLICY = {
    "Drain": POLICY_MAX_EXPORT,
    "Hold": POLICY_HOLD_BATTERY,
    "Charge": POLICY_SOLAR_CHARGE,
    "Off": POLICY_PREDBAT,
}


def phase_to_policy(phase):
    """Map a curtailment phase (compute_proposed_phase output) to a v30 dispatch
    policy name for input_select.sig_dispatch_policy (RD9/RD3):
    Drain→Max Export, Hold→Hold Battery, Charge→Solar Charge Battery, Off→Predbat.
    Unknown input → Predbat (fail safe: hand back rather than mis-drive)."""
    return _PHASE_TO_POLICY.get(phase, POLICY_PREDBAT)


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


def smooth_overflow_samples(samples, now_minutes, window_minutes=30):
    """R64 — rolling MEDIAN of the overflow estimate over a trailing window.

    The Solcast-derived overflow estimate is noisy cycle-to-cycle. Measured
    2026-07-28 09:00-14:40: the value fell 13.01 -> 6.53 kWh (a real -6.48 kWh
    trend as the day burned off) but moved UP on 27 of 59 steps, +3.77 kWh in
    total. Path length 14.02 kWh against a net 6.48 — a **2.16x noise ratio**.
    That wobble reaches the floor at 1.2x and chatters any threshold SOC happens
    to be sitting on.

    **Median, not mean.** Solcast revisions arrive as single-slot jumps. A median
    rejects a one-cycle spike outright; a mean folds 1/N of it into the floor.

    **Direction matters (R25).** On a falling series — the normal shape of a day —
    the median sits ABOVE the raw value, so we assume slightly MORE overflow, take
    a slightly LOWER floor, and drain slightly MORE. That is the safe direction:
    over-drain costs one round-trip, under-drain costs the whole clipped surplus.

    Args:
        samples: list of (minute, value) tuples, any order.
        now_minutes: current minute-of-day.
        window_minutes: trailing window width.

    Returns:
        float — median of in-window samples, or None if there are none (caller
        should fall back to the raw value; history is empty after every restart).
    """
    if not samples:
        return None
    cutoff = now_minutes - window_minutes
    vals = sorted(v for m, v in samples if m >= cutoff)
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def required_headroom_kwh(overflow_kwh, max_reserved_kwh, safety_factor=1.05):
    """**The** definition of "how much battery headroom does the forecast overflow
    require?". Every site that asks this question calls this function.

        required = safety_factor x overflow + min(max_reserved, overflow)

    Two terms: the R9 safety multiplier (forecast error *during* overflow) and the
    R45 tapered reserve, which cannot exceed the overflow itself — near safe_time
    it tapers to 0 so the battery can fill.

    **Why this exists as a function.** The same question was expressed in five
    places in three different formulas: two with no reserve term, two with the
    MAX_RESERVED constant, one with the R49-reduced `effective_max_reserved`. On
    2026-07-28 the weakest version (`no_drain`) vetoed a drain the strongest
    version (the Headroom Floor) had correctly called, leaving the battery 1.67
    kWh short of its p90 defence on a clear day. Differences between callers must
    be explicit arguments, never separate expressions — see the Charter,
    *One quantity, one definition*.

    Args:
        overflow_kwh: remaining forecast overflow (p90 per R7/R42, smoothed R64).
        max_reserved_kwh: the R45 reserve ceiling for this caller. Pass
            `effective_max_reserved` (R49 may have reduced it), or 0.0 for the
            planning-time callers that deliberately hold no reserve.
        safety_factor: OVERFLOW_SAFETY_FACTOR.

    Returns:
        float kWh — headroom the battery must have for the overflow to fit.
    """
    ov = max(0.0, overflow_kwh)
    return safety_factor * ov + min(max_reserved_kwh, ov)


def compute_no_overflow_charge_target(overnight_target_kwh, soc_max, overflow_p90_kwh, max_reserved_kwh, safety_factor=1.05):
    """RD28: the charge target once curtailment no longer competes for the same kWh.

        target = min(overnight_target, soc_max - required_headroom(overflow_p90))

    **Why a different target from `charge_below`.** `charge_below` is the P10
    recovery floor — a DEADLINE, not a target: "be at least this high now, or a P10
    afternoon will not get you there". Deferring to it is correct while curtailment
    wants the same kWh, because every kWh banked early is headroom lost at the peak.

    Once `overflow_p90` reaches 0 nothing competes. Deferring then earns only an
    export credit and risks buying the same energy back overnight at import rates,
    which is the worse side of that trade. So bank to tonight's actual need and
    then hold.

    Live 2026-08-06 18:02 — the case this exists for: overflow_p90 0.0, SOC 5.66
    kWh, overnight_target 6.62, charge_below 5.15. SOC sat ABOVE charge_below, so
    the Schmitt said Hold and CM exported the surplus while 0.96 kWh short of
    tonight's need, on a P10 margin of 0.51 kWh — about eight minutes of surplus.

    **The min() is the safeguard.** "No overflow left" is a forecast and forecasts
    move, so while p90 is still non-zero the target is clamped to whatever leaves
    room for it. It uses `required_headroom_kwh` rather than a second expression —
    the 2026-07-28 lesson that five spellings of this question let the weakest one
    veto the strongest.

    Args:
        overnight_target_kwh: tonight's need (R55 morning-gap target).
        soc_max: usable battery capacity, kWh.
        overflow_p90_kwh: remaining forecast overflow.
        max_reserved_kwh: the R45 reserve ceiling for this caller.
        safety_factor: mirrors OVERFLOW_SAFETY_FACTOR (which lives in the plugin,
            so the default is the literal here, as in required_headroom_kwh).

    Returns:
        float kWh — charge to this, then Hold. Never negative.
    """
    headroom = required_headroom_kwh(overflow_p90_kwh, max_reserved_kwh, safety_factor)
    return max(0.0, min(overnight_target_kwh, soc_max - headroom))


def compute_overflow_fits_margin(battery_headroom_kwh, overflow_kwh, safety_factor=1.05, max_reserved_kwh=1.8):
    """Headroom margin against the SAFETY-FACTORED overflow requirement, kWh.

        margin = headroom - (safety_factor × overflow + min(max_reserved, overflow))

    This is the same requirement the Headroom Floor (`overflow_floor`) applies,
    exposed so the `no_drain` veto can key off it too. Positive = the surplus
    genuinely fits with the p90 defence intact; negative = we are short and must
    still be draining.

    Why it matters (2026-07-28): `no_drain` previously compared bare overflow
    against headroom with a flat buffer. On SOC 10.03 / headroom 8.05 / p90 6.60
    that read "fits" (margin 1.45) while the Headroom Floor wanted 9.72 kWh —
    short by 1.67. The band correctly put SOC above drain_above and the weaker
    veto forced Hold anyway. R25/R42/R43 are one-directional (bigger overflow
    estimate → more drain → safer), so the margin-carrying test must win.

    Args:
        battery_headroom_kwh: soc_max - soc.
        overflow_kwh: remaining forecast overflow (p90 per R7/R42).
        safety_factor: OVERFLOW_SAFETY_FACTOR.
        max_reserved_kwh: R45 tapered reserve ceiling.

    Returns:
        float kWh — signed margin. Positive means it fits.
    """
    return battery_headroom_kwh - required_headroom_kwh(overflow_kwh, max_reserved_kwh, safety_factor)


def compute_shed_rate(pv_kw, load_kw, export_cap_kw):
    """R63 — the rate at which the battery can actually be drained, kW.

        shed_rate = export_cap - max(0, pv - load)

    The drain lever is not the export cap: PV is refilling the battery at the
    same time we are selling. What we can genuinely shed is the cap MINUS the
    surplus already flowing into it.

    Goes NEGATIVE once `pv - load > export_cap` — past that point we export flat
    out and the battery still charges from the excess, so no headroom can be
    made at all. That crossing is R63's T_lockout, and it is the exact moment
    R25's "no levers" begins.

    Args:
        pv_kw: current/forecast PV generation, kW.
        load_kw: current/forecast house load, kW.
        export_cap_kw: effective export ceiling (DNO or R60 effective cap), kW.

    Returns:
        float kW — signed. Positive = headroom can be made at this rate.
    """
    return export_cap_kw - max(0.0, pv_kw - load_kw)


def compute_max_sheddable(scale, lat_deg, lon_deg, day_of_year, from_utc_hours, lockout_utc_hours, export_cap_kw, base_load=MIN_BASE_LOAD_KW, step_minutes=5, load_forecast_kw=None):
    """R63 — how much headroom can still be made before the lever inverts, kWh.

        max_sheddable = ∫ max(0, shed_rate(t)) dt   from now to T_lockout

    Integrated, not sampled: `shed_rate` falls continuously as PV climbs, so the
    instantaneous rate materially over-states what remains achievable. Negative
    instantaneous rates contribute 0 rather than subtracting — this measures
    drain CAPACITY, not the net SOC trajectory.

    Uses the same clear-sky geometry model as compute_solar_overflow
    (pv(t) = scale × sin(elev(t))). Per R9 geometry is preferred over per-slot
    forecast, which is too noisy to time a deadline against.

    Args:
        scale: float kW — clear-sky scale (peak_pv / sin(elevation_at_peak)).
        lat_deg, lon_deg: location.
        day_of_year: 1-366.
        from_utc_hours: integration start (decimal UTC hours, i.e. now).
        lockout_utc_hours: T_lockout — when pv - load first exceeds the cap.
        export_cap_kw: effective export ceiling, kW.
        base_load: minimum household load offsetting PV, kW.
        step_minutes: integration step size.
        load_forecast_kw: optional list of kW per step from from_utc_hours.

    Returns:
        float kWh — drain capacity remaining before lockout. 0.0 if the window
        is empty, inverted, or already past.
    """
    if scale <= 0 or lockout_utc_hours <= from_utc_hours:
        return 0.0
    step_hours = step_minutes / 60.0
    total = 0.0
    t = from_utc_hours
    idx = 0
    while t < lockout_utc_hours:
        elev = solar_elevation(lat_deg, lon_deg, t, day_of_year)
        pv_kw = scale * max(0.0, math.sin(math.radians(elev)))
        if load_forecast_kw is not None and idx < len(load_forecast_kw):
            load_kw = max(base_load, load_forecast_kw[idx])
        else:
            load_kw = base_load
        rate = compute_shed_rate(pv_kw, load_kw, export_cap_kw)
        if rate > 0:
            total += rate * min(step_hours, lockout_utc_hours - t)
        t += step_hours
        idx += 1
    return total


def drain_deadline_breached(headroom_needed_kwh, max_sheddable_kwh, engaged=False, hyst_kwh=R63_HYST_KWH, drainable_kwh=None):
    """R63 — is the drain still achievable before the lever inverts?

    True when we need more headroom than we can still make by T_lockout, i.e.
    we are already behind and must be at Max Export now.

    It can only ever fire EARLIER than the plain energy test
    (`SOC > drain_above`), never later, so on days with slack it is silent and
    behaviour is unchanged. That is what makes it safe on a live control path.

    **Hysteresis, not a latch.** Draining is what CLEARS this condition —
    headroom grows, so headroom_needed falls. The loop is self-correcting and
    must stay closed; a one-way latch would hold Max Export after the drain had
    succeeded and empty the battery. `engaged` widens the release threshold by
    `hyst_kwh` so it doesn't chatter at the boundary.

    Args:
        headroom_needed_kwh: safety × remaining_overflow + buffer − current
            headroom. <= 0 means we already have enough room.
        max_sheddable_kwh: from compute_max_sheddable().
        engaged: whether R63 is currently driving (previous cycle's result).
        hyst_kwh: release band, kWh.
        drainable_kwh: how much the battery can still give (soc - the floor it
            may drain to). <= 0 releases regardless of the deadline: there is no
            headroom left to make, so firing achieves nothing. None skips the
            check (pure-function callers testing the deadline alone).

    Returns:
        bool — True if the drain cannot be completed in the time remaining.
    """
    if headroom_needed_kwh <= 0:
        return False
    # Nothing left in the battery to shed -> the question is moot. Forcing Max
    # Export here overrides the Schmitt band's correct Hold with an instruction
    # that cannot be carried out. Observed live 2026-07-29 07:39: SOC 0.54 kWh
    # sitting exactly on drain_above 0.54, policy reading "active Drain
    # (override max_export)" with 0.00 kWh drainable.
    if drainable_kwh is not None and drainable_kwh <= 0:
        return False
    if engaged:
        return headroom_needed_kwh > (max_sheddable_kwh - hyst_kwh)
    return headroom_needed_kwh > max_sheddable_kwh


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
    global_scale=1.0,
):
    """
    Integrate overflow energy from Solcast per-slot forecast (R53, v20).

    Replaces compute_solar_overflow's clear-sky `scale × sin(elev)` model.
    Preserves Solcast's day-shape (cloud, rain, ramp) by reading per-slot
    pv_estimate directly instead of fitting a single scalar to peak.

    For each integration step, looks up the Solcast 30-min slot containing
    that UTC time, takes the band's kW value, then:
        overflow_step = max(0, pv_kw - effective_load_kw - dno_limit) × step_h

    `global_scale` multiplies pv_kw across the WHOLE integration — distinct from
    `calibration_ratio`, which is windowed (below). Added 2026-08-11 for RD31,
    which had been passing its whole-day ratio into `calibration_ratio` and so
    only ever scaled the first 30 minutes: on an ~8 h remaining window that is
    ~6% of the slots, and `overflow_tracking` came out equal to `overflow_p50`
    (live 09:39, day_ratio 0.694, both 16.43). The two compose if both are
    given; RD31 passes calibration_ratio=1.0 because multiplying a 30-minute
    ratio by a whole-day one would double-count.

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
    capped_global = max(0.0, global_scale)
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
        # Whole-day scale: EVERY slot (RD31). Must come before the windowed
        # calibration so the two compose rather than one masking the other.
        if capped_global != 1.0:
            pv_kw = pv_kw * capped_global
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
            # str() first: HA hands this back as a DATETIME, not the ISO string
            # the slice assumes, and a datetime is not subscriptable. Both
            # "2026-08-05T11:00..." and "2026-08-05 11:00..." slice to "11:00"
            # at [11:16], so one normalisation covers both shapes.
            ps = str(slot["period_start"])
            time_part = ps[11:16]  # "HH:MM"
            h, m = int(time_part[:2]), int(time_part[3:5])
            local_h = h + m / 60.0 + 0.25  # mid-point of 30-min slot
            utc_h = local_h - local_offset_hours
        except (TypeError, ValueError, IndexError, KeyError):
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
                # See p90_scale_from_forecast: period_start arrives as a datetime
                # from HA, and TypeError was not caught, so this silently
                # degraded every band to the (0, 0, cached_p90) fallback.
                ps = str(slot["period_start"])
                time_part = ps[11:16]
                h, m = int(time_part[:2]), int(time_part[3:5])
                local_h = h + m / 60.0 + 0.25
                utc_h = local_h - local_offset_hours
            except (TypeError, ValueError, IndexError, KeyError):
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
