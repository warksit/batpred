# -----------------------------------------------------------------------------
# Curtailment Manager Plugin for Predbat — v18
# Solar-geometry floor algorithm to eliminate solar curtailment
#
# Works WITH the HA automation (curtailment_manager_dynamic_export_limit):
#   - Plugin (5-min): computes floor + dispatch policy, publishes sensors
#   - HA heartbeat (~1min): sole SIG register writer, acts on input_select.sig_dispatch_policy
#
# Control model (v30, DC-coupled SIG):
#   CM's ONLY job is minimising curtailment, £-aware. Predbat owns everything else
#   (price, evening export, saving sessions, overnight reserve). So CM has the wheel
#   ONLY inside the curtailment window: pre-PV drain (R52/R62 headroom) → real-time
#   overflow management → release at safe_time (R6/RD6).
#   Driving:  policy = Max Export / Hold Battery / Solar Charge Battery, read_only=True
#             (base.set_read_only suppresses Predbat — the CM↔Predbat mutex, R3).
#   Handback: policy = Predbat, read_only=False (safe_time / low-SOC handover / off).
#
# Activation (R5): BOTH must be true:
#   1. remaining_overflow > 0 — solar geometry curve predicts overflow
#   2. solcast_remaining - load_remaining > (soc_max - soc_kw) — battery fills
#
# Floor (R9): soc_max - overflow_integral × 1.0
#   overflow_integral = ∫ max(0, scale × sin(elev) - base_load - DNO) dt
#   integrated from now to safe_time
#
# Scale (R42): p90_peak / sin(elev_at_p90_peak) — near-perfect day worst case
#   Floor always uses p90_scale. actual_scale only used for safe_time (R21/R43).
#
# Safe time (R19): when scale × sin(elev) < DNO + base_load
#   Deactivate at safe_time: restore MSC, Predbat fills battery (R6)
#
# Floor ratchet (R11): floor can only rise within a day — headroom cannot be reclaimed
# -----------------------------------------------------------------------------

import json
import math
import os
from collections import deque
from datetime import datetime, timezone

from curtailment_calc import (
    apply_no_surplus_drain_hold,
    compute_morning_gap,
    compute_remaining_overflow,
    compute_solar_overflow,
    compute_solcast_overflow,
    compute_expected_overflow,
    compute_p10_recovery_floor,
    compute_max_sheddable,
    compute_overflow_fits_margin,
    required_headroom_kwh,
    smooth_overflow_samples,
    drain_deadline_breached,
    compute_effective_export_cap,
    compute_charge_below,
    DEEP_DISCHARGE_FLOOR_KWH,
    compute_drain_above,
    compute_drain_above_source,
    classify_forecast_tracking,
    estimate_session_end_kwh,
    compute_proposed_phase,
    phase_to_policy,
    POLICY_MAX_EXPORT,
    compute_pre_pv_target,
    compute_floor_with_source,
    compute_session_reserve,
    should_defer_to_charge,
    compute_pv_start_time,
    p90_scale_from_forecast,
    p_scales_from_forecast,
    simulate_soc_trajectory,
    smooth_load_forecast,
    solar_elevation,
    compute_release_time,
    MIN_BASE_LOAD_KW,
)
from plugin_system import PredBatPlugin

# SIG entity names (Mum's system) — read-only monitoring inputs.
# (Control-register writes were removed with the legacy apply() path; the HA
# heartbeat/guard own inverter control now.)
SIG_PV_POWER = "sensor.sigen_plant_pv_power"
SIG_LOAD_POWER = "sensor.sigen_plant_consumed_power"
SIG_GRID_EXPORT_POWER = "sensor.sigen_plant_grid_export_power"
# Live plant SOC — used for fail-closed (Predbat defaults missing soc to 0.0,
# which looks like "battery empty" and drove a night re-take on 2026-07-29).
SIG_BATTERY_SOC_PCT = "sensor.sigen_plant_battery_state_of_charge"

# HA input helper entity IDs
HA_ENABLE = "input_boolean.curtailment_manager_enable"

# v30 (DC-coupled) policy control — RD9. Gated behind its own flag (default off)
# so the plugin can be DEPLOYED dormant and staged-enabled separately from the
# legacy curtailment_manager_enable.
SIG_POLICY_CONTROL_ENABLE = "input_boolean.sig_plugin_policy_control"
# RD13 manual override: when on (and the gate is on) the plugin keeps the machine
# LIVE (heartbeat + read_only) but STOPS writing the policy select, so a hand-set
# policy sticks instead of being overwritten every cycle. Off = automated control.
# RD13a (2026-07-28): manual override is ONE entity. Override is active iff the
# select is anything but "Off"; the value IS the policy to hold.
#   Off / Max Export / Hold Battery / Solar Charge Battery
# NO Predbat option — handing back is the plugin's decision (RD6), not a manual
# mode, and "Off" already means "you decide".
#
# The old input_boolean.sig_manual_override is GONE. It was redundant state
# derivable from the select, so all it could add was divergence: the select
# reading "Max Export" while the boolean said off (plugin quietly back in
# control), or the reverse. A first attempt kept both and bridged them with an
# automation — a shim for a problem that only existed because of the second entity.
SIG_OVERRIDE_SELECT = "input_select.sig_override"
OVERRIDE_OFF = "Off"
SIG_POLICY_SELECT = "input_select.sig_dispatch_policy"
SIG_KEEP_FLOOR_HELPER = "input_number.sig_keep_floor_pct"
# v32 (2026-07-21): single drain-floor helper — the ONE SOC below which CM stops
# selling the battery to grid. Replaces the three coincident 5% floors (heartbeat
# sig_hard_floor_pct, plugin sig_low_soc_handover_pct, hardcoded keep-clamp). It
# governs both the low-SOC→MSC handover and the published keep-floor minimum, and
# the heartbeat reads the same helper for its dispatch≤PV clamp. Default 2.8% =
# the deep-discharge floor (0.5 kWh), so the pre-dawn drain can reach it; it can
# only ever RAISE the floor above 2.8% (drain_above is hard-floored at 0.5 kWh).
# NB the hardware discharge cut-off is 0% by design (rails at device extremes),
# so this software floor is the operational protection, not the BMS.
SIG_DRAIN_FLOOR_HELPER = "input_number.sig_drain_floor_pct"
DEFAULT_DRAIN_FLOOR_PCT = 2.8
# Plugin owns the single-writer handoff: EXACTLY ONE of these two automations is
# enabled at any time, so the two writers can never overlap.
#   CM driving   → heartbeat ON,  mapper OFF
#   handed back  → heartbeat OFF, mapper ON  (+ parked in EMS-MSC, RD2 — never app modes)
# The mapper stays a plain stock automation with no mutex condition of its own;
# being disabled IS the mutex.
#
# 2026-07-27: the mapper had been disabled since the 2026-07-15 swap and nothing
# re-enabled it, so Predbat had no control path at all — it asked for Discharging
# twice overnight on 07-26 and select.sigen_plant_remote_ems_control_mode never
# moved. Toggling it here is what closes that hole.
# 2026-07-28: the Predbat->SIG chain is THREE automations, not one. Toggling only
# the mode mapper left the other two live, and they write PLANT registers:
#   predbat_requested_mode_action        -> EMS control mode + grid_import_limitation
#   predbat_max_discharging_limit_action -> ess_max_discharging_limit  (from
#                                           input_number.discharge_rate)
#   predbat_max_charging_limit_action    -> ess_max_charging_limit     (from
#                                           input_number.charge_rate)
# Predbat's Freeze Charging sets discharge_rate=0, so the discharging mapper wrote
# ess_max_discharging_limit=0 at 04:01:14 — eight seconds BEFORE the mode mapper —
# and stayed enabled while CM drove, hardware-locking the battery for 4.5 hours.
# The mutex must disable the whole CHAIN, not just its front door.
SIG_HEARTBEAT_AUTOMATION = "automation.sig_dispatch_heartbeat"
PREDBAT_MAPPER_AUTOMATIONS = (
    "automation.predbat_requested_mode_action",
    "automation.predbat_max_discharging_limit_action",
    "automation.predbat_max_charging_limit_action",
)
# Predbat's own inputs — set back to neutral before freezing its mapper chain, so
# Predbat's mappers undo whatever registers they wrote (see _neutralise_predbat).
PREDBAT_MODE_SELECT = "input_select.predbat_requested_mode"
PREDBAT_MODE_DEMAND = "Demand"
PREDBAT_DISCHARGE_RATE = "input_number.discharge_rate"
PREDBAT_CHARGE_RATE = "input_number.charge_rate"
SIG_RATED_DISCHARGE_SENSOR = "sensor.sigen_plant_ess_rated_discharging_power"
SIG_RATED_CHARGE_SENSOR = "sensor.sigen_plant_ess_rated_charging_power"
SIG_EMS_MODE_SELECT = "select.sigen_plant_remote_ems_control_mode"
SIG_EMS_MODE_MSC = "Maximum Self Consumption"
DEFAULT_KEEP_FLOOR_PCT = 38.0  # overnight reserve default on handback (RD10)
POLICY_PREDBAT = "Predbat"
# v31 floor/handback (2026-07-19): saving-session reserve (Octopus sensor CM can
# read directly) + early-handback buffer (fit p90 overflow with this to spare).
SIG_SAVING_SESSION = "binary_sensor.octopus_energy_a_4ba7c915_octoplus_saving_sessions"
# RD14c: DISPATCH is driven off the Octoplus CALENDAR, not the binary sensor —
# Octopus publishes the sensor ~1 min late at each edge, which cost 5 min 46 s
# of selling past the paid window on 2026-07-28. The heartbeat reads this entity;
# the display layer MUST read the same one, or the card can disagree with the
# dispatcher at exactly the moments that matter.
SIG_SAVING_SESSION_CALENDAR = "calendar.octopus_energy_a_4ba7c915_octoplus_saving_sessions"
HA_EARLY_HANDBACK_BUFFER = "input_number.curtailment_early_handback_buffer_kwh"
EARLY_HANDBACK_BUFFER_DEFAULT = 1.5
# v32: the "overflow fits headroom" buffer is now a Hold gate (not a deactivate
# trigger). Hysteresis so we don't flap Hold<->Drain right at the boundary.
FITS_HYST_KWH = 0.5

PREDICT_STEP = 5
SOC_MARGIN_KWH = 0.5

# SIG/Solcast sensor entities
SIG_DAILY_PV = "sensor.sigen_plant_daily_pv_energy"  # 2026-07-15 swap: PV on SIG MPPTs, third_party (SMA) sensor dead
PREDBAT_PV_TODAY = "sensor.predbat_pv_today"
SOLCAST_TODAY = "sensor.solcast_pv_forecast_forecast_today"
SOLCAST_REMAINING = "sensor.solcast_pv_forecast_forecast_remaining_today"

# Safety factor: 20% extra headroom on overflow estimate (R9).
# 1.2 provides ~1.8 kWh of additional buffer beyond R45's 10% cap, giving
# ~3.6 kWh total protection against LoadML over-prediction errors (~2 kW of
# phantom load over a 2h window). Yesterday's LoadML contamination was ~6 kWh
# — this doesn't fully cover that but substantially reduces breach probability.
# R64: trailing window for the overflow-estimate median. 30 min ~= 6 plugin
# cycles — long enough to reject single-slot Solcast revisions, short enough to
# follow the day burning off (measured lag ~+0.3 kWh on a falling series).
OVERFLOW_SMOOTH_WINDOW_MIN = 30

# R9 overflow safety factor. 1.2 -> 1.05 on 2026-07-30 (user decision).
#
# WHY 1.05 AND NOT 1.2: this multiplies an already-conservative input. Overflow
# is fed from the p90 Solcast band, AND overflow is an integral above a
# threshold, so forecast conservatism is amplified before the factor applies at
# all. Measured across the April fixture replay: a 13% generation over-forecast
# became a 36% overflow over-forecast (3.36x leverage), and actual overflow
# never once exceeded the p90-derived estimate in 11 days. 1.2 on top reserved
# roughly double the headroom actually needed -- and over-reserving is not free,
# it is paid for as a deeper pre-dawn drain and the overnight import after it.
#
# HONEST CAVEAT: those fixtures were measured through the AC-coupled SMA, which
# clipped PV above the inverter ceiling and so UNDERSTATED actual overflow,
# flattering p90. The first DC-coupled day (19 Jul) showed only 16% margin vs
# p90 against 56% mean in April. 1.05 is therefore a deliberate step, not a
# settled number.
#
# TO REFINE -- use the meters, do not re-derive from fixtures. Since 2026-07-29
# actual overflow is metered exactly by
#     sensor.curtailment_overflow_power  -> _energy (integral) -> _daily (utility_meter)
# which clips at native sensor resolution and only then integrates, so the daily
# total survives HA's hourly downsampling. Reconstructing from 5-minute
# statistics instead understated a broken-cloud day by 63%, and that data
# expires with the ~10-day recorder window.
#
# Compare sensor.curtailment_overflow_daily (actual) against the daily max of
# sensor.predbat_curtailment_overflow_p90 (forecast) over a few weeks of
# DC-coupled days: the factor should cover the worst observed actual/p90 ratio
# with a little margin. See test_R9_overflow_safety_factor_is_1_05.
OVERFLOW_SAFETY_FACTOR = 1.05

# Human labels for _policy_override (dashboard / reason string).
# Internal codes stay as keys for logic; never show them raw on Why This Mode.
#   no_drain   = surplus already fits (or past safe_time): suppress Max Export only;
#                Charge still allowed for the evening reserve. Not a user mode.
#   hold       = force flat (e.g. pre-PV wait after drain)
#   max_export = force drain (e.g. R63 last chance for headroom)
OVERRIDE_LABELS = {
    "no_drain": "surplus fits",
    "hold": "holding flat",
    "max_export": "must drain",
}
# `no_drain` covers two different situations. "surplus fits" is a comparison
# against the forecast overflow — a non-statement once that forecast is zero
# (observed on the card 2026-08-03 19:29: "surplus fits · fits · 53% spare"
# with overflow_p90 = 0.0, i.e. nothing to fit and nothing to spare it against).
NO_OVERFLOW_LABEL = "no overflow left"

# O1 (2026-08-04): sundown requires the sun to actually be down. Ten nights of
# live data separate perfectly at the first handback — flap nights 9.4-12.7 deg,
# clean nights 2.5-5.4 deg (real site, lat 52.31N). 8.0 sits inside the 4.0 deg
# gap: it blocks every flap-triggering moment and permits every clean one.
SUNDOWN_ELEV_DEG = 8.0

# v33 (2026-08-06): fraction of the battery held back through the DAWN GAP — the
# window between PV START and PV MEETING LOAD, during which the battery is still
# the only thing standing between house load and the grid. Released by measured
# crossover, so over-reserving is self-correcting and the number needs no
# precision: 10% of 18.08 kWh = 1.81 kWh against a measured August need of ~0.6
# kWh, and releasing it costs ~27 min at the 3.68 kW cap against ~3 h of slack
# before overflow starts. See _dawn_floor_kwh.
DAWN_RESERVE_FRACTION = 0.10

# Do not hand back in the run-up to a joined saving session — see _session_imminent.
# 30 min comfortably covers the 5-minute plugin cycle plus the ownership handover,
# without pinning CM active for a session hours away.
SESSION_IMMINENT_MINS = 30.0

# Human labels for the arm of compute_drain_above that set the Headroom Floor.
# The card REPORTS these (Charter: never re-derive a second decision).
DRAIN_SOURCE_LABELS = {
    "session_protect": "saving session",
    "overflow_floor": "P90 overflow",
    "deep_floor": "deep-discharge floor",
    "dawn_reserve": "dawn reserve (PV not yet covering load)",
    "reserve": "inverter reserve",
    "inactive": "CM inactive",
}

# v19 tapered cap (R45): reserved headroom = min(MAX_RESERVED_KWH, remaining_overflow).
# At peak overflow the buffer clamps at 1.8 kWh (10% of 18.08 kWh = current R45 cap).
# As overflow winds down toward safe_time, buffer tapers to 0 and max_target_soc
# approaches soc_max — battery reaches ~100% before handoff to MSC, rather than
# capping at 90% and relying on post-release MSC to fill 90→100%.
MAX_RESERVED_KWH = 1.8

# v20 dynamic buffer reduction (R49): on confirmed-cloudy afternoons, scale the
# reserved buffer down. Solcast over-forecasted the day → less true overflow
# headroom is needed → battery aims higher.
#   Trigger gate: minutes_now >= 14:00 local (post-DHW, peak likely past)
#   Cumulative ratio: SIG_DAILY_PV / (SOLCAST_TODAY - SOLCAST_REMAINING) < 0.9
#   Recent ratio:    last-hour delta_actual / delta_solcast_so_far < 0.95
#   Both must hold — recent_ratio guards against clouds clearing late afternoon.
# When triggered, effective_max_reserved = max(0.5, MAX_RESERVED_KWH × 0.7) = 1.26.
BUFFER_REDUCE_FACTOR = 0.7
BUFFER_REDUCE_FLOOR_KWH = 0.5

# R55 (v20): overnight_target = morning_gap × (1 + safety_pct/100) + soc_keep.
#   morning_gap: forecast load deficit (Σ max(0, load-pv)) until tomorrow's PV
#   safety_pct:  optional extra margin on the load forecast (default 0%)
#   soc_keep:    the existing forecast-error buffer (set by Predbat planning,
#                bumped by cold_weather_plugin on cold nights)
# Published as a sensor; used as floor for effective_keep in calculate(),
# replacing the old "always charge to 100%" behaviour (R57).
HA_OVERNIGHT_SAFETY_PCT = "input_number.curtailment_overnight_safety_pct"
OVERNIGHT_SAFETY_PCT_DEFAULT = 0.0  # default 0%; lean on soc_keep as buffer
BUFFER_REDUCE_MIN_LOCAL_HOUR = 14
BUFFER_REDUCE_CUMULATIVE_RATIO = 0.9
BUFFER_REDUCE_RECENT_RATIO = 0.95
BUFFER_REDUCE_MIN_SOLCAST_KWH = 10.0
PV_HISTORY_LEN = 15  # 15 × 5 min = 75 min — enough room for 60-min lookback

# v21 confidence-weighted overflow (R50): blend three forecast bands by Solcast
# analysis.confidence. Tunable via input_number helpers.
#
# R50a (2026-07-28): the blend is RETIRED as the live path — the floor uses
# overflow_p90 per R7/R42/R43. HIGH now defaults to 1.0, which the gate in
# _expected_overflow() reads as "never blend". Set
# input_number.curtailment_confidence_high below 1.0 to re-enable R50 from the
# dashboard with no code change; the blend function and both helpers are intact.
#
# Why: R25 derives overflow from solar geometry precisely because forecast data is
# too noisy, R7 says p90 only, R42 picks p90 as the worst case, R43 is deliberately
# asymmetric toward MORE drain, and R11 forbids ever lowering the floor — headroom
# is cheap early and impossible late. Blending toward p10 assumes NO overflow, which
# is the one assumption R25 rules out. Replaying R50's own justification day
# (2026-04-28) shows the p90 floor stops the drain at 39.9%, not the 1.9% it cites,
# so p90 was never the cause; over-drain protection is R59a's job and now works.
CONFIDENCE_BLEND_OFF = 1.0  # HIGH at/above this → pure p90, no blending (R50a)
CONFIDENCE_HIGH_DEFAULT = 1.0  # R50a: was 0.85. ≥ CONFIDENCE_BLEND_OFF → always p90
CONFIDENCE_LOW_DEFAULT = 0.60  # only used when the blend is re-enabled
# Default to HIGH when Solcast doesn't expose confidence — preserves
# pre-R50 behaviour (always-p90) on environments without the attribute
# (tests, integrations that don't pass it through). Real Solcast always
# provides analysis.confidence, so this default is rarely used in prod.
CONFIDENCE_DEFAULT = 0.9
HA_CONFIDENCE_HIGH = "input_number.curtailment_confidence_high"
HA_CONFIDENCE_LOW = "input_number.curtailment_confidence_low"

# v22 R52 pre-PV drain: activate before sunrise on confirmed-overflow days
# so we drain at full DNO rate while drain capacity is uncontested by PV.
# Two-stage drain: pre-PV target = soc_keep + buffer%; post-PV target = R50 floor.
HA_GSHP_CH_ACTIVE = "input_boolean.gshp_ch_active"
HA_PRE_PV_BUFFER_PCT = "input_number.curtailment_pre_pv_buffer_pct"
PRE_PV_BUFFER_PCT_DEFAULT = 20.0
PRE_PV_OVERFLOW_THRESHOLD_KWH = 1.0  # Min forecast overflow to bother with pre-PV drain
PV_START_THRESHOLD_KW = 0.5  # PV "started" when scale × sin(elev) ≥ this

# State persistence file (Bug 2 / R46): preserves _peak_pv, _peak_pv_time and the
# R48/R49 latches across plugin restarts within the same day. `floor_ratchet` is no
# longer written (R11 removed 2026-07-28); stale keys in existing files are ignored.
STATE_FILE_NAME = "curtailment_state.json"


class CurtailmentPlugin(PredBatPlugin):
    """
    Curtailment manager v17 — solar geometry floor algorithm.

    Floor derived from integral of overflow above DNO on a worst-case (p90) solar curve.
    Scale from Solcast p90 peak at activation; updated downward if actual peak is lower.
    Deactivates at safe_time, handing back to Predbat MSC to fill the battery.
    """

    # Run before cold_weather (priority 200) which additively boosts best_soc_keep.
    # With curtailment setting the target value first and cold weather boosting on top,
    # the cold weather floor for overnight GSHP load is preserved on overflow + cold days.
    priority = 10

    def __init__(self, base):
        super().__init__(base)
        self._dno_limit = 4.0
        self.last_phase = None
        # Day's peak PV (actual observed) for scale calibration (R43)
        self._peak_pv = 0.0
        self._peak_pv_time = 0
        # p90 scale from Solcast (set at activation, stable through day, R42)
        self._p90_scale = 0.0
        # Retained so the phase sensor can report which band today's sky is
        # tracking — the plugin blends three overflow integrals and never said
        # which one the day actually resembled.
        self._p10_scale = 0.0
        self._p50_scale = 0.0
        self._p90_peak_kw = 0.0
        # Diagnostics published to sensors
        self._remaining_overflow = 0.0
        self._safe_time_str = "none"
        self._floor_scale = 0.0
        self._safe_scale = 0.0
        self._actual_scale = 0.0
        self._last_decision = "init"
        # R4 hysteresis: True while deferring to charge window (Bug 6).
        # Released only when SOC >= soc_keep + 0.2.
        self._r4_deferring = False
        # R48 (Bug 8): one-way ratchet for relaxed soc_keep. Requires two-phase
        # transition — battery must be observed BELOW soc_keep this day
        # (_keep_drained_today) before _keep_recovered can latch on SOC rising
        # back to soc_keep. Without the drain-first guard, the latch fires at
        # midnight rollover when battery is at 100% overnight, defeating R48.
        # _r48_engaged_today: once R48 has fired today, latch on so it doesn't
        # toggle on flickering pv_covering threshold (cloudy morning).
        self._keep_drained_today = False
        self._keep_recovered = False
        self._r48_engaged_today = False
        # R63 drain-deadline engagement (hysteresis, NOT a latch — draining is
        # what clears the breach, so the loop must stay closed).
        self._r63_engaged = False
        self._r63_needed_kwh = 0.0
        self._was_draining = False  # R16a Schmitt state
        self._actual_pv_kw = 0.0
        # v30 policy control (RD9): split thresholds stored by publish() this cycle
        self._charge_below = 0.0
        self._drain_above = 0.0
        # True while the plugin is actively driving the dispatch policy; used to
        # hand back exactly once on the active->off edge (RD10) without clobbering
        # manual/Predbat control on ordinary off cycles.
        self._policy_driving = False
        # R3 read_only mutex: None = unknown (adopt live state on first run), then
        # tracks whether WE set base.set_read_only so we only ever clear our own.
        self._read_only_set = None
        self._session_reserve_kwh = 0.0
        # v32 evening lifecycle (2026-07-20). The plugin stays active to sundown
        # (no early-handback / no safe_time deactivation); the policy override says
        # what to do inside the window when the SOC-vs-band Schmitt is not the right
        # call: "hold" (overflow already fits headroom, or past safe_time — battery
        # flat, sell surplus at the cap, never MSC round-trip), "max_export" (a
        # saving session is live — dump the reserve at the cap), or None (Schmitt
        # makes room). _overflow_fits_latched adds hysteresis so Hold/Drain doesn't
        # flap at the fits boundary (observed 2026-07-20). _session_protect_kwh
        # raises drain_above ahead of a known session so the reserve isn't drained.
        self._policy_override = None
        self._overflow_fits_latched = False
        self._session_active = False
        self._session_protect_kwh = 0.0
        # Which arm of compute_drain_above set the published Headroom Floor.
        self._drain_above_source = "overflow_floor"
        # O1: once sundown is declared below the elevation gate, stay down for the
        # day. Persisted — an evening restart otherwise re-takes the wheel (live
        # 2026-08-03, "PHASE none -> active" at 20:05 after a deploy).
        self._sundown_latched = False
        # v33 dawn-gap reserve: released (one-way, per day) the first time measured
        # PV >= load. Persisted — a deploy during the gap would otherwise re-arm a
        # reserve we had already correctly spent, or vice versa.
        self._dawn_released = False
        # Forecast house load across the dawn gap, carried out of the pre-PV path so
        # the reserve survives the phase boundary. MIN_BASE_LOAD_KW for one hour is
        # the fallback when the pre-PV path has not run this day.
        self._dawn_load_kwh = MIN_BASE_LOAD_KW * 1.0
        # v32 dawn-flap latch: once the pre-PV drain fires, CM owns the day. When the
        # drain completes but PV hasn't arrived (actual_pv < 0.1) and overflow is still
        # forecast, HOLD active instead of handing back to Predbat — otherwise the
        # dawn 0.1kW PV boundary flaps off↔active (observed 2026-07-21 05:39-05:52).
        self._pre_pv_engaged_today = False
        # v32.2 (2026-07-22): start-latch for the pre-PV drain. Once the drain_start
        # timing gate first passes, commit to draining to target — don't re-gate on
        # `now < drain_start` each cycle. drain_start_utc tracks `now` as SOC drains
        # (drain at ~dno shrinks drain_minutes at ~60min/h), so re-gating flips on
        # noise → Max Export↔Hold flapping (observed 2026-07-22 04:27-05:47 BST).
        self._pre_pv_drain_started = False
        # Single-writer handoff: True while CM controls (heartbeat enabled). None =
        # unknown on first run (adopt from read_only). Edge-triggered so we don't
        # spam turn_on/off or overwrite a manual EMS mode every cycle.
        self._cm_controlling = None
        # Caching for on_before_plan
        self._cached_keep = None
        self._cached_at = 0
        self._cached_offset = None
        # Caching for tomorrow forecast
        self._tomorrow_cache = None
        self._tomorrow_cache_at = 0
        # Floor scale from previous cycle (for R11-over-R43 precedence, Bug 3)
        self._last_floor_scale = 0.0
        # PV history for v20 dynamic buffer reduction (R49). Tuples of
        # (minutes_now, solcast_so_far_kwh, sig_daily_pv_kwh) appended each
        # cycle so we can compute a 60-min recent ratio. Not persisted — after
        # plugin restart we wait one cycle window to re-establish.
        self._pv_history = deque(maxlen=PV_HISTORY_LEN)
        # Diagnostics for buffer reduction decision
        self._buffer_reduced = False
        self._effective_max_reserved = MAX_RESERVED_KWH
        # R50 diagnostics: three overflow bands and confidence used to blend
        self._overflow_p10 = 0.0
        self._overflow_p50 = 0.0
        self._overflow_p90 = 0.0
        # R64: raw (unsmoothed) bands, kept so the dashboard can show the
        # divergence — a smoothed value that silently differs from its input is
        # exactly the "silent mechanism" the Charter forbids.
        self._overflow_raw = {"p10": 0.0, "p50": 0.0, "p90": 0.0}
        # (minute, p10, p50, p90) samples for the rolling median.
        self._overflow_history = deque(maxlen=24)
        self._confidence = CONFIDENCE_DEFAULT
        # R55: overnight_target cached from on_before_plan, used by calculate()
        # as the effective_keep floor. None until first plan cycle has run.
        self._overnight_target_kwh = None
        # Cached (value, attrs) tuple for republishing overnight_target on
        # cache-hit paths in on_before_plan.
        self._cached_overnight = None
        # R60: rolling cap samples for effective_dno (last 6 = 30 min @ 5-min cycles)
        self._cap_samples = deque(maxlen=6)
        # R60: full-day cap samples — averaged at day rollover into _yesterday_cap_avg
        self._cap_samples_full_day = []
        # R60: yesterday's daytime mean cap (kW) — persisted, used as fallback
        self._yesterday_cap_avg = None
        # R60: current cycle's effective DNO (diagnostic + use in overflow calls)
        self._effective_dno = 4.0
        # R59: current cycle's P10 recovery floor (diagnostic + use in R54)
        self._p10_recovery_floor = 0.0
        # R59 inputs (diagnostic — the terms feeding p10_recovery)
        self._p10_pv_remaining_kwh = 0.0
        self._p50_pv_remaining_kwh = 0.0
        self._load_remaining_kwh = 0.0
        # Diagnostic: which term of R54 (or which off-path) drove the published target.
        # User-facing strings (shown directly on dashboard tile):
        #   'Overnight Need'      — R54 effective_keep binding
        #   'Curtailment Buffer'  — R54 overflow_floor binding
        #   'P10 Recovery'        — R59 outer-max binding (cloudy-afternoon recovery)
        #   'Reserve'             — hardware floor binding
        #   'Battery Full'        — clamped to soc_max
        #   'Pre-PV Drain'        — R52 pre-sunrise drain target
        #   'Overnight Reserve'   — off path, target = overnight_target_kwh
        #   'No Forecast'         — off (Solcast unavailable)
        #   'Predbat Charging'    — R4 yielded to Predbat charge window
        #   'Initialising'        — plugin not yet run
        self._floor_source = "Initialising"
        # Diagnostic: components of the R54 floor formula at last cycle
        self._effective_keep_kwh = 0.0
        self._overflow_floor_kwh = 0.0
        # Date this state belongs to — lets us detect day rollover in calculate()
        self._state_date = None
        # Silent-fallback audit (unknown-unknowns item 4, 2026-07-07): every
        # degraded-input path logs ONCE per day via _log_once so schema drift
        # in dependencies (Solcast attrs, sensors) can't change behaviour
        # invisibly. Keys re-arm at day rollover.
        self._logged_once = set()
        # Load persisted state (Bug 2) — recovers peak_pv / ratchet across restart
        self._load_state()

    def _state_file_path(self):
        """Return state file path, or None if persistence is not configured.

        Returns None for test environments where base.config_root isn't set —
        avoids cross-test pollution when fresh plugin instances all read/write
        the same file.
        """
        config_root = getattr(self.base, "config_root", None)
        if not config_root:
            return None
        return os.path.join(config_root, STATE_FILE_NAME)

    def _load_state(self):
        """Load persisted state from disk if it matches today's date (Bug 2)."""
        path = self._state_file_path()
        if path is None or not os.path.exists(path):
            return
        try:
            with open(path, "r") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            try:
                self.log("Curtailment: state file invalid/corrupt at {} — ignoring ({})".format(path, exc))
            except Exception:
                pass
            return
        today = datetime.now().strftime("%Y-%m-%d")
        if data.get("date") != today:
            return  # stale — belongs to a previous day, ignore
        self._peak_pv = float(data.get("peak_pv_kw", 0.0))
        self._peak_pv_time = int(data.get("peak_pv_time", 0))
        self._last_floor_scale = float(data.get("last_floor_scale", 0.0))
        self._keep_recovered = bool(data.get("keep_recovered", False))
        self._keep_drained_today = bool(data.get("keep_drained_today", False))
        self._r48_engaged_today = bool(data.get("r48_engaged_today", False))
        self._sundown_latched = bool(data.get("sundown_latched", False))
        self._dawn_released = bool(data.get("dawn_released", False))
        self._dawn_load_kwh = float(data.get("dawn_load_kwh", MIN_BASE_LOAD_KW * 1.0))
        # Restore _pv_history (R49) — without this, every restart kills v20
        # buffer-reduction for the rest of the day until 60 min of fresh
        # samples accumulate.
        history = data.get("pv_history") or []
        self._pv_history.clear()
        for entry in history:
            if isinstance(entry, list) and len(entry) == 3:
                try:
                    self._pv_history.append((int(entry[0]), float(entry[1]), float(entry[2])))
                except (ValueError, TypeError):
                    continue
        # R60: yesterday's mean + today's accumulated samples
        try:
            yc = data.get("yesterday_cap_avg")
            self._yesterday_cap_avg = float(yc) if yc is not None else None
        except (ValueError, TypeError):
            self._yesterday_cap_avg = None
        cap_samples = data.get("cap_samples") or []
        self._cap_samples.clear()
        for s in cap_samples[-6:]:
            try:
                self._cap_samples.append(float(s))
            except (ValueError, TypeError):
                continue
        self._cap_samples_full_day = []
        for s in data.get("cap_samples_full_day") or []:
            try:
                self._cap_samples_full_day.append(float(s))
            except (ValueError, TypeError):
                continue
        self._state_date = today
        try:
            self.log(
                "Curtailment: restored state from {} (peak={:.2f}kW, pv_history={} entries)".format(
                    path,
                    self._peak_pv,
                    len(self._pv_history),
                )
            )
        except Exception:
            pass

    def _save_state(self):
        """Persist state to disk atomically (tmp + rename).

        The write goes to `path.tmp` first; only on full success do we rename
        over the main file. A crash mid-write leaves the .tmp file (which may
        be partial/corrupt) but the main file is untouched. POSIX rename is
        atomic, so readers always see either the old or the new file, never
        a torn one.
        """
        path = self._state_file_path()
        if path is None:
            return
        today = datetime.now().strftime("%Y-%m-%d")
        data = {
            "date": today,
            "peak_pv_kw": self._peak_pv,
            "peak_pv_time": self._peak_pv_time,
            "last_floor_scale": self._last_floor_scale,
            "keep_recovered": self._keep_recovered,
            "keep_drained_today": self._keep_drained_today,
            "r48_engaged_today": self._r48_engaged_today,
            "sundown_latched": self._sundown_latched,
            "dawn_released": self._dawn_released,
            "dawn_load_kwh": self._dawn_load_kwh,
            "pv_history": [list(entry) for entry in self._pv_history],
            "yesterday_cap_avg": self._yesterday_cap_avg,
            "cap_samples": list(self._cap_samples),
            "cap_samples_full_day": list(self._cap_samples_full_day),
        }
        tmp_path = path + ".tmp"
        try:
            with open(tmp_path, "w") as f:
                json.dump(data, f)
            os.replace(tmp_path, path)
            self._state_date = today
        except OSError:
            # Best-effort cleanup of any partial tmp file
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass

    def _reset_for_new_day(self):
        """Reset in-memory daily state. Called when calculate() detects day rollover."""
        # R60: roll today's full-day mean into yesterday's avg for tomorrow.
        # Filtered to during-PV samples means this represents the typical
        # effective ceiling under PV/voltage conditions, not idle hours.
        if self._cap_samples_full_day:
            self._yesterday_cap_avg = sum(self._cap_samples_full_day) / len(self._cap_samples_full_day)
        self._cap_samples_full_day = []
        self._cap_samples.clear()
        self._peak_pv = 0.0
        self._peak_pv_time = 0
        self._last_floor_scale = 0.0
        self._overflow_history.clear()
        self._r63_engaged = False
        self._r63_needed_kwh = 0.0
        self._was_draining = False  # R16a Schmitt state
        self._keep_recovered = False
        self._keep_drained_today = False
        self._r48_engaged_today = False
        self._overflow_fits_latched = False  # v32 Hold-gate hysteresis latch
        self._sundown_latched = False  # O1 dusk-flap latch
        self._dawn_released = False  # v33 dawn-gap reserve re-arms for the new dawn
        self._dawn_load_kwh = MIN_BASE_LOAD_KW * 1.0
        self._pre_pv_engaged_today = False  # v32 dawn-flap latch
        self._pre_pv_drain_started = False  # v32.2 pre-PV drain start-latch
        self._policy_override = None
        self._logged_once = set()  # re-arm the once-per-day fallback logs
        self._state_date = datetime.now().strftime("%Y-%m-%d")

    def _log_once(self, key, msg):
        """Log a degraded-input/fallback message once per day per key.

        Silent `except: pass` fallbacks let dependency schema drift change
        behaviour with no trace (e.g. Solcast renaming analysis.confidence
        would silently pin the system to permanent p90 mode). One line per
        day per condition makes every fallback visible without log spam.
        """
        if key in self._logged_once:
            return
        self._logged_once.add(key)
        try:
            self.log("Curtailment: FALLBACK [{}] {}".format(key, msg))
        except Exception:
            pass

    def register_hooks(self, plugin_system):
        plugin_system.register_hook("on_update", self.on_update, plugin=self)
        plugin_system.register_hook("on_before_plan", self.on_before_plan, plugin=self)

    def on_before_plan(self, context):
        """Reduce best_soc_keep on sunny days when solar will refill the battery.

        Only reduces, never increases. If there's forecast overflow, the battery
        will be refilled by solar, so soc_keep only needs to cover the morning
        energy gap (load minus PV until solar takes over).
        """
        enabled = str(self.base.get_state_wrapper(HA_ENABLE, default="off")).lower() in ("on", "true")
        if not enabled:
            return context

        minutes_now = getattr(self.base, "minutes_now", 720)

        # v20: cache early-return removed. Morning_gap and Solcast slot calls
        # are cheap, and the 30-min cache was masking stale overnight_target
        # values when pv_forecast_minute_step transitioned from empty (first
        # plan after restart) to populated (subsequent plans). Recompute
        # every plan cycle so the sensor always reflects current state.

        pv_step = getattr(self.base, "pv_forecast_minute_step", {})
        load_step = getattr(self.base, "load_minutes_step", {})
        soc_max = getattr(self.base, "soc_max", 10)
        reserve = getattr(self.base, "reserve", 0)
        forecast_minutes = getattr(self.base, "forecast_minutes", 1440)

        if not pv_step:
            # pv_forecast_minute_step is populated by calculate_plan which runs
            # AFTER on_before_plan, so it's empty here on every cycle. The
            # _refresh_overnight_target() call inside calculate() (on_update
            # hook, runs after calculate_plan) is the canonical source of
            # _overnight_target_kwh and the sensor — do not overwrite either
            # here. Touching them with a soc_keep fallback clobbers the good
            # value from the previous cycle's calculate() and corrupts the
            # floor calc in the next cycle (drives target to ~5%).
            self._publish_offset(0.0, {"original_keep": round(context["best_soc_keep"], 2), "reason": "no_pv_forecast"})
            return context

        dno_limit = self.base.get_arg("export_limit", 4000, index=0) / 1000.0
        today_solar_end = min(forecast_minutes, max(PREDICT_STEP, 23 * 60 - minutes_now))

        # After today's solar hours, use tomorrow's forecast window for overnight planning
        if today_solar_end < 60:
            tomorrow_start = 1440 - minutes_now + 5 * 60  # tomorrow 05:00
            tomorrow_end = 1440 - minutes_now + 23 * 60  # tomorrow 23:00
            tomorrow_end = min(tomorrow_end, forecast_minutes)
            if tomorrow_end > tomorrow_start > 0:
                has_pv = any(pv_step.get(m, 0) > 0 for m in range(tomorrow_start, min(tomorrow_start + 120, tomorrow_end), PREDICT_STEP))
                if has_pv:
                    solar_start = tomorrow_start
                    solar_end = tomorrow_end
                    using_tomorrow = True
                else:
                    self._publish_offset(0.0, {"original_keep": round(context["best_soc_keep"], 2), "reason": "no_tomorrow_pv"})
                    self._cached_keep = context["best_soc_keep"]
                    self._cached_at = minutes_now
                    return context
            else:
                self._publish_offset(0.0, {"original_keep": round(context["best_soc_keep"], 2), "reason": "no_tomorrow_window"})
                self._cached_keep = context["best_soc_keep"]
                self._cached_at = minutes_now
                return context
        else:
            solar_start = PREDICT_STEP
            solar_end = today_solar_end
            using_tomorrow = False

        # Compute morning_gap_load for R26 (best_soc_keep adjustment) only.
        # _overnight_target_kwh and the overnight_target sensor are set by
        # _refresh_overnight_target() in calculate() (sole writer).
        # compute_morning_gap finds sunset/sunrise itself, so we use the
        # full forecast horizon as the upper bound.
        morning_gap_load = compute_morning_gap(
            pv_step,
            load_step,
            start_minute=solar_start,
            end_minute=forecast_minutes,
            step_minutes=PREDICT_STEP,
            values_are_kwh=True,
        )

        # Use trajectory to check if battery will fill
        peak_soc, net_charge, last_danger = simulate_soc_trajectory(
            pv_step,
            load_step,
            soc_max,
            soc_max,
            dno_limit,
            energy_ratio=1.0,
            start_minute=solar_start,
            end_minute=solar_end,
            step_minutes=PREDICT_STEP,
            values_are_kwh=True,
        )

        will_fill = peak_soc > soc_max * 0.90

        if not will_fill:
            self._publish_offset(0.0, {"original_keep": round(context["best_soc_keep"], 2), "will_fill": False, "using_tomorrow": using_tomorrow})
            self._cached_keep = context["best_soc_keep"]
            self._cached_at = minutes_now
            return context

        # R26 (separate from R55): on big-overflow days, reduce best_soc_keep
        # so Predbat doesn't plan unnecessary overnight import to reach a high
        # keep — tomorrow's overflow PV will fill the battery anyway. Uses
        # morning_gap + tiny margin (the historical R26 value), NOT the larger
        # overnight_target (which is for live SOC management, not planning).
        R26_PLAN_MARGIN_KWH = 0.5
        solar_adjusted_keep = max(morning_gap_load + R26_PLAN_MARGIN_KWH, reserve)

        remaining_overflow_total = compute_remaining_overflow(
            pv_step,
            load_step,
            dno_limit,
            start_minute=solar_start,
            end_minute=solar_end,
            step_minutes=PREDICT_STEP,
            values_are_kwh=True,
        )
        current_keep = context["best_soc_keep"]

        headroom_with_current_keep = soc_max - current_keep
        # max_reserved=0: the plan-time keep adjustment deliberately holds no R45
        # reserve — that buffer belongs to the live drain target. An explicit
        # ARGUMENT, so the difference is visible rather than drifting (Charter).
        if required_headroom_kwh(remaining_overflow_total, 0.0, OVERFLOW_SAFETY_FACTOR) <= headroom_with_current_keep:
            self._publish_offset(0.0, {"morning_gap_kwh": round(morning_gap_load, 2), "overflow_kwh": round(remaining_overflow_total, 2), "original_keep": round(current_keep, 2), "reason": "overflow_fits_in_headroom"})
            self._cached_keep = current_keep
            self._cached_at = minutes_now
            return context

        if solar_adjusted_keep < current_keep:
            self.log("Curtailment: reducing best_soc_keep {:.2f} -> {:.2f} kWh (morning_gap={:.2f}, net_charge={:.2f})".format(current_keep, solar_adjusted_keep, morning_gap_load, net_charge))
            context["best_soc_keep"] = solar_adjusted_keep
            self._publish_offset(round(solar_adjusted_keep - current_keep, 2), {"morning_gap_kwh": round(morning_gap_load, 2), "net_charge_kwh": round(net_charge, 2), "original_keep": round(current_keep, 2), "adjusted_keep": round(solar_adjusted_keep, 2)})
        else:
            self._publish_offset(0.0, {"morning_gap_kwh": round(morning_gap_load, 2), "net_charge_kwh": round(net_charge, 2), "original_keep": round(current_keep, 2)})

        self._cached_keep = context["best_soc_keep"]
        self._cached_at = minutes_now
        return context

    def get_config(self):
        """Read configuration from HA input helpers and Predbat config."""
        enabled = self.base.get_state_wrapper(HA_ENABLE, default="off")
        enabled = str(enabled).lower() in ("on", "true")
        dno_limit = self.base.get_arg("export_limit", 4000, index=0) / 1000.0
        return enabled, dno_limit

    def _publish_offset(self, value, attrs):
        """Publish curtailment solar offset sensor and cache for reuse."""
        # state_class: HA keeps long-term statistics only for sensors that have
        # one. Without it this is gone after the recorder window (~10 days).
        attrs.update({"friendly_name": "Curtailment Solar SOC Keep Offset", "unit_of_measurement": "kWh", "state_class": "measurement", "icon": "mdi:solar-power"})
        self.base.dashboard_item("sensor.{}_curtailment_solar_offset".format(self.base.prefix), value, attrs)
        self._cached_offset = (value, attrs)

    def _get_overnight_safety_pct(self):
        """R55: read curtailment_overnight_safety_pct helper (0-100, default 50)."""
        try:
            v = float(self.base.get_state_wrapper(HA_OVERNIGHT_SAFETY_PCT, default=OVERNIGHT_SAFETY_PCT_DEFAULT))
        except (ValueError, TypeError):
            v = OVERNIGHT_SAFETY_PCT_DEFAULT
        return max(0.0, min(200.0, v))

    def _publish_overnight_target(self, value_kwh, attrs):
        """R55: publish overnight_target sensor (morning_gap × (1 + safety_pct/100))."""
        attrs.update(
            {
                "friendly_name": "Curtailment Overnight Target",
                "unit_of_measurement": "kWh",
                "device_class": "energy",
                "state_class": "measurement",
                "icon": "mdi:weather-night",
            }
        )
        self.base.dashboard_item("sensor.{}_curtailment_overnight_target".format(self.base.prefix), value_kwh, attrs)
        self._cached_overnight = (value_kwh, attrs)

    def _refresh_overnight_target(self):
        """R55: compute morning_gap → overnight_target and publish.

        Called from calculate() (on_update hook) where
        pv_forecast_minute_step IS populated (it's set by calculate_plan
        which runs earlier in update_pred). on_before_plan runs BEFORE
        calculate_plan so pv_step is empty there.
        """
        try:
            pv_step = getattr(self.base, "pv_forecast_minute_step", {}) or {}
            load_step = getattr(self.base, "load_minutes_step", {}) or {}
            soc_max = getattr(self.base, "soc_max", 10)
            reserve = getattr(self.base, "reserve", 0)
            soc_keep = float(getattr(self.base, "best_soc_keep", 0) or 0)
            forecast_minutes = getattr(self.base, "forecast_minutes", 1440)
            if not pv_step or not soc_max:
                # Pre-startup: no plan yet. Publish soc_keep as fallback so
                # the dashboard isn't blank — but don't cache morning_gap.
                target = max(min(soc_keep, soc_max), float(reserve or 0.0)) if soc_max else 0.0
                self._publish_overnight_target(
                    round(target, 2),
                    {
                        "morning_gap_kwh": 0.0,
                        "safety_pct": round(self._get_overnight_safety_pct(), 1),
                        "soc_keep_kwh": round(soc_keep, 2),
                        "soc_pct": round((target / soc_max * 100.0) if soc_max else 0.0, 1),
                        "source": "no_plan_yet",
                    },
                )
                return
            # compute_morning_gap finds sunset and sunrise from pv_step
            # (sustained low/high PV transitions) and integrates load deficit
            # between them. No arbitrary horizon cap needed.
            morning_gap_load = compute_morning_gap(
                pv_step,
                load_step,
                start_minute=PREDICT_STEP,
                end_minute=forecast_minutes,
                step_minutes=PREDICT_STEP,
                values_are_kwh=True,
            )
            # Translate load energy → battery drawdown by accounting for
            # discharge inefficiency (battery_loss_discharge × inverter_loss).
            # Predbat already applies these in its predict trajectory; without
            # the same factor our morning_gap underestimates by the same %
            # (e.g. 5.29 kWh load → 6.20 kWh battery drawdown for SIG with
            # 12% inverter loss + 3% battery discharge loss).
            discharge_efficiency = self._discharge_efficiency()
            morning_gap_battery = morning_gap_load / discharge_efficiency

            safety_pct = self._get_overnight_safety_pct()
            target = morning_gap_battery * (1.0 + safety_pct / 100.0) + soc_keep
            target = max(target, float(reserve or 0.0))
            target = min(target, float(soc_max or 0.0))
            self._overnight_target_kwh = target
            soc_pct = (target / soc_max * 100.0) if soc_max else 0.0
            self._publish_overnight_target(
                round(target, 2),
                {
                    "morning_gap_kwh": round(morning_gap_battery, 2),
                    "morning_gap_load_kwh": round(morning_gap_load, 2),
                    "discharge_efficiency": round(discharge_efficiency, 3),
                    "safety_pct": round(safety_pct, 1),
                    "soc_keep_kwh": round(soc_keep, 2),
                    "soc_pct": round(soc_pct, 1),
                    "source": "calculate",
                },
            )
        except Exception as exc:
            self._log_once("overnight_target_error", "_refresh_overnight_target failed: {} — overnight_target frozen at previous value".format(exc))

    def _get_p90_scale(self, lat, lon, doy, local_offset):
        """Get clear-sky scale from Solcast p90 forecast (R42).

        Reads detailedForecast attribute from today's Solcast sensor.
        Falls back to yesterday's scale if unavailable (R44).
        """
        try:
            detailed = self._get_solcast_detailed()
            if detailed:
                scale, peak_kw, peak_utc = p90_scale_from_forecast(detailed, lat, lon, doy, local_offset)
                if scale > 0:
                    self._p90_scale = scale
                    self._p90_peak_kw = peak_kw
                    return scale, peak_kw, peak_utc
        except Exception as exc:
            self._log_once("p90_scale_error", "p90 scale derivation failed: {}".format(exc))
        # Fallback: yesterday's scale (changes ~1° elevation per day, R44)
        self._log_once("p90_scale_fallback", "Solcast unavailable — using cached p90 scale {:.2f}".format(self._p90_scale))
        return self._p90_scale, self._p90_peak_kw, 0.0

    def _get_solcast_detailed(self):
        """Return Solcast detailedForecast list, or [] if unavailable/untrusted.

        Gates (unknown-unknowns item 4, 2026-07-07):
        - dataCorrect is False → Solcast itself says the data is bad; reject.
        - Slot dates != today → stale forecast. compute_solcast_overflow parses
          only HH:MM from period_start, so yesterday's forecast would otherwise
          be consumed as today's with no error.
        Rejection falls back to the clear-sky model / cached scale — same
        degraded modes as "Solcast missing", now visible via _log_once.
        """
        try:
            data_correct = self.base.get_state_wrapper(SOLCAST_TODAY, attribute="dataCorrect", default=None)
            if data_correct is not None and str(data_correct).lower() == "false":
                self._log_once("solcast_datacorrect", "Solcast dataCorrect=False — ignoring detailedForecast")
                return []
            detailed = self.base.get_state_wrapper(SOLCAST_TODAY, attribute="detailedForecast", default=[])
            if not isinstance(detailed, list) or not detailed:
                return []
            now_local = getattr(self.base, "now_utc", None)  # local-tz-aware (misnamed)
            if now_local is not None:
                today_str = now_local.strftime("%Y-%m-%d")
                slot_date = str(detailed[0].get("period_start", ""))[:10]
                if slot_date and slot_date != today_str:
                    self._log_once("solcast_stale_date", "Solcast detailedForecast dated {} but today is {} — ignoring stale forecast".format(slot_date, today_str))
                    return []
            return detailed
        except Exception as exc:
            self._log_once("solcast_detailed_error", "detailedForecast read failed: {}".format(exc))
        return []

    def _compute_calibration_ratio(self, minutes_now, solcast_remaining):
        """R58 live calibration ratio: actual_pv_last_30min / solcast_last_30min.

        Reads SIG_DAILY_PV and uses pv_history (which already tracks
        (minutes_now, solcast_so_far_kwh, sig_daily_pv_kwh)) to derive a
        recent ratio. Returns 1.0 if not enough history yet (need at least
        one entry ~25-35 min old). Caller should pass the ratio to
        compute_solcast_overflow which clamps at CALIBRATION_RATIO_MAX.
        """
        try:
            sig_daily_pv = float(self.base.get_state_wrapper(SIG_DAILY_PV, default=0))
        except (ValueError, TypeError):
            return 1.0
        try:
            solcast_today_kwh = float(self.base.get_state_wrapper(SOLCAST_TODAY, default=0))
        except (ValueError, TypeError):
            return 1.0
        solcast_so_far = max(0.0, solcast_today_kwh - solcast_remaining)
        target_past = minutes_now - 30
        oldest = None
        for entry in self._pv_history:
            if abs(entry[0] - target_past) <= 5:
                oldest = entry
                break
        if oldest is None:
            return 1.0
        delta_solcast = solcast_so_far - oldest[1]
        delta_actual = sig_daily_pv - oldest[2]
        if delta_solcast < 0.05:
            # Solcast says no PV in the last 30 min — calibration is not
            # meaningful (could be sunrise/sunset edge). Don't scale.
            return 1.0
        return max(0.0, delta_actual / delta_solcast)

    def _refresh_effective_max_reserved(self, minutes_now, solcast_remaining):
        """R49 buffer reduction — sets self._effective_max_reserved for this cycle.

        Called BEFORE any consumer of the buffer (the no_drain fits-check, R63's
        headroom_needed, and the Headroom Floor). Previously this ran inline just
        above the floor, so the two earlier callers used the raw MAX_RESERVED_KWH
        constant and disagreed with the floor by up to 0.54 kWh on exactly the
        cloudy afternoons R49 fires on — a latent repeat of the 2026-07-28
        no_drain defect. One quantity, one definition (Charter).
        """
        try:
            solcast_today_kwh = float(self.base.get_state_wrapper(SOLCAST_TODAY, default=0))
        except (ValueError, TypeError):
            solcast_today_kwh = 0.0
        try:
            sig_daily_pv = float(self.base.get_state_wrapper(SIG_DAILY_PV, default=0))
        except (ValueError, TypeError):
            sig_daily_pv = 0.0
        solcast_so_far = max(0.0, solcast_today_kwh - solcast_remaining)

        effective = MAX_RESERVED_KWH
        self._buffer_reduced = False
        if minutes_now >= BUFFER_REDUCE_MIN_LOCAL_HOUR * 60 and solcast_so_far > BUFFER_REDUCE_MIN_SOLCAST_KWH:
            cumulative_ratio = sig_daily_pv / solcast_so_far if solcast_so_far > 0 else 1.0
            target_past = minutes_now - 60
            oldest = None
            for entry in self._pv_history:
                if abs(entry[0] - target_past) <= 10:
                    oldest = entry
                    break
            if oldest is not None:
                delta_solcast = solcast_so_far - oldest[1]
                delta_actual = sig_daily_pv - oldest[2]
                recent_ratio = delta_actual / delta_solcast if delta_solcast > 0.1 else 1.0
                if cumulative_ratio < BUFFER_REDUCE_CUMULATIVE_RATIO and recent_ratio < BUFFER_REDUCE_RECENT_RATIO:
                    effective = max(BUFFER_REDUCE_FLOOR_KWH, MAX_RESERVED_KWH * BUFFER_REDUCE_FACTOR)
                    self._buffer_reduced = True
        # Append after the lookup so the current sample doesn't match itself
        self._pv_history.append((minutes_now, solcast_so_far, sig_daily_pv))
        self._effective_max_reserved = effective
        return effective

    def _compute_overflow_band(self, band, scale_fallback, lat, lon, doy, utc_hours, safe_utc, dno_limit_kw, load_fc, calibration_ratio, detailed):
        """R53/R50/R58: compute overflow integral for one Solcast band.

        Uses compute_solcast_overflow when detailedForecast has 4+ slots
        (i.e. at least 2 hours of forecast — enough for the integral to
        make sense). Otherwise falls back to compute_solar_overflow with
        the band's clear-sky scale.

        scale_fallback is used only on the fallback path (e.g. integration
        tests that supply only one Solcast slot for scale derivation).
        """
        if detailed and len(detailed) >= 4:
            return compute_solcast_overflow(
                detailed_forecast=detailed,
                from_utc_hours=utc_hours,
                to_utc_hours=safe_utc,
                dno_limit=dno_limit_kw,
                load_forecast_kw=load_fc,
                local_offset_hours=0.0,
                band=band,
                calibration_ratio=calibration_ratio,
            )
        if scale_fallback <= 0:
            return 0.0
        return compute_solar_overflow(
            scale_fallback,
            lat,
            lon,
            doy,
            utc_hours,
            safe_utc,
            dno_limit_kw,
            load_forecast_kw=load_fc,
        )

    def _get_p_scales(self, lat, lon, doy, local_offset):
        """R50: get all three forecast band scales (p10/p50/p90).

        Returns (p10_scale, p50_scale, p90_scale). Any band whose peak < 0.5 kW
        or with a parse error yields 0.0 for that band — caller should guard.
        Falls back gracefully: if Solcast unavailable, returns (0, 0, p90_cached).
        """
        try:
            detailed = self._get_solcast_detailed()
            if detailed:
                p10, p50, p90 = p_scales_from_forecast(detailed, lat, lon, doy, local_offset)
                # At least p90 should be valid on a normal day; missing p10/p50
                # is unusual but not fatal (we'll treat their integrals as p90's).
                return p10, p50, p90
        except Exception as exc:
            self._log_once("p_scales_error", "band scale derivation failed: {}".format(exc))
        # Fallback: use cached p90 for all three (degenerates to current pre-R50)
        self._log_once("p_scales_fallback", "Solcast unavailable — band scales degenerate to cached p90 {:.2f}".format(self._p90_scale))
        return 0.0, 0.0, self._p90_scale

    def _get_confidence_thresholds(self):
        """Read R50 input_number helpers; fall back to defaults if missing/invalid."""
        try:
            high = float(self.base.get_state_wrapper(HA_CONFIDENCE_HIGH, default=CONFIDENCE_HIGH_DEFAULT))
        except (ValueError, TypeError):
            high = CONFIDENCE_HIGH_DEFAULT
        try:
            low = float(self.base.get_state_wrapper(HA_CONFIDENCE_LOW, default=CONFIDENCE_LOW_DEFAULT))
        except (ValueError, TypeError):
            low = CONFIDENCE_LOW_DEFAULT
        # Sanity: ensure 0 <= low < high <= 1
        high = max(0.05, min(1.0, high))
        low = max(0.0, min(high - 0.05, low))
        return low, high

    def _expected_overflow(self):
        """R50a: the overflow estimate feeding the floor. p90 unless the blend is on.

        Single gate for both consumers (the R62 pre-PV target and the live R9 floor)
        so they can never disagree about which estimate the day is being sized on.

        Default is pure p90 (R7/R42/R43). Setting
        input_number.curtailment_confidence_high below 1.0 re-enables the R50 blend
        from the dashboard without a code change.
        """
        low, high = self._get_confidence_thresholds()
        if high >= CONFIDENCE_BLEND_OFF:
            return self._overflow_p90
        return compute_expected_overflow(
            p10=self._overflow_p10,
            p50=self._overflow_p50,
            p90=self._overflow_p90,
            confidence=self._confidence,
            low=low,
            high=high,
        )

    def _get_solcast_confidence(self):
        """Read Solcast analysis.confidence; fall back to CONFIDENCE_DEFAULT.

        The fallback is dangerous if it becomes permanent: 0.9 ≥ conf_high
        pins the R50 blend to pure p90 (maximum aggression) every day. A
        Solcast schema change here MUST be visible — hence _log_once.
        """
        try:
            analysis = self.base.get_state_wrapper(SOLCAST_TODAY, attribute="analysis", default={}) or {}
            if isinstance(analysis, dict) and "confidence" in analysis:
                return float(analysis["confidence"])
        except (ValueError, TypeError, KeyError) as exc:
            self._log_once("confidence_error", "analysis.confidence read failed ({}) — default {} = permanent p90 mode".format(exc, CONFIDENCE_DEFAULT))
            return CONFIDENCE_DEFAULT
        self._log_once("confidence_missing", "Solcast analysis.confidence missing — default {} pins R50 blend to p90".format(CONFIDENCE_DEFAULT))
        return CONFIDENCE_DEFAULT

    def _is_gshp_ch_active(self):
        """R52: read input_boolean.gshp_ch_active. Default True (winter) if missing."""
        try:
            state = self.base.get_state_wrapper(HA_GSHP_CH_ACTIVE, default="on")
            return str(state).lower() in ("on", "true", "1")
        except (ValueError, TypeError):
            return True  # default safe — no pre-PV drain

    def _pre_pv_buffer_pct(self):
        """R52: read input_number.curtailment_pre_pv_buffer_pct (0-50%)."""
        try:
            v = float(self.base.get_state_wrapper(HA_PRE_PV_BUFFER_PCT, default=PRE_PV_BUFFER_PCT_DEFAULT))
            return max(0.0, min(50.0, v))
        except (ValueError, TypeError):
            return PRE_PV_BUFFER_PCT_DEFAULT

    def _pre_pv_drain_decision(self, lat, lon, doy, local_offset, utc_hours, dno_limit_kw):
        """R52: should plugin activate pre-PV drain? Returns (target_kwh, str) or None.

        Conditions for pre-PV drain (all must hold):
        1. CH off (input_boolean.gshp_ch_active = off) — overnight battery free.
        2. Forecast overflow > threshold — meaningful drain target.
        3. p90_scale derivable + pv_start_time computable.
        4. SOC > target_at_pv_start — there's something to drain.
        5. now ≥ drain_start_time — late enough that finishing at PV-start is feasible.

        Returns the target SOC (in kWh) and a diagnostic string when active,
        or None when plugin should stay Off.
        """
        if self._is_gshp_ch_active():
            return None
        # Forecast overflow must justify the drain — uses self._overflow_p90 from
        # _publish_forecast_overflow earlier in this cycle.
        if self._overflow_p90 < PRE_PV_OVERFLOW_THRESHOLD_KWH:
            return None

        p10_scale, p50_scale, p90_scale = self._get_p_scales(lat, lon, doy, local_offset)
        self._p10_scale, self._p50_scale = p10_scale, p50_scale
        if p90_scale < 0.5:
            return None

        # Update self._p90_scale / self._floor_scale so dashboard reflects state
        # even before the post-PV path runs.
        self._p90_scale = p90_scale
        self._floor_scale = p90_scale
        self._safe_scale = p90_scale

        _minutes, pv_start_utc = compute_pv_start_time(p90_scale, lat, lon, doy, PV_START_THRESHOLD_KW, utc_hours)
        if pv_start_utc is None:
            return None
        if pv_start_utc <= utc_hours:
            return None  # PV crossing was earlier today — should be on post-PV path

        soc_kw = float(getattr(self.base, "soc_kw", 0))
        soc_max = float(getattr(self.base, "soc_max", 18.08))
        soc_keep = float(getattr(self.base, "best_soc_keep", 0))
        reserve = float(getattr(self.base, "reserve", 0) or 0)

        # R62: forecast-driven target. Blend the overflow bands (already
        # computed this cycle by _publish_forecast_overflow, against the R60
        # effective cap) by Solcast confidence, then let the R54-shaped
        # overflow floor set the drain depth. The legacy soc_keep + buffer%
        # value survives as a ceiling only.
        expected_overflow = self._expected_overflow()
        # Dawn load: house load the battery must carry from PV-start until PV
        # covers load (the R61 no-drain window). Crossing at base load + the
        # pv_covering margin; falls back to ~1h of base load if no crossing.
        _cm, cover_utc = compute_pv_start_time(p90_scale, lat, lon, doy, MIN_BASE_LOAD_KW + 0.5, utc_hours)
        dawn_load_kwh = MIN_BASE_LOAD_KW * 1.0
        if cover_utc is not None and pv_start_utc is not None and cover_utc > pv_start_utc:
            load_step = getattr(self.base, "load_minutes_step", {}) or {}
            step_h = PREDICT_STEP / 60.0
            start_off = max(0, int((pv_start_utc - utc_hours) * 60))
            cover_off = max(start_off, int((cover_utc - utc_hours) * 60))
            dawn_load_kwh = sum(max(load_step.get(m, 0.0), MIN_BASE_LOAD_KW * step_h) for m in range(start_off, cover_off, PREDICT_STEP))

        # v33: carry it out of this path. The pre-PV phase ENDS at PV start, but the
        # gap this number measures runs on past that boundary — see _dawn_floor_kwh.
        self._dawn_load_kwh = dawn_load_kwh

        buffer_pct = self._pre_pv_buffer_pct()
        target_kwh = compute_pre_pv_target(
            soc_keep=soc_keep,
            soc_max=soc_max,
            buffer_pct=buffer_pct,
            reserve=reserve,
            expected_overflow_kwh=expected_overflow,
            dawn_load_kwh=dawn_load_kwh,
            max_reserved_kwh=MAX_RESERVED_KWH,
            safety_factor=OVERFLOW_SAFETY_FACTOR,
        )

        if soc_kw <= target_kwh + 0.1:
            return None  # already at/below pre-PV target

        drain_amount = soc_kw - target_kwh
        drain_minutes = drain_amount / dno_limit_kw * 60.0
        drain_start_utc = pv_start_utc - drain_minutes / 60.0

        # v32.2: the timing gate is a START gate ONLY. drain_start_utc tracks `now`
        # as SOC drains (draining at ~dno shrinks drain_minutes at ~60min/h), so
        # re-checking `now < drain_start` every cycle hovers at equality and flips
        # on noise → Max Export↔Hold flapping. Once the drain has begun, commit and
        # run to target (the soc<=target check above is the clean exit). Latch clears
        # at the day rollover.
        if utc_hours < drain_start_utc and not self._pre_pv_drain_started:
            return None  # too early — wait (only gates the START)
        self._pre_pv_drain_started = True

        # Safe-time string for dashboard
        pv_start_local = pv_start_utc + local_offset
        pv_start_str = "{:02d}:{:02d}".format(int(pv_start_local) % 24, int((pv_start_local % 1) * 60))
        decision = "pre-PV drain target={:.2f}kWh pv_start={} drain_start≈{:.0f}min ago".format(target_kwh, pv_start_str, max(0.0, (utc_hours - drain_start_utc) * 60))
        return target_kwh, decision

    def _publish_forecast_overflow(self, lat, lon, doy, local_offset, utc_hours, dno_limit_kw):
        """Update self._overflow_p10/p50/p90 from current Solcast forecast.

        Called from Off paths (e.g. pre-dawn) so the dashboard reflects the
        forecast even before activation. Silently no-ops if Solcast data is
        unavailable or geometry won't compute — values stay at previous cycle.

        Uses the R53 per-slot Solcast integral when detailedForecast has
        enough slots; falls back to the clear-sky model otherwise.
        """
        try:
            p10_scale, p50_scale, p90_scale = self._get_p_scales(lat, lon, doy, local_offset)
            self._p10_scale, self._p50_scale = p10_scale, p50_scale
            if p90_scale < 0.5:
                return
            threshold_kw = dno_limit_kw + MIN_BASE_LOAD_KW
            safe_mins, safe_utc = compute_release_time(p90_scale, lat, lon, doy, threshold_kw, utc_hours)
            if not safe_mins or safe_mins <= 0:
                return
            load_step = getattr(self.base, "load_minutes_step", {})
            step_hours = PREDICT_STEP / 60.0
            to_kw = 1.0 / step_hours
            safe_offset_mins = max(PREDICT_STEP, int((safe_utc - utc_hours) * 60))
            load_fc = [load_step.get(m, 0) * to_kw for m in range(0, safe_offset_mins, PREDICT_STEP)]
            load_fc = smooth_load_forecast(load_fc, window_minutes=60, step_minutes=PREDICT_STEP)
            detailed = self._get_solcast_detailed()
            try:
                solcast_remaining = float(self.base.get_state_wrapper(SOLCAST_REMAINING, default=0))
            except (ValueError, TypeError):
                solcast_remaining = 0.0
            minutes_now = getattr(self.base, "minutes_now", 720)
            calibration_ratio = self._compute_calibration_ratio(minutes_now, solcast_remaining)
            # R58: per-band, no R43 max(p_scale, actual_scale) collapse.
            p10_fb = max(p10_scale, self._actual_scale)
            p50_fb = max(p50_scale, self._actual_scale)
            p90_fb = max(p90_scale, self._actual_scale)
            # R60: use effective_dno (realistic export ceiling) instead of theoretical
            # dno_limit_kw for the overflow integral. The threshold for safe_time
            # (above) keeps full DNO — that's a geometric "when does PV drop below
            # ceiling" marker, not a control threshold.
            eff_dno = getattr(self, "_effective_dno", dno_limit_kw)
            self._overflow_p10 = round(self._compute_overflow_band("pv_estimate10", p10_fb, lat, lon, doy, utc_hours, safe_utc, eff_dno, load_fc, calibration_ratio, detailed), 2)
            self._overflow_p50 = round(self._compute_overflow_band("pv_estimate", p50_fb, lat, lon, doy, utc_hours, safe_utc, eff_dno, load_fc, calibration_ratio, detailed), 2)
            self._overflow_p90 = round(self._compute_overflow_band("pv_estimate90", p90_fb, lat, lon, doy, utc_hours, safe_utc, eff_dno, load_fc, calibration_ratio, detailed), 2)
        except Exception as exc:
            self._log_once("forecast_overflow_error", "_publish_forecast_overflow failed: {} — overflow bands frozen at previous cycle (pre-PV drain gating affected)".format(exc))

    def _pre_pv_phase(self, lat, lon, doy, local_offset, utc_hours, dno_limit_kw, soc_kw, soc_max):
        """Pre-dawn / winter-morning branch: no PV observed yet today.

        Extracted from calculate() to stay under the C901 ratchet (the hook's own
        note: split calculate along its phase boundaries, never raise the pin).
        Returns a (floor, phase) tuple; the caller returns it verbatim.
        """
        # Compute forecast integrals from current Solcast so dashboard
        # shows expected overflow before activation.
        self._publish_forecast_overflow(lat, lon, doy, local_offset, utc_hours, dno_limit_kw)

        # R52: pre-PV drain — if forecast says big overflow + CH off + we
        # have time to drain at DNO before PV starts, activate now.
        pre_pv = self._pre_pv_drain_decision(lat, lon, doy, local_offset, utc_hours, dno_limit_kw)
        if pre_pv is not None:
            target_kwh, decision_str = pre_pv
            self._last_decision = "active (pre-PV): " + decision_str
            self._last_floor_scale = 0.0
            self._floor_source = "Pre-PV Drain"
            # R62: stamp the published-threshold inputs with the pre-PV
            # target. Without this, publish() derives drain_above from
            # YESTERDAY EVENING'S _effective_keep_kwh/_overflow_floor_kwh
            # (e.g. 14.95 after an R61 dusk hold) and the HA automation
            # never drains below yesterday's level — pre-PV drain would
            # silently do nothing. p10_recovery is likewise stale from
            # dusk; pre-dawn recovery is meaningless (whole PV day ahead),
            # so clear it — charge_below then rests on soc_keep/deep floor.
            self._effective_keep_kwh = round(target_kwh, 2)
            self._overflow_floor_kwh = round(target_kwh, 2)
            self._p10_recovery_floor = 0.0
            self._pre_pv_engaged_today = True
            self._save_state()
            return target_kwh, "active"

        # v32 dawn-flap fix: the pre-PV drain has finished (pre_pv is None) but PV
        # hasn't arrived yet. If it fired today and overflow is still forecast,
        # HOLD active (battery flat at the drained level) rather than handing back
        # to Predbat/MSC — otherwise actual PV flickering across the 0.1kW boundary
        # at dawn flaps off↔active and churns the policy/heartbeat (2026-07-21).
        # This block only runs pre-dawn (peak not yet observed), so it can't affect
        # the evening; Hold at PV≈0 = cover load from battery, no sell, no absorb.
        if self._pre_pv_engaged_today and self._overflow_p90 > PRE_PV_OVERFLOW_THRESHOLD_KWH:
            self._policy_override = "hold"
            self._floor_source = "Pre-PV Hold"
            self._effective_keep_kwh = round(soc_kw, 2)
            self._overflow_floor_kwh = round(soc_kw, 2)
            self._p10_recovery_floor = 0.0
            self._last_decision = "active (pre-PV hold): drain done, awaiting PV"
            self._save_state()
            return soc_kw, "active"

        if self._is_session_dispatching():
            # RD14-own: a joined session is live and CM owns it. The heartbeat
            # can only force Max Export while CM holds the wheel and the select
            # is not `Predbat`, so handing back here would stop the sell.
            # Hold is the right posture — no usable PV to manage, but we must
            # still own the plant so the heartbeat can dispatch.
            self._policy_override = "hold"
            self._floor_source = "Saving Session"
            self._effective_keep_kwh = round(soc_kw, 2)
            self._overflow_floor_kwh = round(soc_kw, 2)
            self._p10_recovery_floor = 0.0
            self._last_decision = "active (session): no PV, holding the wheel for the session"
            return soc_kw, "active"

        self._last_decision = "off: no PV yet"
        self._floor_source = "Overnight Reserve"
        return soc_max, "off"

    def calculate(self, dno_limit_kw):
        """Compute floor and phase for this cycle.

        Scale from Solcast p90 (R42). Floor uses required_headroom_kwh with
        OVERFLOW_SAFETY_FACTOR (currently 1.05, R9). No floor ratchet (R11 removed).
        Sundown deactivates; safe_time drives a Hold/no_drain override (R6 v32).

        Returns:
            (floor_kwh, phase) where phase is "active" or "off"
        """
        # Day rollover: if our in-memory state belongs to yesterday, reset.
        # Handles plugin running continuously across midnight (state file may
        # still hold yesterday's date; we don't want to carry yesterday's peak_pv
        # into today's actual_scale computation).
        today = datetime.now().strftime("%Y-%m-%d")
        if self._state_date is not None and self._state_date != today:
            self._reset_for_new_day()

        # v32: default the evening-lifecycle override + session protection to
        # neutral each cycle. Only the main active path (after the sundown/Hold
        # gate) sets them; the pre-PV drain and other early-return paths must not
        # inherit a stale "hold" (which would block the pre-PV drain) or a stale
        # session floor from yesterday evening.
        self._policy_override = None
        self._session_protect_kwh = 0.0

        # R55: refresh overnight_target sensor every cycle. calculate() runs
        # via on_update which fires AFTER calculate_plan in update_pred, so
        # pv_forecast_minute_step is populated here (unlike on_before_plan
        # which runs BEFORE calculate_plan). Done early so the sensor still
        # publishes even when we return early (off paths below).
        self._refresh_overnight_target()

        soc_kw = getattr(self.base, "soc_kw", 0)
        soc_max = getattr(self.base, "soc_max", 10)

        if not soc_max:
            return soc_max, "off"

        minutes_now = getattr(self.base, "minutes_now", 720)

        # Get location
        try:
            lat = float(self.base.get_state_wrapper("zone.home", attribute="latitude", default=0))
            lon = float(self.base.get_state_wrapper("zone.home", attribute="longitude", default=0))
        except (ValueError, TypeError):
            return soc_max, "off"

        if lat == 0 and lon == 0:
            return soc_max, "off"

        # NOTE: base.now_utc is misnamed in Predbat — it's a *local-tz-aware*
        # datetime (datetime.now(local_tz)), not UTC. `.hour` on it returns
        # local hour. Convert to actual UTC before extracting components.
        now_local_aware = getattr(self.base, "now_utc", None)
        if now_local_aware is None:
            return soc_max, "off"
        now_utc = now_local_aware.astimezone(timezone.utc)

        utc_hours = now_utc.hour + now_utc.minute / 60.0 + now_utc.second / 3600.0
        doy = now_utc.timetuple().tm_yday
        # Use tz-aware datetime offset so midnight rollover works.
        # The naive (minutes_now / 60 − utc_hours) breaks across midnight: at
        # 00:30 BST = 23:30 UTC of previous day, minutes_now ≈ 30 but utc_hours
        # ≈ 23.5, giving local_offset ≈ −23 instead of +1.
        utc_offset = now_local_aware.utcoffset()
        if utc_offset is not None:
            local_offset = utc_offset.total_seconds() / 3600.0
        else:
            local_offset = (minutes_now / 60.0) - utc_hours  # legacy fallback

        # Read actual PV
        try:
            actual_pv = float(self.base.get_state_wrapper(SIG_PV_POWER, default=0))
        except (ValueError, TypeError):
            actual_pv = 0.0
        self._actual_pv_kw = actual_pv

        # R60: sample the live voltage-throttle cap to estimate effective DNO.
        # Two conditions BOTH required:
        #   1. throttle actually active (vcap < DNO − 0.2). Sampling at 6am
        #      when grid is empty and we're exporting 4 kW with cap=4 tells
        #      us nothing about peak-PV conditions — voltage was low because
        #      no one else was exporting. Mid-day on the same site, cap might
        #      throttle to 2.5. Pre-peak samples dilute the mean toward DNO.
        #   2. export pushing against the cap (export > vcap − 0.3). The cap
        #      reading is only meaningful when we're testing it.
        # If neither fires (no throttle today / no peak surplus), no samples
        # are collected and effective_dno falls back to yesterday's mean →
        # then DNO. That's correct: when nothing was throttled, use DNO.
        try:
            vcap = float(self.base.get_state_wrapper("input_number.voltage_throttle_filtered_cap", default=dno_limit_kw))
        except (ValueError, TypeError):
            vcap = dno_limit_kw
        try:
            actual_export = float(self.base.get_state_wrapper(SIG_GRID_EXPORT_POWER, default=0))
        except (ValueError, TypeError):
            actual_export = 0.0
        throttle_active = vcap < (dno_limit_kw - 0.2)
        at_cap = vcap > 0 and actual_export > (vcap - 0.3)
        if throttle_active and at_cap:
            self._cap_samples.append(vcap)
            self._cap_samples_full_day.append(vcap)
        # Always recompute effective_dno (uses fallbacks when samples are sparse)
        self._effective_dno = compute_effective_export_cap(
            today_samples_kw=list(self._cap_samples),
            yesterday_avg_kw=self._yesterday_cap_avg,
            dno_kw=dno_limit_kw,
            min_samples=3,
            hard_floor_kw=2.0,
        )

        # R50 (v21): always refresh confidence so the published value reflects
        # current Solcast (e.g., tomorrow's confidence at midnight) even when
        # the plugin returns early. Otherwise the dashboard shows stale default.
        self._confidence = round(self._get_solcast_confidence(), 2)

        # Guard: no PV yet (pre-dawn / winter morning)
        if self._peak_pv < 0.1 and actual_pv < 0.1:
            return self._pre_pv_phase(lat, lon, doy, local_offset, utc_hours, dno_limit_kw, soc_kw, soc_max)

        # Track actual peak for scale calibration (R43)
        if actual_pv > self._peak_pv:
            self._peak_pv = actual_pv
            self._peak_pv_time = minutes_now

        # R56 (v20): peak reset moved to AFTER sundown check below so the
        # sundown detector can compare today's observed peak against the
        # current PV level without the end-of-day reset wiping it out first.

        # Get p90 scale from Solcast (R42)
        p90_scale, _p90_peak_kw, _p90_peak_utc = self._get_p90_scale(lat, lon, doy, local_offset)

        if p90_scale < 0.5:
            # No Solcast data and no yesterday's scale — cannot compute safely
            if self._is_session_dispatching():
                # RD14-own: keep the wheel so the heartbeat can dispatch, but do
                # NOT drive the Schmitt off a forecast we have just declared
                # unusable — hold position instead.
                self._policy_override = "hold"
                self._floor_source = "Saving Session"
                self._effective_keep_kwh = round(soc_kw, 2)
                self._overflow_floor_kwh = round(soc_kw, 2)
                self._p10_recovery_floor = 0.0
                self._last_decision = "active (session): no Solcast, holding the wheel for the session"
                return soc_kw, "active"
            self._last_decision = "off: p90_scale<0.5 (no Solcast)"
            self._floor_source = "No Forecast"
            return soc_max, "off"

        # Compute actual scale from observed peak (R43)
        actual_scale = 0.0
        if self._peak_pv >= 1.0:
            peak_utc_h = (self._peak_pv_time / 60.0) - local_offset
            peak_elev = solar_elevation(lat, lon, peak_utc_h, doy)
            sin_peak = math.sin(math.radians(peak_elev))
            if sin_peak >= 0.05:
                actual_scale = self._peak_pv / sin_peak

        # floor uses max(p90_scale, actual_scale) — asymmetric (R43):
        #   - actual > p90 (sunnier than forecast) → bigger overflow, lower floor, safer
        #   - actual < p90 (cloudier) → keep p90_scale (afternoon could still clear)
        # Prevents DNO breach on days where reality exceeds the 90th percentile forecast.
        self._actual_scale = actual_scale  # diagnostics (Bug 5)

        if actual_scale > p90_scale:
            floor_scale = actual_scale
        else:
            floor_scale = p90_scale

        # safe_scale drives safe_time (R19/R21).
        # Before peak confirmed: use p90_scale (conservative — don't deactivate early).
        # After peak confirmed (≥60 min past peak): use actual_scale.
        #   - actual > p90 (very clear day) → later safe_time (more conservative, R21)
        #   - actual < p90 (cloudy day) → earlier safe_time → earlier MSC recovery → better sunset SOC
        peak_confirmed = self._peak_pv_time > 0 and (minutes_now - self._peak_pv_time) >= 60
        if actual_scale > 0 and peak_confirmed:
            safe_scale = actual_scale
        else:
            safe_scale = p90_scale

        self._floor_scale = floor_scale
        self._safe_scale = safe_scale

        # Compute safe_time: when scale × sin(elev) < DNO + base_load (R19)
        threshold_kw = dno_limit_kw + MIN_BASE_LOAD_KW
        safe_mins, safe_utc = compute_release_time(safe_scale, lat, lon, doy, threshold_kw, utc_hours)

        reached_safe_time = False
        if safe_mins is None:
            # Can't compute — assume far future (very high scale, unusual)
            safe_utc = utc_hours + 12.0
            safe_mins = 720
            self._safe_time_str = "none"
        elif safe_mins <= 0:
            # RD6 (v30): we are past safe_time — PV can no longer exceed the export
            # cap, so there is no curtailment left to manage. Flag it for the
            # deactivation check below (hand the machine back to Predbat). Keep a
            # small integration slice so any residual math returns ~0.
            reached_safe_time = True
            safe_local = safe_utc + local_offset
            self._safe_time_str = "{:02d}:{:02d}".format(int(safe_local) % 24, int((safe_local % 1) * 60))
            safe_utc = utc_hours + 0.1
            safe_mins = 6
        else:
            safe_local = safe_utc + local_offset
            self._safe_time_str = "{:02d}:{:02d}".format(int(safe_local) % 24, int((safe_local % 1) * 60))

        # Load forecast from LoadML (per 5-min slot, values in kWh). Convert to kW and
        # align with the integration steps. Lets overflow formula credit real daytime
        # load (DHW, EV, etc.) that absorbs PV instead of assuming a flat 0.5 kW minimum.
        load_step = getattr(self.base, "load_minutes_step", {})
        step_hours = PREDICT_STEP / 60.0
        to_kw = 1.0 / step_hours
        safe_offset_mins = max(PREDICT_STEP, int((safe_utc - utc_hours) * 60))
        load_forecast_kw = [load_step.get(m, 0) * to_kw for m in range(0, safe_offset_mins, PREDICT_STEP)]
        load_forecast_kw = smooth_load_forecast(load_forecast_kw, window_minutes=60, step_minutes=PREDICT_STEP)

        # R50/R53/R58: three overflow integrals, one per forecast band.
        # Per-slot Solcast (R53) when detailedForecast available, falls back to
        # clear-sky scale when Solcast is missing/short. R58 calibration ratio
        # multiplies the next 30 min of Solcast slots only (capped at 1.5x) —
        # replaces R43's global max(p_scale, actual_scale) collapse that broke
        # band spread on sunny mornings.
        p10_scale, p50_scale, _p90_check = self._get_p_scales(lat, lon, doy, local_offset)
        # Retain for the tracking-band debug metric. This is the MAIN active path —
        # the other two call sites are the pre-PV/forecast-publish paths, so
        # instrumenting only those left the published scales at their init 0.0 all
        # day and the band read "unknown" (observed live 2026-08-05 13:02).
        self._p10_scale, self._p50_scale = p10_scale, p50_scale
        p10_fb = max(p10_scale, actual_scale)
        p50_fb = max(p50_scale, actual_scale)
        # p90_fb is floor_scale (already max(p90, actual) from R43 above)
        detailed = self._get_solcast_detailed()
        try:
            solcast_remaining_kwh = float(self.base.get_state_wrapper(SOLCAST_REMAINING, default=0))
        except (ValueError, TypeError):
            solcast_remaining_kwh = 0.0
        calibration_ratio = self._compute_calibration_ratio(minutes_now, solcast_remaining_kwh)
        # R60: feed band integrals the realistic export ceiling, not theoretical DNO.
        # Smaller effective_dno → bigger forecast overflow → curt_floor lower → more
        # drain. Sized correctly when voltage-throttle is active, falls back to DNO
        # when not. self._effective_dno was set near the top of calculate().
        eff_dno = self._effective_dno
        overflow_p10 = self._compute_overflow_band("pv_estimate10", p10_fb, lat, lon, doy, utc_hours, safe_utc, eff_dno, load_forecast_kw, calibration_ratio, detailed)
        overflow_p50 = self._compute_overflow_band("pv_estimate", p50_fb, lat, lon, doy, utc_hours, safe_utc, eff_dno, load_forecast_kw, calibration_ratio, detailed)
        overflow_p90 = self._compute_overflow_band("pv_estimate90", floor_scale, lat, lon, doy, utc_hours, safe_utc, eff_dno, load_forecast_kw, calibration_ratio, detailed)

        # Read confidence and tunable thresholds from helpers
        confidence = self._get_solcast_confidence()

        # R64: rolling median over OVERFLOW_SMOOTH_WINDOW_MIN. The raw estimate
        # wobbles ~2.16x its net daily movement (measured 2026-07-28), which reaches
        # the floor at 1.2x and chatters whatever threshold SOC is sitting on.
        # Median (not mean) so a single-slot Solcast revision is rejected outright.
        # On a falling series this lags HIGH -> more assumed overflow -> lower floor
        # -> more drain, which is R25's safe direction.
        self._overflow_raw = {"p10": round(overflow_p10, 2), "p50": round(overflow_p50, 2), "p90": round(overflow_p90, 2)}
        self._overflow_history.append((minutes_now, overflow_p10, overflow_p50, overflow_p90))
        for idx, raw in ((1, overflow_p10), (2, overflow_p50), (3, overflow_p90)):
            sm = smooth_overflow_samples([(h[0], h[idx]) for h in self._overflow_history], minutes_now, OVERFLOW_SMOOTH_WINDOW_MIN)
            if sm is not None:
                if idx == 1:
                    overflow_p10 = sm
                elif idx == 2:
                    overflow_p50 = sm
                else:
                    overflow_p90 = sm

        # Diagnostics must be stashed BEFORE _expected_overflow() reads them.
        self._overflow_p10 = round(overflow_p10, 2)
        self._overflow_p50 = round(overflow_p50, 2)
        self._overflow_p90 = round(overflow_p90, 2)
        self._confidence = round(confidence, 2)

        # R50a: p90 per R7/R42/R43 unless the blend is re-enabled from the dashboard.
        remaining_overflow = self._expected_overflow()
        self._remaining_overflow = round(remaining_overflow, 2)

        # Activation check (R5): overflow predicted AND battery would fill
        try:
            solcast_remaining = float(self.base.get_state_wrapper(SOLCAST_REMAINING, default=0))
        except (ValueError, TypeError):
            solcast_remaining = 0.0

        load_remaining = sum(load_step.get(m, 0) * to_kw * step_hours for m in range(PREDICT_STEP, safe_offset_mins + PREDICT_STEP, PREDICT_STEP))

        battery_headroom = soc_max - soc_kw
        total_excess = max(0.0, solcast_remaining - load_remaining)

        will_fill = total_excess > battery_headroom

        # v32 evening lifecycle (2026-07-20, supersedes v31 early-handback + RD6
        # "deactivate at safe_time"). The plugin stays ACTIVE for the whole PV
        # window and only deactivates at SUNDOWN (peak observed, actual PV ≈ 0),
        # handing the machine back to Predbat for the night. safe_time and
        # "overflow fits headroom" NO LONGER deactivate — they now drive a Hold
        # override (below). Reason: the v31 early-handback released to EMS-MSC while
        # PV still flowed; MSC stops exporting at the cap and banks the excess into
        # the battery, which round-trips back out later (~10% loss). Observed live
        # 2026-07-20. Keeping CM in Hold pins export at the cap and only banks the
        # TRUE excess (PV−cap−load) — Hold and Max Export are physically identical
        # whenever PV surplus ≥ cap (heartbeat ceiling clamp), so this is safe.
        #
        # sundown must be peak-guarded: pre-dawn is handled by the "no PV yet" early
        # return above, so a genuine end-of-day PV≈0 is the only path here.
        peaked = self._peak_pv > 0.5
        past_safe = reached_safe_time
        # Sundown must NOT hand back while a joined saving session is still
        # running. The heartbeat can only force Max Export while CM holds the
        # wheel AND the select is not `Predbat` (RD14c), so deactivating here
        # does not just change who reports the decision — it stops the sell.
        # Live 2026-08-03: PV crossed 0.1 kW at 19:37:40, CM deactivated at
        # 19:40:16 and disabled the heartbeat; export went 3.7 kW -> 0 with 20
        # minutes of the paid 19:00-20:00 window left.
        #
        # We stay active and change nothing else: the select stays wherever the
        # Schmitt put it and the heartbeat keeps dispatching off the calendar.
        # Pinning the select to Max Export from here is what RD14c deliberately
        # removed (it caused the 5 min 46 s over-run at session end).
        sundown = self._is_sundown(peaked, actual_pv, lat, lon, utc_hours, doy)
        if sundown:
            self._sundown_latched = True
            self._last_decision = "off: sundown (peak={:.1f}, actual_pv={:.2f})".format(self._peak_pv, actual_pv)
            self._floor_source = "Overnight Reserve"
            self._policy_override = None
            self._overflow_fits_latched = False
            # v32: do NOT reset _peak_pv here. It must persist through the evening so
            # `peaked` stays True and sundown keeps returning off every cycle until
            # PV=0 overnight. The old evening reset (minutes>1200) combined with the
            # removed past_safe deactivation would leave peaked False on a dusk PV
            # blip and strand the plugin active overnight. _reset_for_new_day clears
            # peak at the midnight rollover (pre-dawn is the "no PV yet" early return).
            self._save_state()
            return soc_max, "off"

        self._refresh_effective_max_reserved(minutes_now, solcast_remaining)

        # Hold gate (ex-early-handback condition, correct action). Once the battery
        # headroom can absorb ALL remaining p90 ("what if the clouds clear")
        # overflow with a buffer to spare, there is nothing left to make room for →
        # Hold (battery flat, export surplus at the cap), NOT drain. Hysteresis
        # (FITS_HYST_KWH) so we don't flap Hold↔Drain right at the boundary — the
        # v31 handback oscillated there on 2026-07-20. past_safe (solar geometry
        # says no clip possible) also forces Hold. A live saving session overrides
        # everything with Max Export (dump the reserve — set below once we know it).
        try:
            early_buffer = float(self.base.get_state_wrapper(HA_EARLY_HANDBACK_BUFFER, default=EARLY_HANDBACK_BUFFER_DEFAULT))
        except (TypeError, ValueError):
            early_buffer = EARLY_HANDBACK_BUFFER_DEFAULT
        # 2026-07-28: this compared BARE p90 against headroom, while the Headroom
        # Floor requires safety_factor × p90 + the R45 reserve. On a 6.6 kWh
        # overflow the two differ by 1.67 kWh, so the weaker test vetoed a drain
        # the band had correctly called — leaving us short of the p90 defence.
        # Key off the same safety-factored requirement (R25/R42/R43 are
        # one-directional: bigger estimate → more drain → safer). Low-overflow
        # days still report fits, preserving RD17's evening-reserve Charge.
        fits_margin = compute_overflow_fits_margin(battery_headroom, overflow_p90, OVERFLOW_SAFETY_FACTOR, self._effective_max_reserved)
        if self._overflow_fits_latched:
            self._overflow_fits_latched = peaked and fits_margin >= (early_buffer - FITS_HYST_KWH)
        else:
            self._overflow_fits_latched = peaked and fits_margin >= early_buffer
        # R63 drain deadline: the fits-check above is a pure ENERGY test — it asks
        # "does the surplus fit", never "can I still MAKE it fit". Shed rate is
        # cap - max(0, pv - load), which inverts once PV-load clears the cap
        # (T_lockout, the rising mirror of safe_time), so a trigger that first
        # fires mid-peak has no authority left (R25). Fire early when the headroom
        # we will need can no longer be shed before lockout. Only meaningful
        # BEFORE lockout — past it there is no action left to take.
        needed_kwh = required_headroom_kwh(remaining_overflow, self._effective_max_reserved, OVERFLOW_SAFETY_FACTOR) - battery_headroom
        lock_mins, lock_utc = compute_pv_start_time(floor_scale, lat, lon, doy, self._effective_dno + MIN_BASE_LOAD_KW, utc_hours)
        if lock_utc is not None and lock_mins is not None and lock_mins > 0:
            # The R45-tapered buffer is computed further down; MAX_RESERVED_KWH is
            # its ceiling, so using it here can only make R63 fire slightly EARLY,
            # which is the safe direction.
            sheddable_kwh = compute_max_sheddable(floor_scale, lat, lon, doy, utc_hours, lock_utc, self._effective_dno)
            # How much the battery can still GIVE. R63 asks "can I make this
            # headroom in time?" — if there is none left to make, the question is
            # moot and it must release to the Schmitt band (which already reads
            # Hold once SOC is no longer above drain_above). Live 2026-07-29
            # 07:39: SOC 0.54 kWh exactly on drain_above 0.54, 0.00 drainable,
            # yet the policy read "active Drain (override max_export)".
            try:
                hard_floor_pct = float(self.base.get_state_wrapper(SIG_DRAIN_FLOOR_HELPER, default=DEFAULT_DRAIN_FLOOR_PCT))
            except (TypeError, ValueError):
                hard_floor_pct = DEFAULT_DRAIN_FLOOR_PCT
            r63_floor = max(float(getattr(self.base, "reserve", 0) or 0), DEEP_DISCHARGE_FLOOR_KWH, soc_max * hard_floor_pct / 100.0)
            drainable_kwh = float(soc_kw) - r63_floor
            was_engaged = self._r63_engaged
            self._r63_engaged = drain_deadline_breached(needed_kwh, sheddable_kwh, engaged=was_engaged, drainable_kwh=drainable_kwh)
            if self._r63_engaged and not was_engaged:
                self.log("Curtailment: R63 drain deadline — need {:.2f} kWh headroom, only {:.2f} kWh sheddable before lockout {:.2f}Z -> Max Export".format(needed_kwh, sheddable_kwh, lock_utc))
            elif was_engaged and not self._r63_engaged:
                self.log("Curtailment: R63 cleared — need {:.2f} kWh, {:.2f} kWh sheddable".format(needed_kwh, sheddable_kwh))
        else:
            # Past lockout (or no crossing today): the lever has no authority, so
            # R63 has no answer. Release rather than hold Max Export pointlessly.
            self._r63_engaged = False
        self._r63_needed_kwh = round(needed_kwh, 2)

        # RD14c (2026-07-28): the plugin no longer drives DISPATCH for a saving
        # session — the heartbeat does, natively off the Octoplus calendar.
        # _session_active is still read because the PLANNING half below uses it
        # (reserve energy ahead of a session, but stop protecting it once the
        # session is running and we are meant to be selling it).
        #
        # Why this moved out: the plugin's override PINNED the select to Max
        # Export. At session end the heartbeat computes policy = raw_policy —
        # still Max Export — so dumping continued until the plugin's next 5-min
        # cycle. Measured 2026-07-28: session ended 19:30:00, released 19:35:46,
        # 5 min 46 s of selling the battery past the paid window. The heartbeat
        # cannot fix that edge while the plugin overrides the select.
        self._session_active = self._is_saving_session_active()
        if self._r63_engaged:
            # Outranks no_drain: that says "it fits right now", R63 says "it won't
            # fit later and this is the last chance to do anything about it".
            self._policy_override = "max_export"
        elif self._overflow_fits_latched or past_safe:
            # v32.1 (2026-07-22): "no_drain" — no curtailment risk left (overflow
            # fits, or past safe_time), so DRAIN is a pointless round-trip and is
            # suppressed. But the Schmitt still runs: CHARGE fires when SOC is below
            # the P10 recovery floor (charge_below) so we bank PV for the evening
            # reserve on a low-overflow day, and Hold otherwise. Previously this
            # forced Hold for all SOC, which masked the evening-reserve Charge and
            # left the battery flat all day on an overcast/low-overflow day.
            self._policy_override = "no_drain"
        else:
            self._policy_override = None

        # Active: compute floor (R9/R10)
        soc_keep = getattr(self.base, "best_soc_keep", 0)
        reserve = getattr(self.base, "reserve", 0)

        # Two-stage floor: overflow_floor is the headroom reservation from the
        # forecast integral (the thing the ratchet protects). soc_keep / reserve
        # are DYNAMIC clamps applied after ratchet — so when cold weather boost
        # ends or on_before_plan reduces keep, the final floor follows without
        # being held up by yesterday's ratcheted value.
        #
        # R45 (v19 taper): reserve headroom equal to remaining_overflow, capped
        # at MAX_RESERVED_KWH (10% of soc_max). The cap is only binding during
        # peak overflow. At the tail, buffer tapers with remaining_overflow, so
        # max_target_soc approaches soc_max as safe_time nears — battery fills
        # to ~100% with MSC only picking up any residual.
        #
        # R49 (v20 dynamic reduction): on confirmed-cloudy afternoons, reduce
        # the cap. If actual PV is tracking ≥10% under forecast (cumulative)
        # AND the most recent hour confirms it (recent ratio < 0.95), the day
        # won't deliver as much overflow as p90 anticipated. Reducing buffer
        # from 1.8 → 1.26 kWh raises max_target_soc by ~3% so the battery
        # aims higher rather than reserving headroom we won't need. The recent
        # ratio gate prevents reduction firing when clouds clear late.
        try:
            solcast_today_kwh = float(self.base.get_state_wrapper(SOLCAST_TODAY, default=0))
        except (ValueError, TypeError):
            solcast_today_kwh = 0.0
        try:
            sig_daily_pv = float(self.base.get_state_wrapper(SIG_DAILY_PV, default=0))
        except (ValueError, TypeError):
            sig_daily_pv = 0.0
        solcast_so_far = max(0.0, solcast_today_kwh - solcast_remaining)

        # R49 already ran earlier this cycle (_refresh_effective_max_reserved), so
        # the fits-check, R63 and this floor all share one buffer value.
        # R45 tapered reserve alone (no safety multiplier) — the ceiling the battery
        # may charge to. R48 compares against this, so it stays separate from the
        # full requirement below.
        max_target_soc = soc_max - min(self._effective_max_reserved, max(0.0, remaining_overflow))
        overflow_floor = max(soc_max - required_headroom_kwh(remaining_overflow, self._effective_max_reserved, OVERFLOW_SAFETY_FACTOR), 0.0)

        # R11 floor ratchet REMOVED 2026-07-28. It clamped
        # `overflow_floor = max(overflow_floor, previous)`, so the floor could only
        # rise within a day — reserving LESS headroom over time, the opposite of its
        # stated rationale ("headroom already reserved cannot be reclaimed"). Its
        # only release was `floor_scale` rising (R43), and R43 is gone
        # (floor_scale = p90_scale unconditionally), so it could never let go: on
        # 2026-07-28 it held the floor at 15.76 kWh from a 0.44 kWh-overflow moment
        # at dawn while p90 climbed to 12.28 kWh, blocking the drain all day.
        # Floor stability now comes from stable INPUTS (p90-derived floor_scale, the
        # smooth geometry integral per R25), not from clamping the output.
        self._last_floor_scale = floor_scale

        # Bug 8 (R48): relaxed soc_keep when BOTH (a) overflow won't fit with base
        # keep AND (b) PV currently covers load. One-way ratchet: once SOC has
        # recovered to soc_keep_base, lock back to base for rest of day. Gives
        # extra overflow headroom on big-PV mornings without compromising
        # afternoon reserve protection.
        RELAXED_KEEP_KWH = 0.5
        PV_MARGIN_KW = 0.5
        try:
            actual_load = float(self.base.get_state_wrapper(SIG_LOAD_POWER, default=0))
        except (ValueError, TypeError):
            actual_load = 0.0

        room_with_base_keep = max_target_soc - soc_keep
        # max_reserved=0 as above — R48 is a keep-relaxation decision, not the drain target.
        needs_room = required_headroom_kwh(remaining_overflow, 0.0, OVERFLOW_SAFETY_FACTOR) > room_with_base_keep
        pv_covering = (self._actual_pv_kw - actual_load) > PV_MARGIN_KW

        # R48 latch: only mark "recovered" after we've actually been drained
        # below soc_keep this day. Without _keep_drained_today, the latch
        # fires at midnight rollover (battery at 100% overnight → soc_kw >=
        # soc_keep trivially) and R48 never triggers on a real morning.
        if soc_kw < soc_keep:
            self._keep_drained_today = True
        if self._keep_drained_today and soc_kw >= soc_keep:
            self._keep_recovered = True

        # R48 engagement latch: once R48 fires for the first time today,
        # remember it via _r48_engaged_today. On subsequent cycles use the
        # latch instead of re-evaluating pv_covering (which oscillates around
        # the 0.5 kW threshold on cloudy mornings — caused 5 toggles on
        # 2026-04-25 06:11-09:58 BST). Latch clears when _keep_recovered.
        r48_should_engage = needs_room and pv_covering and not self._keep_recovered and soc_keep > RELAXED_KEEP_KWH
        if r48_should_engage:
            self._r48_engaged_today = True

        if self._r48_engaged_today and not self._keep_recovered and soc_keep > RELAXED_KEEP_KWH:
            effective_keep = RELAXED_KEEP_KWH
        else:
            # R55 (v20): overnight_target_kwh (computed from morning_gap +
            # safety_pct in on_before_plan) acts as a FLOOR on effective_keep.
            # Without this raise, R26's reduce-only adjustment leaves keep
            # too low to cover overnight on no-overflow days. Cold weather
            # plugin already boosts soc_keep — max() preserves both.
            effective_keep = soc_keep
            if self._overnight_target_kwh is not None:
                effective_keep = max(effective_keep, self._overnight_target_kwh)

        # Option A (2026-06-15): don't drain below current SOC while PV isn't
        # covering load. The overnight target legitimately shrinks toward
        # sunrise, but morning_gap's "sunrise" (PV≥0.3kW) precedes PV exceeding
        # load — draining to it empties the battery before PV relieves it
        # (05:11 BST activation drained 7.6%→2.7%, then imported). No surplus =
        # nothing to make room for. R52 pre-PV drain is a separate path.
        effective_keep = apply_no_surplus_drain_hold(effective_keep, soc_kw, pv_covering)

        # R59: P10 recovery floor — minimum SOC needed to recover to overnight
        # target on a worst-case (P10) PV day. Acts as a lower bound alongside
        # `reserve` in the outer max — never lowers the floor, only raises it
        # if existing rules would drain too low for cloudy-afternoon recovery.
        try:
            p10_pv_remaining = float(self.base.get_state_wrapper(SOLCAST_REMAINING, attribute="estimate10", default=0))
        except (ValueError, TypeError):
            p10_pv_remaining = 0.0
        try:
            p50_pv_remaining = float(self.base.get_state_wrapper(SOLCAST_REMAINING, attribute="estimate", default=0))
        except (ValueError, TypeError):
            p50_pv_remaining = p10_pv_remaining
        # load_remaining was computed earlier (R5 activation check).
        # Use Solcast P10 (pessimistic) — guarantee we hit overnight target
        # even on a worse-than-median PV day. Over-charging cost is one
        # round-trip; under-charging cost is the overnight grid-fill bill.
        # v31: recovery target = overnight LOAD need + saving-session export
        # reserve (Octopus sensor). They don't overlap — load is consumption,
        # the session is discretionary export — so they add. This is the ONE
        # thing CM needs to be aware of that Predbat "sees"; the drain target
        # (drain_above) stays pure curtailment.
        self._session_reserve_kwh = round(self._get_session_reserve_kwh(dno_limit_kw), 2)
        # v32(a): ahead of a KNOWN saving session, keep session_reserve = duration ×
        # cap in the battery so there is something to sell at the peak. Raise both
        # the recovery target (charge_below) AND the drain floor (drain_above, via
        # _session_protect_kwh — compute_drain_above is otherwise pure curtailment).
        # During the session itself we DUMP the reserve (Max Export override), so
        # only protect it while the session is upcoming, not active. This pulls
        # session handling back into CM until the RD7 Predbat mapper lands.
        overnight_target = self._overnight_target_kwh if self._overnight_target_kwh is not None else effective_keep
        if self._session_reserve_kwh > 0 and not self._session_active:
            reserve_for_recovery = self._session_reserve_kwh
            self._session_protect_kwh = min(soc_max, overnight_target + self._session_reserve_kwh)
        else:
            reserve_for_recovery = 0.0
            self._session_protect_kwh = 0.0
        overnight_for_recovery = overnight_target + reserve_for_recovery
        p10_recovery = compute_p10_recovery_floor(
            overnight_target_kwh=overnight_for_recovery,
            p10_pv_remaining_kwh=p10_pv_remaining,
            load_remaining_kwh=load_remaining,
        )
        self._p10_recovery_floor = round(p10_recovery, 2)
        self._p10_pv_remaining_kwh = round(p10_pv_remaining, 2)
        self._p50_pv_remaining_kwh = round(p50_pv_remaining, 2)
        self._load_remaining_kwh = round(load_remaining, 2)

        # R54 (v20) + R59: single drain-target rule with P10 recovery lower bound.
        # Inner min: overflow_floor & effective_keep are "drain TO this level" —
        #   lower wins (more drain).
        # Outer max: clamp above reserve, p10_recovery, and inner min — highest
        #   of these "minimum SOC" requirements wins.
        # On big-overflow days: overflow_floor < effective_keep → drain to
        #   overflow_floor (curtailment wins, R48 latch already relaxed
        #   effective_keep so this is safe). Late afternoon: p10_recovery rises
        #   and starts capping how low we go.
        # On no/small-overflow days: overflow_floor → soc_max → drain only
        #   to effective_keep (overnight target, replaces R45 100% chase).
        floor, self._floor_source = compute_floor_with_source(
            reserve=reserve,
            p10_recovery=p10_recovery,
            overflow_floor=overflow_floor,
            effective_keep=effective_keep,
        )
        # Stash diagnostics for sensor publishing
        self._effective_keep_kwh = round(effective_keep, 2)
        self._overflow_floor_kwh = round(overflow_floor, 2)
        floor = min(floor, soc_max)
        if floor >= soc_max:
            self._floor_source = "Battery Full"

        self._last_decision = "active: overflow={:.2f} floor={:.2f}kWh".format(remaining_overflow, floor)

        # Persist state so restarts recover (Bug 2 / R46)
        self._save_state()

        return floor, "active"

    def _compute_tomorrow_forecast(self):
        """Compute curtailment forecast for tomorrow using p90 solar geometry.

        Uses tomorrow's Solcast total and p90 scale to estimate overflow and safe_time.
        Only computes after today's PV is done. Cached for 30 minutes.
        """
        minutes_now = getattr(self.base, "minutes_now", 720)

        if self._tomorrow_cache is not None:
            since = minutes_now - self._tomorrow_cache_at
            if since < 0:
                since += 1440
            if since < 30:
                return self._tomorrow_cache

        pv_step = getattr(self.base, "pv_forecast_minute_step", {})
        load_step = getattr(self.base, "load_minutes_step", {})
        soc_max = getattr(self.base, "soc_max", 10)
        forecast_minutes = getattr(self.base, "forecast_minutes", 1440)
        dno_limit = self.base.get_arg("export_limit", 4000, index=0) / 1000.0
        step_hours = PREDICT_STEP / 60.0
        to_kw = 1.0 / step_hours

        # Wait until today's forecast PV is essentially done
        solar_end_today = min(forecast_minutes, max(PREDICT_STEP, 23 * 60 - minutes_now))
        last_pv_slot = 0
        for m in range(PREDICT_STEP, solar_end_today, PREDICT_STEP):
            if pv_step.get(m, 0) > 0:
                last_pv_slot = m
        if last_pv_slot > 30:
            available_at = minutes_now + last_pv_slot + 30
            available_h = (available_at // 60) % 24
            available_m = available_at % 60
            prefix = self.base.prefix
            self.base.dashboard_item(
                "sensor.{}_curtailment_tomorrow".format(prefix),
                "Pending",
                {
                    "friendly_name": "Curtailment Tomorrow Forecast",
                    "icon": "mdi:solar-power-variant-outline",
                    "available_at": "{:02d}:{:02d}".format(available_h, available_m),
                },
            )
            self._tomorrow_cache = None
            self._tomorrow_cache_at = minutes_now
            return None

        # Tomorrow's solar window
        tomorrow_start = 1440 - minutes_now + 5 * 60
        tomorrow_end = 1440 - minutes_now + 23 * 60
        tomorrow_end = min(tomorrow_end, forecast_minutes)

        if tomorrow_end <= tomorrow_start or tomorrow_start < 0:
            self._tomorrow_cache = None
            self._tomorrow_cache_at = minutes_now
            return None

        has_pv = any(pv_step.get(m, 0) > 0 for m in range(tomorrow_start, min(tomorrow_start + 120, tomorrow_end), PREDICT_STEP))
        if not has_pv:
            self._tomorrow_cache = None
            self._tomorrow_cache_at = minutes_now
            return None

        # Tomorrow Solcast total
        try:
            solcast_tomorrow = float(self.base.get_state_wrapper("sensor.solcast_pv_forecast_forecast_tomorrow", default=0))
        except (ValueError, TypeError):
            solcast_tomorrow = 0
        if solcast_tomorrow <= 0:
            solcast_tomorrow = sum(pv_step.get(m, 0) * to_kw * step_hours for m in range(tomorrow_start, tomorrow_end, PREDICT_STEP))

        # Safe_time from p90 solar geometry for tomorrow
        release_time_str = "unknown"
        release_end = tomorrow_end
        try:
            lat = float(self.base.get_state_wrapper("zone.home", attribute="latitude", default=0))
            lon = float(self.base.get_state_wrapper("zone.home", attribute="longitude", default=0))
            # base.now_utc is local-tz-aware (misnamed in Predbat). Convert to real UTC.
            now_local_aware = getattr(self.base, "now_utc", None)
            if lat and lon and now_local_aware:
                now_utc = now_local_aware.astimezone(timezone.utc)
                tomorrow_doy = (now_utc.timetuple().tm_yday % 365) + 1
                utc_now = now_utc.hour + now_utc.minute / 60.0
                # tz-aware utcoffset (midnight-safe); see calculate() for context
                utc_offset = now_local_aware.utcoffset()
                if utc_offset is not None:
                    local_offset = utc_offset.total_seconds() / 3600.0
                else:
                    local_offset = (minutes_now / 60.0) - utc_now

                # Get p90 scale for tomorrow from detailedForecast of tomorrow sensor
                try:
                    det_tomorrow = self.base.get_state_wrapper("sensor.solcast_pv_forecast_forecast_tomorrow", attribute="detailedForecast", default=[])
                    t_scale, _t_peak, _t_utc = p90_scale_from_forecast(det_tomorrow, lat, lon, tomorrow_doy, local_offset) if det_tomorrow else (0.0, 0.0, 0.0)
                except Exception:
                    t_scale = 0.0

                if t_scale < 0.5:
                    # Fall back to peak from pv_step shape
                    peak_pv_kw = 0
                    for m in range(tomorrow_start, tomorrow_end, PREDICT_STEP):
                        pv_kw = pv_step.get(m, 0) * to_kw
                        if pv_kw > peak_pv_kw:
                            peak_pv_kw = pv_kw
                            peak_offset = m
                    if peak_pv_kw > 1.0:
                        peak_abs_utc = (utc_now + peak_offset / 60.0) % 24
                        peak_elev = solar_elevation(lat, lon, peak_abs_utc, tomorrow_doy)
                        sin_elev = math.sin(math.radians(peak_elev))
                        if sin_elev > 0.05:
                            t_scale = peak_pv_kw / sin_elev

                if t_scale >= 0.5:
                    threshold = dno_limit + MIN_BASE_LOAD_KW
                    scan_abs_utc = (utc_now + tomorrow_start / 60.0) % 24
                    rel_mins, crossing_utc = compute_release_time(t_scale, lat, lon, tomorrow_doy, threshold, scan_abs_utc)
                    if crossing_utc:
                        crossing_local = crossing_utc + local_offset
                        release_time_str = "{:02d}:{:02d}".format(int(crossing_local) % 24, int((crossing_local % 1) * 60))
                        release_end = tomorrow_start + min(int(rel_mins), tomorrow_end - tomorrow_start)
        except Exception:
            pass

        # PV and load from PV start to safe_time
        pv_start = tomorrow_start
        for m in range(tomorrow_start, tomorrow_end, PREDICT_STEP):
            if pv_step.get(m, 0) > 0:
                pv_start = m
                break

        per_slot_to_release = sum(pv_step.get(m, 0) for m in range(pv_start, release_end, PREDICT_STEP))
        per_slot_total = sum(pv_step.get(m, 0) for m in range(tomorrow_start, tomorrow_end, PREDICT_STEP))
        fraction = (per_slot_to_release / per_slot_total) if per_slot_total > 0 else 1.0

        pv_to_release = solcast_tomorrow * fraction
        pv_after_release = solcast_tomorrow * (1 - fraction)
        load_to_release = sum(load_step.get(m, 0) * to_kw * step_hours for m in range(pv_start, release_end, PREDICT_STEP))

        # R60 for tomorrow: estimate effective DNO from TODAY'S full-day cap mean
        # (most recent daytime data available — by the time this forecast runs,
        # today's PV is done). Falls back to yesterday's mean, then DNO.
        # Subtract exportable energy from excess so "will_activate" reflects
        # ACTUAL curtailment risk after realistic export, not raw PV-load.
        tomorrow_eff_dno = compute_effective_export_cap(
            today_samples_kw=self._cap_samples_full_day,
            yesterday_avg_kw=self._yesterday_cap_avg,
            dno_kw=dno_limit,
            min_samples=10,
            hard_floor_kw=2.0,
        )
        overflow_window_hours = max(0.0, (release_end - pv_start) / 60.0)
        exportable_kwh = tomorrow_eff_dno * overflow_window_hours
        excess = max(0, pv_to_release - load_to_release - exportable_kwh)

        morning_gap = compute_morning_gap(
            pv_step,
            load_step,
            start_minute=tomorrow_start,
            end_minute=tomorrow_end,
            step_minutes=PREDICT_STEP,
            values_are_kwh=True,
        )

        # Activation: excess > headroom to full from estimated morning SOC
        estimated_morning_soc = morning_gap + 0.5
        headroom = soc_max - estimated_morning_soc
        will_activate = excess > headroom

        reserve = getattr(self.base, "reserve", 0)
        if will_activate and excess > morning_gap:
            soc_keep = round(reserve, 2)
        else:
            soc_keep = round(max(morning_gap + 0.5, reserve), 2)

        forecast = {
            "will_activate": will_activate,
            "pv_to_release_kwh": round(pv_to_release, 1),
            "pv_after_release_kwh": round(pv_after_release, 1),
            "load_to_release_kwh": round(load_to_release, 1),
            "exportable_kwh": round(exportable_kwh, 1),
            "tomorrow_eff_dno_kw": round(tomorrow_eff_dno, 2),
            "excess_kwh": round(excess, 1),
            "headroom_kwh": round(headroom, 1),
            "morning_gap_kwh": round(morning_gap, 2),
            "safe_time": release_time_str,
            "soc_keep_kwh": soc_keep,
            "solcast_kwh": round(solcast_tomorrow, 1),
        }

        self._tomorrow_cache = forecast
        self._tomorrow_cache_at = minutes_now
        return forecast

    def _publish_tomorrow_forecast(self, forecast):
        """Publish tomorrow's curtailment forecast as a sensor."""
        prefix = self.base.prefix
        if forecast["will_activate"]:
            state = "Active"
            attrs = dict(forecast)
        else:
            state = "Inactive"
            attrs = {
                "will_activate": False,
                "excess_kwh": 0,
                "solcast_kwh": round(forecast.get("solcast_kwh", 0), 1),
                "morning_gap_kwh": round(forecast.get("morning_gap_kwh", 0), 2),
                "soc_keep_kwh": round(forecast.get("soc_keep_kwh", 0), 2),
            }
        attrs["friendly_name"] = "Curtailment Tomorrow Forecast"
        attrs["icon"] = "mdi:solar-power-variant-outline"
        self.base.dashboard_item("sensor.{}_curtailment_tomorrow".format(prefix), state, attrs)

    def publish(self, phase, floor_kwh, dno_limit_kw):
        """Publish curtailment monitoring sensors via dashboard_item.

        Phase sensor shows Active/Off (plugin's strategic decision) plus the
        floor, thresholds and forecast diagnostics. v30: the plugin no longer
        controls the inverter — the dispatch policy (see _publish_dispatch_policy)
        and the HA heartbeat do that. These sensors are for monitoring only.
        """
        prefix = self.base.prefix
        soc_max = getattr(self.base, "soc_max", 10)

        # When plugin is Off, the published target_soc reflects the overnight
        # target (= what we'd drain to if we were active) instead of soc_max
        # (the placeholder return value of calculate() for off paths).
        # Phase tile already says "Off" so no info is lost.
        target_kwh = floor_kwh
        if phase != "active" and self._overnight_target_kwh is not None:
            target_kwh = min(self._overnight_target_kwh, soc_max) if soc_max else self._overnight_target_kwh

        floor_pct = round(target_kwh / soc_max * 100, 1) if soc_max > 0 else 100
        state = "Active" if phase == "active" else "Off"

        tracking_band, tracking_pct = classify_forecast_tracking(self._actual_scale, self._p10_scale, self._p50_scale, self._p90_scale)

        self.base.dashboard_item(
            "sensor.{}_curtailment_phase".format(prefix),
            state,
            {
                "friendly_name": "Curtailment Phase",
                "icon": "mdi:solar-power-variant",
                "floor_pct": floor_pct,
                "floor_scale": round(self._floor_scale, 2),
                "safe_scale": round(self._safe_scale, 2),
                "p90_scale": round(self._p90_scale, 2),
                "p50_scale": round(self._p50_scale, 2),
                "p10_scale": round(self._p10_scale, 2),
                "actual_scale": round(self._actual_scale, 2),
                # Which Solcast band today's sky is actually tracking. Without it,
                # "was the forecast wrong or was the control wrong?" cannot be
                # answered after the fact.
                "tracking_band": tracking_band,
                "tracking_pct": tracking_pct,
                "peak_pv_kw": round(self._peak_pv, 2),
                "peak_pv_time": self._peak_pv_time,
                "overflow_kwh": round(self._remaining_overflow, 2),
                "overflow_p10": self._overflow_p10,
                "overflow_p50": self._overflow_p50,
                "overflow_p90": self._overflow_p90,
                "confidence": self._confidence,
                "safe_time": self._safe_time_str,
                "buffer_reduced": self._buffer_reduced,
                "effective_max_reserved": round(self._effective_max_reserved, 2),
                "effective_dno_kw": round(self._effective_dno, 2),
                "p10_recovery_floor_kwh": self._p10_recovery_floor,
                "effective_keep_kwh": self._effective_keep_kwh,
                "overflow_floor_kwh": self._overflow_floor_kwh,
                "floor_source": self._floor_source,
                "yesterday_cap_avg_kw": round(self._yesterday_cap_avg, 2) if self._yesterday_cap_avg is not None else None,
                "cap_samples_today": len(self._cap_samples),
                "last_decision": self._last_decision,
            },
        )

        self.base.dashboard_item(
            "sensor.{}_curtailment_target_soc".format(prefix),
            floor_pct,
            {
                "friendly_name": "Curtailment Target SOC",
                "unit_of_measurement": "%",
                "state_class": "measurement",
                "icon": "mdi:battery-charging-medium",
                "target_kwh": round(target_kwh, 2),
                "source": self._floor_source,
            },
        )

        # Dedicated sensor for HA history graph: which term drives target_soc.
        # Categorical state lets you see in the recorder when it transitioned
        # between e.g. 'pre_pv_drain' → 'effective_keep' → 'overnight_target'.
        self.base.dashboard_item(
            "sensor.{}_curtailment_floor_source".format(prefix),
            self._floor_source,
            {
                "friendly_name": "Curtailment Floor Source",
                "icon": "mdi:source-branch",
                "effective_keep_kwh": self._effective_keep_kwh,
                "overflow_floor_kwh": self._overflow_floor_kwh,
                "p10_recovery_floor_kwh": self._p10_recovery_floor,
            },
        )

        # The three candidate floors, promoted from attributes on floor_source to
        # dedicated sensors — same reasoning as the overflow_p* bands above.
        #
        # WHY: floor_source is categorical, so HA cannot keep long-term
        # statistics for it, and attributes are not retained either. Publishing
        # the components numerically means the floor's COMPOSITION survives the
        # recorder window: which term won is reconstructable after the fact
        # (whichever equals target_soc), and each term's trajectory is visible.
        # Without this, "why was the floor there?" is unanswerable a fortnight
        # later — which is exactly the position the 2026-07-29 safety-factor
        # review found itself in.
        # NOTE the labels must match compute_floor_with_source() exactly — it
        # returns the human-readable winner ("Curtailment Buffer" / "P10
        # Recovery" / "Reserve"), not the variable name. effective_keep is
        # deliberately absent: v31 dropped it as a drain target, so it is no
        # longer one of the terms the floor is chosen from.
        for suffix, value, friendly, source_label, icon in (
            ("floor_overflow", self._overflow_floor_kwh, "Curtailment Floor: Overflow (P90)", "Curtailment Buffer", "mdi:battery-arrow-down"),
            ("floor_p10_recovery", self._p10_recovery_floor, "Curtailment Floor: P10 Recovery", "P10 Recovery", "mdi:battery-arrow-up"),
        ):
            self.base.dashboard_item(
                "sensor.{}_curtailment_{}".format(prefix, suffix),
                round(value, 2) if isinstance(value, (int, float)) else None,
                {
                    "friendly_name": friendly,
                    "unit_of_measurement": "kWh",
                    "device_class": "energy",
                    "state_class": "measurement",
                    "icon": icon,
                    "is_winner": self._floor_source == source_label,
                },
            )

        # Split-threshold control: Charge only if SOC < charge_below, Drain only
        # if SOC > drain_above, Hold otherwise. The two thresholds are independent
        # by design — on cloudy/deficit days charge_below can climb above
        # drain_above (Charge wins, no Drain). drain_above is computed WITHOUT
        # the p10_recovery clamp so it stays anchored to the curtailment-buffer
        # floor (R54 inner-min) regardless of recovery requirements.
        plugin_active = phase == "active"
        reserve = getattr(self.base, "reserve", 0)
        soc_keep_kwh = float(getattr(self.base, "best_soc_keep", 0) or 0)
        # Published charge_below is clamped via compute_charge_below — never
        # tell the HA automation that less than soc_keep, p10 recovery floor,
        # or the deep-discharge floor is fine. The deep-discharge floor (0.5
        # kWh) bites when on_before_plan relaxes soc_keep toward 0 on sunny-
        # tomorrow days: without it charge_target = min(0, drain_above) = 0
        # and the YAML exports while battery is at empty (observed 2026-06-04).
        # The R54 floor input (self._p10_recovery_floor) is NOT clamped so
        # that R48's effective_keep relaxation still works on overflow days.
        if plugin_active:
            charge_below = round(compute_charge_below(self._p10_recovery_floor, soc_keep_kwh), 2)
            # v33: the hard-floor arm is the DAWN RESERVE until measured PV covers
            # load, then POST_DAWN_FLOOR_KWH. Evaluated here rather than inside
            # compute_drain_above so the pure function stays free of live sensor
            # reads and the latch has exactly one owner.
            dawn_floor = self._dawn_floor_kwh(soc_max)
            drain_above = round(compute_drain_above(reserve, self._overflow_floor_kwh, self._effective_keep_kwh, self._session_protect_kwh, dawn_floor), 2)
            # Name the arm that won so the sensor (and the card) can explain a
            # floor that its own P90 attributes do not account for.
            drain_above_source = compute_drain_above_source(reserve, self._overflow_floor_kwh, self._session_protect_kwh, dawn_floor)
        else:
            charge_below = 0.0
            drain_above = round(soc_max, 2)
            drain_above_source = "inactive"
        self._drain_above_source = drain_above_source

        # Store for _publish_dispatch_policy (RD9): the v30 policy selection reuses
        # the same split thresholds this cycle rather than recomputing them.
        self._charge_below = charge_below
        self._drain_above = drain_above

        self.base.dashboard_item(
            "sensor.{}_curtailment_charge_below".format(prefix),
            charge_below,
            {
                # "Overnight Floor": the SOC we must stay ABOVE to get through
                # tonight. Driven by P10 GENERATION still available to refill us
                # (R59b). Deliberately named to contrast with the Headroom Floor
                # below, which is driven by P90 overflow and pulls the other way.
                "friendly_name": "Overnight Floor (P10 generation)",
                "unit_of_measurement": "kWh",
                "device_class": "energy",
                "state_class": "measurement",
                "icon": "mdi:battery-arrow-up",
                "soc_pct": round(charge_below / soc_max * 100.0, 1) if soc_max else None,
                "p10_pv_remaining_kwh": self._p10_pv_remaining_kwh,
                "p50_pv_remaining_kwh": self._p50_pv_remaining_kwh,
                "load_remaining_kwh": self._load_remaining_kwh,
                "p10_surplus_kwh": round(max(0.0, self._p10_pv_remaining_kwh - self._load_remaining_kwh), 2),
                "overnight_target_kwh": round(self._overnight_target_kwh, 2) if self._overnight_target_kwh is not None else None,
                "confidence": self._confidence,
                "drives": "SOC below this -> Solar Charge (bank PV for tonight)",
            },
        )
        self.base.dashboard_item(
            "sensor.{}_curtailment_p10_pv_remaining".format(prefix),
            self._p10_pv_remaining_kwh,
            {
                "friendly_name": "Curtailment P10 PV Remaining",
                "unit_of_measurement": "kWh",
                "device_class": "energy",
                "state_class": "measurement",
                "icon": "mdi:weather-cloudy",
            },
        )
        self.base.dashboard_item(
            "sensor.{}_curtailment_load_remaining".format(prefix),
            self._load_remaining_kwh,
            {
                "friendly_name": "Curtailment Load Remaining (to safe_time)",
                "unit_of_measurement": "kWh",
                "device_class": "energy",
                "state_class": "measurement",
                "icon": "mdi:home-lightning-bolt",
            },
        )
        self.base.dashboard_item(
            "sensor.{}_curtailment_drain_above".format(prefix),
            drain_above,
            {
                # "Headroom Floor": the SOC we must drain DOWN to so today's
                # forecast surplus fits in the battery. Driven by P90 OVERFLOW
                # (R7/R42/R43). Pulls against the Overnight Floor above; the gap
                # between them is the Hold band.
                "friendly_name": "Headroom Floor (P90 overflow)",
                "unit_of_measurement": "kWh",
                "device_class": "energy",
                "state_class": "measurement",
                "icon": "mdi:battery-arrow-down",
                "soc_pct": round(drain_above / soc_max * 100.0, 1) if soc_max else None,
                "overflow_p90_kwh": self._overflow_p90,
                "overflow_floor_kwh": round(self._overflow_floor_kwh, 2) if self._overflow_floor_kwh is not None else None,
                # Which arm of the max actually set this floor. Without it the
                # sensor publishes only the P90 terms and cannot explain itself
                # on a saving-session day (live 2026-08-03: 10.22 vs a published
                # overflow_floor of 3.39).
                "source": drain_above_source,
                "source_label": DRAIN_SOURCE_LABELS.get(drain_above_source, drain_above_source),
                "session_reserve_kwh": round(self._session_reserve_kwh, 2) if self._session_reserve_kwh else 0.0,
                "session_protect_kwh": round(self._session_protect_kwh, 2) if self._session_protect_kwh else 0.0,
                "session_start": self._get_session_start(),
                "drives": "SOC above this -> Max Export (sell down to make room)",
            },
        )
        # R50 diagnostics promoted to dedicated sensors so HA recorder retains
        # statistics (state_class=measurement) for trend graphs and forecast-vs-actual
        # analysis. Same values as the corresponding overflow_p* attributes on
        # sensor.{prefix}_curtailment_phase.
        for suffix, value, friendly, raw_key in (
            ("overflow_p10", self._overflow_p10, "Curtailment Overflow P10", "p10"),
            ("overflow_p50", self._overflow_p50, "Curtailment Overflow P50", "p50"),
            ("overflow_p90", self._overflow_p90, "Curtailment Overflow P90", "p90"),
        ):
            self.base.dashboard_item(
                "sensor.{}_curtailment_{}".format(prefix, suffix),
                value,
                {
                    "friendly_name": friendly,
                    # R64: state is the SMOOTHED value (what drives the floor);
                    # the raw estimate is published alongside so the filter is
                    # never a silent mechanism.
                    "raw_kwh": self._overflow_raw.get(raw_key),
                    "smoothing_window_min": OVERFLOW_SMOOTH_WINDOW_MIN,
                    "unit_of_measurement": "kWh",
                    "device_class": "energy",
                    "state_class": "measurement",
                    "icon": "mdi:solar-power-variant",
                },
            )
        self.base.dashboard_item(
            "sensor.{}_curtailment_confidence".format(prefix),
            self._confidence,
            {
                "friendly_name": "Curtailment Forecast Confidence",
                "state_class": "measurement",
                "icon": "mdi:gauge",
            },
        )

    def _set_policy(self, policy):
        """Set input_select.sig_dispatch_policy, only when it changes."""
        current = self.base.get_state_wrapper(SIG_POLICY_SELECT, default=None)
        if current == policy:
            return
        try:
            self.base.call_service_wrapper("input_select/select_option", entity_id=SIG_POLICY_SELECT, option=policy)
            self.log("Curtailment: policy -> {}".format(policy))
        except Exception as e:
            self._log_once("policy_set_err", "Curtailment: failed to set policy {}: {}".format(policy, e))

    def _set_keep_floor(self, pct):
        """Set input_number.sig_keep_floor_pct, only when it changes materially.

        Clamp to the helper's [2, 100] range BEFORE the change-check so the value we
        write is exactly what HA will store — otherwise an out-of-range intended
        (e.g. 77% vs an old max-60 helper) is stored clamped (60) while our cache
        thinks 77, and the change-check then skips forever, wedging the helper
        (observed 2026-07-22: stuck at 10% since pre-dawn). Log each write and skip
        so the write path is observable live during the current investigation."""
        pct = round(float(pct), 0)
        pct = min(max(pct, 2.0), 100.0)
        try:
            current = float(self.base.get_state_wrapper(SIG_KEEP_FLOOR_HELPER, default=-1))
        except (TypeError, ValueError):
            current = -1
        if abs(current - pct) < 0.5:
            self.log("Curtailment: keep floor unchanged (current={:.0f} intended={:.0f})".format(current, pct))
            return
        try:
            self.base.call_service_wrapper("input_number/set_value", entity_id=SIG_KEEP_FLOOR_HELPER, value=pct)
            self.log("Curtailment: keep floor {:.0f} -> {:.0f}".format(current, pct))
        except Exception as e:
            self._log_once("keepfloor_set_err", "Curtailment: failed to set keep floor {}: {}".format(pct, e))

    def _set_read_only(self, value):
        """R3 mutex: suppress/resume Predbat via its internal read_only flag (NOT an
        HA entity). True while CM drives the inverter; False on handback."""
        self.base.set_read_only = value
        item = self.base.config_index.get("set_read_only")
        if item:
            item["value"] = value
        self.log("Curtailment: read_only -> {} (Predbat {})".format(value, "suppressed" if value else "resumes"))

    def _get_session_reserve_kwh(self, cap_kw):
        """Saving-session export reserve (kWh) from the Octopus sensor: the
        largest of any active/upcoming joined session's duration × cap. 0 if
        none scheduled. This is the 'what's coming' CM reads directly."""
        best_mins = 0.0
        for attr in ("current_joined_event_duration_in_minutes", "next_joined_event_duration_in_minutes"):
            try:
                mins = float(self.base.get_state_wrapper(SIG_SAVING_SESSION, attribute=attr, default=0) or 0)
            except (TypeError, ValueError):
                mins = 0.0
            best_mins = max(best_mins, mins)
        return compute_session_reserve(best_mins, cap_kw, discharge_efficiency=self._discharge_efficiency())

    def _override_label(self):
        """Human label for the active policy override, or None.

        `no_drain` splits: "surplus fits" is a verdict about the forecast
        overflow, so once that forecast is zero the honest words are "no
        overflow left" — there is nothing to fit."""
        if not self._policy_override:
            return None
        if self._policy_override == "no_drain" and not float(self._overflow_p90 or 0.0):
            return NO_OVERFLOW_LABEL
        return OVERRIDE_LABELS.get(self._policy_override)

    def _get_session_end(self):
        """Datetime the active joined saving session ends, or None.

        Uses the binary sensor's tz-aware `current_joined_event_end`. Unlike the
        dispatch decision (which must read the calendar — RD14c), an end TIME is
        not a control decision, and this attribute carries an explicit UTC
        offset where the calendar's `end_time` string does not."""
        try:
            raw = self.base.get_state_wrapper(SIG_SAVING_SESSION, attribute="current_joined_event_end", default=None)
        except (TypeError, ValueError):
            return None
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw))
        except (TypeError, ValueError):
            return None

    def _session_minutes_remaining(self):
        """Minutes until the live session ends (0 if unknown/finished)."""
        end = self._get_session_end()
        if end is None:
            return 0.0
        try:
            now = self.base.now_utc if getattr(self.base, "now_utc", None) else datetime.now(end.tzinfo)
            if now.tzinfo is None:
                now = now.replace(tzinfo=end.tzinfo)
            return max(0.0, (end - now).total_seconds() / 60.0)
        except (TypeError, ValueError, AttributeError):
            return 0.0

    def _is_sundown(self, peaked, actual_pv, lat, lon, utc_hours, doy):
        """O1 (2026-08-04): has the sun actually gone down?

        PV alone is too noisy. Ten nights of live transitions separate PERFECTLY
        on solar elevation at the first handback:

            flapped:  10.5, 12.7, 9.4 deg    (3 nights, 2-6 extra transitions)
            clean:     5.4, 3.8, 2.9, 3.2, 4.4, 4.2, 2.5 deg   (7 nights)

        No overlap, 4.0 deg of margin (real site, lat 52.31N). Verified
        2026-08-04, the first night on the gate: ONE transition, 20:00:53 at
        6.3 deg, no flapping.

        These figures were FIRST derived at 55.86N — the hardcoded MockBase
        location, not zone.home — which read every elevation ~3 deg high. 8.0
        falls inside the gap at both latitudes, so the deployed behaviour was
        right by luck. Re-tune from zone.home, never from the test rig. At 11-15 deg the sun is still well up, so PV
        under 100 W is a CLOUD — CM hands back, PV recovers, CM re-takes the wheel,
        and every toggle of read_only forces a full inverter reset
        (docs/customisation.md:38). Elevation is monotonic through dusk, so this
        cannot flap by construction — unlike a dwell timer, which would only make
        the flapping slower and would need state a deploy would wipe.

        The latch covers what the gate cannot: residual PV noise BELOW the gate,
        and midwinter, where peak elevation at this latitude (~10.7 deg) never
        clears the gate so it is permissive all day. It may only ARM below the gate
        (the caller sets it only when this returns True) — otherwise one heavy
        afternoon storm would latch CM off for the rest of a big-overflow day.

        A live joined session outranks both: handing back would stop the sell.
        """
        if self._is_session_dispatching() or self._session_imminent():
            return False
        if solar_elevation(lat, lon, utc_hours, doy) >= SUNDOWN_ELEV_DEG:
            return False
        return self._sundown_latched or (peaked and actual_pv < 0.1)

    def _dawn_floor_kwh(self, soc_max):
        """The hard-floor arm of `compute_drain_above`, gated on the DAWN GAP.

        The gap is PV START -> PV MEETS LOAD. Inside it the battery is still the
        only thing between house load and the import meter, so draining it buys
        headroom hours before overflow can use it and pays for that headroom with
        import. Outside it the battery has no load duty and PV is refilling it, so
        the floor drops to POST_DAWN_FLOOR_KWH and the rest becomes headroom.

        Live 2026-08-06, the shape this exists to stop:

            04:00-05:30  pre-PV drain -> 5.5%
            ~05:30       PV START — pre-PV phase ends, its dawn_load reserve is
                         discarded, Schmitt drains on to drain_above = 2.8%
            06:00-06:55  coasts 2.5% -> 1.3% on house load, importing
            ~06:55       PV MEETS LOAD, 85 minutes too late

        Released on MEASUREMENT, never forecast, and the latch is one-way for the
        day: a cloud at 11:00 must not re-arm a 10% floor and stop the drain in the
        middle of the overflow window. That is the dusk-flap failure (O1) arriving
        at dawn, and the fix is the same shape — latch, do not re-decide.

        Fails CLOSED: unreadable sensors hold the reserve. The cost of holding it
        wrongly is ~27 min of drain against ~3 h of slack; the cost of releasing it
        wrongly is importing the house through the dark.
        """
        if self._dawn_released:
            return self._drain_floor_kwh(soc_max)
        reserve = max(DAWN_RESERVE_FRACTION * soc_max, DEEP_DISCHARGE_FLOOR_KWH + max(0.0, self._dawn_load_kwh))
        try:
            pv_kw = float(self.base.get_state_wrapper(SIG_PV_POWER, default=0) or 0)
            load_kw = float(self.base.get_state_wrapper(SIG_LOAD_POWER, default=0) or 0)
        except (TypeError, ValueError):
            return reserve
        # pv_kw > 0 guards the degenerate overnight case where BOTH read 0 (load
        # sensor unavailable) — 0 >= 0 would otherwise release the reserve at
        # midnight, the one moment it is most needed.
        if pv_kw > 0 and pv_kw >= load_kw:
            self._dawn_released = True
            return self._drain_floor_kwh(soc_max)
        return reserve

    def _drain_floor_kwh(self, soc_max):
        """The released floor, in kWh, from the ONE live drain-floor helper.

        RD15 consolidated three coincident 5% floors into a single helper; adding a
        plugin-side constant for the same quantity would re-create exactly that. The
        helper is also what the heartbeat's sell-clamp reads, so a CM target below it
        would simply be unreachable — one number, one owner.

        Falls back to DEFAULT_DRAIN_FLOOR_PCT, which is deliberately the HIGHER of
        the plausible values: an unreadable helper should under-drain, not over-drain.
        """
        try:
            pct = float(self.base.get_state_wrapper(SIG_DRAIN_FLOOR_HELPER, default=DEFAULT_DRAIN_FLOOR_PCT))
        except (TypeError, ValueError):
            pct = DEFAULT_DRAIN_FLOOR_PCT
        return max(0.0, pct / 100.0 * soc_max)

    def _session_imminent(self, within_minutes=SESSION_IMMINENT_MINS):
        """True when a joined session starts within `within_minutes`.

        A session starting minutes AFTER sundown is the worst case: handing back
        sets the select to `Predbat` and disables the heartbeat, and the heartbeat
        only forces Max Export while the select is NOT Predbat — so the dump never
        starts. CM would re-take on its next cycle (RD14-own) but lose up to 5
        minutes of a paid window. 2026-08-05 had a 20:00 session against a 20:00:53
        sundown the night before, i.e. a coin flip on a ~1 minute margin.

        Deliberately NOT open-ended: a session six hours out must not pin CM active
        all evening burning the battery on house load.
        """
        start = self._get_session_start()
        if not start:
            return False
        try:
            start_dt = datetime.fromisoformat(str(start))
            now = self.base.now_utc if getattr(self.base, "now_utc", None) else datetime.now(start_dt.tzinfo)
            if now.tzinfo is None:
                now = now.replace(tzinfo=start_dt.tzinfo)
            delta_min = (start_dt - now).total_seconds() / 60.0
        except (TypeError, ValueError, AttributeError):
            return False
        return 0 <= delta_min <= within_minutes

    def _discharge_efficiency(self):
        """Battery-to-meter efficiency: battery_loss_discharge x inverter_loss.

        ONE definition, used by the R55 morning gap and the saving-session
        reserve. Both need the same thing — "how much must the battery give up
        to deliver X at the meter" — and they were previously only computed in
        the R55 path, so the session reserve under-reserved by the loss.
        """
        battery_loss_discharge = float(getattr(self.base, "battery_loss_discharge", 1.0) or 1.0)
        inverter_loss = float(getattr(self.base, "inverter_loss", 1.0) or 1.0)
        return max(0.5, battery_loss_discharge * inverter_loss)

    def _is_session_dispatching(self):
        """True while the heartbeat is forcing Max Export for a live saving session.

        Reads the CALENDAR — the same entity, with the same semantics, that
        `sig_dispatch_heartbeat.yaml` uses to compute its effective policy. This
        is a display mirror of a decision made elsewhere, so it must never be a
        second opinion."""
        try:
            return str(self.base.get_state_wrapper(SIG_SAVING_SESSION_CALENDAR, default="off")).lower() in ("on", "true")
        except (TypeError, ValueError):
            return False

    def _get_session_start(self):
        """ISO start time of the active/next joined saving session, or None.

        Published alongside the reserve so the dashboard can say WHEN the floor
        is being held for, not just how much — 'when' is half the explanation."""
        for attr in ("current_joined_event_start", "next_joined_event_start"):
            try:
                value = self.base.get_state_wrapper(SIG_SAVING_SESSION, attribute=attr, default=None)
            except (TypeError, ValueError):
                continue
            if value:
                return str(value)
        return None

    def _is_saving_session_active(self):
        """True while an Octopus saving session is currently running (the binary
        sensor is 'on'). v32(b): during a live session CM forces Max Export to dump
        the reserved energy at the cap, then resumes the lifecycle when it ends."""
        try:
            state = str(self.base.get_state_wrapper(SIG_SAVING_SESSION, default="off")).lower()
        except (TypeError, ValueError):
            return False
        return state in ("on", "true")

    def _set_automation(self, entity, turn_on):
        """Enable/disable an HA automation (the heartbeat register-writer)."""
        service = "automation/turn_on" if turn_on else "automation/turn_off"
        try:
            self.base.call_service_wrapper(service, entity_id=entity)
            self.log("Curtailment: {} -> {}".format(entity, "enabled" if turn_on else "disabled"))
        except Exception as e:
            self._log_once("auto_set_err", "Curtailment: failed to {} {}: {}".format("enable" if turn_on else "disable", entity, e))

    def _park_ems_msc(self):
        """Park the SIG in EMS-MSC — Remote EMS stays ON, control mode Maximum Self
        Consumption. NEVER app modes (RD2): keeps the EMS plane hot so Predbat controls."""
        try:
            self.base.call_service_wrapper("select/select_option", entity_id=SIG_EMS_MODE_SELECT, option=SIG_EMS_MODE_MSC)
            self.log("Curtailment: EMS mode -> {}".format(SIG_EMS_MODE_MSC))
        except Exception as e:
            self._log_once("ems_msc_err", "Curtailment: failed to set EMS-MSC: {}".format(e))

    def _neutralise_predbat(self):
        """Drive Predbat's OWN inputs back to neutral, so Predbat's OWN mappers undo
        the plant registers it clamped — before we disable that chain.

        Principle (Andrew, 2026-07-28): the writer that changed a register should be
        the one to change it back. CM enumerating Predbat's registers is a losing
        game — we already missed ess_max_discharging_limit and grid_import_limitation,
        and a future Predbat mapper would be missed the same way.

        Setting the SOURCE helpers back to neutral makes the existing mappers unwind
        the registers for us, whatever they happen to be:
            requested_mode  = Demand  -> EMS mode = MSC, grid_import_limitation = 100
            discharge_rate  = rated   -> ess_max_discharging_limit = rated
            charge_rate     = rated   -> ess_max_charging_limit    = rated

        Must run BEFORE the mappers are disabled — a disabled mapper cannot relay.
        The heartbeat's own re-open of those registers stays as a backstop for the
        case where this silently fails (both this and _set_automation swallow errors).
        """
        rated_d = self._float_state(SIG_RATED_DISCHARGE_SENSOR, 6.6)
        rated_c = self._float_state(SIG_RATED_CHARGE_SENSOR, 6.6)
        for entity, service, kwargs in (
            (PREDBAT_MODE_SELECT, "input_select/select_option", {"option": PREDBAT_MODE_DEMAND}),
            (PREDBAT_DISCHARGE_RATE, "input_number/set_value", {"value": int(rated_d * 1000)}),
            (PREDBAT_CHARGE_RATE, "input_number/set_value", {"value": int(rated_c * 1000)}),
        ):
            try:
                self.base.call_service_wrapper(service, entity_id=entity, **kwargs)
            except Exception as e:
                self._log_once("neutralise_err", "Curtailment: failed to neutralise {}: {}".format(entity, e))
        self.log("Curtailment: Predbat neutralised (mode=Demand, rates={:.0f}/{:.0f} W) before taking control".format(rated_d * 1000, rated_c * 1000))

    def _float_state(self, entity, default):
        """Read a numeric entity state, falling back on unknown/unavailable."""
        try:
            return float(self.base.get_state_wrapper(entity, default=default))
        except (TypeError, ValueError):
            return default

    def _soc_readable(self):
        """True when the plant SOC entity is a usable number.

        On a failed read Predbat can leave soc_kw at 0.0 ("battery empty") — the
        worst possible default for a controller that may charge or re-take the
        writer chain. If the plant SOC sensor is unavailable we hold position
        and change nothing (rebuild-context §9.4).
        """
        raw = self.base.get_state_wrapper(SIG_BATTERY_SOC_PCT, default="unavailable")
        if raw is None:
            return False
        text = str(raw).strip().lower()
        if text in ("", "unavailable", "unknown", "none"):
            return False
        try:
            pct = float(raw)
        except (TypeError, ValueError):
            return False
        return 0.0 <= pct <= 110.0

    def _set_writer(self, cm_driving):
        """Hand the register-writing role between the heartbeat and the Predbat mapper.

        Exactly one is ever enabled — being disabled IS the mutex, so neither
        automation needs a condition of its own. Always disable the outgoing writer
        BEFORE enabling the incoming one: a brief gap with neither enabled is safe
        (the inverter holds its last setpoint), whereas a brief overlap is two
        writers fighting over the same registers.
        """
        if cm_driving:
            # Let Predbat undo its OWN register writes before we freeze its chain.
            self._neutralise_predbat()
            for auto in PREDBAT_MAPPER_AUTOMATIONS:
                self._set_automation(auto, False)
            self._set_automation(SIG_HEARTBEAT_AUTOMATION, True)
        else:
            self._set_automation(SIG_HEARTBEAT_AUTOMATION, False)
            for auto in PREDBAT_MAPPER_AUTOMATIONS:
                self._set_automation(auto, True)

    def _release_to_predbat(self):
        """Window end (safe_time / off): hand the whole machine back to Predbat.
        Order (RD2/RD6/RD10): swap the writer role (heartbeat off, mapper on), park
        EMS-MSC, set policy Predbat, reset the sell floor, then clear read_only so
        Predbat resumes. The mapper must be live BEFORE read_only clears, or
        Predbat's first requested_mode change lands with nothing listening."""
        self._set_writer(cm_driving=False)
        self._park_ems_msc()
        self._set_policy(POLICY_PREDBAT)
        self._set_keep_floor(DEFAULT_KEEP_FLOOR_PCT)
        if self._read_only_set:
            self._set_read_only(False)
            self._read_only_set = False

    def _publish_dispatch_policy(self, plugin_active, floor_kwh, soc_kwh, soc_max):
        """RD9 (v30): decide the dispatch policy + sell floor from the split-threshold
        phase, ALWAYS publish the intended decision (observe-only visibility), and ACT
        (write input_select.sig_dispatch_policy + sig_keep_floor_pct) only when
        SIG_POLICY_CONTROL_ENABLE is on. Drives while active + above the low-SOC
        handover; below it hands to MSC (RD4 "A"); on the active->off edge hands back
        to Predbat once and resets the sell floor to 38% (RD10)."""
        try:
            low_soc = float(self.base.get_state_wrapper(SIG_DRAIN_FLOOR_HELPER, default=DEFAULT_DRAIN_FLOOR_PCT))
        except (TypeError, ValueError):
            low_soc = DEFAULT_DRAIN_FLOOR_PCT
        soc_pct = soc_kwh / max(soc_max, 0.1) * 100

        # The level the guard stops a session dump at. Bound here so the
        # projection below is safe on every branch, not just the active one.
        session_sell_floor_kwh = 0.0

        # Decide the intended policy + keep floor (pure decision, no side effects yet)
        #
        # RD27 (2026-08-06): there is NO low-SOC handover. While active, CM keeps the
        # wheel at any SOC. The old rule handed the policy select to Predbat below
        # the drain floor — and handed it to NOBODY: `_release_to_predbat` is not on
        # that path, so read_only stayed on and the three Predbat mappers stayed
        # disabled. Predbat could not act, the heartbeat went inert in its Predbat
        # branch, and the plant fell through to the SIG's own Maximum Self
        # Consumption default, quietly filling the battery through the pre-overflow
        # window. Live 2026-08-06: handed back 04:50, still handed back at 09:00,
        # charging 3.5 kW with ZERO export against a 19.89 kWh overflow forecast and
        # a surplus (3.54 kW) still under the 3.68 kW cap — i.e. exportable energy
        # spent on headroom the peak then had to curtail. ~2% of pack, unrecoverable.
        #
        # The handover existed only to escape CM's own drain-floor clamp. RD22 made
        # that clamp sell-only, so there is nothing left to escape: below the floor
        # the Schmitt picks Charge (under charge_below) or Hold, both of which keep
        # the battery off the grid and keep exporting surplus. Handing back is a
        # window-END decision (RD6 safe_time / sundown), never a mid-window one.
        if plugin_active:
            # v32 evening lifecycle override (set in calculate()): "max_export" =
            # dump the saving-session reserve at the cap; "hold" = overflow already
            # fits headroom or past safe_time → battery flat, sell surplus, no
            # MSC round-trip. None → the SOC-vs-band Schmitt makes room as before.
            # Hold and Max Export are physically identical whenever PV surplus ≥ cap
            # (heartbeat ceiling clamp), so the override only bites below the cap.
            if self._policy_override == "max_export":
                schmitt = "Drain"
            elif self._policy_override == "hold":
                # Pure hold (pre-PV dawn wait): battery flat, never charge/drain.
                schmitt = "Hold"
            elif self._policy_override == "no_drain":
                # overflow-fits / past-safe: run the Schmitt but clamp Drain→Hold
                # (no round-trip). Charge still fires for the evening reserve.
                schmitt = compute_proposed_phase(soc_kwh, self._charge_below, self._drain_above, True, was_draining=self._was_draining)
                if schmitt == "Drain":
                    schmitt = "Hold"
            else:
                schmitt = compute_proposed_phase(soc_kwh, self._charge_below, self._drain_above, True, was_draining=self._was_draining)
            # R16a: remember whether we are mid-drain so the deadband applies only
            # on the way IN. Without this the drain stops at the deadband edge
            # instead of running to target, which is the flap in mirror image.
            self._was_draining = schmitt == "Drain"
            intended_policy = phase_to_policy(schmitt)
            # Sell floor (v32.3): the guard's "stop Max Export at this SOC" level.
            # Use the curtailment drain target (floor_kwh = overflow_floor) ONLY
            # during a genuine curtailment drain (Schmitt Drain, no override) —
            # incl. the pre-PV drain — so the big-overflow deep drain still works.
            # Otherwise (session dump, Hold, Charge, no_drain) use the overnight
            # reserve, the level we actually preserve. Publishing the rising
            # overflow_floor as the sell floor while Holding read as nonsense on a
            # low-overflow morning (climbed to ~68% at 8% SOC) and under-sold saving
            # sessions (stopped the dump at 68% instead of the overnight reserve).
            is_curtailment_drain = self._policy_override is None and schmitt == "Drain"
            if is_curtailment_drain:
                sell_floor_kwh = floor_kwh
            elif self._overnight_target_kwh:
                sell_floor_kwh = self._overnight_target_kwh
            else:
                sell_floor_kwh = DEFAULT_KEEP_FLOOR_PCT / 100.0 * soc_max
            intended_keep = min(max(sell_floor_kwh / max(soc_max, 0.1) * 100, low_soc), 95.0)
            # The level the guard will actually stop a session dump at — the
            # projection must not run through it.
            session_sell_floor_kwh = sell_floor_kwh
            # At-a-glance reason: mode + human override label + SOC% + band%
            # (never mix units; never expose internal codes like no_drain).
            # kWh stays on attributes for detail / cards that opt in.
            charge_pct = self._charge_below / max(soc_max, 0.1) * 100.0
            drain_pct = self._drain_above / max(soc_max, 0.1) * 100.0
            ovr_label = self._override_label()
            ovr = " · {}".format(ovr_label) if ovr_label else ""
            # reason is the one-line WHY only. SOC, the band, the drain-floor
            # source and the override label are all published as attributes and
            # rendered by the card as their own lines — restating them here
            # printed every fact twice (observed 2026-08-04).
            reason = "{}{}".format(schmitt, ovr)
        elif plugin_active:
            intended_policy = POLICY_PREDBAT
            intended_keep = None
            charge_pct = drain_pct = None
            reason = "low-SOC handover · {:.0f}% ≤ {:.0f}% → MSC".format(soc_pct, low_soc)
        else:
            intended_policy = POLICY_PREDBAT
            intended_keep = DEFAULT_KEEP_FLOOR_PCT
            charge_pct = drain_pct = None
            reason = "inactive → Predbat"

        gate = str(self.base.get_state_wrapper(SIG_POLICY_CONTROL_ENABLE, default="off")).lower()
        acting = gate in ("on", "true")
        override_choice = str(self.base.get_state_wrapper(SIG_OVERRIDE_SELECT, default=OVERRIDE_OFF) or OVERRIDE_OFF)
        manual = override_choice not in (OVERRIDE_OFF, "unknown", "unavailable", "")

        # Under manual override the sensor must report what will ACTUALLY happen —
        # the override IS the policy the heartbeat dispatches (RD13a precedence:
        # override > session > select). Publishing the plugin's own preference
        # here made the sensor contradict its own reason string and disagree with
        # the inverter (observed 2026-07-29 08:44: state "Max Export", reason
        # "manual override", override "Hold Battery"). The plugin's preference is
        # still worth showing — it is what resumes when the override clears — so
        # it moves into the reason.
        #
        # The session layer was documented here from the start and never
        # implemented, so the middle rung of the precedence was invisible:
        # RD14c moved session DISPATCH to the heartbeat, which forces Max Export
        # off the calendar WITHOUT writing the select — so nothing downstream
        # could see it. Live 2026-08-03 19:13, mid-session: battery -3.84 kW,
        # export 3.68 kW at the cap, card reading "Hold Battery · surplus fits".
        #
        # Mirrors sig_dispatch_heartbeat.yaml term for term:
        #   policy = override        if override active
        #            else 'Max Export' if (session_live and raw != 'Predbat')
        #            else raw
        # The `!= Predbat` guard matters: once we have handed back, Predbat owns
        # the machine and the heartbeat stands down rather than making a second
        # writer — the display must stand down with it.
        session_dispatch = bool(acting and self._is_session_dispatching() and intended_policy != POLICY_PREDBAT and not manual)
        if acting and manual:
            published_policy = override_choice
            published_reason = "manual · {} (plugin would {})".format(override_choice, intended_policy)
        elif session_dispatch:
            published_policy = POLICY_MAX_EXPORT
            published_reason = "saving session · exporting at cap (plugin would {})".format(intended_policy)
        else:
            published_policy = intended_policy
            published_reason = reason

        # Headroom shortfall (same formula as the floor — never re-derive on the card).
        # need = safety × p90 + min(max_reserved, p90); have = soc_max − soc.
        # Publish % for the at-a-glance card; kWh kept for diagnostics detail.
        headroom_need_kwh = None
        headroom_have_kwh = None
        headroom_short_kwh = None
        headroom_need_pct = None
        headroom_have_pct = None
        headroom_short_pct = None
        try:
            ovf = float(self._overflow_p90 or 0.0)
            # With no forecast overflow there is no headroom question to answer.
            # Publishing "fits · 53% spare" against zero is noise the card then
            # dutifully renders (observed 2026-08-03 19:29). None means "no
            # verdict" and the card omits the line; 0 would read as a real answer.
            if ovf > 0:
                max_res = float(getattr(self, "_effective_max_reserved", MAX_RESERVED_KWH) or MAX_RESERVED_KWH)
                headroom_need_kwh = round(required_headroom_kwh(ovf, max_res, OVERFLOW_SAFETY_FACTOR), 2)
                headroom_have_kwh = round(max(0.0, soc_max - soc_kwh), 2)
                headroom_short_kwh = round(headroom_need_kwh - headroom_have_kwh, 2)
                denom = max(soc_max, 0.1)
                headroom_need_pct = round(headroom_need_kwh / denom * 100.0, 1)
                headroom_have_pct = round(headroom_have_kwh / denom * 100.0, 1)
                headroom_short_pct = round(headroom_short_kwh / denom * 100.0, 1)
        except (TypeError, ValueError):
            pass

        # Where the dump leaves us: the number you actually want mid-session,
        # and one the card cannot derive from anything else it shows.
        session_end_soc_pct = None
        if session_dispatch:
            try:
                end_kwh = estimate_session_end_kwh(
                    soc_kwh=soc_kwh,
                    cap_kw=float(getattr(self, "_effective_dno", 3.68) or 3.68),
                    load_kw=float(self.base.get_state_wrapper(SIG_LOAD_POWER, default=0.5) or 0.5),
                    pv_kw=float(self.base.get_state_wrapper(SIG_PV_POWER, default=0.0) or 0.0),
                    minutes_remaining=self._session_minutes_remaining(),
                    floor_kwh=session_sell_floor_kwh,
                )
                session_end_soc_pct = round(end_kwh / max(soc_max, 0.1) * 100.0, 1)
            except (TypeError, ValueError, AttributeError):
                session_end_soc_pct = None

        # Always publish the intended decision — this is what you watch in observe-only.
        # Attributes are the single source for the Why This Mode card (report, never re-derive).
        try:
            prefix = self.base.prefix
            overnight_target_kwh = float(self._overnight_target_kwh or 0.0) if getattr(self, "_overnight_target_kwh", None) else 0.0
            overnight_target_pct = round(overnight_target_kwh / max(soc_max, 0.1) * 100.0, 1) if overnight_target_kwh else None
            ovr_label = self._override_label()
            attrs = {
                "friendly_name": "Curtailment Intended Policy",
                "icon": "mdi:robot",
                "keep_floor_pct": round(intended_keep, 0) if intended_keep is not None else None,
                "low_soc_handover_pct": low_soc,
                "soc_pct": round(soc_pct, 1),
                "reason": published_reason,
                "acting": acting,
                "manual_override": manual,
                "policy_override": self._policy_override,  # machine code for tests
                "override_label": ovr_label,  # human: surplus fits / holding flat / must drain
                "charge_below_pct": round(charge_pct, 1) if charge_pct is not None else None,
                "drain_above_pct": round(drain_pct, 1) if drain_pct is not None else None,
                "charge_below_kwh": round(self._charge_below, 2) if plugin_active else None,
                "drain_above_kwh": round(self._drain_above, 2) if plugin_active else None,
                "overnight_target_kwh": round(overnight_target_kwh, 2) if overnight_target_kwh else None,
                "overnight_target_pct": overnight_target_pct,
                "overflow_p90_kwh": round(float(self._overflow_p90 or 0.0), 2),
                "overflow_safety_factor": OVERFLOW_SAFETY_FACTOR,
                # What set the drain floor, for the Why This Mode card. % first
                # (A0: at-a-glance is % SOC), kWh as the detail.
                "drain_above_source": self._drain_above_source,
                "drain_above_source_label": DRAIN_SOURCE_LABELS.get(self._drain_above_source, self._drain_above_source),
                "session_reserve_kwh": round(self._session_reserve_kwh, 2) if self._session_reserve_kwh else 0.0,
                "session_reserve_pct": round(self._session_reserve_kwh / max(soc_max, 0.1) * 100.0, 1) if self._session_reserve_kwh else 0.0,
                "session_start": self._get_session_start(),
                # True while the HEARTBEAT (not the select) is driving the dump.
                # The card must not warn "not applied" when the select and the
                # policy in force legitimately differ for this one reason.
                "session_dispatch": session_dispatch,
                "session_end_soc_pct": session_end_soc_pct,
                "headroom_need_kwh": headroom_need_kwh,
                "headroom_have_kwh": headroom_have_kwh,
                "headroom_short_kwh": headroom_short_kwh,
                "headroom_need_pct": headroom_need_pct,
                "headroom_have_pct": headroom_have_pct,
                "headroom_short_pct": headroom_short_pct,
            }
            self.base.dashboard_item(
                "sensor.{}_curtailment_intended_policy".format(prefix),
                published_policy,
                attrs,
            )
        except Exception as e:
            self._log_once("intended_policy_pub_err", "Curtailment: intended policy publish failed: {}".format(e))

        # Single-writer handoff (the plugin owns it). The CM WINDOW = plugin_active
        # while the gate is on; it spans low-SOC dips. The heartbeat register-writer
        # runs ONLY during the window; read_only follows the DRIVE (dropped on the
        # low-SOC handover). On the window edge we take/release control atomically.
        # First run: adopt live state so a restart mid-window reconciles rather than
        # stranding Predbat or double-writing.
        first_run = self._cm_controlling is None
        if first_run:
            self._read_only_set = bool(getattr(self.base, "set_read_only", False))
            self._cm_controlling = self._read_only_set

        if not acting:
            # Observe-only: never hold control. Release cleanly if we somehow were
            # (gate flipped off mid-window, or stale read_only from a crash).
            if self._cm_controlling:
                self._release_to_predbat()
                self._cm_controlling = False
            self._policy_driving = False
            return

        # Every deploy/restart resets plugin state, and the take/release toggles below
        # are EDGE-triggered on _cm_controlling — so an adopted value would leave the
        # automation enables wherever they happened to be. `first_run` forces the edge
        # once, in whichever branch actually applies, so a drifted pair (both on, or
        # both off) can't persist silently for the whole window. Reconciling in a
        # separate step ahead of the branches would double-toggle and briefly enable
        # the wrong writer.
        if manual:
            # RD13 manual override: the user owns the POLICY SELECT — we never write it.
            # But the WRITER ROLE must still follow whatever the policy says, whoever
            # set it. Manual override means "you choose the policy", not "the writer
            # role goes stale".
            #
            # 2026-07-28: this branch used to take CM control unconditionally. When
            # sig_keep_floor_guard hit the reserve during a manual Max Export drain and
            # set policy -> Predbat, the writer role stayed with CM — mappers disabled,
            # read_only on — so Predbat could not act on the policy it had just been
            # handed. Result: nobody driving, inverter left on its own MSC default.
            # The guard was right; the handover was silently incomplete.
            # RD13a: the OVERRIDE is the policy, so the writer role follows it —
            # not sig_dispatch_policy. The select can hold a stale "Predbat" that
            # the plugin itself wrote (e.g. the RD4 low-SOC handover) before the
            # override was set; keying off it disables the executor while the user
            # is still holding a policy.
            #
            # Live failure 2026-07-29 08:56: override "Hold Battery", select
            # "Predbat" from a 3%-SOC handover -> heartbeat disabled -> nothing
            # driving -> dispatch register frozen at 2.89 kW while PV rose to
            # 3.46, so the battery discharged 0.775 kW to cover the gap at 3% SOC.
            #
            # The override select has no "Predbat" option by design, so holding
            # ANY override means CM's executor must drive.
            want_cm = override_choice != POLICY_PREDBAT
            if first_run or self._cm_controlling != want_cm:
                self._set_writer(cm_driving=want_cm)
                self._cm_controlling = want_cm
            # read_only follows the DRIVE: suppress Predbat only while CM's executor
            # holds the wheel. On a manual hand to Predbat it must be cleared, or the
            # mappers are live but Predbat is still muzzled.
            if want_cm and not self._read_only_set:
                self._set_read_only(True)
                self._read_only_set = True
            elif not want_cm and self._read_only_set:
                self._set_read_only(False)
                self._read_only_set = False
            self._policy_driving = want_cm
            return

        if plugin_active:
            # CM window. Take the writer role (window start edge): mapper off so
            # Predbat can't write EMS modes underneath us, heartbeat on.
            if first_run or not self._cm_controlling:
                self._set_writer(cm_driving=True)
                self._cm_controlling = True
            # RD27: CM drives at ANY SOC inside the window. The old low-SOC branch
            # here was the one that actually WROTE the handover (the decision-side
            # copy above only shaped the published reason) — and it handed to nobody:
            # it cleared read_only but left the mappers OFF, because _set_writer(
            # cm_driving=True) runs a few lines up and stays. So Predbat was
            # un-muzzled with no write path, the heartbeat went inert in its Predbat
            # branch, and the plant fell through to the SIG's Maximum Self
            # Consumption default — filling the battery through the pre-overflow
            # window. Live 2026-08-06: 04:50 to 09:00, charging 3.5 kW with ZERO
            # export against a 19.89 kWh overflow forecast, surplus still under the
            # cap. ~2% of pack spent on headroom the peak then had to curtail.
            #
            # Handing back is a window-END decision (RD6 safe_time / sundown) and
            # nothing else. Below the drain floor the Schmitt gives Charge or Hold,
            # and RD22's sell-only clamp already stops the battery being sold — so
            # there is nothing the handover was protecting.
            if not self._read_only_set:
                self._set_read_only(True)
                self._read_only_set = True
            self._set_policy(intended_policy)
            self._set_keep_floor(intended_keep)
            self._policy_driving = True
        else:
            # Window end (safe_time / off): hand the whole machine back to Predbat.
            if first_run or self._cm_controlling:
                self._release_to_predbat()
                self._cm_controlling = False
            self._policy_driving = False

    def on_update(self):
        """Main entry point, called every Predbat cycle. v30: compute the floor,
        publish monitoring sensors, and drive the dispatch policy. The plugin no
        longer writes SIG registers or manages any legacy automation — all
        inverter control is the HA heartbeat/guard acting on the policy select."""
        try:
            enabled, dno_limit = self.get_config()
            self._dno_limit = dno_limit
            soc_max = getattr(self.base, "soc_max", 10)

            if not enabled:
                self.publish("off", soc_max, dno_limit)
                # Hand the policy back once if we were driving (RD10).
                self._publish_dispatch_policy(False, soc_max, getattr(self.base, "soc_kw", 0), soc_max)
                return

            # Fail closed: unreadable plant SOC → hold position, change nothing.
            # Never treat a missing read as 0.0 kWh (night re-take, 2026-07-29).
            if not self._soc_readable():
                self._last_decision = "hold: SOC unavailable"
                self._log_once(
                    "soc_unavailable",
                    "Curtailment: plant SOC unreadable ({}) — holding position, no policy/writer change".format(SIG_BATTERY_SOC_PCT),
                )
                try:
                    prefix = self.base.prefix
                    self.base.dashboard_item(
                        "sensor.{}_curtailment_intended_policy".format(prefix),
                        self.base.get_state_wrapper(SIG_POLICY_SELECT, default=POLICY_PREDBAT) or POLICY_PREDBAT,
                        {
                            "friendly_name": "Curtailment Intended Policy",
                            "icon": "mdi:alert",
                            "reason": "hold · SOC unavailable",
                            "acting": False,
                            "manual_override": False,
                            "soc_pct": None,
                        },
                    )
                except Exception:
                    pass
                return

            floor, phase = self.calculate(dno_limit)

            # Defer to Predbat charge windows when SOC below effective keep (R4).
            # ±0.2 kWh hysteresis via _r4_deferring flag (Bug 6): engage when SOC
            # drops below keep-0.2, release only when SOC ≥ keep+0.2. Prevents
            # 5-min flicker at the boundary.
            soc_kw = getattr(self.base, "soc_kw", 0)
            effective_keep = getattr(self.base, "best_soc_keep", 0)
            if phase != "off":
                # R4 (gated by GSHP CH active flag): only defer to Predbat charge
                # window when heating is active. In summer (CH off), no overnight
                # heating load to cover → plugin handles its own drain.
                gshp_ch = self._is_gshp_ch_active()
                engage_threshold = effective_keep - 0.2
                release_threshold = effective_keep + 0.2
                should_defer = should_defer_to_charge(
                    gshp_ch_active=gshp_ch,
                    soc_kw=soc_kw,
                    soc_keep=effective_keep,
                    was_deferring=self._r4_deferring,
                )
                # RD14-own: a live joined session outranks the R4 charge-window
                # defer. Handing the select to Predbat mid-session stops the
                # heartbeat dispatching (it requires select != Predbat), so the
                # sell would end early. Winter-only path (R4 is GSHP-gated), but
                # winter is exactly when sessions run.
                if should_defer and self._is_session_dispatching():
                    should_defer = False
                    self._r4_deferring = False

                if should_defer:
                    minutes_now = getattr(self.base, "minutes_now", 0)
                    charge_window_best = getattr(self.base, "charge_window_best", [])
                    charge_window_n = self.base.in_charge_window(charge_window_best, minutes_now)
                    if charge_window_n >= 0:
                        charge_limit_best = getattr(self.base, "charge_limit_best", [])
                        if charge_window_n < len(charge_limit_best):
                            charge_limit = charge_limit_best[charge_window_n]
                            if not self.base.is_freeze_charge(charge_limit):
                                if not self._r4_deferring:
                                    self.log("Curtailment: deferring to charge window (SOC {:.1f} < keep-0.2 {:.1f})".format(soc_kw, engage_threshold))
                                phase = "off"
                                floor = soc_max
                                self._last_decision = "off: R4 defer to charge window"
                                self._floor_source = "Predbat Charging"
                                self._r4_deferring = True
                            else:
                                self._r4_deferring = False
                        else:
                            self._r4_deferring = False
                    else:
                        self._r4_deferring = False
                else:
                    if self._r4_deferring:
                        self.log("Curtailment: releasing R4 defer (SOC {:.1f} ≥ keep+0.2 {:.1f})".format(soc_kw, release_threshold))
                    self._r4_deferring = False

            soc_pct = soc_kw / max(soc_max, 0.1) * 100
            floor_pct = floor / max(soc_max, 0.1) * 100

            # Log phase transitions
            if phase != self.last_phase:
                self.log(
                    "Curtailment: PHASE {} -> {} | SOC={:.1f}kWh ({:.0f}%) floor={:.1f}kWh ({:.0f}%) "
                    "overflow={:.1f}kWh dno={:.1f}kW p90_scale={:.1f} floor_scale={:.1f} safe_time={}".format(
                        self.last_phase or "none",
                        phase,
                        soc_kw,
                        soc_pct,
                        floor,
                        floor_pct,
                        self._remaining_overflow,
                        dno_limit,
                        self._p90_scale,
                        self._floor_scale,
                        self._safe_time_str,
                    )
                )
                self.last_phase = phase

            # Publish monitoring sensors, then drive the dispatch policy (RD9).
            # Gated internally by SIG_POLICY_CONTROL_ENABLE (acts only when on;
            # always publishes the intended policy for observation).
            self.publish(phase, floor, dno_limit)
            self._publish_dispatch_policy(phase != "off", floor, soc_kw, soc_max)

            # Tomorrow forecast (separate try/except — don't break today's control)
            try:
                tomorrow = self._compute_tomorrow_forecast()
                if tomorrow:
                    self._publish_tomorrow_forecast(tomorrow)
            except Exception as e:
                self.log("Curtailment: tomorrow forecast error: {}".format(e))

        except Exception as e:
            self.log("Curtailment plugin error: {}".format(e))
            try:
                soc_max = getattr(self.base, "soc_max", 10)
                self.publish("off", soc_max, self._dno_limit)
            except Exception:
                pass
