# -----------------------------------------------------------------------------
# SIG Keep Floor Guard — HA automation YAML test harness
#
# WHY THIS EXISTS (2026-07-30)
# ---------------------------
# The guard is the backstop that stops a Max Export sell eating the overnight
# reserve. It was written when input_select.sig_dispatch_policy was the only
# thing driving the heartbeat.
#
# RD13a changed that. The heartbeat now resolves:
#
#     effective = override        if override not in ('Off', unknown, ...)
#                 'Max Export'    if a saving session is live and raw != Predbat
#                 raw_policy      otherwise
#
# so with input_select.sig_override held at "Max Export" the guard was doubly
# broken:
#   1. its CONDITION tested sig_dispatch_policy, which is not what is driving,
#      so the branch never fired at all; and
#   2. its ACTION wrote sig_dispatch_policy, which the override outranks -- so
#      even had it fired, the sell would have continued.
#
# Net effect: under a manual Max Export override the keep floor did not exist
# and the battery would drain straight through it.
#
# The fix must therefore act on the EFFECTIVE policy and clear the layer that
# is actually driving. It writes BOTH: override -> "Off" and policy ->
# "Predbat". Clearing only the override hands back to a policy select that may
# itself still say Max Export; clearing only the policy leaves the override in
# charge. Both, and the sell stops whichever layer was driving.
#
# Run: cd apps/predbat && python3 tests/test_yaml_keep_floor_guard.py
# -----------------------------------------------------------------------------

import os
import sys

import yaml

YAML_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "ha",
    "sig_keep_floor_guard.yaml",
)

POLICY = "input_select.sig_dispatch_policy"
OVERRIDE = "input_select.sig_override"
SESSION = "binary_sensor.octopus_energy_a_4ba7c915_octoplus_saving_sessions"


def load_doc():
    """Load the guard YAML."""
    with open(YAML_PATH) as f:
        return yaml.safe_load(f)


def _key(doc, *names):
    """HA accepts singular and plural keys; take whichever is present."""
    for n in names:
        if n in doc:
            return doc[n]
    return []


def _cond_matches(cond, states, trigger_id):
    """Evaluate one HA condition against a fake state map."""
    kind = cond.get("condition")
    if kind == "trigger":
        ids = cond["id"]
        return trigger_id in (ids if isinstance(ids, list) else [ids])
    if kind == "state":
        want = cond["state"]
        got = states.get(cond["entity_id"], "unknown")
        return got in (want if isinstance(want, list) else [want])
    if kind == "not":
        return not any(_cond_matches(c, states, trigger_id) for c in cond["conditions"])
    if kind == "or":
        return any(_cond_matches(c, states, trigger_id) for c in cond["conditions"])
    if kind == "and":
        return all(_cond_matches(c, states, trigger_id) for c in cond["conditions"])
    raise RuntimeError("unhandled condition kind: {}".format(kind))


def writes_for(doc, states, trigger_id):
    """Return {entity_id: option} that the guard would write, walking `choose`."""
    for step in _key(doc, "actions", "action"):
        if not (isinstance(step, dict) and "choose" in step):
            continue
        for branch in step["choose"]:
            if all(_cond_matches(c, states, trigger_id) for c in branch["conditions"]):
                out = {}
                for act in branch["sequence"]:
                    if act.get("action") == "input_select.select_option":
                        out[act["target"]["entity_id"]] = act["data"]["option"]
                return out
    return {}


def _sell_stopped(writes):
    """The sell is only genuinely stopped if NEITHER layer still says Max Export."""
    return writes.get(OVERRIDE) == "Off" and writes.get(POLICY) == "Predbat"


def run_yaml_keep_floor_guard_tests():
    """Run the keep-floor-guard harness."""
    print("**** Running keep floor guard YAML tests ****")
    failed = False
    doc = load_doc()

    # --- 1. THE REGRESSION: sell driven by a manual OVERRIDE ---------------
    st = {POLICY: "Hold Battery", OVERRIDE: "Max Export", SESSION: "off"}
    w = writes_for(doc, st, "floor_hit")
    if not w:
        print("  FAILED — floor_hit did not fire while override='Max Export' " "(condition tests sig_dispatch_policy, not the effective policy)")
        failed = True
    elif not _sell_stopped(w):
        print("  FAILED — floor_hit fired but did not stop the sell: {} " "(override outranks a policy write)".format(w))
        failed = True

    # --- 2. Sell driven by the POLICY select, no override ------------------
    st = {POLICY: "Max Export", OVERRIDE: "Off", SESSION: "off"}
    w = writes_for(doc, st, "floor_hit")
    if not _sell_stopped(w):
        print("  FAILED — floor_hit must stop a policy-driven sell, got {}".format(w))
        failed = True

    # --- 3. Not selling -> guard must NOT fire -----------------------------
    for st in (
        {POLICY: "Hold Battery", OVERRIDE: "Off", SESSION: "off"},
        {POLICY: "Solar Charge Battery", OVERRIDE: "Hold Battery", SESSION: "off"},
    ):
        if writes_for(doc, st, "floor_hit"):
            print("  FAILED — floor_hit fired when not selling: {}".format(st))
            failed = True

    # --- 4. Dusk release must also clear the override ----------------------
    st = {POLICY: "Hold Battery", OVERRIDE: "Max Export", SESSION: "off"}
    w = writes_for(doc, st, "dusk")
    if not _sell_stopped(w):
        print("  FAILED — dusk must release BOTH layers, got {}".format(w))
        failed = True

    # --- 5. Dusk must NOT release during a live saving session -------------
    st = {POLICY: "Max Export", OVERRIDE: "Off", SESSION: "on"}
    if writes_for(doc, st, "dusk"):
        print("  FAILED — dusk released during a live saving session")
        failed = True

    # --- 6. Dusk when already fully released -> no-op ----------------------
    st = {POLICY: "Predbat", OVERRIDE: "Off", SESSION: "off"}
    if writes_for(doc, st, "dusk"):
        print("  FAILED — dusk fired when already released to Predbat")
        failed = True

    if not failed:
        print("  keep floor guard: PASSED (override-driven sell, policy-driven sell, " "non-sell no-op, dusk release, session hold-off, idempotent dusk)")
        print("**** All keep floor guard YAML tests PASSED ****")
    return failed


if __name__ == "__main__":
    sys.exit(1 if run_yaml_keep_floor_guard_tests() else 0)
