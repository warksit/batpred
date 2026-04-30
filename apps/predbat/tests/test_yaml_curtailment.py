# -----------------------------------------------------------------------------
# Predbat curtailment — HA automation YAML test harness
#
# Loads ha/curtailment_manager_dynamic_export_limit.yaml at runtime, extracts
# the action[].variables block, and renders each Jinja template against a
# fixture matrix using a mocked HA context (states/state_attr/today_at/now).
#
# Catches Jinja syntax errors, undefined variables, and basic logic regressions
# that would otherwise only surface after deploy. Tests describe SCENARIOS
# (SOC, target, excess, hours, manual mode), not entity layouts — so YAML
# refactors that don't change scenario outcomes don't break the tests.
#
# Run: cd apps/predbat && python3 tests/test_yaml_curtailment.py
# -----------------------------------------------------------------------------

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta

import jinja2
import yaml

YAML_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "ha",
    "curtailment_manager_dynamic_export_limit.yaml",
)
BATTERY_KWH = 18.08
DNO = 4.0


# ---------- Mock HA layer ------------------------------------------------------


class HAState:
    """Minimal stand-in for HA's template context. Bound as Jinja globals."""

    def __init__(self, fixture):
        self._states = fixture["states"]
        self._attrs = fixture["attrs"]
        self._now = fixture["now"]

    def states(self, entity_id):
        v = self._states.get(entity_id)
        if v is None:
            return "unknown"
        return v if isinstance(v, str) else str(v)

    def state_attr(self, entity_id, attr):
        return self._attrs.get((entity_id, attr))

    def today_at(self, time_str):
        h, m = (int(x) for x in time_str.split(":"))
        return self._now.replace(hour=h, minute=m, second=0, microsecond=0)

    def now(self):
        return self._now


# ---------- YAML loader --------------------------------------------------------


def load_variables_from_yaml(path):
    """Return the variables dict from action[].variables.

    Walks the action list rather than indexing — robust to action reordering.
    """
    with open(path) as f:
        doc = yaml.safe_load(f)
    for step in doc.get("action", []):
        if isinstance(step, dict) and "variables" in step:
            return step["variables"]
    raise RuntimeError(f"No 'variables' block found in {path}")


# ---------- Variable evaluator -------------------------------------------------


def _coerce(s):
    """Numeric strings → float; everything else → stripped string."""
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
    env.globals["state_attr"] = ha.state_attr
    env.globals["today_at"] = ha.today_at
    env.globals["now"] = ha.now
    return env


def evaluate_variables(variables, fixture):
    """Render each variable in declaration order against a mocked HA context."""
    ha = HAState(fixture)
    env = _new_env(ha)
    ctx = {}
    for name, raw in variables.items():
        if isinstance(raw, str) and ("{{" in raw or "{%" in raw):
            try:
                rendered = env.from_string(raw).render(**ctx)
            except Exception as e:
                raise RuntimeError(f"render {name!r}: {e}\n  template: {raw!r}\n  ctx: {ctx}") from e
            ctx[name] = _coerce(rendered)
        else:
            ctx[name] = raw
    return ctx


# ---------- Fixture builder ----------------------------------------------------


def build_fixture(
    soc_pct,
    target_pct=None,
    excess=0.0,
    hours_until_safe=3.5,
    manual="Off",
    current_phase="Off",
    voltage_cap=4.0,
    export_cap_raw=None,
    plugin_phase="Active",
    now=None,
):
    """Build an HA-mock fixture from scenario-shaped parameters."""
    now = now or datetime(2026, 4, 30, 13, 0, 0)

    # Synthesise pv/load to give the requested excess (load = 0.5 kW base).
    pv = max(excess, 0.0) + 0.5
    load = pv - excess

    if target_pct is None:
        target_kwh = soc_pct / 100.0 * BATTERY_KWH
    else:
        target_kwh = target_pct / 100.0 * BATTERY_KWH

    if hours_until_safe is None:
        safe_time_str = "none"
    else:
        safe_time_str = (now + timedelta(hours=hours_until_safe)).strftime("%H:%M")

    if export_cap_raw is None:
        export_cap_raw = -1.0 if plugin_phase == "Off" else 2.0

    return {
        "now": now,
        "states": {
            "sensor.sigen_plant_battery_state_of_charge": str(soc_pct),
            "sensor.sigen_plant_pv_power": f"{pv:.3f}",
            "sensor.sigen_plant_consumed_power": f"{load:.3f}",
            "sensor.predbat_curtailment_export_target": str(export_cap_raw),
            "input_number.voltage_throttle_filtered_cap": str(voltage_cap),
            "input_select.curtailment_manual_hold": manual,
            "input_text.curtailment_live_phase": current_phase,
            "sensor.predbat_curtailment_phase": plugin_phase,
            "number.sigen_plant_grid_export_limitation": "4",
        },
        "attrs": {
            ("sensor.predbat_curtailment_target_soc", "target_kwh"): target_kwh,
            ("sensor.predbat_curtailment_phase", "safe_time"): safe_time_str,
        },
    }


# ---------- Scenarios ----------------------------------------------------------


@dataclass
class Scenario:
    name: str
    fixture: dict
    expected_phase: str
    expected_new_limit: float
    tol: float = 0.05


def _scenarios():
    return [
        Scenario(
            "1 Idle (plugin Off)",
            build_fixture(soc_pct=50, plugin_phase="Off", export_cap_raw=-1.0),
            expected_phase="Off",
            expected_new_limit=4.0,
        ),
        Scenario(
            "2 Hold near target",
            build_fixture(soc_pct=60, target_pct=60, excess=2.0, current_phase="Hold", export_cap_raw=2.0),
            expected_phase="Hold",
            expected_new_limit=2.0,
        ),
        Scenario(
            "3 Drain SOC > target+OUTER",
            # plugin sets export_cap_raw=DNO when it wants drain
            build_fixture(soc_pct=80, target_pct=60, excess=5.0, current_phase="Drain", export_cap_raw=4.0),
            expected_phase="Drain",
            expected_new_limit=4.0,
        ),
        Scenario(
            "4 Soft-charge low PV (formula path)",
            build_fixture(soc_pct=60, target_pct=70, excess=2.0, current_phase="Charge", export_cap_raw=2.0),
            expected_phase="Charge",
            # gap=1.808, rate=1.808/3.5=0.52, 2-0.52=1.48 → round(1)=1.5
            expected_new_limit=1.5,
        ),
        Scenario(
            "5 Soft-charge high PV (DNO clamp)",
            build_fixture(soc_pct=60, target_pct=70, excess=5.5, current_phase="Charge", export_cap_raw=2.0),
            expected_phase="Charge",
            # 5.5-0.52=4.98 → DNO clamp at 4.0
            expected_new_limit=4.0,
        ),
        Scenario(
            "6 Active overflow (DNO clamp during Charge)",
            build_fixture(soc_pct=70, target_pct=75, excess=8.0, hours_until_safe=2.0, current_phase="Charge", export_cap_raw=2.0),
            expected_phase="Charge",
            expected_new_limit=4.0,
        ),
        Scenario(
            "7 Late-day soft-charge (formula path, < DNO)",
            # excess=3.82 chosen to land below DNO so the formula path is exercised.
            build_fixture(soc_pct=85, target_pct=87, excess=3.82, hours_until_safe=2.0, current_phase="Charge", export_cap_raw=2.0),
            expected_phase="Charge",
            # gap=0.3616, rate=0.18, 3.82-0.18=3.64 → round(1)=3.6
            expected_new_limit=3.6,
        ),
        Scenario(
            "8 Manual Charge",
            build_fixture(soc_pct=50, excess=5.0, manual="Charge", export_cap_raw=2.0),
            expected_phase="Manual Charge",
            expected_new_limit=0.0,
        ),
        Scenario(
            "9 Manual Drain",
            build_fixture(soc_pct=50, manual="Drain", export_cap_raw=2.0),
            expected_phase="Manual Drain",
            expected_new_limit=4.0,
        ),
        Scenario(
            "10 Voltage cap engages (Hold)",
            build_fixture(soc_pct=60, target_pct=60, excess=5.0, voltage_cap=2.0, current_phase="Hold", export_cap_raw=4.0),
            expected_phase="Hold",
            expected_new_limit=2.0,
        ),
        Scenario(
            "11 hours_remaining ≤ 0 (legacy fallback)",
            build_fixture(soc_pct=60, target_pct=70, excess=5.0, hours_until_safe=-0.5, current_phase="Charge", export_cap_raw=2.0),
            expected_phase="Charge",
            expected_new_limit=0.0,
        ),
        Scenario(
            "12 safe_time='none' (pre-PV bootstrap → Charge)",
            build_fixture(soc_pct=60, target_pct=70, excess=0.0, hours_until_safe=None, current_phase="Off", export_cap_raw=2.0),
            expected_phase="Charge",
            expected_new_limit=0.0,
        ),
    ]


# ---------- Runner -------------------------------------------------------------


def run_yaml_curtailment_tests():
    print("**** Running YAML curtailment automation tests ****")
    try:
        variables = load_variables_from_yaml(YAML_PATH)
    except Exception as e:
        print(f"  FAILED to load YAML: {e}")
        return True

    failed = False
    for s in _scenarios():
        try:
            ctx = evaluate_variables(variables, s.fixture)
        except RuntimeError as e:
            print(f"  {s.name}: FAILED — render error\n    {e}")
            failed = True
            continue

        actual_phase = ctx.get("phase")
        actual_limit = ctx.get("new_limit")

        if not isinstance(actual_phase, str):
            print(f"  {s.name}: FAILED — 'phase' not a string: {actual_phase!r}")
            failed = True
            continue
        actual_phase = actual_phase.strip()

        if actual_phase != s.expected_phase:
            print(f"  {s.name}: FAILED — phase {actual_phase!r} != {s.expected_phase!r}")
            failed = True
            continue

        try:
            actual_limit_f = float(actual_limit)
        except (TypeError, ValueError):
            print(f"  {s.name}: FAILED — new_limit not numeric: {actual_limit!r}")
            failed = True
            continue

        if abs(actual_limit_f - s.expected_new_limit) > s.tol:
            print(f"  {s.name}: FAILED — new_limit {actual_limit_f} != " f"{s.expected_new_limit} (tol {s.tol})")
            failed = True
            continue

        print(f"  {s.name}: ok (phase={actual_phase}, new_limit={actual_limit_f:.2f})")

    if not failed:
        print("**** All YAML curtailment tests PASSED ****")
    return failed


if __name__ == "__main__":
    sys.exit(1 if run_yaml_curtailment_tests() else 0)
