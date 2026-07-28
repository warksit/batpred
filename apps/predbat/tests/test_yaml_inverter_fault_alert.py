#!/usr/bin/env python3
# SIG Inverter Fault Alert — message-diagnosis harness.
#
# "Running but idle" has TWO causes and they need different actions:
#   - ESS limits shut  -> battery hardware-clamped (our own stale Predbat freeze)
#   - limits open      -> genuine meter comms fault (mySigen 4001_2)
#
# 2026-07-28: the alert assumed the meter every time. It fired CORRECTLY at 05:33
# while the battery was clamped (ess_max_discharging_limit=0 since 04:01, Max Export
# commanding 6.6 kW into a locked battery) but told us to check mySigen, which showed
# no alerts — so it read as a false alarm and was dismissed. The lockout ran to 08:35.
# Correct detection, wrong diagnosis, so the alert trained us to ignore it.
#
# Renders the `fault_type` variable the way HA would (in order, StrictUndefined) and
# asserts the diagnosis matches the register state.
#
# Run: cd apps/predbat && python3 tests/test_yaml_inverter_fault_alert.py
import os
import sys

import jinja2
import yaml

HERE = os.path.dirname(__file__)
YAML_PATH = os.path.join(HERE, "..", "ha", "sig_inverter_fault_alert.yaml")

RUNNING_STATE = "sensor.sigen_plant_plant_running_state"
DISCH_LIMIT = "number.sigen_plant_ess_max_discharging_limit"
RATED_DISCH = "sensor.sigen_plant_ess_rated_discharging_power"
IMPORT_LIMIT = "number.sigen_plant_grid_import_limitation"


def _load():
    with open(YAML_PATH) as f:
        return yaml.safe_load(f)


def _render_fault_type(auto, states):
    """Render the action `variables` block in order, like HA does."""
    variables = auto["action"][0]["variables"]
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    ctx = {
        "states": lambda e: states.get(e, "unknown"),
        "is_state": lambda e, v: states.get(e) == v,
    }
    for key, tmpl in variables.items():
        rendered = env.from_string(tmpl).render(**ctx).strip()
        low = rendered.lower()
        if low in ("true", "false"):
            ctx[key] = low == "true"
        else:
            try:
                ctx[key] = float(rendered)
            except ValueError:
                ctx[key] = rendered
    return ctx["fault_type"]


def _mock(running="Running", disch=9.6, rated=9.6, imp=100):
    return {
        RUNNING_STATE: running,
        DISCH_LIMIT: str(disch),
        RATED_DISCH: str(rated),
        IMPORT_LIMIT: str(imp),
        "sensor.sigen_inverter_phase_a_voltage": "242",
        "sensor.sigen_inverter_grid_frequency": "50.0",
        "sensor.sigen_plant_battery_state_of_charge": "44.6",
        "sensor.sigen_plant_pv_power": "0.0",
    }


def test_clamped_battery_is_not_reported_as_a_meter_fault():
    """The 2026-07-28 05:33 case: Running, idle, ESS discharge limit shut."""
    msg = _render_fault_type(_load(), _mock(running="Running", disch=0.0))
    assert "CLAMPED" in msg, f"clamped battery must not be reported as a meter fault: {msg}"
    assert "4001_2" not in msg, f"must not send the user to mySigen for a clamp: {msg}"
    assert "discharge_rate" in msg, f"must name the actual thing to check: {msg}"
    print("PASS  clamped: discharge limit 0 -> battery-clamped diagnosis, not meter")


def test_blocked_import_also_reads_as_clamped():
    """grid_import_limitation=0 is the other half of a Predbat freeze."""
    msg = _render_fault_type(_load(), _mock(running="Running", disch=9.6, imp=0))
    assert "CLAMPED" in msg, f"import block must read as clamped: {msg}"
    print("PASS  clamped: import limit 0 -> battery-clamped diagnosis")


def test_open_limits_still_report_meter_fault():
    """Limits open and still idle -> genuinely the meter. Must not be swallowed."""
    msg = _render_fault_type(_load(), _mock(running="Running", disch=9.6, imp=100))
    assert "Meter Communication Fault" in msg, f"open limits must still flag the meter: {msg}"
    assert "4001_2" in msg, f"meter fault must still point at mySigen: {msg}"
    assert "CLAMPED" not in msg, f"must not cry clamp when limits are open: {msg}"
    print("PASS  meter: limits open -> meter fault diagnosis retained")


def test_protective_states_unchanged():
    """G99 protective disconnects keep their benign 'no action needed' text."""
    for state, expect in (
        ("Shutdown", "Protective Disconnect"),
        ("Environmental Abnormality", "Environmental Abnormality"),
        ("Standby", "Standby"),
    ):
        msg = _render_fault_type(_load(), _mock(running=state, disch=0.0))
        assert expect in msg, f"{state}: expected {expect!r}, got {msg}"
        assert "No action needed" in msg, f"{state} must stay benign: {msg}"
        # A clamp must never hijack a protective-state message.
        assert "CLAMPED" not in msg, f"{state} must not be reported as clamped: {msg}"
    print("PASS  protective: Shutdown / Environmental / Standby unchanged and benign")


def test_critical_sound_only_for_genuine_faults():
    """Protective states stay non-critical; clamp and meter faults stay critical."""
    auto = _load()
    sound = auto["action"][1]["data"]["data"]["push"]["sound"]
    assert "is_protective" in str(sound.get("critical")), f"critical flag must key off is_protective: {sound}"
    print("PASS  sound: critical gated on is_protective")


def main():
    for t in (
        test_clamped_battery_is_not_reported_as_a_meter_fault,
        test_blocked_import_also_reads_as_clamped,
        test_open_limits_still_report_meter_fault,
        test_protective_states_unchanged,
        test_critical_sound_only_for_genuine_faults,
    ):
        t()
    print("test_yaml_inverter_fault_alert: ALL PASSED")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"FAIL — {e}")
        sys.exit(1)
