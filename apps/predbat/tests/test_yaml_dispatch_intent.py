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

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from curtailment_calc import session_sell_floor_kwh  # noqa: E402

HERE = os.path.dirname(__file__)
HELPERS_PATH = os.path.join(HERE, "..", "ha", "sig_dispatch_intent_helpers.yaml")
HEARTBEAT_PATH = os.path.join(HERE, "..", "ha", "sig_dispatch_heartbeat.yaml")
# What a live session MEANS comes from the discrimination sensor, not the
# Octoplus calendar — the calendar is on for Power Ups (free import) as well as
# Power Downs (paid), so keying the Max Export forcing on it exports through a
# free-import hour. See ha/octoplus_session_helpers.yaml and OCTOPUS_SESSIONS.md.
SESSION_SENSOR = "binary_sensor.octoplus_power_down_active"
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


def reference(override, select, session, pv, load_raw, soc, cap_w=3680, hard=2.8, reserve=38.0):
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
    # RD44: during a paid session the sell stops at the overnight reserve, not the
    # deep-discharge floor. max() so the deep floor still binds if it is higher —
    # the clamp can only ever sell LESS.
    floor = max(hard, reserve) if session else hard
    pre = min(raw, pv) if (soc <= floor and p == "Max Export") else raw
    return p, round(max(min(pre, ceil), 0), 2)


def _matrix():
    for select in ("Predbat", "Max Export", "Hold Battery", "Solar Charge Battery"):
        for override in ("Off", "Hold Battery", "Max Export", "Solar Charge Battery", "Predbat"):
            for session in (False, True):
                # The last three sit in the band between the deep floor (2.8) and
                # the reserve (38) — where a session clamps and an ordinary drain
                # does not. Without them this matrix cannot see RD44 at all.
                for pv, load, soc in ((0.1, 1.5, 1.3), (1.329, 0.364, 1.7), (8.0, 0.5, 50), (0.0, 0.4, 90), (3.0, 3.0, 2.8), (3.19, 0.48, 1.8), (1.2, 0.5, 30.0), (0.8, 0.6, 38.0), (2.0, 0.7, 38.1)):
                    yield override, select, session, pv, load, soc


def _states(override, select, session, pv, load, soc, hard=2.8, reserve=38.0):
    return {
        "input_select.sig_override": override,
        "input_select.sig_dispatch_policy": select,
        SESSION_SENSOR: "on" if session else "off",
        "sensor.sigen_plant_pv_power": str(pv),
        "sensor.sigen_plant_total_load_power": str(load),
        "sensor.sigen_plant_battery_state_of_charge": str(soc),
        "input_number.dno_export_limit_w": "3680",
        "input_number.sig_drain_floor_pct": str(hard),
        "input_number.sig_keep_floor_pct": str(reserve),
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


def test_session_sell_stops_at_the_overnight_reserve():
    """RD44: a paid session must not sell the night.

    Andrew, 2026-08-18: "It should stop at overnight reserve." The plugin already
    PUBLISHED that — `sig_keep_floor_pct` carries `overnight_target_kwh` for a
    session dump, and the plugin says "dump, which still stops at the overnight
    reserve (RD20)" — but the keep-floor guard deliberately stands off a live
    session, so the only bound left was the 1.0% deep-discharge floor.

    Live 2026-08-17: the 18:00 dump ran to 30.8% against a 38% reserve and stopped
    only because the window closed at 19:00.
    """
    st = _states("Off", "Hold Battery", True, pv=1.2, load=0.5, soc=30.0)
    policy, kw = render_sensors(st)
    assert policy == "Max Export", "the session must still force Max Export, got {}".format(policy)
    assert abs(kw - 1.2) < 0.011, "below the 38% reserve the session must dispatch PV only (1.2), got {}".format(kw)
    print("PASS  session sell stops at the overnight reserve")


def test_session_sell_runs_while_above_the_reserve():
    """...and is untouched above it — this must not cut a session short."""
    st = _states("Off", "Hold Battery", True, pv=1.2, load=0.5, soc=45.0)
    policy, kw = render_sensors(st)
    assert policy == "Max Export", "got {}".format(policy)
    assert abs(kw - 6.6) < 0.011, "above the reserve the session must sell at full tilt (6.6), got {}".format(kw)
    print("PASS  session sell runs at full tilt above the reserve")


def test_ordinary_drain_still_uses_the_deep_floor():
    """The reserve floor is SESSION-ONLY. A curtailment drain must still be able to
    run to the deep floor — that is R25 headroom, and clamping it at 38% would
    delete the drain mechanism in all but name."""
    st = _states("Off", "Max Export", False, pv=1.2, load=0.5, soc=30.0)
    policy, kw = render_sensors(st)
    assert policy == "Max Export", "got {}".format(policy)
    assert abs(kw - 6.6) < 0.011, "no session: SOC 30 is far above the 2.8 deep floor and must sell at 6.6, got {}".format(kw)
    print("PASS  an ordinary curtailment drain still runs to the deep floor")


def test_session_floor_never_sells_below_the_deep_floor():
    """max(), not a swap: if the reserve is ever LOWER than the deep floor, the deep
    floor still binds. The clamp may only ever sell less."""
    st = _states("Off", "Hold Battery", True, pv=0.9, load=0.5, soc=2.0, hard=2.8, reserve=1.0)
    _policy, kw = render_sensors(st)
    assert abs(kw - 0.9) < 0.011, "deep floor must still bind when the reserve is lower, got {}".format(kw)
    print("PASS  the deep-discharge floor still binds under the session floor")


def test_session_floor_fails_safe_when_the_reserve_is_unreadable():
    """An unreadable reserve helper must stop the sell EARLY, not sell the night.

    Same direction as RD23's choice for `hard`: the default (38) is the plugin's
    DEFAULT_KEEP_FLOOR_PCT, so a missing helper behaves like a normal reserve.
    """
    st = _states("Off", "Hold Battery", True, pv=1.1, load=0.5, soc=20.0)
    del st["input_number.sig_keep_floor_pct"]
    _policy, kw = render_sensors(st)
    assert abs(kw - 1.1) < 0.011, "unreadable reserve must fall back to 38 and clamp to PV, got {}".format(kw)
    print("PASS  an unreadable reserve fails safe (stops the sell)")


def test_jinja_session_floor_matches_the_python_definition():
    """RD44 floor lives in TWO languages, so probe the REAL clamp point.

    The clamp is Jinja (it sets the dispatch setpoint); the end-SOC projection is
    Python (it feeds the card). The Charter's rule for a quantity that must live
    in two places is a test that renders every copy. If they drift, the card
    promises a stop the dispatcher will not make — the exact failure RD44 exists
    to remove.

    Written the second time. The first version rebuilt the floor expression inline
    and compared THAT to Python — a third copy, which passed happily with the real
    template drifted from max() to min(). This renders the deployed
    `sig_dispatch_kw` either side of the Python floor and asserts the clamp turns
    on exactly there, so it cannot pass without touching the real subject.
    """
    soc_max = 18.08
    for hard, reserve in ((1.0, 38.0), (2.8, 38.0), (45.0, 38.0), (5.0, 5.0), (0.0, 95.0)):
        floor_pct = session_sell_floor_kwh(hard, reserve, soc_max) / soc_max * 100.0
        below = _states("Off", "Hold Battery", True, pv=1.2, load=0.5, soc=max(0.0, floor_pct - 0.1), hard=hard, reserve=reserve)
        above = _states("Off", "Hold Battery", True, pv=1.2, load=0.5, soc=min(100.0, floor_pct + 0.1), hard=hard, reserve=reserve)
        _p, kw_below = render_sensors(below)
        _p, kw_above = render_sensors(above)
        assert abs(kw_below - 1.2) < 0.011, "hard={} reserve={}: just BELOW the python floor ({:.2f}%) the dispatcher must clamp to PV, got {}".format(hard, reserve, floor_pct, kw_below)
        if floor_pct < 99.9:
            assert abs(kw_above - 6.6) < 0.011, "hard={} reserve={}: just ABOVE the python floor ({:.2f}%) the dispatcher must sell, got {}".format(hard, reserve, floor_pct, kw_above)
    print("PASS  the deployed clamp turns on exactly at the Python floor")


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
        test_session_sell_stops_at_the_overnight_reserve,
        test_session_sell_runs_while_above_the_reserve,
        test_ordinary_drain_still_uses_the_deep_floor,
        test_session_floor_never_sells_below_the_deep_floor,
        test_session_floor_fails_safe_when_the_reserve_is_unreadable,
        test_jinja_session_floor_matches_the_python_definition,
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
