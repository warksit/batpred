# -----------------------------------------------------------------------------
# Curtailment Manager Plugin for Predbat
# Iterative target SOC algorithm to eliminate solar curtailment
#
# Works WITH the HA automation (curtailment_manager_dynamic_export_limit):
#   - Plugin (5-min): computes target SOC, switches EMS mode, publishes sensors
#   - HA automation (~5s): reactive export limit control (reads export_target sensor)
#
# Control model (SIG inverter):
#   Active:  D-ESS mode, read_only=True (suppresses Predbat inverter control)
#   Inactive: MSC mode, read_only=False (Predbat resumes)
# -----------------------------------------------------------------------------

from curtailment_calc import compute_remaining_overflow, compute_target_soc, should_activate
from plugin_system import PredBatPlugin

# SIG entity names (Mum's system)
SIG_EMS_MODE = "select.sigen_plant_remote_ems_control_mode"
SIG_EXPORT_LIMIT = "number.sigen_plant_grid_export_limitation"
SIG_IMPORT_LIMIT = "number.sigen_plant_grid_import_limitation"
SIG_CHARGE_LIMIT = "number.sigen_plant_ess_charge_cut_off_state_of_charge"

# HA input helper entity IDs
HA_ENABLE = "input_boolean.curtailment_manager_enable"
HA_BUFFER = "input_number.curtailment_manager_buffer"

PREDICT_STEP = 5
SOC_MARGIN_KWH = 0.5


class CurtailmentPlugin(PredBatPlugin):
    """
    Curtailment manager — computes target SOC from forecast overflow,
    switches EMS mode, and publishes sensors for the HA automation.

    Algorithm (v5 iterative target SOC):
    1. remaining_overflow = sum of max(0, excess - DNO) × step_hours for future slots
    2. target_soc = battery_max - remaining_overflow
    3. SOC > target: drain (export_target = DNO limit)
    4. SOC ≈ target: hold (export_target = -1, HA automation tracks PV-load)
    5. SOC < target: charge from PV (export_target = 0)
    """

    def __init__(self, base):
        super().__init__(base)
        self.last_ems_mode = None
        self.last_import_limit = None
        self.last_charge_limit = None
        self.was_active = False
        self._dno_limit = 4.0
        self.last_phase = None

    def register_hooks(self, plugin_system):
        plugin_system.register_hook("on_update", self.on_update)

    def get_config(self):
        """Read configuration from HA input helpers and Predbat config."""
        enabled = self.base.get_state_wrapper(HA_ENABLE, default="off")
        enabled = str(enabled).lower() in ("on", "true")

        # DNO limit from Predbat's export_limit config (apps.yaml, in Watts)
        dno_limit = self.base.get_arg("export_limit", 4000, index=0) / 1000.0

        # Buffer for forecast error (kWh)
        buffer = self.base.get_state_wrapper(HA_BUFFER, default=1.0)
        try:
            buffer = float(buffer)
        except (ValueError, TypeError):
            buffer = 1.0

        return enabled, dno_limit, buffer

    def calculate(self, dno_limit_kw, buffer_kwh=1.0):
        """
        Compute curtailment target SOC using v5 iterative algorithm.

        Returns (target_soc_kwh, remaining_overflow_kwh, phase, export_target_kw)
        """
        pv_step = getattr(self.base, "pv_forecast_minute_step", {})
        load_step = getattr(self.base, "load_minutes_step", {})
        soc_kw = getattr(self.base, "soc_kw", 0)
        soc_max = getattr(self.base, "soc_max", 10)
        forecast_minutes = getattr(self.base, "forecast_minutes", 1440)

        if not pv_step or not soc_max:
            return soc_max, 0, "off", -1

        # Only look at TODAY's solar (up to 23:00) to avoid over-draining
        # for tomorrow's overflow. No PV after 23:00 even in UK midsummer.
        minutes_now = getattr(self.base, "minutes_now", 720)
        solar_end_minute = min(forecast_minutes, max(PREDICT_STEP, 23 * 60 - minutes_now))

        # Compute remaining overflow from next step to end of today's solar
        # Predbat forecast values are kWh per step (not kW)
        step_to_kw = 60.0 / PREDICT_STEP  # kWh-per-step → kW
        remaining_overflow = compute_remaining_overflow(pv_step, load_step, dno_limit_kw, start_minute=PREDICT_STEP, end_minute=solar_end_minute, step_minutes=PREDICT_STEP, values_are_kwh=True)

        target_soc_kwh = compute_target_soc(remaining_overflow, soc_max, buffer_kwh)
        active = should_activate(remaining_overflow)

        # Safety net: activate if PV > DNO and battery near full,
        # even if forecast missed it. Should not fire if forecast is accurate —
        # if it does, investigate why the forecast didn't predict overflow.
        pv_now_kw = pv_step.get(0, 0) * step_to_kw
        if not active and pv_now_kw > dno_limit_kw and soc_kw >= soc_max - SOC_MARGIN_KWH:
            self.log("Curtailment: WARNING — real-time activation (forecast missed overflow). " "PV {:.1f}kW > DNO {:.1f}kW, SOC {:.1f}kWh near full".format(pv_now_kw, dno_limit_kw, soc_kw))
            active = True
            target_soc_kwh = soc_max - SOC_MARGIN_KWH

        if not active:
            return soc_max, 0, "off", -1

        # Phase determination based on SOC vs target
        if soc_kw > target_soc_kwh + SOC_MARGIN_KWH:
            # Above target — drain battery toward target
            phase = "active"
            export_target_kw = dno_limit_kw
        elif soc_kw < target_soc_kwh - SOC_MARGIN_KWH:
            # Below target — charge from PV toward target
            phase = "charge"
            export_target_kw = 0
        else:
            # At target — hold, HA automation tracks PV-load
            phase = "hold"
            export_target_kw = -1

        return target_soc_kwh, remaining_overflow, phase, export_target_kw

    def publish(self, phase, target_soc_kwh, remaining_overflow_kwh, export_target_kw, dno_limit_kw):
        """Publish curtailment sensors via dashboard_item."""
        prefix = self.base.prefix
        soc_max = getattr(self.base, "soc_max", 10)
        target_pct = round(target_soc_kwh / soc_max * 100, 1) if soc_max > 0 else 100

        self.base.dashboard_item(
            "sensor.{}_curtailment_phase".format(prefix),
            phase.capitalize(),
            {
                "friendly_name": "Curtailment Phase",
                "icon": "mdi:solar-power-variant",
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

    def write_sig(self, ems_mode, import_limit, charge_limit):
        """Write SIG entities, only when values change."""
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
        activating = phase in ("active", "hold", "charge")

        if activating:
            if not self.was_active:
                self.log("Curtailment activating (phase={})".format(phase))

            # D-ESS mode, block grid import, allow solar charging to 100%
            self.write_sig(
                ems_mode="Command Discharging (ESS First)",
                import_limit=0,
                charge_limit=100,
            )

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

            enabled, dno_limit, buffer = self.get_config()
            self._dno_limit = dno_limit

            if not enabled:
                if self.was_active:
                    self.apply("off", -1)
                soc_max = getattr(self.base, "soc_max", 10)
                self.publish("off", soc_max, 0, -1, dno_limit)
                return

            target_soc_kwh, remaining_overflow, phase, export_target_kw = self.calculate(dno_limit, buffer)

            # Don't take inverter control until PV is generating
            pv_step = getattr(self.base, "pv_forecast_minute_step", {})
            pv_now_kw = pv_step.get(0, 0) * (60.0 / PREDICT_STEP)
            if phase != "off" and pv_now_kw < 0.1:
                if self.last_phase != "off":
                    self.log("Curtailment: waiting for PV (currently {:.2f}kW)".format(pv_now_kw))
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
                    "overflow={:.1f}kWh buffer={:.1f}kWh dno={:.1f}kW".format(
                        self.last_phase or "none", phase,
                        soc_kw, soc_pct, target_soc_kwh, target_pct,
                        remaining_overflow, buffer, dno_limit,
                    )
                )
                self.last_phase = phase

            self.publish(phase, target_soc_kwh, remaining_overflow, export_target_kw, dno_limit)
            self.apply(phase, export_target_kw)

        except Exception as e:
            self.log("Curtailment plugin error: {}".format(e))
            soc_max = getattr(self.base, "soc_max", 10)
            self.publish("off", soc_max, 0, -1, dno_limit)
            if self.was_active:
                try:
                    self.apply("off", -1)
                except Exception:
                    pass
