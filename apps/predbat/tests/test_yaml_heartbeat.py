#!/usr/bin/env python3
# SIG Dispatch Heartbeat — structural + dispatch-formula regression guard.
#
# Structural (RD2): handback stays in EMS control (Remote EMS ON + Maximum Self
# Consumption) and the heartbeat NEVER turns Remote EMS off (no app modes).
#
# Dispatch formula: renders the action `variables` (in order, like HA) with mock
# states and asserts the ceiling (export-limited, capped at the 6.6 kW inverter
# rating), PV-tracking on a dip, the 6.6 (not 6.0) high-load case, and the
# hard-floor clamp. Plus the live trigger (>0.1 kW / 2 s).
#
# Run: cd apps/predbat && python3 tests/test_yaml_heartbeat.py
import os
import sys

import jinja2
import yaml

HERE = os.path.dirname(__file__)
YAML_PATH = os.path.join(HERE, "..", "ha", "sig_dispatch_heartbeat.yaml")
REMOTE_EMS_SWITCH = "switch.sigen_plant_remote_ems_controlled_by_home_assistant"


def _load():
    with open(YAML_PATH) as f:
        return yaml.safe_load(f)


def _iter_actions(node):
    if isinstance(node, dict):
        if "action" in node or "service" in node:
            yield node
        for v in node.values():
            yield from _iter_actions(v)
    elif isinstance(node, list):
        for v in node:
            yield from _iter_actions(v)


def _render_dispatch(auto, states):
    """Render the action `variables` block in order with a mock states()."""
    variables = auto["action"][0]["variables"]
    env = jinja2.Environment()

    def states_fn(entity):
        return states.get(entity, "unknown")

    ctx = {"states": states_fn, "is_state": lambda e, v: states.get(e) == v}
    for key, tmpl in variables.items():
        rendered = env.from_string(tmpl).render(**ctx).strip()
        try:
            ctx[key] = float(rendered)
        except ValueError:
            ctx[key] = rendered
    return ctx["dispatch_kw"]


def _mock(policy, pv, load, soc, cap_w=3680, hard=12):
    return {
        "input_select.sig_dispatch_policy": policy,
        "sensor.sigen_plant_pv_power": str(pv),
        "sensor.sigen_plant_total_load_power": str(load),
        "sensor.sigen_plant_battery_state_of_charge": str(soc),
        "input_number.dno_export_limit_w": str(cap_w),
        "input_number.sig_hard_floor_pct": str(hard),
    }


def test_structural():
    auto = _load()
    choose = auto["action"][1]["choose"]
    predbat = next((b for b in choose if "Predbat" in " ".join(str(c) for c in b["conditions"])), None)
    assert predbat is not None, "no Predbat handback branch"
    acts = list(_iter_actions(predbat["sequence"]))
    assert any((a.get("action") or a.get("service")) == "select.select_option" and a.get("data", {}).get("option") == "Maximum Self Consumption" for a in acts), "handback must select EMS-MSC"
    for a in _iter_actions(auto["action"]):
        if (a.get("action") or a.get("service")) == "switch.turn_off" and REMOTE_EMS_SWITCH in str(a.get("target", {})):
            raise AssertionError("heartbeat must NEVER turn Remote EMS off (RD2)")
    print("PASS  structural: handback EMS-MSC, never turns Remote EMS off")


def test_live_trigger():
    auto = _load()
    stale = next((t for t in auto["trigger"] if t.get("id") == "stale_setpoint"), None)
    assert stale is not None, "stale_setpoint trigger missing"
    assert "> 0.1" in stale["value_template"], "live trigger must use 0.1 kW threshold"
    assert str(stale.get("for")) in ("0:00:02", "00:00:02"), f"live trigger must debounce ~2 s, got {stale.get('for')}"
    print("PASS  live trigger: 0.1 kW deviation, 2 s debounce")


def test_dispatch_ceiling_overflow():
    # Hold, high PV over the cap → dispatch pinned at load+cap (export-limited).
    d = _render_dispatch(_load(), _mock("Hold Battery", pv=6.34, load=0.55, soc=50))
    assert abs(d - 4.23) < 0.01, f"overflow Hold must pin at load+cap 4.23, got {d}"
    print(f"PASS  ceiling: Hold @PV6.34 → {d:.2f} (load+cap, not 6)")


def test_dispatch_tracks_pv_on_dip():
    # Hold, PV below the cap → dispatch tracks PV (battery flat).
    d = _render_dispatch(_load(), _mock("Hold Battery", pv=3.0, load=0.55, soc=50))
    assert abs(d - 3.0) < 0.01, f"Hold below cap must track PV=3.0, got {d}"
    print(f"PASS  dip: Hold @PV3.0 → {d:.2f} (tracks PV)")


def test_dispatch_high_load_uses_66():
    # Max Export, high load → ceiling = inverter 6.6, NOT 6.0.
    d = _render_dispatch(_load(), _mock("Max Export", pv=5.0, load=3.0, soc=50))
    assert abs(d - 6.6) < 0.01, f"high-load Max Export must reach 6.6 (inverter max), got {d}"
    print(f"PASS  inverter max: Max Export @load3.0 → {d:.2f} (6.6, not 6.0)")


def test_dispatch_hard_floor_clamp():
    # Below the hard floor, dispatch clamped to PV so battery can't discharge.
    d = _render_dispatch(_load(), _mock("Max Export", pv=2.0, load=0.55, soc=10, hard=12))
    assert abs(d - 2.0) < 0.01, f"below hard floor dispatch must clamp to PV=2.0, got {d}"
    print(f"PASS  hard floor: SOC10% Max Export @PV2.0 → {d:.2f} (≤PV, no discharge)")


def main():
    for t in (test_structural, test_live_trigger, test_dispatch_ceiling_overflow, test_dispatch_tracks_pv_on_dip, test_dispatch_high_load_uses_66, test_dispatch_hard_floor_clamp):
        t()
    print("test_yaml_heartbeat: ALL PASSED")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"FAIL — {e}")
        sys.exit(1)
