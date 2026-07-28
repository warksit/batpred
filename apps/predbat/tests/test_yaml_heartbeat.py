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

import datetime as _dt

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
    # HA provides a `bool` filter; plain Jinja does not. Mirror it so the harness
    # sees the same truthiness rules the automation will.
    env.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).strip().lower() in ("true", "1", "yes", "on")

    def states_fn(entity):
        return states.get(entity, "unknown")

    ctx = {
        "states": states_fn,
        "is_state": lambda e, v: states.get(e) == v,
        "state_attr": lambda e, a: states.get("{}|{}".format(e, a)),
        "now": lambda: states.get("__now__", _dt.datetime(2026, 7, 28, 12, 0, tzinfo=_dt.timezone.utc)),
        "as_datetime": lambda v: v if isinstance(v, _dt.datetime) else _dt.datetime.fromisoformat(str(v)),
    }
    for key, tmpl in variables.items():
        rendered = env.from_string(tmpl).render(**ctx).strip()
        try:
            ctx[key] = float(rendered)
        except ValueError:
            ctx[key] = rendered
    _render_dispatch.ctx = ctx
    return ctx["dispatch_kw"]


def _mock(policy, pv, load, soc, cap_w=3680, hard=12):
    return {
        "input_select.sig_override": "Off",
        "input_select.sig_dispatch_policy": policy,
        "sensor.sigen_plant_pv_power": str(pv),
        "sensor.sigen_plant_total_load_power": str(load),
        "sensor.sigen_plant_battery_state_of_charge": str(soc),
        "input_number.dno_export_limit_w": str(cap_w),
        "input_number.sig_drain_floor_pct": str(hard),
    }


def _session_mock(policy, session_on, pv=2.0, load=0.5, soc=60):
    """Mock with the Octoplus saving-session CALENDAR on or off (RD14c)."""
    m = _mock(policy, pv, load, soc)
    m["calendar.octopus_energy_a_4ba7c915_octoplus_saving_sessions"] = "on" if session_on else "off"
    m.setdefault("input_select.sig_override", "Off")
    return m


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


def test_heartbeat_and_predbat_never_drive_together():
    """While policy == Predbat the heartbeat must write NOTHING but the EMS enable.

    Mutual exclusion, not negotiation: the heartbeat is the sole register writer
    for the active policies, and Predbat is the sole writer once handed back.

    Regression 2026-07-27: the Predbat branch re-asserted MSC on EVERY run when
    the EMS mode was not MSC — i.e. exactly when Predbat had just set Command
    Charging / Command Discharging. `stale_setpoint` is already gated to the
    active policies, but the 1-minute `beat` is not, so Predbat's mode was
    reverted within a minute. Enabling predbat_requested_mode_action alone
    therefore achieved nothing.

    Fix: the MSC write fires only on the policy_change trigger — the transition
    into handback. Turning Remote EMS on stays unconditional (Predbat cannot
    control without it, and it is not a control decision).
    """
    auto = _load()
    choose = auto["action"][1]["choose"]
    predbat = next((b for b in choose if "Predbat" in " ".join(str(c) for c in b["conditions"])), None)
    assert predbat is not None, "no Predbat handback branch"

    msc_step = None
    for step in predbat["sequence"]:
        if not isinstance(step, dict) or "if" not in step:
            continue
        if any((a.get("action") or a.get("service")) == "select.select_option" and a.get("data", {}).get("option") == "Maximum Self Consumption" for a in _iter_actions(step.get("then", []))):
            msc_step = step
            break
    assert msc_step is not None, "no guarded MSC write in the Predbat branch"

    guard = " ".join(str(c) for c in msc_step["if"])
    assert "policy_change" in guard, "MSC write must be gated on the policy_change trigger (one-shot handback), else the 1-min beat stomps Predbat"

    # Nothing else in the Predbat branch may write a control register.
    allowed = {"switch.turn_on", "select.select_option"}
    for a in _iter_actions(predbat["sequence"]):
        svc = a.get("action") or a.get("service")
        if svc is None:
            continue
        assert svc in allowed, f"Predbat branch must not call {svc!r} — heartbeat must stay inert while Predbat drives"
        if svc == "switch.turn_on":
            assert REMOTE_EMS_SWITCH in str(a.get("target", {})), "only the Remote EMS enable may be written unconditionally"

    # The live setpoint trigger must never fire under Predbat policy.
    stale = next((t for t in auto["trigger"] if t.get("id") == "stale_setpoint"), None)
    assert stale is not None, "stale_setpoint trigger missing"
    assert "'Predbat'" not in stale["value_template"].replace("!= 'Predbat'", ""), "stale_setpoint must be gated to active policies only"
    for p in ("Max Export", "Hold Battery", "Solar Charge Battery"):
        assert p in stale["value_template"], f"stale_setpoint must list active policy {p!r}"
    print("PASS  exclusion: heartbeat inert under Predbat policy (MSC one-shot on policy_change only)")


def test_active_policy_reopens_ess_and_import_limits():
    """An active policy MUST re-open the registers a Predbat freeze leaves clamped.

    Predbat's Freeze Charging/Discharging locks the battery with
    ess_max_discharging_limit=0 and blocks import with grid_import_limitation=0.
    Those are PLANT registers, not owned by the policy select — disabling the
    mapper does not clear them. Without this the heartbeat writes dispatch into a
    hardware-locked battery and nothing moves.

    Regressed TWICE:
      2026-07-26  fixed by 5bbdedeb
      2026-07-28  the v8.46.4 port predated 5bbdedeb, so re-deploying the
                  heartbeat from the ported file dropped it again — SOC sat flat
                  at 44.6% for 4.5 h under Max Export on a clear morning.
    Hence this test.
    """
    auto = _load()
    choose = auto["action"][1]["choose"]
    active = next((b for b in choose if "Max Export" in " ".join(str(c) for c in b["conditions"])), None)
    assert active is not None, "no active-policy branch"

    writes = {}
    for a in _iter_actions(active["sequence"]):
        if (a.get("action") or a.get("service")) == "number.set_value":
            ent = str(a.get("target", {}).get("entity_id", ""))
            writes[ent] = a.get("data", {}).get("value")

    for reg in ("number.sigen_plant_ess_max_discharging_limit", "number.sigen_plant_ess_max_charging_limit", "number.sigen_plant_grid_import_limitation"):
        assert reg in writes, f"active policy must re-open {reg} — a Predbat freeze would otherwise persist"
    assert str(writes["number.sigen_plant_grid_import_limitation"]) == "100", f"import limit must be re-opened to 100, got {writes['number.sigen_plant_grid_import_limitation']!r}"

    # The ESS registers are DERIVED from the rate helpers by
    # predbat_max_(dis)charging_limit_action. Resetting only the register leaves the
    # helper at 0, so the next state change on it re-clamps — reset the source too.
    helpers = {}
    for a in _iter_actions(active["sequence"]):
        if (a.get("action") or a.get("service")) == "input_number.set_value":
            helpers[str(a.get("target", {}).get("entity_id", ""))] = a.get("data", {}).get("value")
    for h in ("input_number.discharge_rate", "input_number.charge_rate"):
        assert h in helpers, f"active policy must reset {h} — it is the SOURCE of the ESS clamp"

    # And the trigger must notice a clamp, not just a dispatch drift.
    stale = next(t for t in auto["trigger"] if t.get("id") == "stale_setpoint")
    tmpl = stale["value_template"]
    assert "ess_max_discharging_limit" in tmpl, "stale_setpoint must fire on a clamped discharge limit"
    assert "grid_import_limitation" in tmpl, "stale_setpoint must fire on a blocked import limit"
    print("PASS  re-open: active policy clears ESS + import clamps and resets the rate helpers")


def test_manual_override_is_a_trigger():
    """Flipping the override select must re-evaluate dispatch immediately.

    It changes who is driving (RD13); without this trigger the heartbeat waits up
    to a minute for the beat. It carries its OWN id so the one-shot MSC handback —
    gated on policy_change — does not re-park the unit when the override is
    toggled while already handed back.
    """
    auto = _load()
    ids = {t.get("id") for t in auto["trigger"]}
    assert "override_change" in ids, f"manual override trigger missing, got ids {ids}"
    ov = next(t for t in auto["trigger"] if t.get("id") == "override_change")
    assert ov.get("entity_id") == "input_select.sig_override", ov
    assert ov.get("id") != "policy_change", "override must not share the policy_change id — it would re-park on every toggle"
    print("PASS  trigger: manual override re-evaluates dispatch (own id, not policy_change)")


def test_live_trigger():
    auto = _load()
    stale = next((t for t in auto["trigger"] if t.get("id") == "stale_setpoint"), None)
    assert stale is not None, "stale_setpoint trigger missing"
    assert "> 0.1" in stale["value_template"], "live trigger must use 0.1 kW threshold"
    assert stale.get("for") in (None, 0, "0:00:00", "00:00:00"), f"live trigger must fire immediately (no debounce), got for={stale.get('for')}"
    print("PASS  live trigger: 0.1 kW deviation, immediate (no debounce)")


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


def test_dispatch_max_export_always_66():
    # v32: Max Export commands 6.6 (inverter max) regardless of load — the SIG's
    # grid_export_limitation clamps export to the DNO cap in hardware. Low load
    # (would be load+cap=4.18 under the old formula) and high load both → 6.6.
    d_lo = _render_dispatch(_load(), _mock("Max Export", pv=8.0, load=0.5, soc=50))
    d_hi = _render_dispatch(_load(), _mock("Max Export", pv=5.0, load=3.0, soc=50))
    assert abs(d_lo - 6.6) < 0.01, f"Max Export @low load must command 6.6 (not load+cap), got {d_lo}"
    assert abs(d_hi - 6.6) < 0.01, f"Max Export @high load must command 6.6, got {d_hi}"
    print(f"PASS  Max Export = 6.6 regardless of load: lo={d_lo:.2f} hi={d_hi:.2f}")


def test_dispatch_hold_still_tracks_load_ceiling():
    # Hold is UNCHANGED — it must still pin at load+cap (not 6.6), so it stays flat
    # and doesn't drain on a PV dip. PV over cap → dispatch = load+cap.
    d = _render_dispatch(_load(), _mock("Hold Battery", pv=8.0, load=0.5, soc=50))
    assert abs(d - 4.18) < 0.01, f"Hold must pin at load+cap 4.18 (NOT 6.6), got {d}"
    print(f"PASS  Hold ceiling unchanged: Hold @PV8 load0.5 → {d:.2f} (load+cap, not 6.6)")


def test_dispatch_hard_floor_clamp():
    # Below the drain floor, dispatch clamps to PV so the battery can't discharge.
    d = _render_dispatch(_load(), _mock("Max Export", pv=2.0, load=0.55, soc=2, hard=2.8))
    assert abs(d - 2.0) < 0.01, f"below drain floor dispatch must clamp to PV=2.0, got {d}"
    print(f"PASS  drain floor: SOC2% Max Export @PV2.0 → {d:.2f} (≤PV, no discharge)")


def test_dispatch_drives_between_2_8_and_5():
    # v32: SOC 4% is ABOVE the 2.8% drain floor → Max Export commands 6.6 (drives),
    # NOT clamped to PV. (Old 5% floor would have clamped to PV here.)
    d = _render_dispatch(_load(), _mock("Max Export", pv=2.0, load=0.55, soc=4, hard=2.8))
    assert abs(d - 6.6) < 0.01, f"SOC4% > 2.8% floor: Max Export must command 6.6, not clamp to PV, got {d}"
    print(f"PASS  drain floor: SOC4% Max Export → {d:.2f} (drives 6.6, above 2.8% floor)")


def test_dispatch_drain_floor_default_2_8():
    # No helper set → the '| float(2.8)' default applies: SOC 2% clamps to PV.
    ctx = dict(_mock("Max Export", pv=2.0, load=0.55, soc=2))
    del ctx["input_number.sig_drain_floor_pct"]
    d = _render_dispatch(_load(), ctx)
    assert abs(d - 2.0) < 0.01, f"default 2.8% floor: SOC2% must clamp to PV, got {d}"
    print(f"PASS  drain floor default 2.8%: SOC2% → {d:.2f}")


def test_rd14c_live_session_forces_max_export():
    """RD14c: a live saving session forces Max Export even though the policy
    select still says Hold. Source is the Octoplus CALENDAR, which is "on when a
    saving session that the account has joined is active" — joined-only, so an
    un-joined session can never make us export for free.
    """
    _render_dispatch(_load(), _session_mock("Hold Battery", session_on=True))
    ctx = _render_dispatch.ctx
    assert ctx["policy"] == "Max Export", f"live session must force Max Export, got {ctx['policy']}"
    assert ctx["raw_policy"] == "Hold Battery", "the select itself must be untouched"
    print("PASS  RD14c: live session -> Max Export")


def test_rd14c_releases_at_the_planned_end():
    """The edge that was actually measured wrong: on 2026-07-28 the plugin
    released Max Export at 19:35:46 for a session that ended at 19:30:00 —
    5 min 46 s of dumping the battery at the cap past the paid window.

    The moment the calendar clears, dispatch must fall back to the select. This
    is also the `| bool` regression guard: `session_live` renders to the STRING
    "False", which is truthy in Jinja, so without the filter a session would
    start correctly and never release.
    """
    _render_dispatch(_load(), _session_mock("Hold Battery", session_on=False))
    ctx = _render_dispatch.ctx
    assert ctx["policy"] == "Hold Battery", f"must fall back to the select when the calendar clears, got {ctx['policy']}"
    print("PASS  RD14c: calendar off -> released to the select")


def test_rd14c_does_not_seize_control_from_predbat():
    """Writer ownership: if CM has handed back, Predbat's mappers are enabled.
    Forcing Max Export would put two writers on the registers — the 2026-07-26
    and 2026-07-28 failures. A session must NOT override handback."""
    _render_dispatch(_load(), _session_mock("Predbat", session_on=True))
    assert _render_dispatch.ctx["policy"] == "Predbat", "must stay handed back during a session"
    print("PASS  RD14c: does not seize control from Predbat")


def test_rd14c_uses_native_calendar_triggers():
    """Native calendar triggers, not a template window. HA schedules these at the
    exact event boundary; the previous template approach depended on the beat and
    duplicated the window expression between trigger and action."""
    trigs = _load()["trigger"]
    ids = [t.get("id") for t in trigs]
    assert "session_start" in ids and "session_end" in ids, f"calendar triggers missing: {ids}"
    cal = [t for t in trigs if t.get("platform") == "calendar"]
    assert len(cal) == 2, f"expected exactly 2 calendar triggers, got {len(cal)}"
    assert {t.get("event") for t in cal} == {"start", "end"}, f"need both edges: {cal}"
    for t in cal:
        assert "octoplus_saving_sessions" in t.get("entity_id", ""), t
    print("PASS  RD14c: native calendar start/end triggers")


def test_rd14c_no_template_window_math_remains():
    """Guard against reintroducing the hand-rolled window. The calendar entity is
    the single source of truth for whether a session is live."""
    import yaml as _y

    dumped = _y.dump(_load())
    assert "as_datetime" not in dumped, "template window math must not return"
    assert "next_joined_event" not in dumped, "must not fall back to the lagging binary sensor"
    print("PASS  RD14c: no window math / binary-sensor fallback remains")


def test_rd13a_override_select_outranks_everything():
    """RD13a: manual override is the SELECT alone — active iff not "Off", and its
    value IS the policy. It outranks a live saving session: a human holding a
    policy can see something the automation cannot.

    There is no boolean. It was redundant state derivable from the select, so the
    only thing it could add was divergence.
    """
    m = _session_mock("Hold Battery", session_on=True)
    m["input_select.sig_override"] = "Solar Charge Battery"
    _render_dispatch(_load(), m)
    ctx = _render_dispatch.ctx
    assert ctx["policy"] == "Solar Charge Battery", f"override must outrank the session dump, got {ctx['policy']}"
    print("PASS  RD13a: override select outranks a live session")


def test_rd13a_off_hands_back_to_the_plugin():
    """ "Off" means the plugin decides — the select must contribute nothing."""
    m = _session_mock("Hold Battery", session_on=False)
    m["input_select.sig_override"] = "Off"
    _render_dispatch(_load(), m)
    assert _render_dispatch.ctx["policy"] == "Hold Battery", "Off -> plugin's select rules"
    print("PASS  RD13a: Off -> plugin's select rules")


def test_rd13a_policy_has_no_stray_whitespace():
    """The policy expression must be ONE line. A folded `>-` block leaves trailing
    whitespace, so "Max Export " silently fails every `policy in [...]` comparison
    in the choose below — dispatch would do nothing at all."""
    pol = _load()["action"][0]["variables"]["policy"]
    assert "\n" not in pol, "policy must be a single expression, not a folded block"
    m = _session_mock("Hold Battery", session_on=True)
    m["input_select.sig_override"] = "Max Export"
    _render_dispatch(_load(), m)
    got = _render_dispatch.ctx["policy"]
    assert got == got.strip() and got == "Max Export", f"policy must render clean, got {got!r}"
    print("PASS  RD13a: policy renders with no stray whitespace")


def test_rd13a_no_override_boolean_anywhere():
    """The boolean is deleted, not merely unused."""
    import yaml as _y

    assert "sig_manual_override" not in _y.dump(_load()), "the override boolean must be gone from the heartbeat"
    print("PASS  RD13a: no override boolean in the heartbeat")


def main():
    for t in (
        test_rd13a_override_select_outranks_everything,
        test_rd13a_off_hands_back_to_the_plugin,
        test_rd13a_policy_has_no_stray_whitespace,
        test_rd13a_no_override_boolean_anywhere,
        test_rd14c_live_session_forces_max_export,
        test_rd14c_releases_at_the_planned_end,
        test_rd14c_does_not_seize_control_from_predbat,
        test_rd14c_uses_native_calendar_triggers,
        test_rd14c_no_template_window_math_remains,
        test_structural,
        test_heartbeat_and_predbat_never_drive_together,
        test_active_policy_reopens_ess_and_import_limits,
        test_manual_override_is_a_trigger,
        test_live_trigger,
        test_dispatch_ceiling_overflow,
        test_dispatch_tracks_pv_on_dip,
        test_dispatch_max_export_always_66,
        test_dispatch_hold_still_tracks_load_ceiling,
        test_dispatch_hard_floor_clamp,
        test_dispatch_drives_between_2_8_and_5,
        test_dispatch_drain_floor_default_2_8,
    ):
        t()
    print("test_yaml_heartbeat: ALL PASSED")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"FAIL — {e}")
        sys.exit(1)
