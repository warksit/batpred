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

import math

from curtailment_calc import (
    compute_morning_gap,
    compute_remaining_overflow,
    compute_solar_overflow,
    p90_scale_from_forecast,
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

# SIG/Solcast sensor entities
SIG_DAILY_PV = "sensor.sigen_plant_daily_third_party_inverter_energy"
PREDBAT_PV_TODAY = "sensor.predbat_pv_today"
SOLCAST_TODAY = "sensor.solcast_pv_forecast_forecast_today"
SOLCAST_REMAINING = "sensor.solcast_pv_forecast_forecast_remaining_today"

# Safety factor: 25% buffer on overflow headroom (R9)
OVERFLOW_SAFETY_FACTOR = 1.0


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

        # Caching: morning (06:00-12:00) recalculate each cycle, other times every 30 min
        if self._cached_keep is not None:
            minutes_since = minutes_now - self._cached_at
            if minutes_since < 0:
                minutes_since += 1440  # wrapped past midnight
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
        dno_limit = self.base.get_arg("export_limit", 4000, index=0) / 1000.0
        return enabled, dno_limit

    def _publish_offset(self, value, attrs):
        """Publish curtailment solar offset sensor and cache for reuse."""
        attrs.update({"friendly_name": "Curtailment Solar SOC Keep Offset", "unit_of_measurement": "kWh", "icon": "mdi:solar-power"})
        self.base.dashboard_item("sensor.{}_curtailment_solar_offset".format(self.base.prefix), value, attrs)
        self._cached_offset = (value, attrs)

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

    def calculate(self, dno_limit_kw):
        """Compute floor using v17 solar geometry model.

        Scale from Solcast p90 (R42). Floor = soc_max - overflow_integral × 1.25 (R9).
        Floor ratchet: floor can only rise (R11). Deactivate at safe_time (R6).

        Returns:
            (floor_kwh, phase) where phase is "active" or "off"
        """
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

        now_utc = getattr(self.base, "now_utc", None)
        if now_utc is None:
            return soc_max, "off"

        utc_hours = now_utc.hour + now_utc.minute / 60.0 + now_utc.second / 3600.0
        doy = now_utc.timetuple().tm_yday
        local_offset = (minutes_now / 60.0) - utc_hours  # hours ahead of UTC

        # Read actual PV
        try:
            actual_pv = float(self.base.get_state_wrapper(SIG_PV_POWER, default=0))
        except (ValueError, TypeError):
            actual_pv = 0.0
        self._actual_pv_kw = actual_pv

        # Guard: no PV yet (pre-dawn / winter morning)
        if self._peak_pv < 0.1 and actual_pv < 0.1:
            return soc_max, "off"

        # Track actual peak for scale calibration (R43)
        if actual_pv > self._peak_pv:
            self._peak_pv = actual_pv
            self._peak_pv_time = minutes_now

        # Reset peak tracking at end of day
        if actual_pv < 0.1 and minutes_now > 1200:
            self._peak_pv = 0.0
            self._peak_pv_time = 0

        # Get p90 scale from Solcast (R42)
        p90_scale, _p90_peak_kw, _p90_peak_utc = self._get_p90_scale(lat, lon, doy, local_offset)

        if p90_scale < 0.5:
            # No Solcast data and no yesterday's scale — cannot compute safely
            self._export_target = -2
            return soc_max, "off"

        # Compute actual scale from observed peak (R43)
        actual_scale = 0.0
        if self._peak_pv >= 1.0:
            peak_utc_h = (self._peak_pv_time / 60.0) - local_offset
            peak_elev = solar_elevation(lat, lon, peak_utc_h, doy)
            sin_peak = math.sin(math.radians(peak_elev))
            if sin_peak >= 0.05:
                actual_scale = self._peak_pv / sin_peak

        # floor always uses p90_scale (worst-case clear day).
        # actual_scale is unreliable for floor: cloud at peak hour gives false low scale,
        # and afternoon could still be clear. Safe to always reserve p90 headroom.
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
            # Past safe_time → deactivate (R6/R12)
            safe_local = safe_utc + local_offset
            self._safe_time_str = "{:02d}:{:02d}".format(int(safe_local) % 24, int((safe_local % 1) * 60))
            self._floor_ratchet = None
            self._export_target = -2
            self._remaining_overflow = 0.0
            return soc_max, "off"
        else:
            safe_local = safe_utc + local_offset
            self._safe_time_str = "{:02d}:{:02d}".format(int(safe_local) % 24, int((safe_local % 1) * 60))

        # Compute remaining overflow from solar geometry (R9)
        remaining_overflow = compute_solar_overflow(floor_scale, lat, lon, doy, utc_hours, safe_utc, dno_limit_kw)
        self._remaining_overflow = round(remaining_overflow, 2)

        # Activation check (R5): overflow predicted AND battery would fill
        try:
            solcast_remaining = float(self.base.get_state_wrapper(SOLCAST_REMAINING, default=0))
        except (ValueError, TypeError):
            solcast_remaining = 0.0

        load_step = getattr(self.base, "load_minutes_step", {})
        step_hours = PREDICT_STEP / 60.0
        to_kw = 1.0 / step_hours
        safe_offset_mins = max(PREDICT_STEP, int((safe_utc - utc_hours) * 60))
        load_remaining = sum(load_step.get(m, 0) * to_kw * step_hours for m in range(PREDICT_STEP, safe_offset_mins + PREDICT_STEP, PREDICT_STEP))

        battery_headroom = soc_max - soc_kw
        total_excess = max(0.0, solcast_remaining - load_remaining)

        overflow_active = remaining_overflow > 0.1
        will_fill = total_excess > battery_headroom

        if not overflow_active or not will_fill:
            self._floor_ratchet = None
            self._export_target = -2
            return soc_max, "off"

        # Active: compute floor (R9/R10)
        soc_keep = getattr(self.base, "best_soc_keep", 0)
        reserve = getattr(self.base, "reserve", 0)

        floor = soc_max - remaining_overflow * OVERFLOW_SAFETY_FACTOR
        floor = max(floor, max(soc_keep, reserve))
        floor = min(floor, soc_max)

        # Floor ratchet: floor can only rise within a day (R11)
        if self._floor_ratchet is not None:
            floor = max(floor, self._floor_ratchet)
        self._floor_ratchet = floor

        # Three-state: Charge when SOC below floor (R16/R38), else Drain/Hold split by HA automation
        if soc_kw < floor - 0.5:
            self._export_target = 0.0
        else:
            self._export_target = dno_limit_kw

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
            now_utc = getattr(self.base, "now_utc", None)
            if lat and lon and now_utc:
                tomorrow_doy = (now_utc.timetuple().tm_yday % 365) + 1
                utc_now = now_utc.hour + now_utc.minute / 60.0
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

        floor_pct = round(floor_kwh / soc_max * 100, 1) if soc_max > 0 else 100
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
                "overflow_kwh": round(self._remaining_overflow, 2),
                "safe_time": self._safe_time_str,
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

            # Defer to Predbat charge windows when SOC below effective keep (R4)
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

            self.apply(phase)
            self.publish(phase, floor, dno_limit, export_target=self._export_target)

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
