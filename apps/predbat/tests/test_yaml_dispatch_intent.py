#!/usr/bin/env python3
# SIG dispatch intent helpers — the single point of truth (RD26).
#
# ha/sig_dispatch_intent_helpers.yaml defines the effective policy and the dispatch
# setpoint ONCE. sig_dispatch_heartbeat must READ them, never recompute them — that
# duplication is what produced three divergences on 2026-08-06.
#
# Two guards here:
#   1. the sensors still produce the values they replaced (a reference oracle,
#      checked against the migration on the day it landed)
#   2. the heartbeat does not contain the arithmetic any more — so a future edit
#      cannot quietly re-inline a second copy
#
# Run: cd apps/predbat && python3 tests/test_yaml_dispatch_intent.py
import os
import sys

import jinja2
import yaml

HERE = os.path.dirname(__file__)
HELPERS_PATH = os.path.join(HERE, "..", "ha", "sig_dispatch_intent_helpers.yaml")
HEARTBEAT_PATH = os.path.join(HERE, "..", "ha", "sig_dispatch_heartbeat.yaml")
CALENDAR = "calendar.octopus_energy_a_4ba7c915_octoplus_saving_sessions"
ACTIVE_POLICIES = ("Max Export", "Hold Battery", "Solar Charge Battery")


def _load(path):
    with open(path) as f:
        return yaml.safe_load(f)


def _env():
    env = jinja2.Environment()
    env.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).strip().lower() in ("true", "1", "yes", "on")
    return env


def render_sensors(states):
    """Render both intent sensors in dependency order, as HA would."""
    doc = _load(HELPERS_PATH)
    by_id = {s["unique_id"]: s for s in doc["sensors"]}
    env = _env()
    resolved = dict(states)

    def ctx():
        return {"states": lambda e: resolved.get(e, "unknown"), "is_state": lambda e, v: resolved.get(e) == v}

    policy = env.from_string(by_id["sig_effective_policy"]["state"]).render(**ctx()).strip()
    resolved["sensor.sig_effective_policy"] = policy
    dispatch = env.from_string(by_id["sig_dispatch_kw"]["state"]).render(**ctx()).strip()
    return policy, float(dispatch)


def reference(override, select, session, pv, load_raw, soc, cap_w=3680, hard=2.8):
    """The oracle: the behaviour the helpers replaced, stated independently.

    Verified equal to the pre-refactor heartbeat inline formulas over the full
    matrix on 2026-08-06 before the arithmetic was removed from the automation.
    """
    p = override if override not in ("Off", "unknown", "unavailable", "") else ("Max Export" if (session and select != "Predbat") else select)
    cap = cap_w / 1000.0
    load = max(load_raw, 0.2)
    if p == "Max Export":
        raw, ceil = 6.6, 6.6
    elif p == "Hold Battery":
        raw, ceil = max(pv, load), min(load + cap, 6.6)
    else:
        raw, ceil = load, min(load + cap, 6.6)
    pre = min(raw, pv) if (soc <= hard and p == "Max Export") else raw
    return p, round(max(min(pre, ceil), 0), 2)


def _matrix():
    for select in ("Predbat", "Max Export", "Hold Battery", "Solar Charge Battery"):
        for override in ("Off", "Hold Battery", "Max Export", "Solar Charge Battery", "Predbat"):
            for session in (False, True):
                for pv, load, soc in ((0.1, 1.5, 1.3), (1.329, 0.364, 1.7), (8.0, 0.5, 50), (0.0, 0.4, 90), (3.0, 3.0, 2.8), (3.19, 0.48, 1.8)):
                    yield override, select, session, pv, load, soc


def _states(override, select, session, pv, load, soc, hard=2.8):
    return {
        "input_select.sig_override": override,
        "input_select.sig_dispatch_policy": select,
        CALENDAR: "on" if session else "off",
        "sensor.sigen_plant_pv_power": str(pv),
        "sensor.sigen_plant_total_load_power": str(load),
        "sensor.sigen_plant_battery_state_of_charge": str(soc),
        "input_number.dno_export_limit_w": "3680",
        "input_number.sig_drain_floor_pct": str(hard),
    }


def test_intent_sensors_match_reference():
    """The single source of truth must produce what the two copies produced."""
    bad = []
    n = 0
    for override, select, session, pv, load, soc in _matrix():
        n += 1
        got_p, got_kw = render_sensors(_states(override, select, session, pv, load, soc))
        want_p, want_kw = reference(override, select, session, pv, load, soc)
        if got_p != want_p:
            bad.append("POLICY ovr={} sel={} sess={}: got {!r} want {!r}".format(override, select, session, got_p, want_p))
        elif abs(got_kw - want_kw) > 1e-6:
            bad.append("KW ovr={} sel={} sess={} pv={} load={} soc={}: got {} want {}".format(override, select, session, pv, load, soc, got_kw, want_kw))
    assert not bad, "{} of {} cases differ:\n  {}".format(len(bad), n, "\n  ".join(bad[:8]))
    print("PASS  intent sensors match reference over {} cases".format(n))


def test_sell_clamp_is_sell_only():
    """RD22, restated against the single source: below the drain floor Max Export
    clamps to PV, Hold and Solar Charge still cover load."""
    st = _states("Max Export", "Predbat", False, pv=0.311, load=0.359, soc=1.3)
    assert abs(render_sensors(st)[1] - 0.31) < 0.011, "Max Export below floor must clamp to PV"
    st = _states("Hold Battery", "Predbat", False, pv=0.311, load=0.359, soc=1.3)
    assert abs(render_sensors(st)[1] - 0.359) < 0.011, "Hold below floor must still cover load"
    st = _states("Solar Charge Battery", "Predbat", False, pv=0.311, load=0.359, soc=1.3)
    assert abs(render_sensors(st)[1] - 0.359) < 0.011, "Solar Charge below floor must still cover load"
    print("PASS  sell-clamp is sell-only (RD22) at the single source")


def test_dispatch_never_emits_a_bare_unknown():
    """Fail-safe: with every source entity missing the sensor must still render a
    number, and consumers must be able to tell "no opinion" from a real 0 —
    0 is a legitimate setpoint, so it cannot double as the error value."""
    _, kw = render_sensors({})
    assert isinstance(kw, float), "must render a number even with no inputs"
    doc = _load(HEARTBEAT_PATH)
    blob = yaml.safe_dump(doc)
    assert "float(-1)" in blob, "consumers must read the setpoint as | float(-1) so a missing sensor is distinguishable from a real 0 kW"
    print("PASS  fail-safe: renders a number; consumers use float(-1) sentinel")


def _heartbeat_logic_text():
    """Every place the heartbeat could hide a second copy of the maths."""
    doc = _load(HEARTBEAT_PATH)
    trig = next(t for t in doc["trigger"] if t.get("id") == "stale_setpoint")["value_template"]
    variables = yaml.safe_dump(doc["action"][0]["variables"])
    # Strip {# ... #} comments — they legitimately DESCRIBE the maths.
    out = []
    for text in (trig, variables):
        while "{#" in text and "#}" in text:
            text = text[: text.index("{#")] + text[text.index("#}") + 2 :]
        out.append(text)
    return out


def test_heartbeat_defers_to_intent_sensors():
    """The heartbeat must READ the intent sensors, not recompute them.

    This is the guard that keeps the single point of truth single. Without it,
    a future edit can reintroduce the arithmetic into one copy and we are back to
    2026-08-06, when the clamp existed in two places and the policy in two forms.
    """
    trig, variables = _heartbeat_logic_text()
    for name, text in (("stale_setpoint trigger", trig), ("action variables", variables)):
        assert "sensor.sig_effective_policy" in text, "{} must read the shared policy sensor".format(name)
        assert "sensor.sig_dispatch_kw" in text, "{} must read the shared setpoint sensor".format(name)
        # The arithmetic itself must be gone. These tokens only ever appear in a
        # local re-derivation of the dispatch.
        # `dno_export_limit_w` is NOT banned in the action: the export-limit
        # register write legitimately needs the DNO cap, and that is not part of
        # the dispatch computation. It IS banned in the trigger, which has no
        # business reading it at all.
        banned = ["6.6 if", "| max if", "sigen_plant_pv_power", "sig_drain_floor_pct"]
        if name == "stale_setpoint trigger":
            banned.append("dno_export_limit_w")
        for banned_token in banned:
            assert banned_token not in text, "{} still contains {!r} — the dispatch maths has been re-inlined; it belongs ONLY in sig_dispatch_intent_helpers.yaml".format(name, banned_token)
    print("PASS  heartbeat defers to the intent sensors (no second copy)")


def test_triggers_fire_on_the_derived_sensor_not_its_inputs():
    """RD26 corollary — the race this cost us live on 2026-08-06 11:42.

    The heartbeat used to trigger on `input_select.sig_override` /
    `input_select.sig_dispatch_policy` and compute the policy inline, so trigger
    and value were always consistent. RD26 moved the value into a template sensor
    but LEFT the triggers on the inputs. HA fires the state trigger on the input
    immediately; the derived sensor recomputes a moment later. Trace of the run:

        trigger:   state of input_select.sig_override, "Off" -> "Max Export"
        variables: policy = "Predbat"   dispatch_kw = 0.68      <- stale
        -> took the Predbat branch, wrote nothing, finished in 12 ms

    The override is the human's immediate lever; it did nothing for 47 s until the
    next :00 beat, with the plant self-consuming at 4.4 kW into the battery on an
    overflow day.

    Fix: trigger on `sensor.sig_effective_policy`. A derived sensor cannot change
    before it has been computed, so the ordering is correct by construction rather
    than by luck. `stale_setpoint` already references `sensor.sig_dispatch_kw` and
    is safe for the same reason.
    """
    doc = _load(HEARTBEAT_PATH)
    state_triggers = [t for t in doc["trigger"] if t.get("platform") == "state" or t.get("trigger") == "state"]
    entities = {t.get("entity_id") for t in state_triggers}
    assert "sensor.sig_effective_policy" in entities, "policy changes must be triggered from the DERIVED sensor, or the action reads a stale value"
    for raw in ("input_select.sig_override", "input_select.sig_dispatch_policy"):
        assert raw not in entities, "{} must NOT be a trigger — it fires before sensor.sig_effective_policy has recomputed (live 2026-08-06 11:42, 47 s of nothing)".format(raw)
    print("PASS  triggers fire on the derived sensor, not its inputs")


def run():
    """Run the dispatch-intent harness."""
    print("**** SIG dispatch intent (single point of truth) ****")
    failed = False
    for fn in (
        test_intent_sensors_match_reference,
        test_sell_clamp_is_sell_only,
        test_dispatch_never_emits_a_bare_unknown,
        test_heartbeat_defers_to_intent_sensors,
        test_triggers_fire_on_the_derived_sensor_not_its_inputs,
    ):
        try:
            fn()
        except AssertionError as e:
            print("FAIL — {}".format(e))
            failed = True
    print("test_yaml_dispatch_intent: {}".format("FAILURES" if failed else "ALL PASSED"))
    return failed


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
