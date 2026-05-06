# -----------------------------------------------------------------------------
# Predbat voltage seek controller — HA automation YAML test harness
#
# Loads ha/voltage_seek_controller.yaml at runtime, extracts the
# action[].variables block, and renders each Jinja template against a
# fixture matrix using a mocked HA context (states/state_attr).
#
# Why this exists: on 2026-05-06 a python_transform-based edit appended
# `ramp_zero_v` and `ramp_full_v` AFTER the `new_cap` template that referenced
# them. HA evaluates variables top-down, so `new_cap` saw them as Undefined,
# every branch silently fell to `else { cap }`, and the seek controller froze
# the cap for ~2 hours during peak PV — contributing to a SIG OVP trip.
# StrictUndefined here will raise on any reference-before-definition, catching
# this exact class of bug pre-deploy.
#
# Run: cd apps/predbat && python3 tests/test_yaml_voltage_seek.py
# -----------------------------------------------------------------------------

import os
import sys
from dataclasses import dataclass

import jinja2
import yaml

YAML_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "ha",
    "voltage_seek_controller.yaml",
)
DNO = 4.0


# ---------- Mock HA layer ------------------------------------------------------


class HAState:
    def __init__(self, states):
        self._states = states

    def states(self, entity_id):
        v = self._states.get(entity_id)
        if v is None:
            return "unknown"
        return v if isinstance(v, str) else str(v)


# ---------- YAML loader --------------------------------------------------------


def load_variables_from_yaml(path):
    with open(path) as f:
        doc = yaml.safe_load(f)
    for step in doc.get("action", []):
        if isinstance(step, dict) and "variables" in step:
            return step["variables"]
    raise RuntimeError(f"No 'variables' block found in {path}")


def load_set_value_template(path):
    """The action that writes to filtered_cap — usually `{{ new_cap | float | round(2) }}`."""
    with open(path) as f:
        doc = yaml.safe_load(f)
    for step in doc.get("action", []):
        if isinstance(step, dict) and step.get("action") == "input_number.set_value":
            return step["data"]["value"]
    raise RuntimeError(f"No input_number.set_value action found in {path}")


# ---------- Variable evaluator -------------------------------------------------


def _coerce(s):
    if not isinstance(s, str):
        return s
    s = s.strip()
    try:
        return float(s)
    except ValueError:
        return s


def _new_env(ha):
    env = jinja2.Environment(
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.globals["states"] = ha.states
    return env


def evaluate(variables, set_value_tpl, states):
    """Render variables in declaration order, then the set_value template.

    Returns the final cap value the automation would write.
    """
    ha = HAState(states)
    env = _new_env(ha)
    ctx = {}
    for name, raw in variables.items():
        if isinstance(raw, str) and ("{{" in raw or "{%" in raw):
            rendered = env.from_string(raw).render(**ctx)
            ctx[name] = _coerce(rendered)
        else:
            ctx[name] = raw
    final = env.from_string(set_value_tpl).render(**ctx)
    return float(final)


# ---------- Fixture builder ----------------------------------------------------


def build_states(v, cap, target=252.0, deadband=1.0):
    return {
        "sensor.sigen_inverter_phase_a_voltage": str(v),
        "input_number.voltage_throttle_filtered_cap": str(cap),
        "input_number.voltage_seek_target_v": str(target),
        "input_number.voltage_seek_deadband_v": str(deadband),
    }


# ---------- Scenarios ----------------------------------------------------------


@dataclass
class Scenario:
    name: str
    v: float
    cap: float
    expected: float
    target: float = 252.0
    deadband: float = 1.0
    tol: float = 0.01


SCENARIOS = [
    # Hold band (target=252, deadband=1) — hold band = (251, 252]
    Scenario("hold band centre — cap unchanged", v=251.5, cap=3.5, expected=3.5),
    Scenario("hold band upper edge (V=target) — cap unchanged", v=252.0, cap=4.0, expected=4.0),
    Scenario("hold band lower edge (target-deadband+ε) — cap unchanged", v=251.5, cap=2.0, expected=2.0),
    # Down ramp tracks target: cap_max = max(0, dno - (V - target))
    Scenario("V=target+0.5 → cap_max=3.5, ratchet from 4", v=252.5, cap=4.0, expected=3.5),
    Scenario("V=target+1 → cap_max=3", v=253.0, cap=4.0, expected=3.0),
    Scenario("V=target+1 → cap stays if already < cap_max", v=253.0, cap=2.0, expected=2.0),
    Scenario("V=target+2 → cap_max=2", v=254.0, cap=4.0, expected=2.0),
    Scenario("V=target+3 → cap_max=1", v=255.0, cap=4.0, expected=1.0),
    Scenario("V=target+4 → cap=0 (ramp full)", v=256.0, cap=4.0, expected=0.0),
    Scenario("V=target+5 → cap=0 (above ramp width)", v=257.0, cap=4.0, expected=0.0),
    Scenario("V=263 (real spike) → cap=0", v=263.0, cap=3.5, expected=0.0),
    # Climb (V < target-deadband=251)
    Scenario("V=250 climb base step from 0", v=250.0, cap=0.0, expected=0.05),
    Scenario("V=247 climb adaptive (under_v=4)", v=247.0, cap=0.0, expected=0.20),
    Scenario("V=246 climb hits max", v=246.0, cap=0.0, expected=0.25),
    Scenario("V=240 climb clamped to step_up_max=0.4", v=240.0, cap=0.0, expected=0.4),
    Scenario("climb saturates at DNO", v=240.0, cap=4.0, expected=4.0),
    # Adjustable target — ramp NOW tracks target (V=target → cap=4, V=target+4 → 0)
    Scenario("target=255 V=251 climb (under_v=3 → step=0.15)", v=251.0, cap=3.0, expected=3.15, target=255.0),
    Scenario("target=255 V=255 (=target) hold → cap unchanged", v=255.0, cap=3.0, expected=3.0, target=255.0),
    Scenario("target=255 V=256 (=target+1) → cap_max=3", v=256.0, cap=4.0, expected=3.0, target=255.0),
    Scenario("target=255 V=259 (=target+4) → cap=0", v=259.0, cap=4.0, expected=0.0, target=255.0),
    Scenario("target=250 V=251 (=target+1) → cap_max=3", v=251.0, cap=4.0, expected=3.0, target=250.0),
    Scenario("target=250 V=254 (=target+4) → cap=0", v=254.0, cap=4.0, expected=0.0, target=250.0),
]


# ---------- Regression test: bug-pattern detection -----------------------------


def test_bug_pattern_caught():
    """Move ramp_zero_v / ramp_full_v after new_cap and confirm StrictUndefined raises.

    This is the exact bug shape from 2026-05-06: a python_transform appended
    those constants at the end of the variables dict, leaving `new_cap` to
    reference Undefined values. Without StrictUndefined the controller silently
    fell through every branch to `else { cap }` and froze the cap for ~2 hours.
    """
    variables = load_variables_from_yaml(YAML_PATH)
    set_value_tpl = load_set_value_template(YAML_PATH)
    states = build_states(v=256.0, cap=3.16)

    # Reorder: move ramp_width_v to AFTER new_cap.
    broken = {}
    moved = {}
    for k, v in variables.items():
        if k == "ramp_width_v":
            moved[k] = v
        else:
            broken[k] = v
    broken.update(moved)

    try:
        evaluate(broken, set_value_tpl, states)
    except jinja2.UndefinedError:
        return True  # expected — harness caught the bug
    raise AssertionError("Bug pattern NOT caught — out-of-order variables should raise UndefinedError under StrictUndefined")


# ---------- Test runner --------------------------------------------------------


def main():
    variables = load_variables_from_yaml(YAML_PATH)
    set_value_tpl = load_set_value_template(YAML_PATH)

    # Bug-pattern regression test runs first (fast, separate from scenarios).
    try:
        test_bug_pattern_caught()
        print("PASS  bug-pattern regression: out-of-order vars raise UndefinedError")
    except AssertionError as e:
        print(f"FAIL  bug-pattern regression: {e}")
        sys.exit(1)

    failed = 0
    for s in SCENARIOS:
        states = build_states(s.v, s.cap, s.target, s.deadband)
        try:
            actual = evaluate(variables, set_value_tpl, states)
        except jinja2.UndefinedError as e:
            print(f"FAIL  {s.name}: UndefinedError {e}")
            failed += 1
            continue
        except Exception as e:
            print(f"FAIL  {s.name}: {type(e).__name__}: {e}")
            failed += 1
            continue
        ok = abs(actual - s.expected) <= s.tol
        marker = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"{marker}  {s.name}: V={s.v}, cap={s.cap}, target={s.target} → got {actual} expected {s.expected}")

    print()
    if failed:
        print(f"{failed}/{len(SCENARIOS)} scenarios failed")
        sys.exit(1)
    print(f"All {len(SCENARIOS)} scenarios passed")


if __name__ == "__main__":
    main()
