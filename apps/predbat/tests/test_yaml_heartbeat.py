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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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


def _with_intent(states):
    """Add the RD26 intent sensors, rendered from the SINGLE source of truth.

    The heartbeat no longer computes the policy or the setpoint — it reads
    sensor.sig_effective_policy / sensor.sig_dispatch_kw. Rendering them here from
    ha/sig_dispatch_intent_helpers.yaml keeps every dispatch assertion below
    meaningful end-to-end, and means a change to the helpers is exercised by the
    whole heartbeat suite rather than only by its own file.

    Done at render time, not in _mock, because some tests mutate the states dict
    after building it (e.g. deleting the drain-floor helper to test its default).
    """
    import test_yaml_dispatch_intent as intent

    st = dict(states)
    policy, kw = intent.render_sensors(st)
    st.setdefault("sensor.sig_effective_policy", policy)
    st.setdefault("sensor.sig_dispatch_kw", str(kw))
    return st


def _render_dispatch(auto, states):
    """Render the action `variables` block in order with a mock states()."""
    states = _with_intent(states)
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
    """Mock with a live PAID session (Power Down) on or off (RD14c).

    Drives binary_sensor.octoplus_power_down_active, not the Octoplus calendar:
    the calendar is on for Power Ups too, so it cannot say what a session MEANS.
    See ha/octoplus_session_helpers.yaml and ha/OCTOPUS_SESSIONS.md.
    """
    m = _mock(policy, pv, load, soc)
    m["binary_sensor.octoplus_power_down_active"] = "on" if session_on else "off"
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


def test_manual_override_acts_immediately():
    """Flipping the override select must re-evaluate dispatch immediately (RD13).

    The GUARANTEE is unchanged; the mechanism moved. It used to be a state trigger
    on input_select.sig_override. Once RD26 put the policy in a template sensor that
    became a race — HA fires on the input at once, the sensor recomputes a moment
    later, and the run reads the OLD policy. Live 2026-08-06 11:42:13: the override
    run saw policy="Predbat", took the Predbat branch, wrote nothing in 12 ms, and
    the override did nothing for 47 s until the next beat.

    So the override now reaches the heartbeat through sensor.sig_effective_policy,
    which changes whenever the override does. Same immediacy, no stale read.

    The old worry — that an override toggle while handed back would re-park the
    unit via the policy_change-gated MSC write — is resolved by construction:
    Off -> Hold moves the EFFECTIVE policy to Hold, so the run takes the ACTIVE
    branch, not the Predbat one. And Hold -> Off SHOULD park in MSC; that is RD24,
    the fix for the stranded PCS Remote Control.
    """
    auto = _load()
    state_triggers = {t.get("entity_id"): t for t in auto["trigger"] if t.get("platform") == "state"}
    assert "sensor.sig_effective_policy" in state_triggers, f"override must reach the heartbeat via the derived policy sensor, got {list(state_triggers)}"
    assert state_triggers["sensor.sig_effective_policy"].get("id") == "policy_change"
    assert "input_select.sig_override" not in state_triggers, "triggering on the raw override races the derived sensor (47 s of nothing, live 2026-08-06)"
    print("PASS  trigger: override acts immediately via sensor.sig_effective_policy")


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


def _eval_condition(cond, trigger_id, states):
    """Evaluate the subset of HA condition types the heartbeat uses."""
    if isinstance(cond, str):
        raise AssertionError("template condition not supported by this evaluator: {}".format(cond))
    kind = cond.get("condition")
    if kind == "trigger":
        ids = cond["id"]
        return trigger_id in (ids if isinstance(ids, list) else [ids])
    if kind == "state":
        want = cond["state"]
        actual = states.get(cond["entity_id"])
        return actual in (want if isinstance(want, list) else [want])
    if kind == "not":
        return not all(_eval_condition(c, trigger_id, states) for c in cond["conditions"])
    if kind == "or":
        return any(_eval_condition(c, trigger_id, states) for c in cond["conditions"])
    if kind == "and":
        return all(_eval_condition(c, trigger_id, states) for c in cond["conditions"])
    raise AssertionError("unhandled condition type {!r}".format(kind))


def _msc_write_fires(trigger_id, ems_mode):
    """Would the Predbat branch write Maximum Self Consumption, given this
    trigger and this live EMS mode?"""
    auto = _load()
    predbat = next(b for b in auto["action"][1]["choose"] if "Predbat" in " ".join(str(c) for c in b["conditions"]))
    step = None
    for s in predbat["sequence"]:
        if isinstance(s, dict) and "if" in s:
            if any((a.get("action") or a.get("service")) == "select.select_option" and a.get("data", {}).get("option") == "Maximum Self Consumption" for a in _iter_actions(s.get("then", []))):
                step = s
    assert step is not None, "no guarded MSC write in the Predbat branch"
    states = {"select.sigen_plant_remote_ems_control_mode": ems_mode}
    return all(_eval_condition(c, trigger_id, states) for c in step["if"])


def _trigger_vars(auto, states):
    """Render the stale_setpoint trigger's INTERNAL working (`p`, `want`).

    The trigger is a run of `{% set %}` statements followed by exactly one output
    expression. Swapping that final expression lets the harness see the values the
    trigger is deciding on, instead of only its boolean — which is what let three
    divergences ship today.
    """
    states = _with_intent(states)
    trig = next(t for t in auto["trigger"] if t.get("id") == "stale_setpoint")
    tmpl = trig["value_template"]
    cut = tmpl.rindex("{{")
    assert "want" in tmpl[:cut], "trigger no longer computes `want` — rewrite this harness with it"
    env = jinja2.Environment()
    env.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).strip().lower() in ("true", "1", "yes", "on")
    ctx = {"states": lambda e: states.get(e, "unknown"), "is_state": lambda e, v: states.get(e) == v}
    out = env.from_string(tmpl[:cut] + "{{ p }}|{{ want }}").render(**ctx).strip()
    p, want = out.split("|")
    return p, float(want)


ACTIVE_POLICIES = ("Max Export", "Hold Battery", "Solar Charge Battery")


def test_trigger_and_action_can_never_diverge():
    """THE anti-divergence guard.

    HA gives a template trigger no access to the action's `variables`, so the
    dispatch logic is necessarily written TWICE — once to decide "has the live
    setpoint drifted?", once to compute what to write. Three divergences shipped
    on 2026-08-06 alone:

      1. the RD22 sell-only clamp landed in the action copy only
      2. ...and the first test for it passed anyway, because at the live numbers
         the two copies differed by less than the trigger's own 0.1 kW tolerance
      3. the trigger's policy ignores `sig_override` entirely, so under a manual
         override with the select on Predbat the trigger is disarmed and the fast
         corrector is dead — only the 60 s beat writes, and the battery absorbs
         every PV sag in between (live: commanded 1.45, PV 1.329, battery -0.135)

    So do not test the copies separately. Render BOTH over a matrix and assert
    they agree — on the value AND on whether they consider the policy active.
    A future edit to one copy alone cannot pass this.
    """
    auto = _load()
    cases = []
    for select in ("Predbat", "Max Export", "Hold Battery", "Solar Charge Battery"):
        for override in ("Off", "Hold Battery", "Max Export", "Solar Charge Battery", "Predbat"):
            for session in (False, True):
                for pv, load, soc in ((0.1, 1.5, 1.3), (1.329, 0.364, 1.7), (8.0, 0.5, 50), (0.0, 0.4, 90), (3.0, 3.0, 2.8)):
                    st = _mock(select, pv, load, soc, hard=2.8)
                    st["input_select.sig_override"] = override
                    st["binary_sensor.octoplus_power_down_active"] = "on" if session else "off"
                    cases.append(st)

    mismatches = []
    for st in cases:
        trig_p, want = _trigger_vars(auto, st)
        dispatch = _render_dispatch(auto, st)
        act_p = _render_dispatch.ctx["policy"]
        tag = "select={} override={} session={} pv={} load={} soc={}".format(
            st["input_select.sig_dispatch_policy"],
            st["input_select.sig_override"],
            st["binary_sensor.octoplus_power_down_active"],
            st["sensor.sigen_plant_pv_power"],
            st["sensor.sigen_plant_total_load_power"],
            st["sensor.sigen_plant_battery_state_of_charge"],
        )
        if trig_p != act_p:
            mismatches.append("POLICY {}: trigger says {!r}, action says {!r}".format(tag, trig_p, act_p))
        elif abs(want - dispatch) > 1e-6:
            mismatches.append("SETPOINT {}: trigger wants {:.3f}, action writes {:.3f}".format(tag, want, dispatch))
        # Armed exactly when the action would drive. Disarmed-but-driving is the
        # live 2026-08-06 fault: no fast correction under a manual override.
        armed = trig_p in ACTIVE_POLICIES
        drives = act_p in ACTIVE_POLICIES
        if armed != drives:
            mismatches.append("ARMING {}: trigger armed={}, action drives={}".format(tag, armed, drives))

    assert not mismatches, "trigger and action have diverged in {} of {} cases:\n  {}".format(len(mismatches), len(cases), "\n  ".join(mismatches[:8]))
    print("PASS  trigger/action equivalence over {} state combinations".format(len(cases)))


def test_predbat_branch_self_heals_pcs_remote_control():
    """Entering the Predbat branch must unwind PCS Remote Control HOWEVER it was
    entered — not only via the policy_change trigger.

    Live 2026-08-06 07:38. The policy select read Predbat; the override was set
    to Hold Battery, which makes the EFFECTIVE policy Hold, so the heartbeat took
    the ACTIVE branch and wrote PCS Remote Control + a 1.08 kW setpoint. Setting
    the override back to Off returned the effective policy to Predbat — but the
    select never changed, so only `override_change` fired and the MSC handback,
    gated on `policy_change`, was skipped. Result: heartbeat inert, mappers
    disabled, Predbat read-only, and the plant left exporting against a 1.08 kW
    setpoint nobody owned, discharging a 1.4% battery.

    Keying the self-heal on the MODE rather than on which trigger fired also
    covers an HA restart, which `override_change` alone would not.
    """
    assert _msc_write_fires("override_change", "PCS Remote Control") is True, "override-driven entry into the Predbat branch must unwind PCS Remote Control"
    assert _msc_write_fires("beat", "PCS Remote Control") is True, "the 1-min beat must self-heal a stranded PCS Remote Control (covers an HA restart)"
    print("PASS  Predbat branch self-heals PCS Remote Control on any trigger")


def test_self_heal_cannot_stomp_predbat_own_modes():
    """The other half, and the reason this is safe.

    `predbat_requested_mode_action` only ever selects Maximum Self Consumption,
    Command Charging (Grid First), or Command Discharging (PV First) — it never
    selects PCS Remote Control, which is the heartbeat's own mode. So keying the
    self-heal on that value cannot fight Predbat.

    The regression this protects (2026-07-27): the Predbat branch used to
    re-assert MSC on EVERY run when the mode was not MSC — i.e. exactly when
    Predbat had just commanded Charging/Discharging — so Predbat's mode was
    reverted within a minute and enabling the mapper achieved nothing.
    """
    for mode in ("Command Charging (Grid First)", "Command Discharging (PV First)"):
        assert _msc_write_fires("beat", mode) is False, "the beat must NOT revert Predbat's own {!r}".format(mode)
        assert _msc_write_fires("override_change", mode) is False, "an override change must NOT revert Predbat's own {!r}".format(mode)
        # The deliberate handback is still allowed to park in MSC (RD2).
        assert _msc_write_fires("policy_change", mode) is True, "policy_change handback must still park in MSC from {!r}".format(mode)
    assert _msc_write_fires("beat", "Maximum Self Consumption") is False, "already MSC — no pointless write every minute"
    print("PASS  self-heal cannot stomp Command Charging/Discharging; RD2 park intact")


def _render_stale_trigger(auto, states):
    """Render the `stale_setpoint` template trigger to its boolean."""
    states = _with_intent(states)
    trig = next(t for t in auto["trigger"] if t.get("id") == "stale_setpoint")
    env = jinja2.Environment()
    env.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).strip().lower() in ("true", "1", "yes", "on")
    ctx = {
        "states": lambda e: states.get(e, "unknown"),
        "is_state": lambda e, v: states.get(e) == v,
    }
    return env.from_string(trig["value_template"]).render(**ctx).strip().lower() == "true"


def test_stale_trigger_agrees_with_dispatch():
    """The stale_setpoint trigger RE-IMPLEMENTS the dispatch maths to decide
    whether the live setpoint has drifted. If the two copies disagree, the
    trigger fires on every state change forever (mode: restart), because the
    setpoint it demands is one the action will never write.

    This is the `required_headroom_kwh` lesson in YAML: one quantity, two
    expressions. The harness rendered only the ACTION variables, so the RD22
    clamp gate landed in one copy and not the other — caught by reading the
    deployed JSON, not by the green test.
    """
    # The gap between the two copies must EXCEED the trigger's own 0.1 kW
    # tolerance, or the test passes on the drift it exists to catch. At the live
    # 06:48 numbers (pv 0.311, load 0.359) the disagreement is only 0.048 and is
    # invisible; a normal evening load makes it 1.4 kW.
    st = _mock("Hold Battery", pv=0.1, load=1.5, soc=1.3, hard=2.8)
    st["switch.sigen_plant_remote_ems_controlled_by_home_assistant"] = "on"
    st["sensor.sigen_inverter_ess_rated_discharge_power"] = "6.6"
    st["number.sigen_plant_ess_max_discharging_limit"] = "6.6"
    st["number.sigen_plant_grid_import_limitation"] = "100"

    dispatch = _render_dispatch(_load(), st)
    # The register already holds exactly what the action would write.
    st["number.sigen_plant_active_power_fixed_adjustment"] = str(dispatch)
    assert _render_stale_trigger(_load(), st) is False, "trigger demands a setpoint the action will never write ({:.3f}) — permanent re-fire loop".format(dispatch)
    print("PASS  stale trigger agrees with dispatch {:.3f} (no re-fire loop)".format(dispatch))


def test_drain_floor_does_not_strand_hold_below_the_floor():
    """The drain floor stops CM SELLING an empty battery. It must NOT stop the
    battery covering house load.

    Live 2026-08-06 06:48, SOC 1.3% under a manual Hold override:

        pv 0.311  load 0.359  ->  raw = max(pv,load) = 0.359
        soc 1.3 <= hard 2.8   ->  clamped to min(raw, pv) = 0.311
        battery -0.003 kW (idle), import 0.027 kW

    The plant was commanded to output exactly PV, so the 0.048 kW shortfall came
    off the grid while the battery sat holding 0.235 kWh. The clamp overrode the
    policy AND the human override — it is applied after policy selection, so under
    the old form no policy could use the battery below the floor.

    That is RD4's prohibition ("never forced to import while it holds charge")
    violated by the mechanism meant to protect a battery that needs no protection:
    the SIG simply imports at 0%, there is no cliff (Andrew, 2026-08-06).
    """
    # RD26: the value now arrives via sensor.sig_dispatch_kw, which rounds to the
    # 2 dp the register takes anyway (0.359 -> 0.36). The register write is
    # unchanged — it was always `| round(2)`.
    d = _render_dispatch(_load(), _mock("Hold Battery", pv=0.311, load=0.359, soc=1.3, hard=2.8))
    assert abs(d - 0.36) < 0.001, "Hold below the drain floor must still cover load (0.36), got {} — battery stranded, load imported".format(d)
    print("PASS  drain floor: Hold @SOC1.3% covers load {:.2f} (not clamped to PV)".format(d))


def test_drain_floor_still_blocks_selling_below_the_floor():
    """The other half: Max Export below the floor must STILL clamp to PV.

    Both halves in one commit deliberately — the change is "the clamp applies to
    SELLING, not to load-covering", and a test that only pins the new behaviour
    would let the old requirement (R5, stop selling at the floor) be deleted by
    accident.
    """
    d = _render_dispatch(_load(), _mock("Max Export", pv=0.311, load=0.359, soc=1.3, hard=2.8))
    assert abs(d - 0.31) < 0.001, "Max Export below the drain floor must clamp to PV (0.31), got {}".format(d)
    print("PASS  drain floor: Max Export @SOC1.3% still clamped to PV {:.2f}".format(d))


def test_drain_floor_solar_charge_not_stranded():
    """Solar Charge dispatches `load`. Below the floor the old clamp cut that to
    PV as well, so a charging policy imported the shortfall instead of letting the
    battery bridge it — the same defect, on the policy least able to justify it.
    """
    d = _render_dispatch(_load(), _mock("Solar Charge Battery", pv=0.311, load=0.359, soc=1.3, hard=2.8))
    assert abs(d - 0.36) < 0.001, "Solar Charge below the floor must still cover load (0.36), got {}".format(d)
    print("PASS  drain floor: Solar Charge @SOC1.3% covers load {:.2f}".format(d))


def test_rd14c_live_session_forces_max_export():
    """RD14c: a live saving session forces Max Export even though the policy
    select still says Hold. Source is the Octoplus CALENDAR, which is "on when a
    saving session that the account has joined is active" — joined-only, so an
    un-joined session can never make us export for free.
    """
    _render_dispatch(_load(), _session_mock("Hold Battery", session_on=True))
    ctx = _render_dispatch.ctx
    assert ctx["policy"] == "Max Export", f"live session must force Max Export, got {ctx['policy']}"
    # RD26: `raw_policy` is no longer an action variable — the select is read
    # inside sensor.sig_effective_policy. Assert against the select itself, which
    # is the actual claim: the session must not have written to it.
    assert _session_mock("Hold Battery", session_on=True)["input_select.sig_dispatch_policy"] == "Hold Battery", "the select itself must be untouched"
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
    duplicated the window expression between trigger and action.

    Two things are asserted together because they are two halves of one fact:
    the calendar says WHEN a session runs, the discrimination sensor says WHAT it
    is. The calendar is on for Power Ups as well as Power Downs, so waking on it
    alone would leave the heartbeat unable to tell an export hour from a
    free-import hour. And it must be the `power_down` name — the legacy
    `saving_sessions` calendar is removed January 2027, after which these
    triggers would simply stop firing, with no error and no log.
    """
    trigs = _load()["trigger"]
    ids = [t.get("id") for t in trigs]
    assert "session_start" in ids and "session_end" in ids, f"calendar triggers missing: {ids}"
    cal = [t for t in trigs if t.get("platform") == "calendar"]
    assert len(cal) == 2, f"expected exactly 2 calendar triggers, got {len(cal)}"
    assert {t.get("event") for t in cal} == {"start", "end"}, f"need both edges: {cal}"
    for t in cal:
        entity = t.get("entity_id", "")
        assert "octoplus_power_down" in entity, f"must trigger on the power_down calendar, got {entity!r}"
        assert "saving_sessions" not in entity, f"legacy calendar name is removed January 2027: {entity!r}"
    state_entities = {t.get("entity_id") for t in trigs if t.get("platform") == "state"}
    assert "binary_sensor.octoplus_power_down_active" in state_entities, "the heartbeat must also wake when the session CATEGORY changes — the calendar cannot distinguish a Power Up from a Power Down"
    print("PASS  RD14c: native calendar start/end triggers + category trigger")


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
        test_manual_override_acts_immediately,
        test_live_trigger,
        test_dispatch_ceiling_overflow,
        test_dispatch_tracks_pv_on_dip,
        test_dispatch_max_export_always_66,
        test_dispatch_hold_still_tracks_load_ceiling,
        test_dispatch_hard_floor_clamp,
        test_dispatch_drives_between_2_8_and_5,
        test_dispatch_drain_floor_default_2_8,
        test_trigger_and_action_can_never_diverge,
        test_predbat_branch_self_heals_pcs_remote_control,
        test_self_heal_cannot_stomp_predbat_own_modes,
        test_stale_trigger_agrees_with_dispatch,
        test_drain_floor_does_not_strand_hold_below_the_floor,
        test_drain_floor_still_blocks_selling_below_the_floor,
        test_drain_floor_solar_charge_not_stranded,
    ):
        t()
    print("test_yaml_heartbeat: ALL PASSED")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"FAIL — {e}")
        sys.exit(1)
