# -----------------------------------------------------------------------------
# Curtailment Manager Plugin for Predbat — v10
# Overflow-vs-headroom algorithm to eliminate solar curtailment
#
# Works WITH the HA automation (curtailment_manager_dynamic_export_limit):
#   - Plugin (5-min): computes floor from overflow forecast, publishes sensors
#   - HA automation (~5s): binary export limit (0 if SOC < floor, else min(excess, DNO))
#
# Control model (SIG inverter):
#   Active:  D-ESS mode, read_only=True (suppresses Predbat inverter control)
#   Inactive: MSC mode, read_only=False (Predbat resumes)
#
# v10 activation (one check):
#   remaining_overflow * 1.10 > available_headroom (soc_max * 0.95 - soc_kw)
#
# v10 phases (binary):
#   charge:  SOC < floor → export=0, absorb all PV
#   managed: SOC >= floor → export=DNO, SIG handles overflow naturally
#   off:     No overflow risk → hand to Predbat
# -----------------------------------------------------------------------------

import math

from curtailment_calc import (
    compute_morning_gap,
    compute_remaining_overflow,
    simulate_soc_trajectory,
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

# SIG/Solcast sensor entities for energy ratio
SIG_DAILY_PV = "sensor.sigen_plant_daily_third_party_inverter_energy"
PREDBAT_PV_TODAY = "sensor.predbat_pv_today"

# Safety factors
OVERFLOW_SAFETY_FACTOR = 1.10  # 10% buffer on battery absorption
SOC_CAP_FACTOR = 0.95  # 95% cap until safe_time (spike headroom)


class CurtailmentPlugin(PredBatPlugin):
    """
    Curtailment manager v10.1 — overflow-vs-headroom algorithm.

    Activation: overflow * 1.10 > headroom to 95% (with hysteresis).
    Floor: soc_max - overflow * 1.10, capped at 95% until safe_time.
    Control: plugin sets D-ESS + Active/Off. HA automation handles
    real-time export (charge/drain/hold) based on SOC vs floor.
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
        self._energy_ratio = 1.0
        self._load_ratio = 1.0
        # Day's peak PV for solar geometry calibration
        self._peak_pv = 0.0
        self._peak_pv_time = 0
        self._release_scale = 0
        self._release_crossing = "none"
        self._remaining_overflow = 0
        # Caching for on_before_plan
        self._cached_keep = None
        self._cached_at = 0
        self._cached_offset = None
        # Caching for tomorrow forecast
        self._tomorrow_cache = None
        self._tomorrow_cache_at = 0

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
        today_solar_end = min(forecast_minutes, max(PREDICT_STEP, 23 * 60 - minutes_now))

        # After today's solar hours (evening/night), use tomorrow's forecast window.
        # The keep reduction is for TONIGHT's charge — needs tomorrow's overflow data.
        if today_solar_end < 60:
            # Less than 1 hour of today's solar left — use tomorrow's window
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

        # Will battery fill? Use 90% as activation threshold for planning
        will_fill = peak_soc > soc_max * 0.90

        if not will_fill:
            self._publish_offset(0.0, {"original_keep": round(context["best_soc_keep"], 2), "will_fill": False, "using_tomorrow": using_tomorrow})
            self._cached_keep = context["best_soc_keep"]
            self._cached_at = minutes_now
            return context

        morning_gap = compute_morning_gap(
            pv_step,
            load_step,
            start_minute=solar_start,
            end_minute=solar_end,
            step_minutes=PREDICT_STEP,
            values_are_kwh=True,
        )

        margin = 0.5
        solar_adjusted_keep = max(morning_gap + margin, reserve)

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

        # Only reduce keep if the overflow actually needs the headroom.
        # Headroom with current keep: soc_max - current_keep.
        # If overflow fits in that headroom, don't reduce — keep the cold
        # weather boost and other safety buffers intact.
        headroom_with_current_keep = soc_max - current_keep
        if remaining_overflow_total * OVERFLOW_SAFETY_FACTOR <= headroom_with_current_keep:
            self._publish_offset(0.0, {"morning_gap_kwh": round(morning_gap, 2), "overflow_kwh": round(remaining_overflow_total, 2), "original_keep": round(current_keep, 2), "reason": "overflow_fits_in_headroom"})
            self._cached_keep = current_keep
            self._cached_at = minutes_now
            return context

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
        try:
            total_cl = float(self.base.get_state_wrapper(PREDBAT_PV_TODAY, attribute="totalCL", default=0))
            remaining_cl = float(self.base.get_state_wrapper(PREDBAT_PV_TODAY, attribute="remainingCL", default=0))
        except (ValueError, TypeError):
            total_cl = 0
            remaining_cl = 0

        # Fallback: if calibrated forecast attributes are unavailable (e.g. first boot),
        # use raw Solcast sensors.
        if total_cl <= 0:
            try:
                forecast_today = float(self.base.get_state_wrapper("sensor.solcast_pv_forecast_forecast_today", default=0))
                forecast_remaining = float(self.base.get_state_wrapper("sensor.solcast_pv_forecast_forecast_remaining_today", default=0))
                total_cl = forecast_today
                remaining_cl = forecast_remaining
            except (ValueError, TypeError):
                pass

        forecast_produced = total_cl - remaining_cl

        # Blend: smoothly ramp from 1.0 toward real ratio over first 15% of daily forecast
        threshold = total_cl * 0.15
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

        # 15% blend: stable early, responsive by mid-morning
        threshold = predicted_today * 0.15
        blend = min(1.0, predicted_so_far / max(threshold, 0.5))
        raw_ratio = actual_load_total / max(predicted_so_far, 0.5)
        load_ratio = 1.0 + (raw_ratio - 1.0) * blend

        return load_ratio

    def _update_peak_pv(self, minutes_now, actual_pv_kw):
        """Track the day's peak PV for solar geometry scale calibration.

        Updates whenever a new peak is seen. Resets when PV drops to zero
        in the evening (minutes_now > 1200 = after 20:00).
        """
        if actual_pv_kw > self._peak_pv:
            self._peak_pv = actual_pv_kw
            self._peak_pv_time = minutes_now
        # Reset after PV dies in evening
        if actual_pv_kw < 0.1 and minutes_now > 1200:
            self._peak_pv = 0.0
            self._peak_pv_time = 0
            self._release_scale = 0

    def _compute_solar_release(self, minutes_now, actual_pv_kw, dno_limit_kw):
        """Compute whether we're past the solar safe time.

        Uses the day's peak PV to calibrate a clear-sky model:
        pv = scale * sin(elevation). Safe when this drops below DNO + base_load.

        Returns (past_safe_time, scale, crossing_time_str).
        """
        self._update_peak_pv(minutes_now, actual_pv_kw)

        try:
            lat = float(self.base.get_state_wrapper("zone.home", attribute="latitude", default=0))
            lon = float(self.base.get_state_wrapper("zone.home", attribute="longitude", default=0))
        except (ValueError, TypeError):
            return False, 0, "none", None

        if lat == 0 and lon == 0:
            return False, 0, "none", None

        now_utc = getattr(self.base, "now_utc", None)
        if now_utc is None:
            return False, 0, "none", None

        utc_hours = now_utc.hour + now_utc.minute / 60.0 + now_utc.second / 3600.0
        doy = now_utc.timetuple().tm_yday

        if self._peak_pv < 1.0:
            return False, 0, "none", None, None

        # Before peak: don't RELEASE (uncap 95%), but still estimate safe_time
        # for the energy balance cutoff.
        before_peak = minutes_now <= self._peak_pv_time

        # Compute scale from elevation at peak time
        local_offset_hours = (minutes_now / 60.0) - utc_hours
        peak_utc_hours = (self._peak_pv_time / 60.0) - local_offset_hours
        peak_elev = solar_elevation(lat, lon, peak_utc_hours, doy)
        sin_peak = math.sin(math.radians(peak_elev))
        if sin_peak < 0.05:
            return True, 0, "low_sun", 0

        scale = self._peak_pv / sin_peak
        self._release_scale = scale
        threshold = dno_limit_kw + MIN_BASE_LOAD_KW

        release_mins, crossing_utc = compute_release_time(scale, lat, lon, doy, threshold, utc_hours)

        if release_mins is None:
            return False, scale, "none", None

        # Format crossing time for diagnostics
        crossing_local = crossing_utc + local_offset_hours
        crossing_str = "{:02d}:{:02d}".format(int(crossing_local) % 24, int((crossing_local % 1) * 60))
        self._release_crossing = crossing_str

        past = release_mins <= 0 and not before_peak
        crossing_label = "before_peak ({})".format(crossing_str) if before_peak else crossing_str
        return past, scale, crossing_label, max(0, release_mins)

    @staticmethod
    def _compute_curtailment(pv_step, load_step, solcast_total, pv_ratio, load_ratio, window_start, window_end, soc_kw, soc_max, dno_limit, was_active=False, floor_min=0):
        """Single curtailment calculation — shared by live and tomorrow (R31).

        Computes activation (is there a problem?) and floor (what's the solution?)
        from totals-based energy balance. One function, zero divergence.

        Args:
            pv_step, load_step: forecast dicts (kWh per step)
            solcast_total: Solcast PV total for this window (kWh)
            pv_ratio, load_ratio: energy/load scaling (1.0 for tomorrow)
            window_start, window_end: forecast window (minutes from now)
            soc_kw: battery SOC at start of window (kWh)
            soc_max: battery capacity (kWh)
            dno_limit: max export (kW)
            was_active: for hysteresis (live only)
            floor_min: minimum floor (soc_keep/reserve for live, 0 for tomorrow)

        Returns:
            (active, floor_kwh, absorption_kwh)
        """
        step_hours = PREDICT_STEP / 60.0
        to_kw = 1.0 / step_hours
        soc_cap = soc_max * SOC_CAP_FACTOR

        # --- Balance end: last overflow slot (forecast shape for TIMING) ---
        balance_end = 0
        for m in range(window_start, window_end, PREDICT_STEP):
            pv_kw = pv_step.get(m, 0) * to_kw
            load_kw = load_step.get(m, 0) * to_kw
            if pv_kw - load_kw > dno_limit:
                balance_end = m + PREDICT_STEP
        if balance_end == 0:
            balance_end = window_end

        # --- PV: Solcast magnitude x forecast shape fraction ---
        per_slot_to_balance = sum(pv_step.get(m, 0) for m in range(window_start, balance_end, PREDICT_STEP))
        per_slot_total = sum(pv_step.get(m, 0) for m in range(window_start, window_end, PREDICT_STEP))
        fraction = (per_slot_to_balance / per_slot_total) if per_slot_total > 0 else 1.0

        remaining_pv = solcast_total * fraction * pv_ratio
        if solcast_total <= 0:
            remaining_pv = per_slot_to_balance * to_kw * step_hours * pv_ratio

        # --- Load: LoadML sum for same window ---
        remaining_load = sum(load_step.get(m, 0) * to_kw * step_hours for m in range(window_start, balance_end, PREDICT_STEP)) * load_ratio

        # --- Activation: will excess fill the battery? (R5/R6) ---
        headroom = soc_cap - soc_kw
        if was_active:
            headroom /= 0.9  # hysteresis: harder to deactivate
        active = (remaining_pv - remaining_load) > headroom

        # --- Absorption: excess - export capacity (R9/R32) ---
        hours_to_balance = max(0, (balance_end - window_start) / 60.0)
        total_excess = max(0, remaining_pv - remaining_load)
        absorption = max(0, total_excess - dno_limit * hours_to_balance)

        if not active or absorption < 0.1:
            return False, soc_cap, 0

        # --- Floor (R9/R10) ---
        floor = soc_cap - absorption * OVERFLOW_SAFETY_FACTOR
        floor = max(floor, floor_min)

        return True, floor, absorption

    def _compute_tomorrow_forecast(self):
        """Compute curtailment forecast for tomorrow using totals-based energy balance.

        Same _compute_absorption as live calculate(), just with tomorrow's window.
        Only computes after today's PV is done (last forecast PV < 30 min away).
        Cached for 30 minutes.
        """
        minutes_now = getattr(self.base, "minutes_now", 720)

        # Cache check
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

        # Tomorrow's solar window in minutes-from-now
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

        # --- PV total from Solcast tomorrow sensor ---
        try:
            solcast_tomorrow = float(self.base.get_state_wrapper("sensor.solcast_pv_forecast_forecast_tomorrow", default=0))
        except (ValueError, TypeError):
            solcast_tomorrow = 0
        if solcast_tomorrow <= 0:
            solcast_tomorrow = sum(pv_step.get(m, 0) * to_kw * step_hours for m in range(tomorrow_start, tomorrow_end, PREDICT_STEP))

        # --- Release time from solar geometry ---
        release_time_str = "unknown"
        release_offset = tomorrow_end  # fallback: whole window
        try:
            lat = float(self.base.get_state_wrapper("zone.home", attribute="latitude", default=0))
            lon = float(self.base.get_state_wrapper("zone.home", attribute="longitude", default=0))
            now_utc = getattr(self.base, "now_utc", None)
            if lat and lon and now_utc:
                tomorrow_doy = (now_utc.timetuple().tm_yday % 365) + 1
                peak_pv_kw = 0
                peak_offset = tomorrow_start
                for m in range(tomorrow_start, tomorrow_end, PREDICT_STEP):
                    pv_kw = pv_step.get(m, 0) * to_kw
                    if pv_kw > peak_pv_kw:
                        peak_pv_kw = pv_kw
                        peak_offset = m

                if peak_pv_kw > 1.0:
                    utc_now = now_utc.hour + now_utc.minute / 60.0
                    local_offset = (minutes_now / 60.0) - utc_now
                    peak_abs_utc = (utc_now + peak_offset / 60.0) % 24
                    peak_elev = solar_elevation(lat, lon, peak_abs_utc, tomorrow_doy)
                    sin_elev = math.sin(math.radians(peak_elev))
                    if sin_elev > 0.05:
                        scale = peak_pv_kw / sin_elev
                        threshold = dno_limit + MIN_BASE_LOAD_KW
                        scan_abs_utc = (utc_now + tomorrow_start / 60.0) % 24
                        rel_mins, crossing_utc = compute_release_time(scale, lat, lon, tomorrow_doy, threshold, scan_abs_utc)
                        if crossing_utc:
                            crossing_local = crossing_utc + local_offset
                            release_time_str = "{:02d}:{:02d}".format(int(crossing_local) % 24, int((crossing_local % 1) * 60))
                            release_offset = min(int(rel_mins), tomorrow_end - tomorrow_start)
        except Exception:
            pass

        # --- Morning gap (per-slot — timing matters for this) ---
        morning_gap = compute_morning_gap(
            pv_step,
            load_step,
            start_minute=tomorrow_start,
            end_minute=tomorrow_end,
            step_minutes=PREDICT_STEP,
            values_are_kwh=True,
        )

        # --- Single shared curtailment calculation — same as live (R31) ---
        estimated_morning_soc = morning_gap + 0.5
        will_activate, floor_kwh, battery_must_absorb = self._compute_curtailment(
            pv_step,
            load_step,
            solcast_tomorrow,
            1.0,
            1.0,
            tomorrow_start,
            tomorrow_end,
            estimated_morning_soc,
            soc_max,
            dno_limit,
        )

        floor_pct = round(floor_kwh / soc_max * 100, 1) if soc_max > 0 else 100
        floor_pct = max(0, min(100, floor_pct))

        # --- Expected soc_keep ---
        reserve = getattr(self.base, "reserve", 0)
        if battery_must_absorb > morning_gap:
            soc_keep = round(reserve, 2)
        else:
            soc_keep = round(max(morning_gap + 0.5, reserve), 2)

        forecast = {
            "total_overflow_kwh": round(battery_must_absorb, 2),
            "floor_pct": floor_pct,
            "will_activate": will_activate,
            "morning_gap_kwh": round(morning_gap, 2),
            "release_time": release_time_str,
            "soc_keep_kwh": soc_keep,
        }

        self._tomorrow_cache = forecast
        self._tomorrow_cache_at = minutes_now
        return forecast

    def _publish_tomorrow_forecast(self, forecast):
        """Publish tomorrow's curtailment forecast as a sensor.

        When Inactive, clear attributes to avoid stale data on dashboard.
        """
        prefix = self.base.prefix
        if forecast["will_activate"]:
            state = "Active"
            attrs = dict(forecast)
        else:
            state = "Inactive"
            attrs = {
                "total_overflow_kwh": 0,
                "floor_pct": 100,
                "will_activate": False,
                "morning_gap_kwh": round(forecast.get("morning_gap_kwh", 0), 2),
                "release_time": "none",
                "soc_keep_kwh": round(forecast.get("soc_keep_kwh", 0), 2),
            }
        attrs["friendly_name"] = "Curtailment Tomorrow Forecast"
        attrs["icon"] = "mdi:solar-power-variant-outline"
        self.base.dashboard_item("sensor.{}_curtailment_tomorrow".format(prefix), state, attrs)

    def calculate(self, dno_limit_kw):
        """Compute floor using v10.1 overflow-vs-headroom algorithm.

        Activation: overflow * factor > headroom to 95% (with hysteresis).
        Floor: soc_max - overflow * 1.10, capped at 95% until safe_time.
        HA automation handles real-time control (charge/drain/hold).

        Returns:
            (floor_kwh, phase) where phase is "active" or "off"
        """
        pv_step = getattr(self.base, "pv_forecast_minute_step", {})
        load_step = getattr(self.base, "load_minutes_step", {})
        soc_kw = getattr(self.base, "soc_kw", 0)
        soc_max = getattr(self.base, "soc_max", 10)
        forecast_minutes = getattr(self.base, "forecast_minutes", 1440)

        if not pv_step or not soc_max:
            return soc_max, "off"

        minutes_now = getattr(self.base, "minutes_now", 720)
        solar_end = min(forecast_minutes, max(PREDICT_STEP, 23 * 60 - minutes_now))

        # --- Energy ratio & load ratio ---
        actual_pv, actual_load, energy_ratio = self._get_energy_ratio()
        self._energy_ratio = energy_ratio
        load_ratio = self._get_load_ratio()
        self._load_ratio = load_ratio

        # --- Solar geometry (need safe_time for energy balance cutoff) ---
        try:
            past_safe_time, release_scale, crossing_str, safe_time_mins = self._compute_solar_release(minutes_now, actual_pv, dno_limit_kw)
            self._release_scale = release_scale
            self._release_crossing = crossing_str
        except Exception:
            past_safe_time = False
            safe_time_mins = None
            self._release_scale = 0
            self._release_crossing = "none"

        # --- Solcast remaining today ---
        try:
            solcast_remaining = float(self.base.get_state_wrapper("sensor.solcast_pv_forecast_forecast_remaining_today", default=0))
        except (ValueError, TypeError):
            solcast_remaining = 0

        # --- Single shared curtailment calculation (R31) ---
        soc_keep = getattr(self.base, "best_soc_keep", 0)
        reserve = getattr(self.base, "reserve", 0)

        active, floor, absorption = self._compute_curtailment(
            pv_step,
            load_step,
            solcast_remaining,
            energy_ratio,
            load_ratio,
            PREDICT_STEP,
            solar_end,
            soc_kw,
            soc_max,
            dno_limit_kw,
            was_active=self.was_active,
            floor_min=max(soc_keep, reserve),
        )
        self._remaining_overflow = round(absorption, 2)

        if not active:
            return soc_max, "off"

        # 95% cap until safe_time
        if past_safe_time:
            floor = min(floor, soc_max)
        else:
            floor = min(floor, soc_max * SOC_CAP_FACTOR)

        return floor, "active"

    def publish(self, phase, floor_kwh, dno_limit_kw):
        """Publish curtailment sensors via dashboard_item.

        Phase sensor shows Active/Off (plugin's strategic decision).
        Real-time phase (Charge/Drain/Hold) is published by the HA automation.
        """
        prefix = self.base.prefix
        soc_max = getattr(self.base, "soc_max", 10)
        floor_pct = round(floor_kwh / soc_max * 100, 1) if soc_max > 0 else 100

        # Plugin publishes Active or Off — HA automation publishes Charge/Drain/Hold
        state = "Off" if phase == "off" else "Active"

        self.base.dashboard_item(
            "sensor.{}_curtailment_phase".format(prefix),
            state,
            {
                "friendly_name": "Curtailment Phase",
                "icon": "mdi:solar-power-variant",
                "energy_ratio": round(self._energy_ratio, 2),
                "load_ratio": round(getattr(self, "_load_ratio", 1.0), 2),
                "floor_pct": floor_pct,
                "battery_absorb_kwh": round(getattr(self, "_remaining_overflow", 0), 2),
                "release_time": getattr(self, "_release_crossing", "none"),
                "release_scale": round(getattr(self, "_release_scale", 0), 1),
                "peak_pv_kw": round(getattr(self, "_peak_pv", 0), 1),
            },
        )

        self.base.dashboard_item(
            "sensor.{}_curtailment_target_soc".format(prefix),
            floor_pct,
            {
                "friendly_name": "Curtailment Target SOC",
                "unit_of_measurement": "%",
                "icon": "mdi:battery-charging-medium",
                "target_kwh": round(floor_kwh, 2),
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

    def apply(self, phase):
        """Apply inverter control based on phase.

        Active:  D-ESS, export=0 (safe default), read_only=true.
                 HA automation takes over export limit within 5 seconds.
        Off:     MSC, export=DNO (cleanup), read_only=false.
        """
        active = phase != "off"

        if active:
            if not self.was_active:
                self.log("Curtailment activating")
                # Set export=0 as safe default before D-ESS mode
                # HA automation overrides within 5 seconds
                self.write_sig(
                    ems_mode="Command Discharging (ESS First)",
                    charge_limit=100,
                    export_limit=0,
                )
            else:
                # Already active — just ensure D-ESS mode (don't touch export limit)
                self.write_sig(
                    ems_mode="Command Discharging (ESS First)",
                    charge_limit=100,
                )

            self._set_read_only(True)
            self.was_active = True

        elif self.was_active:
            # Deactivating: restore MSC
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

    HA_AUTOMATION = "automation.curtailment_manager_dynamic_export_limit"

    def _cleanup_read_only(self):
        """Clear stale state left by a previous plugin run (e.g. after restart)."""
        if not self.was_active and self.base.set_read_only:
            self.log("Curtailment: clearing stale read_only from previous run")
            self._set_read_only(False)
        # Re-enable HA automation on restart (may have been manually disabled)
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

            # Defer to Predbat charge windows ONLY when SOC is below the
            # effective keep floor (battery genuinely needs grid charging,
            # e.g. morning cheap rate before GSHP drains it). Once SOC is
            # at or above keep, curtailment manages — ignore charge windows.
            soc_kw = getattr(self.base, "soc_kw", 0)
            effective_keep = getattr(self.base, "best_soc_keep", 0)
            if phase != "off" and soc_kw < effective_keep:
                minutes_now = getattr(self.base, "minutes_now", 0)
                charge_window_best = getattr(self.base, "charge_window_best", [])
                charge_window_n = self.base.in_charge_window(charge_window_best, minutes_now)
                if charge_window_n >= 0:
                    charge_limit_best = getattr(self.base, "charge_limit_best", [])
                    if charge_window_n < len(charge_limit_best):
                        charge_limit = charge_limit_best[charge_window_n]
                        if not self.base.is_freeze_charge(charge_limit):
                            if self.last_phase != "off":
                                self.log("Curtailment: deferring to charge window (SOC {:.1f} < keep {:.1f})".format(soc_kw, effective_keep))
                            phase = "off"
                            floor = soc_max

            soc_pct = soc_kw / max(soc_max, 0.1) * 100
            floor_pct = floor / max(soc_max, 0.1) * 100

            # Log phase transitions
            if phase != self.last_phase:
                self.log(
                    "Curtailment: PHASE {} -> {} | SOC={:.1f}kWh ({:.0f}%) floor={:.1f}kWh ({:.0f}%) "
                    "overflow={:.1f}kWh dno={:.1f}kW energy_ratio={:.2f}x".format(
                        self.last_phase or "none",
                        phase,
                        soc_kw,
                        soc_pct,
                        floor,
                        floor_pct,
                        self._remaining_overflow,
                        dno_limit,
                        self._energy_ratio,
                    )
                )
                self.last_phase = phase

            # Apply BEFORE publish: EMS mode must be set before sensor publish
            # triggers the HA automation (which requires D-ESS as a condition)
            self.apply(phase)
            self.publish(phase, floor, dno_limit)

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
