# -----------------------------------------------------------------------------
# Predbat charge/discharge rate mappers — HA automation YAML test harness
#
# WHY THIS EXISTS (2026-07-29)
# ---------------------------
# These two automations write PLANT registers (ess_max_charging_limit /
# ess_max_discharging_limit) and are named in the writer-ownership table in
# .claude/CLAUDE.md -- yet they existed ONLY in Home Assistant. No repo file,
# no harness, no review. CLAUDE.md requires the opposite for exactly this
# class of automation.
#
# The cost of that gap: both templates read sensors through a bare `| float`
# with no default. A router/LAN event took the Sigen Modbus link down
# (Failed to connect to 192.168.5.145:502), the rated-power sensors went
# 'unknown', and every trigger died with
#
#     float got invalid input 'unknown' ... but no default was specified
#
# so the charge limit was never written. Nothing caught it because there was
# nothing to catch it with.
#
# The fix GUARDS rather than defaults -- a default fabricates intent. See the
# rationale block in ha/predbat_max_charging_limit_action.yaml.
#
# Run: cd apps/predbat && python3 tests/test_yaml_rate_mappers.py
# -----------------------------------------------------------------------------

import os
import sys

import jinja2
import yaml

HA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ha")

# (file, rate helper, rated-power sensor, target register)
MAPPERS = [
    (
        "predbat_max_charging_limit_action.yaml",
        "input_number.charge_rate",
        "sensor.sigen_plant_ess_rated_charging_power",
        "number.sigen_plant_ess_max_charging_limit",
    ),
    (
        "predbat_max_discharging_limit_action.yaml",
        "input_number.discharge_rate",
        "sensor.sigen_plant_ess_rated_discharging_power",
        "number.sigen_plant_ess_max_discharging_limit",
    ),
]


def load_doc(fname):
    """Load a mapper YAML document."""
    with open(os.path.join(HA_DIR, fname)) as f:
        return yaml.safe_load(f)


def _key(doc, *names):
    """HA accepts singular and plural keys (trigger/triggers); take whichever is present."""
    for n in names:
        if n in doc:
            return doc[n]
    return []


def value_template(doc, target):
    """Return the value template written to the target register."""
    for step in _key(doc, "actions", "action"):
        if isinstance(step, dict) and step.get("action") == "number.set_value":
            if step["target"]["entity_id"] != target:
                raise RuntimeError("mapper writes {} not {}".format(step["target"]["entity_id"], target))
            return step["data"]["value"]
    raise RuntimeError("no number.set_value step found")


def condition_template(doc):
    """Return the guard condition template, or None if the mapper has no guard.

    Kept for the has-a-guard-at-all check; the guard itself is native
    numeric_state, evaluated by guard_passes().
    """
    for cond in _key(doc, "conditions", "condition") or []:
        if isinstance(cond, dict) and cond.get("condition") == "template":
            return cond["value_template"]
    return None


def _numeric_state_passes(cond, states):
    """Evaluate a native numeric_state condition the way HA does.

    Key behaviour: a non-numeric state (unknown / unavailable) makes the
    condition FALSE rather than raising. That is precisely the property being
    relied on to skip the write instead of hard-failing.
    """
    raw = states.get(cond["entity_id"], "unknown")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return False
    if "above" in cond and not value > float(cond["above"]):
        return False
    if "below" in cond and not value < float(cond["below"]):
        return False
    return True


_NO_DEFAULT = object()


def _ha_float(value, default=_NO_DEFAULT):
    """Home Assistant's `float` filter, which is NOT stock Jinja's.

    Stock Jinja returns 0.0 for un-floatable input. HA raises unless an
    explicit default is supplied. That difference is the whole bug: modelling
    it with stock Jinja makes the regression test vacuous, and it also hides
    that a lenient float would write 0.0 to the register -- clamping the
    battery to no charge/discharge, which is worse than failing.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        if default is _NO_DEFAULT:
            raise ValueError("float got invalid input '{}' when rendering template but no default was specified".format(value))
        return default


def _env(states):
    """Jinja env under StrictUndefined with states()/is_number/float wired to a fake state map."""
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.globals["states"] = lambda e: states.get(e, "unknown")

    def _is_number(v):
        try:
            float(v)
            return True
        except (TypeError, ValueError):
            return False

    env.filters["is_number"] = _is_number
    env.filters["float"] = _ha_float
    return env


def render(template, states):
    """Render a template against a fake state map."""
    return _env(states).from_string(template).render()


def guard_passes(doc, states):
    """True if HA would run the actions given these states.

    Handles both native numeric_state guards (what these mappers use) and
    template guards, so the harness keeps working either way.
    """
    conditions = _key(doc, "conditions", "condition") or []
    if not conditions:
        return True
    for cond in conditions:
        if not isinstance(cond, dict):
            continue
        kind = cond.get("condition")
        if kind == "numeric_state":
            if not _numeric_state_passes(cond, states):
                return False
        elif kind == "template":
            if render(cond["value_template"], states).strip().lower() != "true":
                return False
    return True


def run_yaml_rate_mapper_tests():
    """Run the rate-mapper YAML harness."""
    print("**** Running rate mapper YAML tests ****")
    failed = False

    for fname, helper, rated, target in MAPPERS:
        try:
            doc = load_doc(fname)
            tmpl = value_template(doc, target)
        except Exception as e:
            print("  {}: FAILED to load — {}".format(fname, e))
            failed = True
            continue

        # 1. Normal case: helper below rated -> helper wins (kW, 2dp)
        st = {helper: "3300", rated: "6.6"}
        if not guard_passes(doc, st):
            print("  {}: FAILED — guard blocked a fully-readable case".format(fname))
            failed = True
        else:
            got = render(tmpl, st).strip()
            if abs(float(got) - 3.3) > 0.001:
                print("  {}: FAILED — expected 3.3 kW, got {}".format(fname, got))
                failed = True

        # 2. Clamp: helper above rated -> rated wins
        st = {helper: "9600", rated: "6.6"}
        got = render(tmpl, st).strip()
        if abs(float(got) - 6.6) > 0.001:
            print("  {}: FAILED — expected clamp to rated 6.6, got {}".format(fname, got))
            failed = True

        # 3. THE REGRESSION: rated sensor unknown (Modbus link down).
        #    The guard must block, and the template must not raise if evaluated.
        st = {helper: "3300", rated: "unknown"}
        if guard_passes(doc, st):
            print("  {}: FAILED — guard let an 'unknown' rated sensor through; " "this is the 2026-07-29 hard-fail".format(fname))
            failed = True
        try:
            render(tmpl, st)
        except Exception as e:
            print("  {}: FAILED — template raises on unknown rated sensor " "({}); needs a float() default as belt-and-braces".format(fname, e))
            failed = True

        # 4. Rate helper itself unknown -> also blocked
        st = {helper: "unknown", rated: "6.6"}
        if guard_passes(doc, st):
            print("  {}: FAILED — guard let an 'unknown' rate helper through".format(fname))
            failed = True

        # 5. Both unavailable (integration fully down)
        st = {helper: "unavailable", rated: "unavailable"}
        if guard_passes(doc, st):
            print("  {}: FAILED — guard let a fully unavailable integration through".format(fname))
            failed = True

        # 6. The mutex is enable/disable, not a condition in this file — same
        #    reasoning as test_yaml_requested_mode.check_single_writer_mutex.
        for cond in _key(doc, "conditions", "condition") or []:
            if isinstance(cond, dict) and cond.get("entity_id") == "input_select.sig_dispatch_policy":
                print("  {}: FAILED — mapper must not carry a sig_dispatch_policy " "condition; the mutex is the plugin's enable/disable".format(fname))
                failed = True

        if not failed:
            print("  {}: PASSED (normal, clamp, unknown-rated, unknown-helper, unavailable)".format(fname))

    if not failed:
        print("**** All rate mapper YAML tests PASSED ****")
    return failed


if __name__ == "__main__":
    sys.exit(1 if run_yaml_rate_mapper_tests() else 0)
