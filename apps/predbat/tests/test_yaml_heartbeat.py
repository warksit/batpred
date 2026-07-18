#!/usr/bin/env python3
# SIG Dispatch Heartbeat — structural regression guard.
#
# The heartbeat is the sole SIG register writer. RD2: on handback (policy
# Predbat) it MUST stay in EMS control — Remote EMS ON, EMS control mode =
# Maximum Self Consumption — and MUST NEVER turn Remote EMS off / drop to app
# modes (you cannot switch app<->EMS control and back). This test asserts that
# rule directly so it can't silently regress.
#
# Run: cd apps/predbat && python3 tests/test_yaml_heartbeat.py
import os
import sys

import yaml

HERE = os.path.dirname(__file__)
YAML_PATH = os.path.join(HERE, "..", "ha", "sig_dispatch_heartbeat.yaml")

REMOTE_EMS_SWITCH = "switch.sigen_plant_remote_ems_controlled_by_home_assistant"


def _iter_actions(node):
    """Yield every service-call dict anywhere in an action tree."""
    if isinstance(node, dict):
        if "action" in node or "service" in node:
            yield node
        for v in node.values():
            yield from _iter_actions(v)
    elif isinstance(node, list):
        for v in node:
            yield from _iter_actions(v)


def main():
    with open(YAML_PATH) as f:
        auto = yaml.safe_load(f)

    choose = auto["action"][1]["choose"]

    # Find the handback (Predbat) branch.
    predbat_branch = None
    for b in choose:
        conds = " ".join(str(c) for c in b["conditions"])
        if "Predbat" in conds:
            predbat_branch = b
            break
    assert predbat_branch is not None, "no Predbat handback branch found"

    acts = list(_iter_actions(predbat_branch["sequence"]))
    services = [a.get("action") or a.get("service") for a in acts]

    # 1. Handback sets EMS control mode = Maximum Self Consumption.
    sets_msc = any(
        (a.get("action") or a.get("service")) == "select.select_option"
        and a.get("data", {}).get("option") == "Maximum Self Consumption"
        for a in acts
    )
    assert sets_msc, f"handback must select EMS-MSC, got services {services}"

    # 2. Handback must keep Remote EMS ON — turn it on if off, and select the mode.
    turns_on = any(
        (a.get("action") or a.get("service")) == "switch.turn_on"
        and REMOTE_EMS_SWITCH in str(a.get("target", {}))
        for a in acts
    )
    assert turns_on, "handback must ensure Remote EMS is ON (switch.turn_on)"

    # 3. THE rule: the heartbeat must NEVER turn Remote EMS off anywhere
    #    (no reverting to app modes).
    for a in _iter_actions(auto["action"]):
        svc = a.get("action") or a.get("service")
        if svc == "switch.turn_off" and REMOTE_EMS_SWITCH in str(a.get("target", {})):
            raise AssertionError("heartbeat must NEVER turn Remote EMS off (RD2: stay in EMS control, no app modes)")

    print("PASS  handback stays in EMS-MSC (Remote EMS on, mode Maximum Self Consumption)")
    print("PASS  heartbeat never turns Remote EMS off (no app-mode revert)")
    print("test_yaml_heartbeat: ALL PASSED")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"FAIL — {e}")
        sys.exit(1)
