# -----------------------------------------------------------------------------
# Curtailment Manager Plugin for Predbat
# SOC trajectory algorithm (v8) to eliminate solar curtailment
#
# Works WITH the HA automation (curtailment_manager_dynamic_export_limit):
#   - Plugin (5-min): computes phase from SOC trajectory, publishes sensors
#   - HA automation (~5s): reactive export limit control in holding phase
#
# Control model (SIG inverter):
#   Active:  D-ESS mode, read_only=True (suppresses Predbat inverter control)
#   Inactive: MSC mode, read_only=False (Predbat resumes)
#
# v8 phases:
#   charge:  SOC below adaptive floor → block export, absorb all PV
#   export:  Currently overflowing OR SOC above floor + excess PV → export=DNO
#   hold:    PV generating, SOC at floor → HA automation tracks PV-load
#   off:     No overflow expected, PV negligible → hand to Predbat
# -----------------------------------------------------------------------------

import math

from curtailment_calc import (
    compute_morning_gap,
    compute_remaining_overflow,
    simulate_soc_trajectory,
    solar_elevation,
    compute_release_time,
    SAFE_PV_THRESHOLD_KW,
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
SOC_CAP_FACTOR = 0.90  # Target 90% max during overflow — 10% headroom for spikes

# SIG/Solcast sensor entities for energy ratio
SIG_DAILY_PV = "sensor.sigen_plant_daily_third_party_inverter_energy"
PREDBAT_PV_TODAY = "sensor.predbat_pv_today"


class CurtailmentPlugin(PredBatPlugin):
    """
    Curtailment manager v8 — SOC trajectory algorithm.

    Key insight: simulate the battery SOC trajectory over the remaining solar
    day (with curtailment active). If the peak SOC will exceed 95% of capacity,
    the battery will fill — we need to manage export. The floor (target SOC) is
    set so there is exactly enough headroom to absorb the remaining overflow.

    Phases:
        charge:  SOC below adaptive floor → export=0, absorb all PV
        export:  Currently overflowing OR draining above floor → export=DNO
        hold:    PV generating, SOC at floor → HA automation tracks PV-load
        off:     No overflow expected or solar done → hand to Predbat
    """

    priority = 200  # Run after cold_weather_plugin (priority 100)

    def __init__(self, base):
        super().__init__(base)
        self.last_ems_mode = None
        self.last_charge_limit = None
        self.last_export_limit = None
        self.was_active = False
        self._dno_limit = 4.0
        self.last_phase = None
        self._energy_ratio = 1.0  # last computed energy ratio (for publish)
        self._load_ratio = 1.0  # last computed load ratio (for publish)
        self._seen_overflow_today = False  # sticky: once overflow seen, stay active until PV done
        # Rolling PV max for solar geometry release
        self._pv_history = []  # list of (minutes_now, actual_pv_kw)
        self._release_scale = 0
        self._release_crossing = "none"
        # Caching for on_before_plan
        self._cached_keep = None
        self._cached_at = 0  # minutes_now when last computed
        self._cached_offset = None  # (value, attrs) for republishing on cache hit

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

        # Caching: overnight (22:00-06:00) recompute max every 30 min,
        # morning (06:00-12:00) recalculate each cycle, afternoon skip
        if self._cached_keep is not None:
            minutes_since = minutes_now - self._cached_at
            if minutes_since < 0:
                minutes_since += 1440  # wrapped past midnight
            # 06:00=360, 12:00=720, 22:00=1320
            if 360 <= minutes_now < 720:
                pass  # morning: always recalculate
            elif minutes_since < 30:
                context["best_soc_keep"] = min(context["best_soc_keep"], self._cached_keep)
                if self._cached_offset is not None:
                    self.base.dashboard_item(
                        "sensor.{}_curtailment_solar_offset".format(self.base.prefix),
                        self._cached_offset[0],
                        self._cached_offset[1],
                    )
                return context

        pv_step = getattr(self.base, "pv_forecast_minute_step", {})
        load_step = getattr(self.base, "load_minutes_step", {})
        soc_max = getattr(self.base, "soc_max", 10)
        reserve = getattr(self.base, "reserve", 0)
        forecast_minutes = getattr(self.base, "forecast_minutes", 1440)

        if not pv_step:
            self._publish_offset(0.0, {"original_keep": round(context["best_soc_keep"], 2), "reason": "no_pv_forecast"})
            return context

        dno_limit = self.base.get_arg("export_limit", 4000, index=0) / 1000.0
        solar_end = min(forecast_minutes, max(PREDICT_STEP, 23 * 60 - minutes_now))

        # Use trajectory to check if battery will fill tomorrow
        peak_soc, net_charge, last_danger = simulate_soc_trajectory(
            pv_step,
            load_step,
            soc_max,
            soc_max,
            dno_limit,
            energy_ratio=1.0,  # no ratio for forecast
            start_minute=PREDICT_STEP,
            end_minute=solar_end,
            step_minutes=PREDICT_STEP,
            values_are_kwh=True,
        )

        soc_cap = soc_max * SOC_CAP_FACTOR
        will_fill = peak_soc > soc_cap

        if not will_fill:
            self._publish_offset(0.0, {"original_keep": round(context["best_soc_keep"], 2), "will_fill": False})
            self._cached_keep = context["best_soc_keep"]
            self._cached_at = minutes_now
            return context

        morning_gap = compute_morning_gap(
            pv_step,
            load_step,
            start_minute=0,
            end_minute=solar_end,
            step_minutes=PREDICT_STEP,
            values_are_kwh=True,
        )

        margin = 0.5
        solar_adjusted_keep = max(morning_gap + margin, reserve)
        current_keep = context["best_soc_keep"]

        if solar_adjusted_keep < current_keep:
            self.log("Curtailment: reducing best_soc_keep {:.2f} -> {:.2f} kWh (morning_gap={:.2f}, net_charge={:.2f})".format(current_keep, solar_adjusted_keep, morning_gap, net_charge))
            context["best_soc_keep"] = solar_adjusted_keep
            self._publish_offset(round(solar_adjusted_keep - current_keep, 2), {"morning_gap_kwh": round(morning_gap, 2), "net_charge_kwh": round(net_charge, 2), "original_keep": round(current_keep, 2), "adjusted_keep": round(solar_adjusted_keep, 2)})
        else:
            self._publish_offset(0.0, {"morning_gap_kwh": round(morning_gap, 2), "net_charge_kwh": round(net_charge, 2), "original_keep": round(current_keep, 2)})

        self._cached_keep = context["best_soc_keep"]
        self._cached_at = minutes_now
        return context

    def get_config(self):
        """Read configuration from HA input helpers and Predbat config."""
        enabled = self.base.get_state_wrapper(HA_ENABLE, default="off")
        enabled = str(enabled).lower() in ("on", "true")

        # DNO limit from Predbat's export_limit config (apps.yaml, in Watts)
        dno_limit = self.base.get_arg("export_limit", 4000, index=0) / 1000.0

        return enabled, dno_limit

    def _publish_offset(self, value, attrs):
        """Publish curtailment solar offset sensor and cache for reuse."""
        attrs.update({"friendly_name": "Curtailment Solar SOC Keep Offset", "unit_of_measurement": "kWh", "icon": "mdi:solar-power"})
        self.base.dashboard_item("sensor.{}_curtailment_solar_offset".format(self.base.prefix), value, attrs)
        self._cached_offset = (value, attrs)

    def _get_energy_ratio(self):
        """Compute energy ratio: actual cumulative PV / calibrated forecast cumulative PV.

        Uses calibrated forecast (totalCL/remainingCL from predbat_pv_today) to match
        the reference frame of remaining_overflow (which uses calibrated pv_forecast_minute_step).

        Returns (actual_pv, actual_load, energy_ratio) where energy_ratio smoothly
        blends from 1.0 during the first 10% of daily forecast production.
        """
        try:
            actual_pv = float(self.base.get_state_wrapper(SIG_PV_POWER, default=0))
            actual_load = float(self.base.get_state_wrapper(SIG_LOAD_POWER, default=0))
        except (ValueError, TypeError):
            return 0.0, 0.0, 1.0

        try:
            actual_produced = float(self.base.get_state_wrapper(SIG_DAILY_PV, default=0))
        except (ValueError, TypeError):
            actual_produced = 0.0

        # Read calibrated forecast from predbat_pv_today attributes.
        # get_state_wrapper(entity, attribute="name") is the standard pattern
        # used throughout the codebase (see cold_weather_plugin.py, web.py, etc.)
        try:
            total_cl = float(self.base.get_state_wrapper(PREDBAT_PV_TODAY, attribute="totalCL", default=0))
            remaining_cl = float(self.base.get_state_wrapper(PREDBAT_PV_TODAY, attribute="remainingCL", default=0))
        except (ValueError, TypeError):
            total_cl = 0
            remaining_cl = 0

        # Fallback: if calibrated forecast attributes are unavailable (e.g. first boot),
        # use raw Solcast sensors. Note: these are uncalibrated, so if the calibration
        # factor is far from 1.0 the ratio will be slightly off until predbat_pv_today
        # becomes available.
        if total_cl <= 0:
            try:
                forecast_today = float(self.base.get_state_wrapper("sensor.solcast_pv_forecast_forecast_today", default=0))
                forecast_remaining = float(self.base.get_state_wrapper("sensor.solcast_pv_forecast_forecast_remaining_today", default=0))
                total_cl = forecast_today
                remaining_cl = forecast_remaining
            except (ValueError, TypeError):
                pass

        forecast_produced = total_cl - remaining_cl

        # Blend: smoothly ramp from 1.0 toward real ratio over first 10% of daily forecast
        threshold = total_cl * 0.10
        if forecast_produced < 0.5 or threshold < 0.5:
            return actual_pv, actual_load, 1.0

        blend = min(1.0, forecast_produced / threshold)
        raw_ratio = actual_produced / max(forecast_produced, 0.5)
        energy_ratio = 1.0 + (raw_ratio - 1.0) * blend

        return actual_pv, actual_load, energy_ratio

    def _get_load_ratio(self):
        """Compute load ratio: actual cumulative load / predicted cumulative load.

        Uses predbat.load_energy_adjusted (today_so_far) as the predicted load
        consumed so far, and sigen daily load as actual.

        Returns load_ratio with same 10% blend as PV ratio for early stability.
        Ratio > 1 means actual load higher than predicted (less overflow risk).
        Ratio < 1 means actual load lower than predicted (more overflow risk).
        """
        try:
            actual_load_total = float(self.base.get_state_wrapper("sensor.sigen_plant_daily_load_consumption", default=0))
        except (ValueError, TypeError):
            return 1.0

        try:
            predicted_so_far = float(self.base.get_state_wrapper("predbat.load_energy_adjusted", attribute="today_so_far", default=0))
            predicted_today = float(self.base.get_state_wrapper("predbat.load_energy_adjusted", attribute="today", default=0))
        except (ValueError, TypeError):
            return 1.0

        if predicted_so_far < 2.0 or predicted_today < 5.0:
            return 1.0  # too early for meaningful ratio

        # 10% blend: stable early, responsive by mid-morning
        threshold = predicted_today * 0.10
        blend = min(1.0, predicted_so_far / max(threshold, 0.5))
        raw_ratio = actual_load_total / max(predicted_so_far, 0.5)
        load_ratio = 1.0 + (raw_ratio - 1.0) * blend

        return load_ratio

    def _get_rolling_pv_max(self, minutes_now, actual_pv_kw, window_minutes=30):
        """Track rolling max PV over last window_minutes.

        Called each cycle (~5 min). Returns max PV in window — the clear-sky
        envelope value at this time of day.
        """
        self._pv_history.append((minutes_now, actual_pv_kw))
        cutoff = minutes_now - window_minutes
        self._pv_history = [(t, v) for t, v in self._pv_history if t >= cutoff]
        return max(v for _, v in self._pv_history) if self._pv_history else 0.0

    def _compute_solar_release(self, minutes_now, actual_pv_kw, dno_limit_kw):
        """Compute minutes until solar geometry release.

        Uses the rolling 30-min max PV and current solar elevation to calibrate
        a clear-sky PV model: pv = scale * sin(elevation). Finds when this
        drops below DNO + min_base_load, then subtracts lead time for battery
        fill (headroom at 90% floor ≈ 1.8kWh, ~30 min).

        Returns (minutes_until_release, scale, crossing_time_str).
        Returns (None, 0, "none") if cannot compute.
        """
        try:
            lat = float(self.base.get_state_wrapper("zone.home", attribute="latitude", default=0))
            lon = float(self.base.get_state_wrapper("zone.home", attribute="longitude", default=0))
        except (ValueError, TypeError):
            return None, 0, "none"

        if lat == 0 and lon == 0:
            return None, 0, "none"

        now_utc = getattr(self.base, "now_utc", None)
        if now_utc is None:
            return None, 0, "none"

        utc_hours = now_utc.hour + now_utc.minute / 60.0 + now_utc.second / 3600.0
        doy = now_utc.timetuple().tm_yday

        max_pv = self._get_rolling_pv_max(minutes_now, actual_pv_kw)
        if max_pv < 1.0:
            return None, 0, "none"

        elev = solar_elevation(lat, lon, utc_hours, doy)
        sin_elev = math.sin(math.radians(elev))
        if sin_elev < 0.05:
            return 0, 0, "low_sun"

        scale = max_pv / sin_elev
        threshold = dno_limit_kw + MIN_BASE_LOAD_KW

        # Headroom: space from soc_cap to soc_max
        soc_max = getattr(self.base, "soc_max", 10)
        headroom = soc_max * (1.0 - SOC_CAP_FACTOR)

        release_mins, crossing_utc = compute_release_time(scale, lat, lon, doy, threshold, utc_hours, headroom_kwh=headroom)

        if release_mins is None:
            return None, scale, "none"

        # Format crossing time for diagnostics (local time = UTC + tz offset)
        # Use minutes_now to infer local offset: minutes_now is local minutes from midnight
        local_offset_hours = (minutes_now / 60.0) - utc_hours
        crossing_local = crossing_utc + local_offset_hours
        crossing_str = "{:02d}:{:02d}".format(int(crossing_local) % 24, int((crossing_local % 1) * 60))

        return release_mins, scale, crossing_str

    def calculate(self, dno_limit_kw):
        """Compute curtailment phase using v8 SOC trajectory algorithm."""
        pv_step = getattr(self.base, "pv_forecast_minute_step", {})
        load_step = getattr(self.base, "load_minutes_step", {})
        soc_kw = getattr(self.base, "soc_kw", 0)
        soc_max = getattr(self.base, "soc_max", 10)
        forecast_minutes = getattr(self.base, "forecast_minutes", 1440)

        if not pv_step or not soc_max:
            return soc_max, 0, "off", -2

        minutes_now = getattr(self.base, "minutes_now", 720)
        self._minutes_now_cached = minutes_now
        solar_end = min(forecast_minutes, max(PREDICT_STEP, 23 * 60 - minutes_now))

        # --- Energy ratio ---
        actual_pv, actual_load, energy_ratio = self._get_energy_ratio()
        self._energy_ratio = energy_ratio
        actual_excess = actual_pv - actual_load
        currently_overflowing = actual_excess > dno_limit_kw

        # --- Sticky activation ---
        if currently_overflowing:
            if not self._seen_overflow_today:
                self.log("Curtailment: overflow detected ({:.1f}kW excess) — activating for the day".format(actual_excess))
            self._seen_overflow_today = True
        if actual_pv < 0.1:
            self._seen_overflow_today = False

        # --- Load ratio ---
        load_ratio = self._get_load_ratio()
        self._load_ratio = load_ratio

        # --- SOC trajectory: unmanaged (MSC) for activation check ---
        # "Will battery fill if we DON'T manage?" — battery absorbs ALL excess
        peak_unmanaged, _, last_danger = simulate_soc_trajectory(
            pv_step,
            load_step,
            soc_kw,
            soc_max,
            dno_limit_kw,
            energy_ratio=energy_ratio,
            load_ratio=load_ratio,
            start_minute=PREDICT_STEP,
            end_minute=solar_end,
            step_minutes=PREDICT_STEP,
            values_are_kwh=True,
            unmanaged=True,
        )

        # --- SOC trajectory: managed (D-ESS) for floor calculation ---
        # "How much overflow will battery absorb if we export at DNO?"
        peak_soc, net_charge, _ = simulate_soc_trajectory(
            pv_step,
            load_step,
            soc_kw,
            soc_max,
            dno_limit_kw,
            energy_ratio=energy_ratio,
            load_ratio=load_ratio,
            start_minute=PREDICT_STEP,
            end_minute=solar_end,
            step_minutes=PREDICT_STEP,
            values_are_kwh=True,
            unmanaged=False,
        )

        soc_cap = soc_max * SOC_CAP_FACTOR
        will_fill = peak_unmanaged > soc_cap
        self._unmanaged_peak = peak_unmanaged

        # Check for danger slots (PV > 2kW in remaining forecast)
        step_to_kw = 60.0 / PREDICT_STEP
        has_danger = any(pv_step.get(m, 0) * step_to_kw > SAFE_PV_THRESHOLD_KW for m in range(PREDICT_STEP, solar_end, PREDICT_STEP))

        # Store diagnostic info
        self._trajectory_peak = peak_soc
        self._net_charge = net_charge
        self._will_fill = will_fill
        self._last_danger = last_danger
        if will_fill:
            self._activation_reason = "trajectory"
        elif self._seen_overflow_today:
            self._activation_reason = "sticky"
        elif has_danger:
            self._activation_reason = "danger_slots"
        else:
            self._activation_reason = "off"

        # --- Activation ---
        # Only activate if trajectory shows battery will fill OR overflow already seen.
        # Danger slots (PV > 2kW) alone are not enough — moderate PV days have
        # danger slots but the battery won't fill, so curtailment is unnecessary
        # and would block Predbat's charge windows.
        if not will_fill and not self._seen_overflow_today:
            return soc_max, 0, "off", -2

        # --- Pre-overflow drain ---
        # If overflow is coming (will_fill) but hasn't started yet (no overflow seen),
        # drain battery to make room. Target: SOC where remaining overflow fills to
        # soc_cap, leaving headroom. This maximizes export AND prevents filling
        # during the overflow period.
        soc_keep = getattr(self.base, "best_soc_keep", 0)
        reserve = getattr(self.base, "reserve", 0)

        remaining_overflow = compute_remaining_overflow(
            pv_step,
            load_step,
            dno_limit_kw,
            start_minute=PREDICT_STEP,
            end_minute=solar_end,
            step_minutes=PREDICT_STEP,
            values_are_kwh=True,
        )
        drain_target = max(soc_cap - remaining_overflow, soc_keep, reserve)
        self._drain_target = drain_target

        if will_fill and not self._seen_overflow_today and not currently_overflowing and actual_excess > 0:
            if soc_kw > drain_target + SOC_MARGIN_KWH:
                # Drain phase: export PV + discharge battery toward drain target
                self._target_charge_rate = 0
                self._activation_reason = "drain"
                return drain_target, net_charge, "export", -2

        # --- Floor (bidirectional, recomputed each cycle) ---
        floor = soc_cap - max(0, net_charge)
        floor = max(floor, soc_keep, reserve)
        floor = min(floor, soc_max)  # never above 100%

        # Release: solar geometry predicts PV will drop below overflow threshold
        try:
            release_mins, release_scale, crossing_str = self._compute_solar_release(minutes_now, actual_pv, dno_limit_kw)
            self._release_scale = release_scale
            self._release_crossing = crossing_str
            if release_mins is not None and release_mins <= 0:
                floor = soc_max  # safe to charge to 100%
        except Exception:
            # Fallback: old threshold-based release
            remaining_danger = any(pv_step.get(m, 0) * step_to_kw > SAFE_PV_THRESHOLD_KW for m in range(PREDICT_STEP, solar_end, PREDICT_STEP))
            if not remaining_danger and actual_pv < SAFE_PV_THRESHOLD_KW:
                floor = soc_max
            self._release_scale = 0
            self._release_crossing = "fallback"

        target_soc_kwh = floor

        # --- Target charge rate: how fast to reach floor ---
        energy_to_floor = max(0, target_soc_kwh - soc_kw)
        time_remaining_hours = max(last_danger / 60.0, 0.5)  # min 30 min
        if energy_to_floor > 0:
            target_charge_rate = energy_to_floor / time_remaining_hours
        else:
            target_charge_rate = 0.0  # at/above floor, no charging needed
        self._target_charge_rate = round(target_charge_rate, 2)

        # --- Phase (informational — export limit is now rate-based) ---
        if soc_kw < target_soc_kwh - SOC_MARGIN_KWH:
            phase = "charge"  # below floor, charging at controlled rate
        elif soc_kw > target_soc_kwh + 1.0 and actual_excess > 0:
            phase = "export"  # above floor, draining
        elif actual_excess > 0:
            phase = "hold"  # at floor, exporting excess
        else:
            phase = "hold"  # D-ESS on, battery covers deficit

        return target_soc_kwh, net_charge, phase, -2

    def publish(self, phase, target_soc_kwh, _unused, _unused2, dno_limit_kw):
        """Publish curtailment sensors via dashboard_item."""
        prefix = self.base.prefix
        soc_max = getattr(self.base, "soc_max", 10)
        target_pct = round(target_soc_kwh / soc_max * 100, 1) if soc_max > 0 else 100

        # Release time from solar geometry
        release_crossing = getattr(self, "_release_crossing", "none")
        release_scale = getattr(self, "_release_scale", 0)

        self.base.dashboard_item(
            "sensor.{}_curtailment_phase".format(prefix),
            phase.replace("_", " ").title(),
            {
                "friendly_name": "Curtailment Phase",
                "icon": "mdi:solar-power-variant",
                "energy_ratio": round(self._energy_ratio, 2),
                "load_ratio": round(getattr(self, "_load_ratio", 1.0), 2),
                "unmanaged_peak_soc_pct": round(getattr(self, "_unmanaged_peak", 0) / max(soc_max, 0.1) * 100, 1),
                "floor_pct": target_pct,
                "will_fill": getattr(self, "_will_fill", False),
                "activation_reason": getattr(self, "_activation_reason", "off"),
                "target_charge_rate": getattr(self, "_target_charge_rate", 0),
                "drain_target_pct": round(getattr(self, "_drain_target", 0) / max(soc_max, 0.1) * 100, 1),
                "release_time": release_crossing,
                "release_scale": round(release_scale, 1),
            },
        )

        self.base.dashboard_item(
            "sensor.{}_curtailment_target_soc".format(prefix),
            target_pct,
            {
                "friendly_name": "Curtailment Target SOC",
                "unit_of_measurement": "%",
                "icon": "mdi:battery-charging-medium",
                "target_kwh": round(target_soc_kwh, 2),
            },
        )

    def write_sig(self, ems_mode, charge_limit, export_limit=None):
        """Write SIG entities, only when values change.

        Export limit is written FIRST (before EMS mode) to ensure there is
        never a window where D-ESS is active with a stale export limit.
        """
        # Write export limit BEFORE EMS mode to avoid race condition:
        # D-ESS + stale export limit could force-discharge battery to grid
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
        """
        Set read_only via internal flag only — NOT via HA entity.
        Writing the HA entity triggers Predbat's switch event handler
        which forces an inverter reset back to MSC.
        """
        self.base.set_read_only = value
        item = self.base.config_index.get("set_read_only")
        if item:
            item["value"] = value

    def apply(self, phase, export_target_kw):
        """Apply inverter control based on phase. Manages read_only toggle."""
        activating = phase in ("export", "hold", "charge")

        if activating:
            if not self.was_active:
                self.log("Curtailment activating (phase={})".format(phase))

            # Map phase to export limit
            if phase == "charge":
                export_limit = 0
            elif phase == "export":
                export_limit = self._dno_limit
            else:  # holding
                export_limit = None  # HA automation tracks PV-load

            # D-ESS mode, block grid import, allow solar charging to 100%
            # Export limit is written FIRST inside write_sig() to avoid race
            self.write_sig(
                ems_mode="Command Discharging (ESS First)",
                charge_limit=100,
                export_limit=export_limit,
            )

            # In holding mode, HA automation controls export limit directly,
            # so our cached value becomes stale. Clear it so next phase
            # transition will always write the correct value.
            if export_limit is None:
                self.last_export_limit = None

            # Suppress Predbat's normal inverter control
            self._set_read_only(True)
            self.was_active = True

        elif self.was_active:
            # Deactivating: will_fill=false means battery won't fill, MSC is safe.
            # Sticky activation prevents reaching here on real overflow days.
            # No PV guard needed — trust the trajectory.
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

            # Reset tracked values so next activation re-writes everything
            self.last_ems_mode = None
            self.last_charge_limit = None
            self.last_export_limit = None
            self.was_active = False

    def _cleanup_read_only(self):
        """Clear stale read_only left by a previous plugin run (e.g. after restart)."""
        if not self.was_active and self.base.set_read_only:
            self.log("Curtailment: clearing stale read_only from previous run")
            self._set_read_only(False)

    def on_update(self):
        """Main entry point, called every Predbat cycle."""
        try:
            self._cleanup_read_only()

            enabled, dno_limit = self.get_config()
            self._dno_limit = dno_limit

            if not enabled:
                if self.was_active:
                    self.apply("off", -1)
                soc_max = getattr(self.base, "soc_max", 10)
                self.publish("off", soc_max, 0, -1, dno_limit)
                return

            target_soc_kwh, net_charge, phase, export_target_kw = self.calculate(dno_limit)

            # Don't take inverter control until PV is generating
            pv_step = getattr(self.base, "pv_forecast_minute_step", {})
            pv_now_kw = pv_step.get(0, 0) * (60.0 / PREDICT_STEP)
            if phase != "off" and pv_now_kw < 0.1:
                try:
                    actual_pv = float(self.base.get_state_wrapper(SIG_PV_POWER, default=0))
                except (ValueError, TypeError):
                    actual_pv = 0
                if actual_pv < 0.1:
                    if self.last_phase != "off":
                        self.log("Curtailment: waiting for PV (forecast {:.2f}kW, actual {:.1f}kW)".format(pv_now_kw, actual_pv))
                    phase = "off"
                    export_target_kw = -1
                    target_soc_kwh = soc_max

            # Defer to Predbat charge windows ONLY when SOC is below the
            # effective keep floor (battery genuinely needs grid charging,
            # e.g. morning cheap rate before GSHP drains it). Once SOC is
            # at or above keep, curtailment manages — ignore charge windows.
            soc_kw = getattr(self.base, "soc_kw", 0)
            soc_max = getattr(self.base, "soc_max", 10)
            effective_keep = getattr(self.base, "best_soc_keep", 0)
            if phase != "off" and soc_kw < effective_keep:
                charge_window_best = getattr(self.base, "charge_window_best", [])
                minutes_now = getattr(self.base, "minutes_now", 0)
                charge_window_n = self.base.in_charge_window(charge_window_best, minutes_now)
                if charge_window_n >= 0:
                    charge_limit_best = getattr(self.base, "charge_limit_best", [])
                    if charge_window_n < len(charge_limit_best):
                        charge_limit = charge_limit_best[charge_window_n]
                        if not self.base.is_freeze_charge(charge_limit):
                            if self.last_phase != "off":
                                self.log("Curtailment: deferring to charge window (SOC {:.1f} < keep {:.1f})".format(soc_kw, effective_keep))
                            phase = "off"
                            export_target_kw = -1
                            target_soc_kwh = soc_max

            target_pct = target_soc_kwh / max(soc_max, 0.1) * 100
            soc_pct = soc_kw / max(soc_max, 0.1) * 100

            # Log phase transitions
            if phase != self.last_phase:
                self.log(
                    "Curtailment: PHASE {} -> {} | SOC={:.1f}kWh ({:.0f}%) target={:.1f}kWh ({:.0f}%) "
                    "net_charge={:.1f}kWh dno={:.1f}kW energy_ratio={:.2f}x".format(
                        self.last_phase or "none",
                        phase,
                        soc_kw,
                        soc_pct,
                        target_soc_kwh,
                        target_pct,
                        net_charge,
                        dno_limit,
                        self._energy_ratio,
                    )
                )
                self.last_phase = phase

            # Apply BEFORE publish: EMS mode must be set before sensor publish
            # triggers the HA automation (which requires D-ESS as a condition)
            self.apply(phase, export_target_kw)
            self.publish(phase, target_soc_kwh, net_charge, export_target_kw, dno_limit)

        except Exception as e:
            self.log("Curtailment plugin error: {}".format(e))
            soc_max = getattr(self.base, "soc_max", 10)
            self.publish("off", soc_max, 0, -1, self._dno_limit)
            if self.was_active:
                try:
                    self.apply("off", -1)
                except Exception:
                    pass
