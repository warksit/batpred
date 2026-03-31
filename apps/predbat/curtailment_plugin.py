# -----------------------------------------------------------------------------
# Curtailment Manager Plugin for Predbat
# Cumulative energy ratio algorithm (v7) to eliminate solar curtailment
#
# Works WITH the HA automation (curtailment_manager_dynamic_export_limit):
#   - Plugin (5-min): computes phase from overflow timing, publishes sensors
#   - HA automation (~5s): reactive export limit control in holding phase
#
# Control model (SIG inverter):
#   Active:  D-ESS mode, read_only=True (suppresses Predbat inverter control)
#   Inactive: MSC mode, read_only=False (Predbat resumes)
#
# v7 phases:
#   charge:  SOC below adaptive floor → block export, absorb all PV
#   export:  Currently overflowing OR SOC above floor + excess PV → export=DNO
#   hold:    PV generating, SOC at floor → HA automation tracks PV-load
#   off:     No overflow expected, PV negligible → hand to Predbat
# -----------------------------------------------------------------------------

from curtailment_calc import (
    compute_remaining_overflow,
    compute_morning_gap,
    compute_overflow_window,
    compute_post_overflow_energy,
    should_activate,
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
# Sustained sub-DNO slots to confirm overflow window has ended (not just a cloud gap)
OVERFLOW_END_SUSTAINED_SLOTS = 6  # 30 min

# SIG/Solcast sensor entities for energy ratio
SIG_DAILY_PV = "sensor.sigen_plant_daily_third_party_inverter_energy"
PREDBAT_PV_TODAY = "sensor.predbat_pv_today"

SOC_CAP_PCT = 0.95  # 95% cap during overflow as safety backstop
OVERFLOW_RECENT_SLOTS = 6  # 30 min to confirm overflow is truly over before releasing cap


class CurtailmentPlugin(PredBatPlugin):
    """
    Curtailment manager v7 — cumulative energy ratio algorithm.

    Key insight: scale the forecast overflow estimate using the ratio of
    actual cumulative PV produced vs calibrated forecast produced so far
    today. This naturally adapts to over- and under-forecasting without
    requiring instantaneous PV comparisons on volatile days.

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
        self._overflow_history = []  # track recent overflow state for 95% cap
        self._energy_ratio = 1.0  # last computed energy ratio (for publish)
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

        overflow = compute_remaining_overflow(
            pv_step,
            load_step,
            dno_limit,
            start_minute=PREDICT_STEP,
            end_minute=solar_end,
            step_minutes=PREDICT_STEP,
            values_are_kwh=True,
        )

        if overflow <= 0:
            self._publish_offset(0.0, {"overflow_kwh": 0.0, "original_keep": round(context["best_soc_keep"], 2)})
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
            self.log("Curtailment: reducing best_soc_keep {:.2f} -> {:.2f} kWh (morning_gap={:.2f}, overflow={:.2f})".format(current_keep, solar_adjusted_keep, morning_gap, overflow))
            context["best_soc_keep"] = solar_adjusted_keep
            self._publish_offset(round(solar_adjusted_keep - current_keep, 2), {"morning_gap_kwh": round(morning_gap, 2), "overflow_kwh": round(overflow, 2), "original_keep": round(current_keep, 2), "adjusted_keep": round(solar_adjusted_keep, 2)})
        else:
            self._publish_offset(0.0, {"morning_gap_kwh": round(morning_gap, 2), "overflow_kwh": round(overflow, 2), "original_keep": round(current_keep, 2)})

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

    def calculate(self, dno_limit_kw):
        """Compute curtailment phase using v7 cumulative energy ratio algorithm.

        Returns (target_soc_kwh, remaining_overflow_kwh, phase, export_target_kw)
        """
        pv_step = getattr(self.base, "pv_forecast_minute_step", {})
        load_step = getattr(self.base, "load_minutes_step", {})
        soc_kw = getattr(self.base, "soc_kw", 0)
        soc_max = getattr(self.base, "soc_max", 10)
        forecast_minutes = getattr(self.base, "forecast_minutes", 1440)

        if not pv_step or not soc_max:
            return soc_max, 0, "off", -2

        minutes_now = getattr(self.base, "minutes_now", 720)
        solar_end_minute = min(forecast_minutes, max(PREDICT_STEP, 23 * 60 - minutes_now))

        # --- Energy ratio from cumulative PV tracking ---
        actual_pv, actual_load, energy_ratio = self._get_energy_ratio()
        self._energy_ratio = energy_ratio
        actual_excess = actual_pv - actual_load
        currently_overflowing = actual_excess > dno_limit_kw

        # --- Track overflow history for 95% cap ---
        self._overflow_history.append(currently_overflowing)
        if len(self._overflow_history) > OVERFLOW_RECENT_SLOTS:
            self._overflow_history = self._overflow_history[-OVERFLOW_RECENT_SLOTS:]
        recently_overflowing = any(self._overflow_history)

        # --- Remaining overflow from FORECAST (unscaled) ---
        remaining_overflow = compute_remaining_overflow(pv_step, load_step, dno_limit_kw, start_minute=PREDICT_STEP, end_minute=solar_end_minute, step_minutes=PREDICT_STEP, values_are_kwh=True)

        # --- Activation check ---
        if not should_activate(remaining_overflow) and not currently_overflowing:
            self._overflow_history.clear()
            return soc_max, 0, "off", -2

        # --- Post-overflow energy ---
        overflow_start, overflow_end = compute_overflow_window(pv_step, load_step, dno_limit_kw, start_minute=0, end_minute=solar_end_minute, step_minutes=PREDICT_STEP, values_are_kwh=True, sustained_slots=OVERFLOW_END_SUSTAINED_SLOTS)

        post_start = (overflow_end or 0) + PREDICT_STEP
        post_overflow_energy = compute_post_overflow_energy(pv_step, load_step, after_minute=post_start, end_minute=solar_end_minute, step_minutes=PREDICT_STEP, values_are_kwh=True)

        # --- Floor from forecast scaled by energy ratio ---
        soc_keep = getattr(self.base, "best_soc_keep", 0)
        reserve = getattr(self.base, "reserve", 0)

        adjusted_overflow = remaining_overflow * energy_ratio
        adjusted_post = post_overflow_energy * energy_ratio
        floor = max(soc_keep, reserve, soc_max - adjusted_overflow - adjusted_post)

        target_soc_kwh = floor

        # --- 95% cap during overflow ---
        soc_cap = soc_max * SOC_CAP_PCT if recently_overflowing else soc_max

        # --- Reactive phase (SOC vs floor) ---
        post_overflow = remaining_overflow <= 0 and actual_pv > 0.1

        if currently_overflowing:
            phase = "export"
        elif post_overflow and soc_kw < soc_max - 0.1:
            phase = "charge"  # post-overflow greedy top-up (no 95% cap)
            target_soc_kwh = soc_max
        elif soc_kw < target_soc_kwh - SOC_MARGIN_KWH:
            phase = "charge"
        elif soc_kw > target_soc_kwh + 1.0 and actual_excess > 0:
            phase = "export"  # drain toward floor
        elif actual_excess > 0:
            phase = "hold"
        else:
            phase = "hold"  # D-ESS stays on, battery covers deficit

        return target_soc_kwh, remaining_overflow, phase, -2

    def publish(self, phase, target_soc_kwh, remaining_overflow_kwh, export_target_kw, dno_limit_kw):
        """Publish curtailment sensors via dashboard_item."""
        prefix = self.base.prefix
        soc_max = getattr(self.base, "soc_max", 10)
        target_pct = round(target_soc_kwh / soc_max * 100, 1) if soc_max > 0 else 100

        self.base.dashboard_item(
            "sensor.{}_curtailment_phase".format(prefix),
            phase.replace("_", " ").title(),
            {
                "friendly_name": "Curtailment Phase",
                "icon": "mdi:solar-power-variant",
                "energy_ratio": round(self._energy_ratio, 2),
            },
        )

        self.base.dashboard_item(
            "sensor.{}_curtailment_export_target".format(prefix),
            round(export_target_kw, 2),
            {
                "friendly_name": "Curtailment Export Target",
                "unit_of_measurement": "kW",
                "icon": "mdi:transmission-tower-export",
                "dno_limit": round(dno_limit_kw, 2),
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

        self.base.dashboard_item(
            "sensor.{}_curtailment_overflow_kwh".format(prefix),
            round(remaining_overflow_kwh, 2),
            {
                "friendly_name": "Curtailment Remaining Overflow",
                "unit_of_measurement": "kWh",
                "icon": "mdi:flash-alert",
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
            # Check if PV is still significant before deactivating D-ESS.
            # Switching Off→MSC while PV is generating caused SIG faults on
            # Mar 25/26. Only restore MSC once PV is negligible (evening/night).
            actual_pv_now = 0.0
            try:
                actual_pv_now = float(self.base.get_state_wrapper(SIG_PV_POWER, default=0))
            except (ValueError, TypeError):
                pass

            if actual_pv_now >= 0.5:
                # PV still generating — keep D-ESS active even though phase is "off"
                self.log("Curtailment: phase off but PV={:.1f}kW — keeping D-ESS".format(actual_pv_now))
                self.write_sig(
                    ems_mode="Command Discharging (ESS First)",
                    charge_limit=100,
                    export_limit=self._dno_limit,
                )
                self._set_read_only(True)
            else:
                # PV negligible — safe to restore MSC
                self.log("Curtailment deactivating, restoring MSC and read_only")
                self.write_sig(
                    ems_mode="Maximum Self Consumption",
                    charge_limit=100,
                )
                # Restore export limit to DNO limit (HA automation leaves it at last value)
                self.base.call_service_wrapper(
                    "number/set_value",
                    entity_id=SIG_EXPORT_LIMIT,
                    value=self._dno_limit,
                )
                self.log("Curtailment: Restored export limit -> {}kW".format(self._dno_limit))

                # Hand back to Predbat
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

            target_soc_kwh, remaining_overflow, phase, export_target_kw = self.calculate(dno_limit)

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

            # Defer to Predbat during planned grid charge windows — but only
            # when the battery actually needs charging. If SOC > 50%, the battery
            # is full enough that curtailment management takes priority over a
            # charge window (no point grid-importing when PV is overflowing).
            soc_kw = getattr(self.base, "soc_kw", 0)
            soc_max = getattr(self.base, "soc_max", 10)
            if phase != "off" and soc_kw / max(soc_max, 0.1) < 0.5:
                charge_window_best = getattr(self.base, "charge_window_best", [])
                minutes_now = getattr(self.base, "minutes_now", 0)
                charge_window_n = self.base.in_charge_window(charge_window_best, minutes_now)
                if charge_window_n >= 0:
                    charge_limit_best = getattr(self.base, "charge_limit_best", [])
                    if charge_window_n < len(charge_limit_best):
                        charge_limit = charge_limit_best[charge_window_n]
                        if not self.base.is_freeze_charge(charge_limit):
                            if self.last_phase != "off":
                                self.log("Curtailment: deferring to Predbat charge window (SOC < 50%)")
                            phase = "off"
                            export_target_kw = -1

            target_pct = target_soc_kwh / max(soc_max, 0.1) * 100
            soc_pct = soc_kw / max(soc_max, 0.1) * 100

            # Log phase transitions
            if phase != self.last_phase:
                self.log(
                    "Curtailment: PHASE {} -> {} | SOC={:.1f}kWh ({:.0f}%) target={:.1f}kWh ({:.0f}%) "
                    "overflow={:.1f}kWh dno={:.1f}kW energy_ratio={:.2f}x".format(
                        self.last_phase or "none",
                        phase,
                        soc_kw,
                        soc_pct,
                        target_soc_kwh,
                        target_pct,
                        remaining_overflow,
                        dno_limit,
                        self._energy_ratio,
                    )
                )
                self.last_phase = phase

            # Apply BEFORE publish: EMS mode must be set before sensor publish
            # triggers the HA automation (which requires D-ESS as a condition)
            self.apply(phase, export_target_kw)
            self.publish(phase, target_soc_kwh, remaining_overflow, export_target_kw, dno_limit)

        except Exception as e:
            self.log("Curtailment plugin error: {}".format(e))
            soc_max = getattr(self.base, "soc_max", 10)
            self.publish("off", soc_max, 0, -1, self._dno_limit)
            if self.was_active:
                try:
                    self.apply("off", -1)
                except Exception:
                    pass
