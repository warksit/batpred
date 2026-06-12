# -----------------------------------------------------------------------------
# Predbat requested-mode mapper — HA automation YAML test harness
#
# Loads ha/predbat_requested_mode_action.yaml and renders the EMS option
# template for each Predbat requested mode, asserting the EXACT SIG mode
# string. Exact-match matters: select.select_option rejects/mismatches on
# stray whitespace, and a wrong mode silently breaks grid charging
# (2026-06-11: overnight cheap-rate charge produced nothing).
#
# Run: cd apps/predbat && python3 tests/test_yaml_requested_mode.py
# -----------------------------------------------------------------------------

import os
import sys

import jinja2
import yaml

YAML_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "ha",
    "predbat_requested_mode_action.yaml",
)

EXPECTED_MAP = {
    "Demand": "Maximum Self Consumption",
    "Charging": "Command Charging (Grid First)",
    "Freeze Charging": "Maximum Self Consumption",
    "Discharging": "Command Discharging (PV First)",
    "Freeze Discharging": "Maximum Self Consumption",
}

# grid_import_limitation per requested mode (freeze modes block import)
EXPECTED_IMPORT_LIMIT = {
    "Demand": 100,
    "Charging": 100,
    "Freeze Charging": 0,
    "Discharging": 100,
    "Freeze Discharging": 0,
}


def load_doc():
    with open(YAML_PATH) as f:
        return yaml.safe_load(f)


def load_option_template(doc):
    for step in doc.get("action", []):
        if isinstance(step, dict) and step.get("action") == "select.select_option":
            return step["data"]["option"]
    raise RuntimeError("No select.select_option step found")


def render_option(template, requested_mode):
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.globals["is_state"] = lambda entity, value: (entity == "input_select.predbat_requested_mode" and value == requested_mode)
    return env.from_string(template).render()


def import_limit_for(doc, requested_mode):
    """Walk the choose block the way HA would and return the import limit set."""
    for step in doc.get("action", []):
        if not (isinstance(step, dict) and "choose" in step):
            continue
        for branch in step["choose"]:
            if _conditions_match(branch["conditions"], requested_mode):
                for act in branch["sequence"]:
                    if act.get("action") == "number.set_value":
                        return act["data_template"]["value"]
    raise RuntimeError("No choose branch matched")


def _conditions_match(conditions, requested_mode):
    for cond in conditions:
        if cond.get("condition") == "state":
            if requested_mode != cond["state"]:
                return False
        elif cond.get("condition") == "not":
            if any(_conditions_match([c], requested_mode) for c in cond["conditions"]):
                return False
    return True


def run_yaml_requested_mode_tests():
    print("**** Running requested-mode mapper YAML tests ****")
    failed = False
    try:
        doc = load_doc()
        template = load_option_template(doc)
    except Exception as e:
        print(f"  FAILED to load YAML: {e}")
        return True

    for mode, expected_ems in EXPECTED_MAP.items():
        try:
            rendered = render_option(template, mode)
        except Exception as e:
            print(f"  {mode}: FAILED — render error: {e}")
            failed = True
            continue
        if rendered != expected_ems:
            print(f"  {mode}: FAILED — EMS {rendered!r} != {expected_ems!r}")
            failed = True
            continue
        try:
            limit = import_limit_for(doc, mode)
        except Exception as e:
            print(f"  {mode}: FAILED — choose walk: {e}")
            failed = True
            continue
        if limit != EXPECTED_IMPORT_LIMIT[mode]:
            print(f"  {mode}: FAILED — import limit {limit!r} != {EXPECTED_IMPORT_LIMIT[mode]!r}")
            failed = True
            continue
        print(f"  {mode}: ok (ems={rendered!r}, import_limit={limit})")

    if not failed:
        print("**** All requested-mode mapper tests PASSED ****")
    return failed


if __name__ == "__main__":
    sys.exit(1 if run_yaml_requested_mode_tests() else 0)
