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
    compute_effective_export_cap,
    compute_charge_below,
    compute_drain_above,
    compute_proposed_phase,
    phase_to_policy,
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

# HA input helper entity IDs
HA_ENABLE = "input_boolean.curtailment_manager_enable"

# v30 (DC-coupled) policy control — RD9. Gated behind its own flag (default off)
# so the plugin can be DEPLOYED dormant and staged-enabled separately from the
# legacy curtailment_manager_enable.
SIG_POLICY_CONTROL_ENABLE = "input_boolean.sig_plugin_policy_control"
SIG_POLICY_SELECT = "input_select.sig_dispatch_policy"
SIG_KEEP_FLOOR_HELPER = "input_number.sig_keep_floor_pct"
SIG_LOW_SOC_HANDOVER_HELPER = "input_number.sig_low_soc_handover_pct"
# Plugin owns the single-writer handoff: it enables the heartbeat (the register
# writer) only while CM drives, and parks the unit in EMS-MSC on handback so
# Predbat controls from the EMS plane. Never app modes (RD2).
SIG_HEARTBEAT_AUTOMATION = "automation.sig_dispatch_heartbeat"
SIG_EMS_MODE_SELECT = "select.sigen_plant_remote_ems_control_mode"
SIG_EMS_MODE_MSC = "Maximum Self Consumption"
DEFAULT_KEEP_FLOOR_PCT = 38.0  # overnight reserve default on handback (RD10)
DEFAULT_LOW_SOC_HANDOVER_PCT = 12.0  # below this, hand to MSC (RD4 "A")
POLICY_PREDBAT = "Predbat"
# v31 floor/handback (2026-07-19): saving-session reserve (Octopus sensor CM can
# read directly) + early-handback buffer (fit p90 overflow with this to spare).
SIG_SAVING_SESSION = "binary_sensor.octopus_energy_a_4ba7c915_octoplus_saving_sessions"
HA_EARLY_HANDBACK_BUFFER = "input_number.curtailment_early_handback_buffer_kwh"
EARLY_HANDBACK_BUFFER_DEFAULT = 1.5

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
OVERFLOW_SAFETY_FACTOR = 1.2

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
CONFIDENCE_HIGH_DEFAULT = 0.85  # ≥ this → use overflow_p90 (current pre-R50)
CONFIDENCE_LOW_DEFAULT = 0.60  # < this → blend toward overflow_p10
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

# State persistence file (Bug 2 / R46): preserves _peak_pv, _peak_pv_time,
# _floor_ratchet across plugin restarts within the same day.
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
        # Floor ratchet: floor can only rise (R11)
        self._floor_ratchet = None
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
        ratchet = data.get("floor_ratchet")
        self._floor_ratchet = float(ratchet) if ratchet is not None else None
        self._last_floor_scale = float(data.get("last_floor_scale", 0.0))
        self._keep_recovered = bool(data.get("keep_recovered", False))
        self._keep_drained_today = bool(data.get("keep_drained_today", False))
        self._r48_engaged_today = bool(data.get("r48_engaged_today", False))
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
                "Curtailment: restored state from {} (peak={:.2f}kW, ratchet={}, pv_history={} entries)".format(
                    path,
                    self._peak_pv,
                    self._floor_ratchet,
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
            "floor_ratchet": self._floor_ratchet,
            "last_floor_scale": self._last_floor_scale,
            "keep_recovered": self._keep_recovered,
            "keep_drained_today": self._keep_drained_today,
            "r48_engaged_today": self._r48_engaged_today,
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
        self._floor_ratchet = None
        self._last_floor_scale = 0.0
        self._keep_recovered = False
        self._keep_drained_today = False
        self._r48_engaged_today = False
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
        if remaining_overflow_total * OVERFLOW_SAFETY_FACTOR <= headroom_with_current_keep:
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
        attrs.update({"friendly_name": "Curtailment Solar SOC Keep Offset", "unit_of_measurement": "kWh", "icon": "mdi:solar-power"})
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
            battery_loss_discharge = float(getattr(self.base, "battery_loss_discharge", 1.0) or 1.0)
            inverter_loss = float(getattr(self.base, "inverter_loss", 1.0) or 1.0)
            discharge_efficiency = max(0.5, battery_loss_discharge * inverter_loss)
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
        conf_low, conf_high = self._get_confidence_thresholds()
        expected_overflow = compute_expected_overflow(
            p10=self._overflow_p10,
            p50=self._overflow_p50,
            p90=self._overflow_p90,
            confidence=self._confidence,
            low=conf_low,
            high=conf_high,
        )
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

        if utc_hours < drain_start_utc:
            return None  # too early — wait

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

    def calculate(self, dno_limit_kw):
        """Compute floor using v17 solar geometry model.

        Scale from Solcast p90 (R42). Floor = soc_max - overflow_integral × 1.25 (R9).
        Floor ratchet: floor can only rise (R11). Deactivate at safe_time (R6).

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
            # Compute forecast integrals from current Solcast so dashboard
            # shows expected overflow before activation.
            self._publish_forecast_overflow(lat, lon, doy, local_offset, utc_hours, dno_limit_kw)

            # R52: pre-PV drain — if forecast says big overflow + CH off + we
            # have time to drain at DNO before PV starts, activate now.
            pre_pv = self._pre_pv_drain_decision(lat, lon, doy, local_offset, utc_hours, dno_limit_kw)
            if pre_pv is not None:
                target_kwh, decision_str = pre_pv
                self._last_decision = "active (pre-PV): " + decision_str
                self._floor_ratchet = target_kwh
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
                self._save_state()
                return target_kwh, "active"

            self._last_decision = "off: no PV yet"
            self._floor_source = "Overnight Reserve"
            return soc_max, "off"

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
        conf_low, conf_high = self._get_confidence_thresholds()

        # R50 blend: confidence-weighted expected overflow.
        # Replaces the always-p90 single-integral approach.
        remaining_overflow = compute_expected_overflow(
            p10=overflow_p10,
            p50=overflow_p50,
            p90=overflow_p90,
            confidence=confidence,
            low=conf_low,
            high=conf_high,
        )
        # Diagnostics
        self._overflow_p10 = round(overflow_p10, 2)
        self._overflow_p50 = round(overflow_p50, 2)
        self._overflow_p90 = round(overflow_p90, 2)
        self._confidence = round(confidence, 2)
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

        # RD6/R6 (v30): CM owns the curtailment window ONLY. Deactivate at safe_time
        # (primary) — past it PV can't exceed the export cap, so there is no
        # curtailment to manage and the whole machine hands back to Predbat (evening
        # export, saving sessions, overnight reserve are Predbat's, not CM's). This
        # supersedes R56 (CM must NOT stay active to drain the battery in the evening).
        #
        # past_safe is a HARD stop on solar geometry alone — it must NOT depend on
        # the observed peak, because the end-of-day peak reset (below) zeroes
        # _peak_pv in the evening; a dusk PV blip (>0.1, so it skips the "no PV yet"
        # early return) would then leave peaked False, the guard would stop firing,
        # and calculate() would fall through to its "active" default → spurious dusk
        # re-activation. Pre-dawn is already handled by the "no PV yet" early return
        # BEFORE this block, so geometry alone is safe. Sundown (peak observed, PV
        # ≤ 0.1) stays as a BACKSTOP for days where safe_time can't be computed.
        peaked = self._peak_pv > 0.5
        past_safe = reached_safe_time
        sundown = peaked and actual_pv < 0.1
        # v31 early handback: once the battery can absorb ALL remaining p90
        # (pessimistic/"what if the clouds clear") overflow with a buffer to
        # spare, there is no clipping risk left even if CM does nothing — so hand
        # the afternoon back to Predbat instead of squatting on control to
        # safe_time. Good days: p90 stays big → exit at safe_time. Fizzled days:
        # exits early, Predbat gets the afternoon. peaked-guarded so it can't fire
        # before real PV.
        try:
            early_buffer = float(self.base.get_state_wrapper(HA_EARLY_HANDBACK_BUFFER, default=EARLY_HANDBACK_BUFFER_DEFAULT))
        except (TypeError, ValueError):
            early_buffer = EARLY_HANDBACK_BUFFER_DEFAULT
        overflow_fits = peaked and (battery_headroom - overflow_p90) >= early_buffer
        if past_safe or sundown or overflow_fits:
            trigger = "safe_time" if past_safe else ("overflow-fits" if overflow_fits else "sundown")
            self._last_decision = "off: {} (peak={:.1f}, actual_pv={:.2f}, p90={:.1f}, room={:.1f})".format(trigger, self._peak_pv, actual_pv, overflow_p90, battery_headroom)
            self._floor_ratchet = None
            self._floor_source = "Overnight Reserve"
            # End-of-day reset: clear peak so tomorrow starts fresh
            if minutes_now > 1200:
                self._peak_pv = 0.0
                self._peak_pv_time = 0
            self._save_state()
            return soc_max, "off"

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

        effective_max_reserved = MAX_RESERVED_KWH
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
                    effective_max_reserved = max(BUFFER_REDUCE_FLOOR_KWH, MAX_RESERVED_KWH * BUFFER_REDUCE_FACTOR)
                    self._buffer_reduced = True
        # Append after the lookup so the current sample doesn't match itself
        self._pv_history.append((minutes_now, solcast_so_far, sig_daily_pv))
        self._effective_max_reserved = effective_max_reserved

        buffer_kwh = min(effective_max_reserved, max(0.0, remaining_overflow))
        max_target_soc = soc_max - buffer_kwh
        overflow_floor = max_target_soc - remaining_overflow * OVERFLOW_SAFETY_FACTOR
        overflow_floor = max(overflow_floor, 0.0)

        # Overflow floor ratchet (R11): only the overflow reservation ratchets.
        # Bypassed when floor_scale has increased (R43 safety path — sunnier than
        # forecast means MORE headroom needed, so allow floor to drop).
        scale_rose = floor_scale > self._last_floor_scale + 0.01
        if self._floor_ratchet is not None and not scale_rose:
            overflow_floor = max(overflow_floor, self._floor_ratchet)
        self._floor_ratchet = overflow_floor
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
        needs_room = remaining_overflow * OVERFLOW_SAFETY_FACTOR > room_with_base_keep
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
        overnight_for_recovery = (self._overnight_target_kwh if self._overnight_target_kwh is not None else effective_keep) + self._session_reserve_kwh
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
                "actual_scale": round(self._actual_scale, 2),
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
            drain_above = round(compute_drain_above(reserve, self._overflow_floor_kwh, self._effective_keep_kwh), 2)
        else:
            charge_below = 0.0
            drain_above = round(soc_max, 2)

        # Store for _publish_dispatch_policy (RD9): the v30 policy selection reuses
        # the same split thresholds this cycle rather than recomputing them.
        self._charge_below = charge_below
        self._drain_above = drain_above

        self.base.dashboard_item(
            "sensor.{}_curtailment_charge_below".format(prefix),
            charge_below,
            {
                "friendly_name": "Curtailment Charge Below (P10 Recovery)",
                "unit_of_measurement": "kWh",
                "device_class": "energy",
                "state_class": "measurement",
                "icon": "mdi:battery-arrow-up",
                "p10_pv_remaining_kwh": self._p10_pv_remaining_kwh,
                "p50_pv_remaining_kwh": self._p50_pv_remaining_kwh,
                "load_remaining_kwh": self._load_remaining_kwh,
                "overnight_target_kwh": round(self._overnight_target_kwh, 2) if self._overnight_target_kwh is not None else None,
                "confidence": self._confidence,
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
                "friendly_name": "Curtailment Drain Above (Curt Floor)",
                "unit_of_measurement": "kWh",
                "device_class": "energy",
                "state_class": "measurement",
                "icon": "mdi:battery-arrow-down",
            },
        )
        # R50 diagnostics promoted to dedicated sensors so HA recorder retains
        # statistics (state_class=measurement) for trend graphs and forecast-vs-actual
        # analysis. Same values as the corresponding overflow_p* attributes on
        # sensor.{prefix}_curtailment_phase.
        for suffix, value, friendly in (
            ("overflow_p10", self._overflow_p10, "Curtailment Overflow P10"),
            ("overflow_p50", self._overflow_p50, "Curtailment Overflow P50"),
            ("overflow_p90", self._overflow_p90, "Curtailment Overflow P90"),
        ):
            self.base.dashboard_item(
                "sensor.{}_curtailment_{}".format(prefix, suffix),
                value,
                {
                    "friendly_name": friendly,
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
        """Set input_number.sig_keep_floor_pct, only when it changes materially."""
        pct = round(float(pct), 0)
        try:
            current = float(self.base.get_state_wrapper(SIG_KEEP_FLOOR_HELPER, default=-1))
        except (TypeError, ValueError):
            current = -1
        if abs(current - pct) < 0.5:
            return
        try:
            self.base.call_service_wrapper("input_number/set_value", entity_id=SIG_KEEP_FLOOR_HELPER, value=pct)
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
        return compute_session_reserve(best_mins, cap_kw)

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

    def _release_to_predbat(self):
        """Window end (safe_time / off): hand the whole machine back to Predbat.
        Order (RD2/RD6/RD10): disable the heartbeat writer, park EMS-MSC, set policy
        Predbat, reset the sell floor, then clear read_only so Predbat resumes."""
        self._set_automation(SIG_HEARTBEAT_AUTOMATION, False)
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
            low_soc = float(self.base.get_state_wrapper(SIG_LOW_SOC_HANDOVER_HELPER, default=DEFAULT_LOW_SOC_HANDOVER_PCT))
        except (TypeError, ValueError):
            low_soc = DEFAULT_LOW_SOC_HANDOVER_PCT
        soc_pct = soc_kwh / max(soc_max, 0.1) * 100

        # Decide the intended policy + keep floor (pure decision, no side effects yet)
        if plugin_active and soc_pct > low_soc:
            schmitt = compute_proposed_phase(soc_kwh, self._charge_below, self._drain_above, True)
            intended_policy = phase_to_policy(schmitt)
            intended_keep = min(max(floor_kwh / max(soc_max, 0.1) * 100, 5.0), 95.0)
            reason = "active {} | soc {:.0f}% band [{:.1f}, {:.1f}] kWh".format(schmitt, soc_pct, self._charge_below, self._drain_above)
        elif plugin_active:
            intended_policy = POLICY_PREDBAT
            intended_keep = None
            reason = "low-SOC handover ({:.0f}% <= {:.0f}%) -> MSC".format(soc_pct, low_soc)
        else:
            intended_policy = POLICY_PREDBAT
            intended_keep = DEFAULT_KEEP_FLOOR_PCT
            reason = "inactive -> hand back to Predbat"

        gate = str(self.base.get_state_wrapper(SIG_POLICY_CONTROL_ENABLE, default="off")).lower()
        acting = gate in ("on", "true")

        # Always publish the intended decision — this is what you watch in observe-only.
        try:
            prefix = self.base.prefix
            self.base.dashboard_item(
                "sensor.{}_curtailment_intended_policy".format(prefix),
                intended_policy,
                {
                    "friendly_name": "Curtailment Intended Policy",
                    "icon": "mdi:robot",
                    "keep_floor_pct": round(intended_keep, 0) if intended_keep is not None else None,
                    "low_soc_handover_pct": low_soc,
                    "soc_pct": round(soc_pct, 1),
                    "reason": reason,
                    "acting": acting,
                },
            )
        except Exception as e:
            self._log_once("intended_policy_pub_err", "Curtailment: intended policy publish failed: {}".format(e))

        # Single-writer handoff (the plugin owns it). The CM WINDOW = plugin_active
        # while the gate is on; it spans low-SOC dips. The heartbeat register-writer
        # runs ONLY during the window; read_only follows the DRIVE (dropped on the
        # low-SOC handover). On the window edge we take/release control atomically.
        # First run: adopt live state so a restart mid-window reconciles rather than
        # stranding Predbat or double-writing.
        if self._cm_controlling is None:
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

        if plugin_active:
            # CM window. Ensure the heartbeat writer is running (window start edge).
            if not self._cm_controlling:
                self._set_automation(SIG_HEARTBEAT_AUTOMATION, True)
                self._cm_controlling = True
            if soc_pct > low_soc:
                # CM drives: suppress Predbat, set the policy.
                if not self._read_only_set:
                    self._set_read_only(True)
                    self._read_only_set = True
                self._set_policy(intended_policy)
                self._set_keep_floor(intended_keep)
            else:
                # RD4 low-SOC handover: let EMS-MSC cover load; drop drive suppression
                # but STAY in the window (heartbeat on) so we re-drive when SOC recovers.
                if self._read_only_set:
                    self._set_read_only(False)
                    self._read_only_set = False
                self._set_policy(POLICY_PREDBAT)
            self._policy_driving = True
        else:
            # Window end (safe_time / off): hand the whole machine back to Predbat.
            if self._cm_controlling:
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
