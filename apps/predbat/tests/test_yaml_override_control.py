#!/usr/bin/env python3
# SIG Override Control — single-select manual override harness.
#
# Manual override used to need TWO actions in the right order: set the policy
# select, then turn on the override boolean. Set the policy WITHOUT the toggle
# (or in the wrong order) and the plugin re-asserts its own policy within ~5
# minutes, silently discarding the manual choice. Observed live 2026-07-28 19:04.
#
# These tests pin the mapping and, critically, the ORDER.
#
# Run: cd apps/predbat && python3 tests/test_yaml_override_control.py
import os
import sys

import yaml

HERE = os.path.dirname(__file__)
YAML_PATH = os.path.join(HERE, "..", "ha", "sig_override_control.yaml")

OVERRIDE_SELECT = "input_select.sig_override"
OVERRIDE_BOOL = "input_boolean.sig_manual_override"
POLICY_SELECT = "input_select.sig_dispatch_policy"


def _load():
    """Parse the automation YAML."""
    with open(YAML_PATH) as f:
        return yaml.safe_load(f)


def _branch(auto, trigger_id, state=None):
    """Return the sequence for the branch matching this trigger id / select state.

    `state=None` means the UNGUARDED branch for that trigger (the take-control
    fallthrough), so it must not match a branch that is gated on a specific
    select value — otherwise the "Off" branch shadows it.
    """
    for option in auto["action"][0]["choose"]:
        conds = option["conditions"]
        ids = [c.get("id") for c in conds if c.get("condition") == "trigger"]
        states = [c.get("state") for c in conds if c.get("condition") == "state" and c.get("entity_id") == OVERRIDE_SELECT]
        if trigger_id not in ids:
            continue
        if state is None and not states:
            return option["sequence"]
        if state is not None and state in states:
            return option["sequence"]
    return None


def _actions(seq):
    """Flatten a sequence to (service, entity, option) tuples in order."""
    out = []
    for step in seq or []:
        if "action" in step:
            tgt = step.get("target", {}).get("entity_id")
            out.append((step["action"], tgt, step.get("data", {}).get("option")))
        elif "if" in step:
            for inner in step.get("then", []):
                if "action" in inner:
                    tgt = inner.get("target", {}).get("entity_id")
                    out.append((inner["action"], tgt, inner.get("data", {}).get("option")))
    return out


def test_no_predbat_option_is_offered():
    """Handing back to Predbat is the plugin's decision (RD6 safe_time handback),
    not a manual mode. "Off" means "you decide" — offering Predbat here would let
    a human park the machine somewhere the plugin then has to fight."""
    src = open(YAML_PATH).read()
    body = src.split("action:", 1)[1]
    assert "Predbat" not in body, "the override select must not offer a Predbat option"
    print("PASS  no Predbat option in the override mapping")


def test_override_boolean_is_set_before_the_policy():
    """ORDER IS LOAD-BEARING. Writing the policy first leaves a window where the
    plugin's next cycle re-asserts its own choice (`_set_policy` compares against
    the LIVE select), silently discarding the manual one. Boolean first closes it.
    """
    seq = _branch(_load(), "override_select")
    acts = _actions(seq)
    assert acts, "the take-control branch must do something"
    services = [a[0] for a in acts]
    assert "input_boolean.turn_on" in services, f"must enable the override: {acts}"
    assert "input_select.select_option" in services, f"must write the policy: {acts}"
    assert services.index("input_boolean.turn_on") < services.index("input_select.select_option"), f"override must be enabled BEFORE the policy is written, got {services}"
    print("PASS  order: override boolean ON before the policy is written")


def test_off_releases_without_setting_a_policy():
    """Off means "plugin decides". It must clear the override and NOT write a
    policy — the plugin picks one on its next cycle. Writing one here would
    pin a stale choice for up to 5 minutes."""
    seq = _branch(_load(), "override_select", state="Off")
    acts = _actions(seq)
    assert ("input_boolean.turn_off", OVERRIDE_BOOL, None) in acts, f"Off must clear the override: {acts}"
    assert not any(a[1] == POLICY_SELECT for a in acts), f"Off must NOT write a policy: {acts}"
    print("PASS  Off: clears override, writes no policy")


def test_boolean_cleared_elsewhere_resets_the_select():
    """If the override boolean is cleared by a script or by hand, the select must
    follow — otherwise it keeps reading e.g. "Max Export" while the plugin is
    actually back in control, which is exactly the kind of lie that cost hours
    on 2026-07-22."""
    seq = _branch(_load(), "boolean_cleared")
    acts = _actions(seq)
    assert ("input_select.select_option", OVERRIDE_SELECT, "Off") in acts, f"must reset the select to Off: {acts}"
    print("PASS  boolean cleared elsewhere -> select resets to Off")


def test_reset_is_guarded_against_a_loop():
    """The reset branch must not write when the select is already Off, or the
    two triggers can ping-pong."""
    seq = _branch(_load(), "boolean_cleared")
    assert any("if" in step for step in seq), "the reset must be guarded by an if, not written unconditionally"
    print("PASS  reset guarded (no select<->boolean ping-pong)")


def test_mode_allows_queued_runs():
    """Two triggers can fire close together (select change -> boolean change).
    `single` would drop the second run with a warning."""
    auto = _load()
    assert auto.get("mode") in ("queued", "parallel"), f"mode must not drop concurrent runs, got {auto.get('mode')}"
    print(f"PASS  mode: {auto.get('mode')}")


def main():
    """Run every override-control assertion."""
    for t in (
        test_no_predbat_option_is_offered,
        test_override_boolean_is_set_before_the_policy,
        test_off_releases_without_setting_a_policy,
        test_boolean_cleared_elsewhere_resets_the_select,
        test_reset_is_guarded_against_a_loop,
        test_mode_allows_queued_runs,
    ):
        t()
    print("test_yaml_override_control: ALL PASSED")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"FAIL — {e}")
        sys.exit(1)
