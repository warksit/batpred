# -----------------------------------------------------------------------------
# Curtailment Manager Plugin for Predbat — v18
# Solar-geometry floor algorithm to eliminate solar curtailment
#
# Works WITH the HA automation (curtailment_manager_dynamic_export_limit):
#   - Plugin (5-min): computes floor from solar geometry integral, publishes sensors
#   - HA automation (~5s): Charge (SOC < floor-0.5), Drain (SOC > floor+0.5), Hold (at floor)
#
# Control model (SIG inverter):
#   Active:   D-ESS mode, read_only=True (suppresses Predbat inverter control)
#   Inactive: MSC mode, read_only=False (Predbat resumes, fills battery from PV)
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
    compute_morning_gap,
    compute_remaining_overflow,
    compute_solar_overflow,
    compute_solcast_overflow,
    compute_expected_overflow,
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

# SIG entity names (Mum's system)
SIG_EMS_MODE = "select.sigen_plant_remote_ems_control_mode"
SIG_EXPORT_LIMIT = "number.sigen_plant_grid_export_limitation"
SIG_CHARGE_LIMIT = "number.sigen_plant_ess_charge_cut_off_state_of_charge"
SIG_PV_POWER = "sensor.sigen_plant_pv_power"
SIG_LOAD_POWER = "sensor.sigen_plant_consumed_power"

# HA input helper entity IDs
HA_ENABLE = "input_boolean.curtailment_manager_enable"

PREDICT_STEP = 5
SOC_MARGIN_KWH = 0.5

# SIG/Solcast sensor entities
SIG_DAILY_PV = "sensor.sigen_plant_daily_third_party_inverter_energy"
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
        self.last_ems_mode = None
        self.last_charge_limit = None
        self.last_export_limit = None
        self.was_active = False
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
        # Export target published to HA automation (-2 = inactive)
        self._export_target = -2
        self._actual_pv_kw = 0.0
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
        # Date this state belongs to — lets us detect day rollover in calculate()
        self._state_date = None
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
        self._peak_pv = 0.0
        self._peak_pv_time = 0
        self._floor_ratchet = None
        self._last_floor_scale = 0.0
        self._keep_recovered = False
        self._keep_drained_today = False
        self._r48_engaged_today = False
        self._state_date = datetime.now().strftime("%Y-%m-%d")

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
            # R55: still publish overnight_target so dashboard isn't blank.
            # No morning_gap available (pv_forecast_minute_step is set inside
            # calculate_plan, which runs AFTER on_before_plan), fall back to
            # soc_keep alone. Safety pct still applied to morning_gap=0 → 0.
            keep_in = float(context.get("best_soc_keep", 0.0))
            target_kwh = max(min(keep_in, soc_max), float(reserve or 0.0))
            self._overnight_target_kwh = target_kwh
            soc_pct = (target_kwh / soc_max * 100.0) if soc_max else 0.0
            self._publish_overnight_target(
                round(target_kwh, 2),
                {
                    "morning_gap_kwh": 0.0,
                    "safety_pct": round(self._get_overnight_safety_pct(), 1),
                    "soc_keep_kwh": round(keep_in, 2),
                    "soc_pct": round(soc_pct, 1),
                    "source": "no_pv_forecast",
                },
            )
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

        # R55 (v20): compute morning_gap and publish overnight_target.
        # - Window extended to forecast_minutes so the walk reaches tomorrow's
        #   PV (covers tonight's evening + overnight + dawn).
        # - skip_initial_surplus=True so a midday call doesn't short-circuit
        #   on today's PV — we want the UPCOMING overnight deficit, not "what
        #   happens between now and the first sustained-PV slot" (which is now).
        gap_end = max(solar_end, forecast_minutes)
        morning_gap_load = compute_morning_gap(
            pv_step,
            load_step,
            start_minute=solar_start,
            end_minute=gap_end,
            step_minutes=PREDICT_STEP,
            values_are_kwh=True,
            skip_initial_surplus=True,
        )
        # Translate load → battery drawdown (matches Predbat predict trajectory
        # which applies inverter+battery discharge losses).
        battery_loss_discharge = float(getattr(self.base, "battery_loss_discharge", 1.0) or 1.0)
        inverter_loss = float(getattr(self.base, "inverter_loss", 1.0) or 1.0)
        discharge_efficiency = max(0.5, battery_loss_discharge * inverter_loss)
        morning_gap_battery = morning_gap_load / discharge_efficiency
        # R55 formula: battery drawdown + soc_keep buffer + optional extra
        # margin. soc_keep is the forecast-error buffer; morning_gap_battery
        # is what we'll actually pull from the battery overnight.
        safety_pct = self._get_overnight_safety_pct()
        keep_in = float(context.get("best_soc_keep", 0.0))
        overnight_target_kwh = morning_gap_battery * (1.0 + safety_pct / 100.0) + keep_in
        overnight_target_kwh = max(overnight_target_kwh, reserve)
        overnight_target_kwh = min(overnight_target_kwh, soc_max)
        self._overnight_target_kwh = overnight_target_kwh
        soc_pct = (overnight_target_kwh / soc_max * 100.0) if soc_max > 0 else 0.0
        self._publish_overnight_target(
            round(overnight_target_kwh, 2),
            {
                "morning_gap_kwh": round(morning_gap_battery, 2),
                "morning_gap_load_kwh": round(morning_gap_load, 2),
                "discharge_efficiency": round(discharge_efficiency, 3),
                "safety_pct": round(safety_pct, 1),
                "soc_keep_kwh": round(keep_in, 2),
                "soc_pct": round(soc_pct, 1),
                "source": "tomorrow" if using_tomorrow else "today",
            },
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
            morning_gap_load = compute_morning_gap(
                pv_step,
                load_step,
                start_minute=PREDICT_STEP,
                end_minute=forecast_minutes,
                step_minutes=PREDICT_STEP,
                values_are_kwh=True,
                skip_initial_surplus=True,
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
        except Exception:
            pass

    def _get_p90_scale(self, lat, lon, doy, local_offset):
        """Get clear-sky scale from Solcast p90 forecast (R42).

        Reads detailedForecast attribute from today's Solcast sensor.
        Falls back to yesterday's scale if unavailable (R44).
        """
        try:
            detailed = self.base.get_state_wrapper(SOLCAST_TODAY, attribute="detailedForecast", default=[])
            if detailed:
                scale, peak_kw, peak_utc = p90_scale_from_forecast(detailed, lat, lon, doy, local_offset)
                if scale > 0:
                    self._p90_scale = scale
                    self._p90_peak_kw = peak_kw
                    return scale, peak_kw, peak_utc
        except Exception:
            pass
        # Fallback: yesterday's scale (changes ~1° elevation per day, R44)
        return self._p90_scale, self._p90_peak_kw, 0.0

    def _get_solcast_detailed(self):
        """Return Solcast detailedForecast list, or [] if unavailable."""
        try:
            detailed = self.base.get_state_wrapper(SOLCAST_TODAY, attribute="detailedForecast", default=[])
            if isinstance(detailed, list):
                return detailed
        except Exception:
            pass
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
            detailed = self.base.get_state_wrapper(SOLCAST_TODAY, attribute="detailedForecast", default=[])
            if detailed:
                p10, p50, p90 = p_scales_from_forecast(detailed, lat, lon, doy, local_offset)
                # At least p90 should be valid on a normal day; missing p10/p50
                # is unusual but not fatal (we'll treat their integrals as p90's).
                return p10, p50, p90
        except Exception:
            pass
        # Fallback: use cached p90 for all three (degenerates to current pre-R50)
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
        """Read Solcast analysis.confidence; fall back to CONFIDENCE_DEFAULT."""
        try:
            analysis = self.base.get_state_wrapper(SOLCAST_TODAY, attribute="analysis", default={}) or {}
            if isinstance(analysis, dict) and "confidence" in analysis:
                return float(analysis["confidence"])
        except (ValueError, TypeError, KeyError):
            pass
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

        buffer_pct = self._pre_pv_buffer_pct()
        target_kwh = soc_keep + (buffer_pct / 100.0) * soc_max

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
            self._overflow_p10 = round(self._compute_overflow_band("pv_estimate10", p10_fb, lat, lon, doy, utc_hours, safe_utc, dno_limit_kw, load_fc, calibration_ratio, detailed), 2)
            self._overflow_p50 = round(self._compute_overflow_band("pv_estimate", p50_fb, lat, lon, doy, utc_hours, safe_utc, dno_limit_kw, load_fc, calibration_ratio, detailed), 2)
            self._overflow_p90 = round(self._compute_overflow_band("pv_estimate90", p90_fb, lat, lon, doy, utc_hours, safe_utc, dno_limit_kw, load_fc, calibration_ratio, detailed), 2)
        except Exception:
            pass

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
                self._export_target = dno_limit_kw
                self.was_active = True
                self._save_state()
                return target_kwh, "active"

            self._last_decision = "off: no PV yet"
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
            self._export_target = -2
            self._last_decision = "off: p90_scale<0.5 (no Solcast)"
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

        if safe_mins is None:
            # Can't compute — assume far future (very high scale, unusual)
            safe_utc = utc_hours + 12.0
            safe_mins = 720
            self._safe_time_str = "none"
        elif safe_mins <= 0:
            # R56 (v20): past safe_time no longer deactivates — plugin keeps
            # running to drain SOC to effective_keep through the late
            # afternoon. Set integration window to a small slice so overflow
            # integral returns ~0; R54 then makes floor = effective_keep.
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
        overflow_p10 = self._compute_overflow_band("pv_estimate10", p10_fb, lat, lon, doy, utc_hours, safe_utc, dno_limit_kw, load_forecast_kw, calibration_ratio, detailed)
        overflow_p50 = self._compute_overflow_band("pv_estimate", p50_fb, lat, lon, doy, utc_hours, safe_utc, dno_limit_kw, load_forecast_kw, calibration_ratio, detailed)
        overflow_p90 = self._compute_overflow_band("pv_estimate90", floor_scale, lat, lon, doy, utc_hours, safe_utc, dno_limit_kw, load_forecast_kw, calibration_ratio, detailed)

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
        past_safe_time = utc_hours >= safe_utc

        # R56: deactivate at sundown (PV ≤ 0.1 after peak observed). Pre-PV
        # path is handled earlier; reaching here means PV is currently active
        # OR we're seeing a brief PV gap mid-day. The "peak observed" guard
        # ensures we don't deactivate just because PV momentarily dipped on a
        # cloudy morning before any real PV happened today.
        sundown = self._peak_pv > 0.5 and actual_pv < 0.1
        if sundown:
            self._last_decision = "off: sundown (peak={:.1f}, actual_pv={:.2f})".format(self._peak_pv, actual_pv)
            self._floor_ratchet = None
            self._export_target = -2
            # End-of-day reset: clear peak so tomorrow starts fresh
            if minutes_now > 1200:
                self._peak_pv = 0.0
                self._peak_pv_time = 0
            self._save_state()
            return soc_max, "off"
        # past_safe_time stays as a diagnostic only (R19). Plugin runs through
        # late-afternoon to drain SOC down to overnight target via R54.
        _ = past_safe_time

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

        # R54 (v20): single drain-target rule.
        # Both overflow_floor and effective_keep are "drain TO this level" —
        # the lower one wins (more drain). reserve is a hard floor.
        # On big-overflow days: overflow_floor < effective_keep → drain to
        #   overflow_floor (curtailment wins, R48 latch already relaxed
        #   effective_keep so this is safe).
        # On no/small-overflow days: overflow_floor → soc_max → drain only
        #   to effective_keep (overnight target, replaces R45 100% chase).
        floor = max(min(overflow_floor, effective_keep), reserve)
        floor = min(floor, soc_max)

        # Plugin publishes DNO as export cap when active; HA automation decides phase
        # (Charge/Hold/Drain) from SOC vs target with symmetric hysteresis. Plugin just
        # signals "active with this floor" — the automation handles fast-reaction phase
        # transitions on 5-sec cadence.
        self._export_target = dno_limit_kw

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
        excess = max(0, pv_to_release - load_to_release)

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

    def publish(self, phase, floor_kwh, dno_limit_kw, export_target=None):
        """Publish curtailment sensors via dashboard_item.

        Phase sensor shows Active/Off (plugin's strategic decision).
        Real-time phase (Drain/Hold) is published by the HA automation.
        export_target: kW export cap for HA automation. -2 = inactive.
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
            },
        )

        et = export_target if export_target is not None else -2
        self.base.dashboard_item(
            "sensor.{}_curtailment_export_target".format(prefix),
            et,
            {
                "friendly_name": "Curtailment Export Target",
                "unit_of_measurement": "kW",
                "icon": "mdi:transmission-tower-export",
                "dno_limit": dno_limit_kw,
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

        # Set live phase to Off when plugin is off
        if state == "Off":
            try:
                self.base.call_service_wrapper("input_text/set_value", entity_id="input_text.curtailment_live_phase", value="Off")
            except Exception:
                pass

    def write_sig(self, ems_mode, charge_limit, export_limit=None):
        """Write SIG entities, only when values change.

        Export limit is written FIRST (before EMS mode) to ensure there is
        never a window where D-ESS is active with a stale export limit.
        """
        if export_limit is not None and export_limit != self.last_export_limit:
            self.base.call_service_wrapper(
                "number/set_value",
                entity_id=SIG_EXPORT_LIMIT,
                value=export_limit,
            )
            self.last_export_limit = export_limit
            self.log("Curtailment: Set export limit -> {}kW".format(export_limit))

        if ems_mode != self.last_ems_mode:
            self.base.call_service_wrapper(
                "select/select_option",
                entity_id=SIG_EMS_MODE,
                option=ems_mode,
            )
            self.last_ems_mode = ems_mode
            self.log("Curtailment: Set EMS mode -> {}".format(ems_mode))

        if charge_limit != self.last_charge_limit:
            self.base.call_service_wrapper(
                "number/set_value",
                entity_id=SIG_CHARGE_LIMIT,
                value=charge_limit,
            )
            self.last_charge_limit = charge_limit
            self.log("Curtailment: Set charge limit -> {}%".format(charge_limit))

    def _set_read_only(self, value):
        """Set read_only via internal flag only — NOT via HA entity."""
        self.base.set_read_only = value
        item = self.base.config_index.get("set_read_only")
        if item:
            item["value"] = value

    def apply(self, phase):
        """Apply inverter control based on phase.

        Active:  D-ESS, export=0 (safe default), read_only=true.
                 HA automation overrides export limit within 5 seconds.
        Off:     MSC, export=DNO (cleanup), read_only=false.
        """
        active = phase != "off"

        if active:
            if not self.was_active:
                self.log("Curtailment activating")
                self.write_sig(
                    ems_mode="Command Discharging (ESS First)",
                    charge_limit=100,
                    export_limit=0,
                )
            else:
                self.write_sig(
                    ems_mode="Command Discharging (ESS First)",
                    charge_limit=100,
                )

            self._set_read_only(True)
            self.was_active = True

        elif self.was_active:
            self.log("Curtailment deactivating, restoring MSC")
            self.write_sig(
                ems_mode="Maximum Self Consumption",
                charge_limit=100,
            )
            self.base.call_service_wrapper(
                "number/set_value",
                entity_id=SIG_EXPORT_LIMIT,
                value=self._dno_limit,
            )

            self._set_read_only(False)

            self.last_ems_mode = None
            self.last_charge_limit = None
            self.last_export_limit = None
            self.was_active = False

    HA_AUTOMATION = "automation.curtailment_manager_dynamic_export_limit"

    def _cleanup_read_only(self):
        """Clear stale state left by a previous plugin run (e.g. after restart)."""
        if not self.was_active and self.base.set_read_only:
            self.log("Curtailment: clearing stale read_only from previous run")
            self._set_read_only(False)
        if not getattr(self, "_automation_checked", False):
            self._automation_checked = True
            try:
                state = str(self.base.get_state_wrapper(self.HA_AUTOMATION, default="on")).lower()
                if state == "off":
                    self.log("Curtailment: re-enabling HA automation after restart")
                    self.base.call_service_wrapper("automation/turn_on", entity_id=self.HA_AUTOMATION)
            except Exception:
                pass

    def on_update(self):
        """Main entry point, called every Predbat cycle."""
        try:
            self._cleanup_read_only()

            enabled, dno_limit = self.get_config()
            self._dno_limit = dno_limit

            if not enabled:
                if self.was_active:
                    self.apply("off")
                soc_max = getattr(self.base, "soc_max", 10)
                self.publish("off", soc_max, dno_limit)
                return

            floor, phase = self.calculate(dno_limit)
            soc_max = getattr(self.base, "soc_max", 10)

            # Defer to Predbat charge windows when SOC below effective keep (R4).
            # ±0.2 kWh hysteresis via _r4_deferring flag (Bug 6): engage when SOC
            # drops below keep-0.2, release only when SOC ≥ keep+0.2. Prevents
            # 5-min flicker at the boundary.
            soc_kw = getattr(self.base, "soc_kw", 0)
            effective_keep = getattr(self.base, "best_soc_keep", 0)
            if phase != "off":
                engage_threshold = effective_keep - 0.2
                release_threshold = effective_keep + 0.2
                should_defer = (soc_kw < release_threshold) if self._r4_deferring else (soc_kw < engage_threshold)

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

            # Manual hold: keep D-ESS even when plugin goes off
            if phase == "off" and self.was_active:
                manual_hold = self.base.get_state_wrapper("input_select.curtailment_manual_hold", default="Off") != "Off"
                if manual_hold:
                    self.log("Curtailment: manual_hold active — staying in D-ESS despite plugin off")
                    self.apply("active")
                    self.publish("off", floor, dno_limit, export_target=self._export_target)
                    return

            # Publish HA-side sensors BEFORE writing SIG entities. The order
            # matters because the HA automation has a Restore-MSC branch that
            # fires when (manual=Off, phase sensor=Off, EMS!=MSC). If apply()
            # ran first on the active edge, EMS becomes D-ESS while phase is
            # still Off — the automation reverses our EMS write within seconds.
            # Publishing first means phase=Active is visible by the time EMS
            # changes, so branch 3's condition no longer matches.
            self.publish(phase, floor, dno_limit, export_target=self._export_target)
            self.apply(phase)

            # Tomorrow forecast (separate try/except — don't break today's control)
            try:
                tomorrow = self._compute_tomorrow_forecast()
                if tomorrow:
                    self._publish_tomorrow_forecast(tomorrow)
            except Exception as e:
                self.log("Curtailment: tomorrow forecast error: {}".format(e))

        except Exception as e:
            self.log("Curtailment plugin error: {}".format(e))
            soc_max = getattr(self.base, "soc_max", 10)
            self.publish("off", soc_max, self._dno_limit)
            if self.was_active:
                try:
                    self.apply("off")
                except Exception:
                    pass
