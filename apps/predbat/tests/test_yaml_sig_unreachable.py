#!/usr/bin/env python3
# SIG Unreachable Alert — harness.
#
# The 2026-08-15 outage: site power cut, the generator carries Home Assistant
# but not the inverter, every SIG entity unknown from 15:48. Nearly 90 minutes
# passed with no notification, because sig_inverter_fault_alert's poll-liveness
# term deliberately excludes integration outages.
#
# The subtle half is the RESTART. HA comes back on the generator and the Sigen
# entities are born unknown — they never transition, so a state trigger can
# never fire. If `homeassistant: start` is not a trigger, this automation is
# decorative for the exact case it was written for. That is what
# test_restart_is_a_trigger pins.
#
# Renders the `variables` block the way HA would (in order, StrictUndefined) so
# a reordering cannot pass here and fail live.
#
# Run: cd apps/predbat && python3 tests/test_yaml_sig_unreachable.py
import os
import sys
from datetime import datetime, timezone

import jinja2
import yaml

HERE = os.path.dirname(__file__)
YAML_PATH = os.path.join(HERE, "..", "ha", "sig_unreachable_alert.yaml")

SOC = "sensor.sigen_plant_battery_state_of_charge"
RUN = "sensor.sigen_plant_plant_running_state"
PV = "sensor.sigen_plant_pv_power"
METER = "sensor.octopus_energy_electricity_24j0698298_1100025301348_current_demand"

NOW = datetime(2026, 8, 15, 17, 10, 0, tzinfo=timezone.utc)


def _load():
    with open(YAML_PATH) as f:
        return yaml.safe_load(f)


def _triggers(auto):
    """HA accepts `trigger:`/`triggers:`; read either so a key change cannot
    quietly bypass these tests."""
    return auto.get("triggers", auto.get("trigger"))


def _actions(auto):
    """As _triggers, for `action:`/`actions:`."""
    return auto.get("actions", auto.get("action"))


def _variables_block(auto):
    """The one `variables:` step in the action list."""
    for step in _actions(auto):
        if isinstance(step, dict) and "variables" in step:
            return step["variables"]
    raise AssertionError("no variables block found")


def _render(states, dark_since=None):
    """Render the variables block top-down, exactly as HA evaluates it."""
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    changed = dark_since if dark_since is not None else NOW

    class _Ent:
        last_changed = changed

    ctx = {
        "states": lambda e: states.get(e, "unknown"),
        "now": lambda: NOW,
    }
    # `states.sensor.x.last_changed` — attribute access alongside the callable.
    ctx["states"] = type(
        "S",
        (),
        {
            "__call__": staticmethod(lambda e: states.get(e, "unknown")),
            "sensor": type("Sensors", (), {"sigen_plant_battery_state_of_charge": _Ent()})(),
        },
    )()

    out = {}
    for name, expr in _variables_block(_load()).items():
        if isinstance(expr, str) and "{{" in expr:
            rendered = env.from_string(expr).render(**ctx, **out)
            out[name] = rendered.strip()
        else:
            out[name] = expr
        # Re-type the two we compare on, as HA's template engine would.
        if name in ("sig_dark", "meter_dark"):
            out[name] = out[name] == "True"
        if name == "dark_mins":
            out[name] = float(out[name])
        if name == "dark":
            out[name] = ["unavailable", "unknown", "none", ""]
        if name == "dark_since":
            out[name] = changed
    return out


DARK = {SOC: "unknown", RUN: "unknown", PV: "unknown"}
ALIVE = {SOC: "63.0", RUN: "Running", PV: "4.212"}


def test_restart_is_a_trigger():
    """The load-bearing one. After a restart the entities are born unknown and
    never transition, so without `homeassistant: start` this automation cannot
    fire for the case it exists for."""
    kinds = [t.get("trigger", t.get("platform")) for t in _triggers(_load())]
    assert "homeassistant" in kinds, f"HA restart must be a trigger, got {kinds}"
    ha = [t for t in _triggers(_load()) if t.get("trigger", t.get("platform")) == "homeassistant"][0]
    assert ha.get("event") == "start", f"must trigger on start, got {ha.get('event')}"
    print("  test_restart_is_a_trigger: PASSED")


def test_restart_path_waits_before_judging():
    """Every entity is briefly unknown during a restart, healthy ones included.
    Without a settle delay this pages on every routine restart."""
    steps = _actions(_load())
    first = steps[0]
    assert "if" in first, "the restart settle must be the FIRST action"
    gate = first["if"]
    assert any(c.get("id") == "ha_restart" for c in gate), "settle must be gated on the restart trigger"
    delay = first["then"][0]["delay"]
    assert delay.get("minutes", 0) >= 5, f"settle delay must be >= 5 min, got {delay}"
    print("  test_restart_path_waits_before_judging: PASSED")


def test_all_dark_is_required_not_any():
    """One entity dropping out is Modbus jitter. Alerting on `any` would page on
    routine noise and train us to ignore it — the 2026-07-28 lesson."""
    v = _render(DARK)
    assert v["sig_dark"] is True, "all three dark must read as dark"

    for live_one in (SOC, RUN, PV):
        partial = dict(DARK)
        partial[live_one] = ALIVE[live_one]
        assert _render(partial)["sig_dark"] is False, f"{live_one} alive must NOT read as dark"
    print("  test_all_dark_is_required_not_any: PASSED")


def test_healthy_plant_is_never_dark():
    assert _render(ALIVE)["sig_dark"] is False
    print("  test_healthy_plant_is_never_dark: PASSED")


def test_power_cut_and_network_fault_are_told_apart():
    """The triage hint. The smart meter is powered and metered independently, so
    it is the one signal that separates the two causes — and on 2026-08-15 it
    went dark too, which was the correct answer."""
    cut = _render({**DARK, METER: "unknown"})
    assert cut["meter_dark"] is True
    assert "site power" in cut["likely_cause"], cut["likely_cause"]

    net = _render({**DARK, METER: "-3700.0"})
    assert net["meter_dark"] is False
    assert "network" in cut["likely_cause"] or "network" in net["likely_cause"], net["likely_cause"]
    assert "site power" not in net["likely_cause"], net["likely_cause"]
    print("  test_power_cut_and_network_fault_are_told_apart: PASSED")


def _state_gate(auto):
    """The native state condition that gates the notify."""
    for step in _actions(auto):
        if isinstance(step, dict) and step.get("condition") == "state":
            return step
    raise AssertionError("no native state condition found")


def test_short_blip_does_not_alert():
    """The /15 sweep has no `for:`, so the duration gate lives in the condition.
    A 2-minute integration reload must not page."""
    gate = _state_gate(_load())
    assert gate["for"]["minutes"] >= 5, f"gate must require >= 5 min, got {gate['for']}"
    settle = _actions(_load())[0]["then"][0]["delay"]["minutes"]
    assert settle > gate["for"]["minutes"], f"restart settle ({settle}) must exceed the gate ({gate['for']['minutes']}) or the restart path can never satisfy it"
    print("  test_short_blip_does_not_alert: PASSED")


def test_gate_is_native_and_requires_all_three():
    """Native condition over a template: validated at config load rather than
    failing silently at runtime. A list of entity_ids is ANDed by HA, which is
    the all-dark-not-any rule."""
    gate = _state_gate(_load())
    assert set(gate["entity_id"]) == {SOC, RUN, PV}, gate["entity_id"]
    assert set(gate["state"]) == {"unavailable", "unknown"}, gate["state"]
    templates = [s for s in _actions(_load()) if isinstance(s, dict) and s.get("condition") == "template"]
    assert not templates, f"gate must be native, found template conditions: {templates}"
    print("  test_gate_is_native_and_requires_all_three: PASSED")


def test_the_2026_08_15_outage_would_have_paged():
    """Replay: 15:48 dark, checked at 17:10. The real alert stayed silent for
    nearly 90 minutes; this must not."""
    v = _render(DARK, dark_since=datetime(2026, 8, 15, 15, 48, tzinfo=timezone.utc))
    assert v["sig_dark"] is True
    assert v["dark_mins"] == 82.0, f"expected 82 min, got {v['dark_mins']}"
    assert v["dark_mins"] >= 5
    print(f"  test_the_2026_08_15_outage_would_have_paged: PASSED ({v['dark_mins']:.0f} min)")


def test_renotify_is_throttled():
    """A 15-minute sweep would page four times an hour. `mode: single` plus a
    trailing delay makes further triggers drop instead."""
    auto = _load()
    assert auto.get("mode") == "single", auto.get("mode")
    assert auto.get("max_exceeded") == "silent", auto.get("max_exceeded")
    tail = _actions(auto)[-1]
    assert "delay" in tail, f"last step must be the re-notify delay, got {tail}"
    assert tail["delay"].get("minutes", 0) >= 30, f"re-notify must be >= 30 min, got {tail['delay']}"
    print("  test_renotify_is_throttled: PASSED")


def test_notifies_andrew_critically():
    """Nothing can be written to the plant while it is unreachable, and the
    software drain floor is unenforceable — that warrants the critical channel."""
    notifies = [s for s in _actions(_load()) if isinstance(s, dict) and str(s.get("action", "")).startswith("notify.")]
    assert notifies, "must notify"
    assert notifies[0]["action"] == "notify.mobile_app_andrew_iphone", notifies[0]["action"]
    assert notifies[0]["data"]["data"]["push"]["sound"]["critical"] == 1
    print("  test_notifies_andrew_critically: PASSED")


def main():
    for t in (
        test_restart_is_a_trigger,
        test_restart_path_waits_before_judging,
        test_all_dark_is_required_not_any,
        test_healthy_plant_is_never_dark,
        test_power_cut_and_network_fault_are_told_apart,
        test_short_blip_does_not_alert,
        test_gate_is_native_and_requires_all_three,
        test_the_2026_08_15_outage_would_have_paged,
        test_renotify_is_throttled,
        test_notifies_andrew_critically,
    ):
        t()
    print("test_yaml_sig_unreachable: ALL PASSED")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"FAIL — {e}")
        sys.exit(1)
