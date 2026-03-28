# -----------------------------------------------------------------------------
# Curtailment Manager Plugin for Predbat
# Time-aware algorithm (v6) to eliminate solar curtailment
#
# Works WITH the HA automation (curtailment_manager_dynamic_export_limit):
#   - Plugin (5-min): computes phase from overflow timing, publishes sensors
#   - HA automation (~5s): reactive export limit control in holding phase
#
# Control model (SIG inverter):
#   Active:  D-ESS mode, read_only=True (suppresses Predbat inverter control)
#   Inactive: MSC mode, read_only=False (Predbat resumes)
#
# Time-aware phases:
#   Pre-overflow:  drain to target or hold (NEVER absorb — no pointless cycles)
#   Overflow:      export=DNO, battery absorbs excess; drains on PV dips
#   Post-overflow: top up to 100% from remaining PV
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
SIG_IMPORT_LIMIT = "number.sigen_plant_grid_import_limitation"
SIG_CHARGE_LIMIT = "number.sigen_plant_ess_charge_cut_off_state_of_charge"
SIG_PV_POWER = "sensor.sigen_plant_pv_power"
SIG_LOAD_POWER = "sensor.sigen_plant_consumed_power"

# HA input helper entity IDs
HA_ENABLE = "input_boolean.curtailment_manager_enable"

PREDICT_STEP = 5
SOC_MARGIN_KWH = 0.5
# Sustained sub-DNO slots to confirm overflow window has ended (not just a cloud gap)
OVERFLOW_END_SUSTAINED_SLOTS = 6  # 30 min


class CurtailmentPlugin(PredBatPlugin):
    """
    Curtailment manager v6 — time-aware algorithm.

    Key insight: once PV-load exceeds DNO, the battery charges regardless of
    what we do (AC-coupled solar). We can only drain BEFORE overflow starts.
    So phase selection is based on WHEN we are relative to the overflow window,
    not just SOC vs target.

    Phases:
        draining:   Pre-overflow, SOC above target → export=DNO, discharge battery
        holding:    Pre-overflow, SOC at/below target → export=PV-load (HA automation)
        overflow:   During overflow window → export=DNO (absorb excess, drain on dips)
        topping_up: Post-overflow → export=0, charge to 100% from remaining PV
        off:        No overflow expected or solar done → hand to Predbat
    """

    priority = 200  # Run after cold_weather_plugin (priority 100)

    def __init__(self, base):
        super().__init__(base)
        self.last_ems_mode = None
        self.last_import_limit = None
        self.last_charge_limit = None
        self.last_export_limit = None
        self.was_active = False
        self._dno_limit = 4.0
        self.last_phase = None
        self._max_pv_scale = 1.0  # track peak PV scale factor across cycles
        self._was_overflowing = False  # have we seen overflow at all today?
        self._pv_excess_history = []  # rolling actual PV-load history for trend detection
        self._pv_readings = []  # rolling actual PV readings for 5-min average
        # Caching for on_before_plan
        self._cached_keep = None
        self._cached_at = 0  # minutes_now when last computed

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
                return context

        pv_step = getattr(self.base, "pv_forecast_minute_step", {})
        load_step = getattr(self.base, "load_minutes_step", {})
        soc_max = getattr(self.base, "soc_max", 10)
        reserve = getattr(self.base, "reserve", 0)
        forecast_minutes = getattr(self.base, "forecast_minutes", 1440)

        if not pv_step:
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

            self.base.dashboard_item(
                "sensor.{}_curtailment_solar_offset".format(self.base.prefix),
                round(current_keep - solar_adjusted_keep, 2),
                {
                    "friendly_name": "Curtailment Solar SOC Keep Offset",
                    "unit_of_measurement": "kWh",
                    "icon": "mdi:solar-power",
                    "morning_gap_kwh": round(morning_gap, 2),
                    "overflow_kwh": round(overflow, 2),
                    "original_keep": round(current_keep, 2),
                    "adjusted_keep": round(solar_adjusted_keep, 2),
                },
            )
        else:
            self.base.dashboard_item(
                "sensor.{}_curtailment_solar_offset".format(self.base.prefix),
                0.0,
                {
                    "friendly_name": "Curtailment Solar SOC Keep Offset",
                    "unit_of_measurement": "kWh",
                    "icon": "mdi:solar-power",
                    "morning_gap_kwh": round(morning_gap, 2),
                    "overflow_kwh": round(overflow, 2),
                    "original_keep": round(current_keep, 2),
                },
            )

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

    def _get_pv_scale(self, pv_step, dno_limit_kw):
        """Read actual PV/load sensors and compute scale factor vs forecast.

        Returns (actual_pv, actual_load, pv_scale). Scale > 1 means actual
        exceeds forecast (underforecast). Scale < 1 means overforecast.
        Tracks the peak scale factor across cycles for the day.
        """
        step_to_kw = 60.0 / PREDICT_STEP
        try:
            actual_pv = float(self.base.get_state_wrapper(SIG_PV_POWER, default=0))
            actual_load = float(self.base.get_state_wrapper(SIG_LOAD_POWER, default=0))
        except (ValueError, TypeError):
            return 0.0, 0.0, 1.0

        # Rolling 5-min average of actual PV for stable scale comparison.
        # The forecast represents an average for the slot — comparing against
        # a single instantaneous reading is noisy on volatile days.
        self._pv_readings.append(actual_pv)
        if len(self._pv_readings) > 6:  # ~30s per reading × 6 ≈ but plugin runs every 5min, so keep last few cycles
            self._pv_readings = self._pv_readings[-6:]
        avg_pv = sum(self._pv_readings) / len(self._pv_readings)

        forecast_pv_kw = pv_step.get(0, 0) * step_to_kw
        if forecast_pv_kw > 0.5 and avg_pv > 0.5:
            pv_scale = avg_pv / forecast_pv_kw
            if pv_scale > 1.1:
                if pv_scale > self._max_pv_scale:
                    self.log("Curtailment: PV scale {:.2f}x (avg {:.1f}kW vs forecast {:.1f}kW)".format(pv_scale, avg_pv, forecast_pv_kw))
                self._max_pv_scale = max(self._max_pv_scale, pv_scale)
        else:
            pv_scale = 1.0

        return actual_pv, actual_load, pv_scale

    def calculate(self, dno_limit_kw):
        """
        Compute curtailment phase using v6 time-aware algorithm.

        Returns (target_soc_kwh, remaining_overflow_kwh, phase, export_target_kw)
        """
        pv_step = getattr(self.base, "pv_forecast_minute_step", {})
        load_step = getattr(self.base, "load_minutes_step", {})
        soc_kw = getattr(self.base, "soc_kw", 0)
        soc_max = getattr(self.base, "soc_max", 10)
        forecast_minutes = getattr(self.base, "forecast_minutes", 1440)

        if not pv_step or not soc_max:
            return soc_max, 0, "off", -1

        minutes_now = getattr(self.base, "minutes_now", 720)
        solar_end_minute = min(forecast_minutes, max(PREDICT_STEP, 23 * 60 - minutes_now))

        # --- PV scaling and actual trend tracking ---
        actual_pv, actual_load, pv_scale = self._get_pv_scale(pv_step, dno_limit_kw)
        currently_overflowing = (actual_pv - actual_load) > dno_limit_kw

        # Track actual PV trend for post-overflow detection (ground truth).
        # Uses rolling 15-min max of actual excess: if no overflow spike in the
        # last 15 min after we've seen overflow today, switch to topping_up.
        # If a cloud falsely triggers this, the charge gained is negligible
        # (barely any PV during clouds), and the self-correcting mechanism
        # goes back to draining when PV recovers above DNO.
        if currently_overflowing:
            self._was_overflowing = True
        actual_excess_kw = actual_pv - actual_load
        if actual_pv > 0.1:
            self._pv_excess_history.append(actual_excess_kw)
        # Keep 15 min of history (3 cycles × 5 min)
        if len(self._pv_excess_history) > 3:
            self._pv_excess_history = self._pv_excess_history[-3:]

        actual_overflow_ended = False
        if self._was_overflowing and len(self._pv_excess_history) >= 3:
            recent_peak = max(self._pv_excess_history)
            actual_overflow_ended = recent_peak < dno_limit_kw

        # Use peak scale factor for overflow estimate (most conservative)
        effective_scale = max(pv_scale, self._max_pv_scale) if self._max_pv_scale > 1.1 else max(pv_scale, 1.0)
        if effective_scale > 1.1:
            pv_effective = {m: v * effective_scale for m, v in pv_step.items()}
        else:
            pv_effective = pv_step

        # --- Overflow computation ---
        remaining_overflow = compute_remaining_overflow(
            pv_effective,
            load_step,
            dno_limit_kw,
            start_minute=PREDICT_STEP,
            end_minute=solar_end_minute,
            step_minutes=PREDICT_STEP,
            values_are_kwh=True,
        )

        if not should_activate(remaining_overflow) and not currently_overflowing and not actual_overflow_ended:
            self._max_pv_scale = 1.0  # reset for next day
            self._was_overflowing = False
            self._pv_excess_history.clear()
            self._pv_readings.clear()
            return soc_max, 0, "off", -1

        # --- Overflow window timing ---
        overflow_start, overflow_end = compute_overflow_window(
            pv_effective,
            load_step,
            dno_limit_kw,
            start_minute=0,
            end_minute=solar_end_minute,
            step_minutes=PREDICT_STEP,
            values_are_kwh=True,
            sustained_slots=OVERFLOW_END_SUSTAINED_SLOTS,
        )

        # --- Post-overflow energy (for "am I on track?" check) ---
        post_start = (overflow_end or 0) + PREDICT_STEP
        post_overflow_energy = compute_post_overflow_energy(
            pv_effective,
            load_step,
            after_minute=post_start,
            end_minute=solar_end_minute,
            step_minutes=PREDICT_STEP,
            values_are_kwh=True,
        )
        # Scale down if actual PV < forecast (afternoon overforecast detection)
        if pv_scale < 0.9 and actual_pv > 0.5:
            post_overflow_energy *= pv_scale

        # --- Pre-overflow target SOC (adaptive floor) ---
        # Floor = SOC_max - overflow - post_overflow_energy.
        # Adapts every cycle as PV scaling updates:
        #   overflow: uses full pv_scale (more overflow → lower floor → more headroom)
        #   post_overflow: uses min(pv_scale, 1.0) (conservative — don't inflate beyond
        #     forecast, but scale DOWN if actual PV < forecast)
        soc_keep = getattr(self.base, "best_soc_keep", 0)
        reserve = getattr(self.base, "reserve", 0)

        # Cap post-overflow credit: scale down if PV underperforming, never up
        post_credit_scale = min(pv_scale, 1.0) if actual_pv > 0.5 else 1.0
        post_credit = post_overflow_energy * post_credit_scale

        # Floor: enough that overflow + post-overflow charging reaches 100%
        adaptive_floor = soc_max - remaining_overflow - post_credit
        raw_floor = max(soc_keep, reserve, adaptive_floor)

        # No smoothing — PV scale uses rolling average which stabilises the floor.
        # Volatile PV means volatile target, but the HA automation handles
        # real-time decisions so the target just needs to be the best estimate now.
        target_soc_kwh = raw_floor

        # --- Time-based phase determination ---
        # overflow_start/end from FORECAST for pre-overflow drain timing
        pre_overflow = overflow_start is not None and overflow_start > PREDICT_STEP
        in_overflow = currently_overflowing or (overflow_start is not None and overflow_end is not None and overflow_start <= PREDICT_STEP and overflow_end >= 0)
        # Post-overflow uses ACTUAL PV trend (ground truth), not forecast.
        # Triggered by 30 min sustained actual PV-load < DNO after overflow was seen.
        # A brief cloud won't trigger this — requires sustained decline.
        post_overflow = actual_overflow_ended

        # "Am I on track for 100%?" — detect afternoon shortfall
        energy_needed = max(0, soc_max - soc_kw - SOC_MARGIN_KWH)
        falling_behind = post_overflow and actual_pv > 0.1 and post_overflow_energy < energy_needed * 0.8

        # --- Phase (informational — real-time control is in HA automation) ---
        # The HA automation implements the phase matrix in real-time using
        # target_soc_kwh. The plugin phase is for logging/dashboard only.
        if currently_overflowing:
            phase = "export"
        elif post_overflow and soc_kw < soc_max - 0.1 and actual_pv > 0.1:
            phase = "charge"  # post-overflow top-up
        elif (in_overflow or pre_overflow) and soc_kw < target_soc_kwh - SOC_MARGIN_KWH:
            phase = "charge"
        elif (in_overflow or pre_overflow) and soc_kw > target_soc_kwh + 1.0 and actual_excess_kw > 0:
            phase = "export"  # drain toward target
        elif in_overflow or pre_overflow:
            phase = "hold"
        elif post_overflow:
            phase = "hold"
        else:
            phase = "off"

        # Export target is no longer used — HA automation computes it in real-time.
        # Publish -2 as a sentinel so we can detect if the old automation is still running.
        export_target_kw = -2

        return target_soc_kwh, remaining_overflow, phase, export_target_kw

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
                "pv_scale": round(self._max_pv_scale, 2),
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

    def write_sig(self, ems_mode, import_limit, charge_limit, export_limit=None):
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

        if import_limit != self.last_import_limit:
            self.base.call_service_wrapper(
                "number/set_value",
                entity_id=SIG_IMPORT_LIMIT,
                value=import_limit,
            )
            self.last_import_limit = import_limit
            self.log("Curtailment: Set import limit -> {}%".format(import_limit))

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
                import_limit=0,
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
            # Deactivating — restore to MSC, then hand back to Predbat
            self.log("Curtailment deactivating, restoring MSC and read_only")
            self.write_sig(
                ems_mode="Maximum Self Consumption",
                import_limit=100,
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
            self.last_import_limit = None
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

            # Defer to Predbat during planned grid charge windows
            if phase != "off":
                charge_window_best = getattr(self.base, "charge_window_best", [])
                minutes_now = getattr(self.base, "minutes_now", 0)
                charge_window_n = self.base.in_charge_window(charge_window_best, minutes_now)
                if charge_window_n >= 0:
                    charge_limit_best = getattr(self.base, "charge_limit_best", [])
                    if charge_window_n < len(charge_limit_best):
                        charge_limit = charge_limit_best[charge_window_n]
                        if not self.base.is_freeze_charge(charge_limit):
                            if self.last_phase != "off":
                                self.log("Curtailment: deferring to Predbat charge window")
                            phase = "off"
                            export_target_kw = -1

            soc_kw = getattr(self.base, "soc_kw", 0)
            soc_max = getattr(self.base, "soc_max", 10)
            target_pct = target_soc_kwh / max(soc_max, 0.1) * 100
            soc_pct = soc_kw / max(soc_max, 0.1) * 100

            # Log phase transitions
            if phase != self.last_phase:
                self.log(
                    "Curtailment: PHASE {} -> {} | SOC={:.1f}kWh ({:.0f}%) target={:.1f}kWh ({:.0f}%) "
                    "overflow={:.1f}kWh dno={:.1f}kW pv_scale={:.2f}x".format(
                        self.last_phase or "none",
                        phase,
                        soc_kw,
                        soc_pct,
                        target_soc_kwh,
                        target_pct,
                        remaining_overflow,
                        dno_limit,
                        self._max_pv_scale,
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
