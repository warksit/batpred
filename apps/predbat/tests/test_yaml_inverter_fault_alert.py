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
from datetime import datetime, timedelta

import jinja2
import yaml

HERE = os.path.dirname(__file__)
YAML_PATH = os.path.join(HERE, "..", "ha", "sig_inverter_fault_alert.yaml")

RUNNING_STATE = "sensor.sigen_plant_plant_running_state"
DISCH_LIMIT = "number.sigen_plant_ess_max_discharging_limit"
RATED_DISCH = "sensor.sigen_plant_ess_rated_discharging_power"
IMPORT_LIMIT = "number.sigen_plant_grid_import_limitation"


POLICY = "input_select.sig_dispatch_policy"
REQUESTED_MODE = "input_select.predbat_requested_mode"


def _triggers(auto):
    """HA accepts `trigger:` and `triggers:`; the deployed config uses the modern
    plural. Read either so a key-style change cannot quietly bypass these tests."""
    return auto.get("triggers", auto.get("trigger"))


def _actions(auto):
    """As _triggers, for `action:` / `actions:`."""
    return auto.get("actions", auto.get("action"))


def _load(path=None):
    with open(path or YAML_PATH) as f:
        return yaml.safe_load(f)


NOW = datetime(2026, 8, 10, 9, 34, 13)


class _Entity:
    """One entity's object form — `states.sensor.x.last_changed` in HA."""

    def __init__(self, state, age_s):
        self.state = state
        self.last_changed = NOW - timedelta(seconds=age_s)
        self.last_updated = self.last_changed


class _Domain:
    """`states.sensor` — attribute access by object_id."""

    def __init__(self, entities):
        self._entities = entities

    def __getattr__(self, name):
        if name in self._entities:
            return self._entities[name]
        raise jinja2.UndefinedError("states.sensor.{} is not in the mock".format(name))


class _States:
    """HA's `states` is BOTH callable and attribute-accessible. The staleness
    check needs the object form, so the mock has to be both too."""

    def __init__(self, values, ages):
        self._values = values
        self.sensor = _Domain({e.split(".", 1)[1]: _Entity(values.get(e, "unknown"), age) for e, age in ages.items()})

    def __call__(self, entity):
        return self._values.get(entity, "unknown")


def _render_fault_type(auto, states, ages=None):
    """Render the action `variables` block in order, like HA does."""
    variables = _actions(auto)[0]["variables"]
    return _render_block(variables, states, ages)["fault_type"]


def _render_block(variables, states, ages=None):
    """Render an ordered variables dict under StrictUndefined; return the context."""
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    ctx = {
        "states": _States(states, ages or {}),
        "is_state": lambda e, v: states.get(e) == v,
        "now": lambda: NOW,
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
    return ctx


# Default ages: meter fresh (5 s), poll fresh (1 s) -> meter_dead False, so every
# pre-existing test keeps exercising the branch it was written for.
def _ages(grid_age=5, poll_age=1):
    return {
        "sensor.sigen_plant_grid_active_power": grid_age,
        "sensor.sigen_plant_pv_power": poll_age,
        "sensor.sigen_plant_battery_power": poll_age,
    }


def _mock(running="Running", disch=9.6, rated=9.6, imp=100, policy="Max Export", requested_mode="Demand"):
    return {
        RUNNING_STATE: running,
        DISCH_LIMIT: str(disch),
        RATED_DISCH: str(rated),
        IMPORT_LIMIT: str(imp),
        POLICY: policy,
        REQUESTED_MODE: requested_mode,
        "sensor.sigen_inverter_phase_a_voltage": "242",
        "sensor.sigen_inverter_grid_frequency": "50.0",
        "sensor.sigen_plant_battery_state_of_charge": "44.6",
        "sensor.sigen_plant_pv_power": "0.0",
        "sensor.sigen_plant_battery_power": "0.0",
        "sensor.sigen_plant_grid_active_power": "0.0",
    }


def test_clamped_battery_is_not_reported_as_a_meter_fault():
    """The 2026-07-28 05:33 case: Running, idle, ESS discharge limit shut."""
    msg = _render_fault_type(_load(), _mock(running="Running", disch=0.0), _ages())
    assert "CLAMPED" in msg, f"clamped battery must not be reported as a meter fault: {msg}"
    assert "4001_2" not in msg, f"must not send the user to mySigen for a clamp: {msg}"
    assert "discharge_rate" in msg, f"must name the actual thing to check: {msg}"
    print("PASS  clamped: discharge limit 0 -> battery-clamped diagnosis, not meter")


def test_blocked_import_also_reads_as_clamped():
    """grid_import_limitation=0 is the other half of a Predbat freeze."""
    msg = _render_fault_type(_load(), _mock(running="Running", disch=9.6, imp=0), _ages())
    assert "CLAMPED" in msg, f"import block must read as clamped: {msg}"
    print("PASS  clamped: import limit 0 -> battery-clamped diagnosis")


def test_open_limits_still_report_meter_fault():
    """Limits open and still idle -> genuinely the meter. Must not be swallowed."""
    msg = _render_fault_type(_load(), _mock(running="Running", disch=9.6, imp=100), _ages())
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
        msg = _render_fault_type(_load(), _mock(running=state, disch=0.0), _ages())
        assert expect in msg, f"{state}: expected {expect!r}, got {msg}"
        assert "No action needed" in msg, f"{state} must stay benign: {msg}"
        # A clamp must never hijack a protective-state message.
        assert "CLAMPED" not in msg, f"{state} must not be reported as clamped: {msg}"
    print("PASS  protective: Shutdown / Environmental / Standby unchanged and benign")


def test_critical_sound_only_for_genuine_faults():
    """Protective states stay non-critical; clamp and meter faults stay critical."""
    auto = _load()
    sound = _actions(auto)[1]["data"]["data"]["push"]["sound"]
    assert "is_protective" in str(sound.get("critical")), f"critical flag must key off is_protective: {sound}"
    print("PASS  sound: critical gated on is_protective")


def test_predbat_freeze_is_not_a_fault():
    """2026-08-05 05:43. Predbat held SOC at its 6% target overnight, which locks
    the battery via discharge_rate=0 -> ess_max_discharging_limit=0. The alert
    fired, correctly describing the state — but told the user to check that the
    heartbeat was enabled and discharge_rate was not 0, both of which were CORRECT
    at the time (CM had handed back; the zero was Predbat's intent). CM took the
    wheel at 05:55 and the limits re-opened 4 s later.

    An alert that fires on intended behaviour and gives advice that does not apply
    is worse than no alert: it is how the REAL 2026-07-28 lockout got dismissed.
    """
    msg = _render_fault_type(_load(), _mock(running="Running", disch=0.0, imp=0, policy="Predbat", requested_mode="Freeze Charging"), _ages())
    assert "CLAMPED" not in msg, f"Predbat deliberately freezing is not a clamp fault: {msg}"

    msg = _render_fault_type(_load(), _mock(running="Running", disch=0.0, policy="Predbat", requested_mode="Freeze Discharging"), _ages())
    assert "CLAMPED" not in msg, f"export freeze is equally intended: {msg}"
    print("PASS  intended: Predbat owns the wheel and is freezing -> no clamp alert")


def test_clamp_still_fires_when_cm_owns_the_wheel():
    """The case that MUST still alert: CM is driving and the limits are shut, so
    nobody intends the clamp. This is 2026-07-26 / 07-28 — SOC flat for hours with
    Max Export commanding into a locked battery."""
    msg = _render_fault_type(_load(), _mock(running="Running", disch=0.0, policy="Max Export", requested_mode="Demand"), _ages())
    assert "CLAMPED" in msg, f"CM driving into shut limits must still alert: {msg}"
    assert "discharge_rate" in msg

    # And a shut limit while Predbat owns it but is NOT freezing is still a fault.
    msg = _render_fault_type(_load(), _mock(running="Running", disch=0.0, policy="Predbat", requested_mode="Demand"), _ages())
    assert "CLAMPED" in msg, f"Predbat idle with shut limits is nobody's intent: {msg}"
    print("PASS  fault: shut limits with no freeze intent -> still alerts")


def _meter_trigger(auto):
    """The `meter_stale` trigger, by id."""
    for trig in _triggers(auto):
        if trig.get("id") == "meter_stale":
            return trig
    raise AssertionError("no trigger with id 'meter_stale' — the 2026-08-10 fault has no detector")


def test_dead_meter_is_detected_while_the_plant_runs_normally():
    """2026-08-10, the three-hour silence this exists to end.

    mySigen raised 4001 "Electric meter communication anomaly" at 08:23:58. The
    plant kept RUNNING and producing 4.4 kW throughout, so the only meter trigger
    (inverter active power stuck near zero for 2 min) never fired, and HA said
    nothing. The measured signature at 09:34: grid_active_power frozen 2175 s
    while pv_power and battery_power in the same poll updated every 1-2 s.
    """
    auto = _load()
    ctx = _render_block(_actions(auto)[0]["variables"], _mock(running="Running"), _ages(grid_age=2175, poll_age=2))
    assert ctx["meter_dead"] is True, "38 min of frozen grid power with a live poll must read as a dead meter"
    msg = ctx["fault_type"]
    assert "METER DEAD" in msg, f"must name the fault: {msg}"
    assert "4001" in msg, f"must point at the mySigen alert code: {msg}"
    assert "reboot does NOT clear it" in msg, f"must record that today's reboot did not fix it: {msg}"
    assert "CLAMPED" not in msg, f"a dead meter must outrank the clamp diagnosis: {msg}"
    print("PASS  meter dead: 2175 s stale grid + live poll -> METER DEAD diagnosis")


def test_dead_meter_outranks_a_shut_limit():
    """Ordering matters. With the meter dead, `consumed_power` is derived, so the
    clamp heuristic is reading fiction — the meter branch must come FIRST or the
    alert sends you after a battery clamp that may not exist."""
    ctx = _render_block(_actions(_load())[0]["variables"], _mock(running="Running", disch=0.0), _ages(grid_age=2175, poll_age=2))
    assert "METER DEAD" in ctx["fault_type"], f"meter must win over clamp: {ctx['fault_type']}"
    print("PASS  meter dead: outranks the clamp branch")


def test_live_meter_is_never_reported_dead():
    """The gag test. A jittering CT and a legitimately-zero grid must both stay
    silent — `Solar Charge Battery` targets zero grid flow BY DESIGN, so a
    value-based test would false-alarm every time it ran. Only staleness counts."""
    for grid_age in (0, 5, 60, 599):
        ctx = _render_block(_actions(_load())[0]["variables"], _mock(running="Running", policy="Solar Charge Battery"), _ages(grid_age=grid_age, poll_age=1))
        assert ctx["meter_dead"] is False, f"grid {grid_age}s old is a live meter, not a fault"
        assert "METER DEAD" not in ctx["fault_type"], f"grid {grid_age}s old must not alert"
    print("PASS  live meter: fresh grid reading never reported dead (incl. Solar Charge zero-flow)")


def test_stale_poll_is_not_blamed_on_the_meter():
    """If the whole integration stops (HA restart, Modbus drop) EVERYTHING goes
    stale. That is not a meter fault and must not be reported as one — the
    poll-liveness term is what makes the detector specific."""
    ctx = _render_block(_actions(_load())[0]["variables"], _mock(running="Running"), _ages(grid_age=3000, poll_age=3000))
    assert ctx["meter_dead"] is False, "a dead integration is not a dead meter"
    assert "METER DEAD" not in ctx["fault_type"]
    print("PASS  specificity: whole-integration outage is not reported as a meter fault")


def test_trigger_and_variable_agree():
    """Duplicate-logic guard. HA gives triggers no access to `variables`, so the
    staleness test exists in BOTH the `meter_stale` trigger and the `meter_dead`
    variable. Render both over the same cases and assert they never disagree —
    the RD22 sell-clamp shipped with one of two copies fixed (2026-08-06)."""
    trig_tmpl = _meter_trigger(_load())["value_template"]
    variables = _actions(_load())[0]["variables"]
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    for grid_age, poll_age in ((2175, 2), (5, 1), (3000, 3000), (601, 119), (599, 121), (700, 60)):
        states = _mock(running="Running")
        ages = _ages(grid_age=grid_age, poll_age=poll_age)
        ctx = {"states": _States(states, ages), "is_state": lambda e, v: states.get(e) == v, "now": lambda: NOW}
        trig = env.from_string(trig_tmpl).render(**ctx).strip().lower() == "true"
        var = _render_block(variables, states, ages)["meter_dead"]
        assert trig == var, f"grid_age={grid_age} poll_age={poll_age}: trigger says {trig}, variable says {var}"
    print("PASS  duplicate-logic: meter_stale trigger and meter_dead variable agree on all 6 cases")


HOUSEHOLD_SERVICES = ("notify.mobile_app_iphone", "notify.mobile_app_shona_s_ipad")
ANDREW_SERVICE = "notify.mobile_app_andrew_iphone"


def _household_branch(auto):
    """The `if meter_dead` block that notifies the people on site."""
    for step in _actions(auto):
        if "if" in step:
            return step
    raise AssertionError("no conditional branch — the on-site household is never notified")


def _notify_targets(steps):
    return [s.get("action") for s in steps if str(s.get("action", "")).startswith("notify.")]


def test_dead_meter_alerts_the_people_on_site():
    """Only someone AT the property can fix a meter comms fault, so a dead meter
    must reach the household, not just the person who maintains the system.

    2026-08-10: the fault ran from 08:23 with the one alert target being a phone
    that was not on site. Deliberately scoped to the meter branch — see
    test_other_faults_stay_with_the_maintainer."""
    branch = _household_branch(_load())
    targets = _notify_targets(branch["then"])
    for svc in HOUSEHOLD_SERVICES:
        assert svc in targets, f"{svc} must be notified for a dead meter, got {targets}"
    print("PASS  household: dead meter reaches both on-site devices")


def test_other_faults_stay_with_the_maintainer():
    """A clamped battery, a G99 disconnect or a Predbat freeze are not on-site
    actionable — routing those to the household is pure noise, and noise is how
    the real 2026-07-28 lockout got dismissed."""
    branch = _household_branch(_load())
    cond = str(branch["if"])
    assert "meter_dead" in cond, f"the household branch must be gated on meter_dead alone, got {cond}"
    assert "is_clamped" not in cond and "is_protective" not in cond, f"must not widen beyond the meter fault: {cond}"
    print("PASS  household: gated on meter_dead only")


def test_household_message_is_actionable_not_diagnostic():
    """Andrew's message names registers and Sub1G links. That text is useless to
    someone being asked to go and look at the meter — and jargon they cannot act
    on trains them to ignore the alert."""
    ctx = _render_block(_actions(_load())[0]["variables"], _mock(running="Running"), _ages(grid_age=2175, poll_age=2))
    msg = ctx["household_message"]
    for jargon in ("Sub1G", "CT", "DERIVED", "curtailment", "Modbus", "kW"):
        assert jargon not in msg, f"household message must not contain {jargon!r}: {msg}"
    assert "meter" in msg.lower(), f"must say what is wrong: {msg}"
    assert "Andrew" in msg, f"must tell her he already knows, so she does not have to relay it: {msg}"
    print("PASS  household: message is plain and actionable")


def test_household_message_names_the_actual_fix():
    """The fix is a power cycle of the grid meter, done at **a switch IN the
    tack room** that feeds it (Andrew, 2026-08-10).

    Two corrections are baked into this test, both from getting it wrong first:

    1. The first version withheld the procedure entirely and pointed at Andrew,
       because the install was genuinely unknown here. Right while unknown,
       wrong to leave once known — "ring someone" costs a phone call every time
       the fault recurs, and this one recurs.
    2. The second version sent her to "its own small consumer unit above the
       tack room roof". That is where the METER is, not where the SWITCH is.
       She would have been on a roof looking for the wrong thing. The switch is
       inside the tack room.

    So this asserts the SWITCH and its location, and explicitly bans the roof
    wording — a wrong location is worse than no location.
    """
    ctx = _render_block(_actions(_load())[0]["variables"], _mock(running="Running"), _ages(grid_age=2175, poll_age=2))
    msg = ctx["household_message"]
    low = msg.lower()
    assert "switch" in low, f"must name the thing she operates: {msg}"
    assert "tack room" in low, f"must say where that switch is: {msg}"
    assert "roof" not in low, f"the switch is INSIDE the tack room — must not send her to the roof: {msg}"
    assert "consumer unit" not in low, f"she operates a switch, not the consumer unit: {msg}"
    print("PASS  household: names the tack-room switch, not the roof")


def test_household_alert_is_not_critical():
    """Critical overrides silent mode and Do Not Disturb. A meter that cannot
    count is not an emergency — nothing is damaged and export revenue is
    unaffected — and this can fire at 03:00 as easily as at noon."""
    branch = _household_branch(_load())
    for step in branch["then"]:
        if str(step.get("action", "")).startswith("notify."):
            crit = str(step["data"]["data"]["push"]["sound"]["critical"])
            assert crit == "0", f"household alert must not be critical, got {crit}"
    print("PASS  household: normal priority, not critical")


def main():
    for t in (
        test_dead_meter_alerts_the_people_on_site,
        test_other_faults_stay_with_the_maintainer,
        test_household_message_is_actionable_not_diagnostic,
        test_household_message_names_the_actual_fix,
        test_household_alert_is_not_critical,
        test_dead_meter_is_detected_while_the_plant_runs_normally,
        test_dead_meter_outranks_a_shut_limit,
        test_live_meter_is_never_reported_dead,
        test_stale_poll_is_not_blamed_on_the_meter,
        test_trigger_and_variable_agree,
        test_clamped_battery_is_not_reported_as_a_meter_fault,
        test_predbat_freeze_is_not_a_fault,
        test_clamp_still_fires_when_cm_owns_the_wheel,
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
