# Curtailment Manager — Requirements

All changes to the curtailment manager (curtailment_plugin.py, curtailment_calc.py,
HA automation, tests) MUST be checked against these requirements. Do not remove
features without verifying they are not required here.

---

## Charter — how to use and edit this document

**Read this section before changing anything in this file.** It exists because
the way this document was maintained caused real control failures — see
*Why the Charter exists* at the end.

Refer to it as **the Charter** ("check it against the Charter", "the Charter says
one quantity one definition"). Everything below this heading and above
`PART 1 — CURRENT SPEC` is the Charter.

### Structure

| Part | Status | Contains |
|---|---|---|
| **Current Spec** | NORMATIVE — this is what the system does | Principles, then requirements in force |
| **History** | NON-normative — reference only | Superseded requirements, kept for their reasoning |
| **Appendices** | Supporting | Test matrices, incident analyses, scenario tables |

**Only the Current Spec describes the system.** Nothing in History is in force,
ever, regardless of how it is worded. If you find yourself citing a History
requirement as authority for a change, stop — you are building from a decision
that was already reversed.

### Before you edit — the conflict check

Adding a requirement without doing this is how we got here. Every one of these
steps has caught a real bug.

1. **Search the Current Spec for the quantity you are about to define.** If
   another requirement already computes it, you do NOT add a second definition.
   You extend the existing one, or you change it. See *One quantity, one
   definition* below.
2. **Read every requirement your change touches, in full.** Not the summary, not
   the heading — the body, including its Why.
3. **If your change contradicts an existing requirement, resolve the conflict
   before editing.** Do not add a layer that says "this wins when in conflict"
   and leave the old text standing. That is what created four simultaneous
   answers to "when does CM deactivate?".
4. **Superseding means editing the old requirement in place**, then moving its
   body to History with a one-line pointer forward. A superseded requirement
   must never be readable as current.
5. **State what the change costs.** Every requirement trades something. If you
   cannot name what it gives up, you do not yet understand it well enough to
   add it.
6. **Check the code actually matches** when you are done. Several requirements
   here described behaviour the code never implemented, and several described
   behaviour the code had stopped implementing years-equivalent ago.

### Requirement format

Every requirement in the Current Spec carries these fields. The last three are
the ones we keep wishing we had.

```text
### R<n> — <short title> (<date added>)

**What:**      The rule, precisely. Formula if there is one.
**Why:**       The reason it exists. Not "for safety" — the actual mechanism.
**Evidence:**  The incident, date, and numbers that motivated it.
**Removing this would:**  The concrete failure that returns if you delete it.
**Trade-off:** What it costs us when it fires.
**Implemented in:** file.py:function — so drift is findable.
```

**If a requirement has no recorded Why, do not guess it — and do not act on the
requirement until you have one.** Ask the user at the point it becomes relevant,
then write the answer down. A reconstructed-sounding rationale is worse than a
blank, because it stops anyone asking. Mark unknowns explicitly:
`**Why:** not recorded — ask before relying on this.`

**"Removing this would" is the field that matters most.** The drain mechanism
has been removed twice by people who could not see why it was there, and
restored twice. If a requirement cannot say what breaks without it, that is
itself a finding — either write the reason down or delete the requirement.

### One quantity, one definition

**If two pieces of code answer the same question, they must call the same
function.** Not "the same formula" — the same function.

This is the single rule that would have prevented most of the failures below.
When the same question ("does the forecast surplus fit in the battery?") is
expressed independently in several places, the expressions drift apart, and the
weakest one silently wins because it is the one that vetoes.

Before adding a threshold, comparison, or floor, search for an existing one that
answers the same question. Requirements that share a quantity must say so
explicitly and name the shared function.

### Make the active mechanism observable

**If a decision can be reached by more than one path, the system must publish
which path it took.** Silent fallbacks, silent source-switches and silent
overrides are forbidden.

Three concrete instances, all of which cost hours:

- The overflow integral silently switches between Solcast per-slot (R53) and
  solar geometry (R9 fallback) based on `len(detailed) >= 4`, every cycle,
  publishing nothing. The floor behaves differently between them.
- The `no_drain` override was vetoing a correctly-computed Drain with no
  indication anywhere until it was added to the dashboard on 2026-07-28.
- The R11 ratchet held the floor 7.2 kWh above its formula value for a whole
  day. The ratchet value was persisted but never published.

Practically: every override, latch and fallback appears in the `reason` string
on `sensor.predbat_curtailment_intended_policy`, and any quantity that can come
from two sources publishes its source alongside its value. A mechanism you
cannot see is a mechanism you will misdiagnose — and then "fix" something else.

### The dashboard must mirror the plugin, not re-derive it

**After any change to plugin decision logic, check the "Why This Mode" card and
make it match exactly.** The card is the observability surface required above; a
card that disagrees with the plugin is worse than no card, because it is trusted.

It must **report** the plugin's decision (`intended_policy` + its `reason`), never
recompute it. On 2026-07-28 the card did its own Schmitt comparison and read
"Hold" while the plugin was in "Solar Charge Battery (override no_drain)" — it
knew nothing about the overrides. Any threshold shown on the card is context for
the decision, not the decision.

Checklist after a logic change:

1. Does the card still show every input the decision now uses?
2. Does any new override appear in the `reason` string?
3. Does the card still report rather than re-derive?

### Working practices (A0+, 2026-07-30) — stop doing it the old way

These are process rules for *how we build and operate* CM. Companion context:
`.claude/plans/curtailment-rebuild-context.md` (why / loss function) and
`.claude/plans/curtailment-review-recommendations.md` (roadmap). This subsection
is normative for agents and humans editing CM.

| Old way (do not) | New way (do) |
|---|---|
| Ship two copies of `curtailment_plugin.py` (`apps/predbat/` and `plugins/`) | **One tree only.** `plugin_system` must refuse a second load of the same `plugin_name` (hooks append; dict overwrite does not unregister). |
| **Edit stock Predbat files that a Predbat upgrade overwrites** (e.g. `config.py`, `plan.py`, `fetch.py`, `predbat.py`, core modules that come from upstream). Local "ungate this switch" patches die on the next pull and reintroduce silent breakage. | **Do not.** Put site behaviour in **our** tree only: `curtailment_*`, `*_plugin.py`, `ha/*.yaml`, tests, Charter. Prefer HA helpers/automations, plugin hooks (`on_before_plan` / `on_update`), or config *entities* (expert mode, switches). If stock must change, upstream a PR or re-apply via an explicit post-upgrade patch list — never a silent one-off on the live box. |
| Treat missing SOC as `0.0` and keep driving | **Fail closed:** unreadable plant SOC → hold position, change nothing, say so in `reason`. |
| Re-derive headroom / Schmitt / safety factor on the dashboard | **Report** plugin attrs (`reason`, band %, headroom_*, override label). Card may format; it must not invent a second decision. |
| Mix % and kWh on every line of Why This Mode | **At a glance: % SOC** for band and battery; kWh only as secondary detail. |
| Show engineer codes like `no_drain` as if they were modes | **Human labels** in `reason` / `override_label` (e.g. "surplus fits"). Keep internal codes in attrs if needed for tests. |
| Tune from April AC-coupled fixtures | **Meters:** overflow daily + overnight import (failure modes A and B). |
| Add another latch for the next flap | First ask: dual formula seam, or missing fail-closed? Prefer one function + args. |
| Ship control-law changes with footgun cleanups | **Separate deploys** so the next day's A/B can be attributed. |
| Delete a mechanism by deleting tests only | REQUIREMENTS status change + new semantic test asserting post-deletion behaviour. |
| Grow `calculate()` past the complexity ratchet | Split; lower the pin; never raise it to pass CI. |

**CM's job (one sentence):** keep battery room for PV that would otherwise be
curtailed, then hand back to Predbat. Predbat owns price, evening export, saving
sessions, and the overnight plan. Every constant trades under-drain (curtail)
against over-drain (import) — measure both.

### Complexity ratchet

`.flake8` has set `max-complexity = 15` for a long time, but **flake8 is not in
`.pre-commit-config.yaml`**, so the limit has never been enforced. Measured
2026-07-28: `calculate()` is at 49 (ruff's count), 608 lines, ~30 order-dependent
locals. Across the whole tree, 147 functions exceed 15 — but most are upstream
Predbat files that are not ours to fix.

A `ruff --select=C901` hook now gates **only the curtailment files we maintain**,
pinned at **49** — today's worst. It cannot go up. Lower it whenever a function
is split; never raise it to make a commit pass. If a change needs the number
raised, split something instead.

**Why this matters beyond tidiness.** On 2026-07-28 a one-block move inside
`calculate()` broke three things in sequence — `effective_max_reserved`,
`solcast_so_far`, `sig_daily_pv`, `max_target_soc` — because everything in that
function is shared sequential local state. Extracting a method instead worked
first time, because the dependencies became explicit parameters. Long functions
here are not a style preference; they are why edits are risky.

**Tracked follow-up:** split `calculate()` along the phase boundaries its own
comments already mark (pre-dawn/pre-PV decision, band computation, override
selection, floor computation, publish). One extraction per commit, verified
against the full suite each time.

### Why the Charter exists

Concrete failures caused by not doing the above, all confirmed live:

- **Layered supersession.** Three dated sections each claimed "this wins when in
  conflict" without reconciling the text they overrode. Result: R6 had four
  different deactivation rules, three of which still read as current.
- **Superseded text left authoritative.** R59a was withdrawn and replaced by
  R59b, but its body still read as the live spec for ~50 lines. Anyone
  implementing from it rebuilds the exact defect that was removed.
- **Citing a removed requirement.** R50a was justified on R7, which had been
  marked REMOVED elsewhere in this file. The conclusion happened to survive on
  other grounds; it might not have.
- **Same quantity, three formulas.** "Required headroom" was expressed in five
  places in three different ways. On 2026-07-28 the weakest version vetoed a
  drain the strongest version had correctly called, leaving the battery 1.67 kWh
  short of its p90 defence on a clear day.
- **Rationale contradicting mechanism.** R11 says "headroom already reserved
  cannot be reclaimed" while implementing `max()`, which raises the floor and
  therefore reclaims headroom. A stale ratchet blocked the drain for an entire
  day before anyone noticed the words and the code disagreed.
- **Doc describing code that does not exist.** The v20 delta states R9's tapered
  cap was removed. It is still in the code, and load-bearing.

---

## PART 1 — CURRENT SPEC (NORMATIVE)

## Duplicate-logic index — every quantity with more than one site

**Read this before editing any control rule.** Charter: enumerate the sites first.
Four defects on 2026-08-06 were all "this logic exists in N places and I changed
N−1". Built by grepping the *concept* (entity / threshold / policy name), not by
memory — the grep found two sites that memory had not.

| Quantity | Canonical site | Other sites that MUST agree | Guard |
|---|---|---|---|
| **Effective policy** (override > session > select) | `ha/sig_dispatch_intent_helpers.yaml` → `sensor.sig_effective_policy` (RD26) | `sig_dispatch_heartbeat.yaml` **reads** it ✓ · `sig_keep_floor_guard.yaml` **reads** it ✓ (2026-08-06) · `curtailment_plugin.py` **re-derives** it for the published policy ⚠ | `test_trigger_and_action_can_never_diverge`, `test_heartbeat_defers_to_intent_sensors` |
| **Dispatch setpoint** (incl. RD22 sell-clamp) | `sig_dispatch_intent_helpers.yaml` → `sensor.sig_dispatch_kw` (RD26) | `sig_dispatch_heartbeat.yaml` action **and** `stale_setpoint` trigger — both **read** it ✓ | `test_intent_sensors_match_reference` |
| **Who may write `input_select.sig_dispatch_policy`** | `curtailment_plugin._set_policy()` | `sig_keep_floor_guard.yaml` (keep-floor → Hold Battery; dusk → Predbat) | `test_yaml_keep_floor_guard` |
| **Who may write `input_select.sig_override`** | the human | `sig_keep_floor_guard.yaml` (clears to Off) · `sig_manual_override_failsafe_off.yaml` | — ⚠ no test |
| **Mid-window handback ban (RD27)** | `curtailment_plugin._publish_dispatch_policy` | acting path in the same function · `sig_keep_floor_guard.yaml` keep-floor branch | `test_low_soc_never_hands_back_mid_window`, `test_yaml_keep_floor_guard` |
| **`sig_drain_floor_pct`** (sell floor) | the helper itself (RD23) | `sig_dispatch_heartbeat.yaml` clamp · `sig_dispatch_intent_helpers.yaml` · `curtailment_plugin._drain_floor_kwh` · `sig_keep_floor_guard.yaml` | `test_released_floor_comes_from_the_live_helper` |
| **`OVERFLOW_SAFETY_FACTOR`** | `curtailment_plugin.py` = **1.05** | `tests/test_curtailment.py` keeps a *deliberately frozen* `V10_SIM_SAFETY_FACTOR = 1.2` for the legacy v10 scenario model — renamed 2026-08-06 because as `OVERFLOW_SAFETY_FACTOR` it **shadowed** the real one at module scope and silently produced a wrong expected value | `test_r9_safety_factor_is_1_05` |
| **Predbat→SIG register writes** | three mapper automations, not one | `predbat_requested_mode_action` · `predbat_max_discharging_limit_action` · `predbat_max_charging_limit_action` (2026-07-28) | `test_exactly_one_writer_enabled_on_handback` |
| **Grid-meter liveness** (`grid_age > 600 and poll_age < 120`) | `ha/sig_inverter_fault_alert.yaml` `meter_stale` **trigger** | the `meter_dead` **variable** in the same file's action block. HA gives triggers no access to `variables` and vice versa, so both copies are unavoidable (2026-08-10) | `test_trigger_and_variable_agree` — renders BOTH over six cases incl. the boundaries (601/119, 599/121) |

⚠ = known duplicate, no single source yet. One remains: `curtailment_plugin.py`
re-derives the effective policy for the PUBLISHED reason string. It cannot read the
sensor without a round-trip through HA on every cycle, so it stays a duplicate for
now — but it only affects display, never dispatch, and
`test_v32_saving_session_plugin_stays_active_but_delegates_dispatch` pins the
precedence it must mirror.

**Not deployed** (in repo, no HA entity — confirmed 2026-08-06): `sig_saving_session`,
`curtailment_manager_dynamic_export_limit`, `curtailment_stale_phase_watchdog`,
`voltage_seek_controller`. They are not writers today; re-check before assuming.

## Register ownership audit (2026-08-06)

Every SIG control register: who writes it, and what unwinds it on each transition.
Built by enumeration, not recall, after four stranded-state defects in one day.

| Register | Written by | On CM → Predbat | On Predbat → CM |
|---|---|---|---|
| `remote_ems_controlled_by_home_assistant` | heartbeat (ON only, **never** off — RD2) | left ON ✓ | left ON ✓ |
| `remote_ems_control_mode` | heartbeat (PCS Remote Control) · `predbat_requested_mode_action` (MSC / Command Charging / Command Discharging) | `_park_ems_msc()` → MSC ✓ **plus** RD24 self-heal on any beat | heartbeat → PCS ✓ |
| `active_power_fixed_adjustment` | heartbeat only | **not unwound** — left at last dispatch. Inert under MSC, and Predbat never selects PCS, so benign | overwritten each beat ✓ |
| `grid_export_limitation` | heartbeat (= DNO cap) | left at cap — always correct, benign | re-asserted ✓ |
| `grid_import_limitation` | heartbeat (→100) · `predbat_requested_mode_action` (→0 on Freeze Charging) | left open; Predbat owns it again | heartbeat re-opens ✓ (5bbdedeb) |
| `ess_max_discharging_limit` | heartbeat (→rated) · `predbat_max_discharging_limit_action` | left open; Predbat owns it again | heartbeat re-opens ✓ |
| `ess_max_charging_limit` | heartbeat (→rated) · `predbat_max_charging_limit_action` | left open; Predbat owns it again | heartbeat re-opens ✓ |
| `ess_discharge_cut_off_state_of_charge` | **nobody** — 0% by design | — | — |
| `input_number.discharge_rate` / `charge_rate` | heartbeat mirrors to rated · Predbat plans them | `_neutralise_predbat()` **not** called this way ⚠ | `_neutralise_predbat()` ✓ |

**The asymmetry is deliberate and correct.** `_neutralise_predbat()` runs only on
Predbat → CM: Predbat's own mappers unwind Predbat's own registers *before* that
chain is disabled ("the writer that changed a register changes it back"). Going the
other way there is nothing to unwind — every CM register is either re-owned by
Predbat's mappers or inert under MSC.

### Known gap — the guard writes selects but cannot swap the writer role

`sig_keep_floor_guard` writes `sig_dispatch_policy` / `sig_override` directly. It
cannot call `_set_writer()`, so on its DUSK branch the select says `Predbat` while
the heartbeat is still ENABLED and the mappers still DISABLED, until the plugin's
next cycle runs `_release_to_predbat()`. In that window the heartbeat is inert (its
Predbat branch) and the mappers cannot write: **nobody drives, for up to 5 minutes.**

Same shape as the 2026-08-06 04:50→09:00 dead zone, just bounded. Not fixed. Options:
have the guard fire the plugin instead of writing selects, or give the writer-role
swap its own automation triggered by `sensor.sig_effective_policy`. Decide before
next winter — the dusk branch runs every night.

## Status index — what is actually in force

**Check this table before citing any requirement.** It was built on 2026-07-28 by
reading the code, not the prose, because several requirements had been marked
removed while still running and vice versa. Status here beats status anywhere
else in this file.

| ID | Status | Notes / where it lives |
|---|---|---|
| R1 | **AMENDED** | Export cap is now **3.68 kW** (DNO, post-swap) and is **hardware**-enforced by the SIG MPPTs. The old software cap and SMA backstop are retired (v30). |
| R2, R3 | IN FORCE | R3 = `read_only` is the CM↔Predbat mutex. |
| R4 | **IN FORCE, GATED** | `should_defer_to_charge` returns False unless GSHP heating is active. v20 lists R4 "kept unchanged" — that is stale. |
| R5 | IN FORCE (v20 form) | "Is there work to do?" |
| R6 | IN FORCE (**v32 form**) | safe_time no longer *deactivates* — it drives the Hold override. Two earlier rules (v20 sundown, v30 safe_time-handback) are superseded; see History. |
| R7 | ❌ **REMOVED** | Superseded by R53. **Do not cite as authority.** |
| R8 | IN FORCE | |
| R9, R9a | IN FORCE | **Tapered cap IS present** (`curtailment_plugin.py:1624`), contradicting v20's claim it was removed. |
| R10 | IN FORCE (v20 form) | `max(min(curtailment_floor, effective_keep), reserve, deep floor)` |
| R11 | ❌ **REMOVED** | Floor ratchet. Rationale contradicted mechanism; escape hatch (R43) gone; locked the floor for a full day on 2026-07-28. |
| R12 | ❌ REMOVED | v20. |
| R13 | IN FORCE | |
| R14–R18, R38 | ❌ **REMOVED** | The 5-second three-phase export-limit automation is retired (v30, DC-coupled). |
| R19, R20, R21 | IN FORCE | safe_time is a control input again (drives Hold), not just a diagnostic. |
| R25 | IN FORCE | "Worst case, act early". Geometry owns **timing**, Solcast per-slot owns **energy** — resolved 2026-07-28 on 11-fixture replay. |
| R26–R30 | IN FORCE | |
| R34–R37 | IN FORCE | Testing discipline. R36 (TDD) and R37 (never break production for tests). |
| R39 | ❌ **REMOVED** | Restated R11's ratchet; removed with it. |
| R42 | **IN FORCE** | p90 scale from Solcast. v20 demoted it to a calibration knob for R58; the code still uses it structurally as one half of R43's max(). |
| R43 | **IN FORCE** | `floor_scale = max(p90_scale, actual_scale)` is live at `curtailment_plugin.py:1414`. v20 says R58 replaced it; the code kept BOTH. Observed live 2026-07-28 18:00: actual 11.9 > p90 8.5, floor using actual. |
| R44 | IN FORCE | |
| R45 | **IN FORCE** | v20 says "removed, replaced by R57" — **wrong**, the tapered buffer is live and load-bearing. |
| R46 | ❌ REMOVED | v20 lists it under *both* Amended and Removed; Removed is correct. |
| R47 | IN FORCE | State persistence. |
| R48, R49 | IN FORCE | |
| R50 | **DORMANT** | Code retained; re-enabled by setting `curtailment_confidence_high` < 1.0. Not the live path. |
| R50a | IN FORCE | Live path is `overflow_p90`. **Its citations of R7/R42/R43 are to dead requirements** — the conclusion rests on R25's worst-case logic instead. |
| R52 | IN FORCE | Pre-PV drain. Already contains a time-to-drain calculation — the ancestor of R63. |
| R53 | IN FORCE | Solcast per-slot is primary for the energy estimate; beats geometry on 11/11 fixtures (mean error 10.77 vs 15.71 kWh). Smoothed by R64. |
| R54, R55 | IN FORCE | |
| R56 | ❌ SUPERSEDED | by R6/RD6 — CM must not own the evening. |
| R57, R58 | IN FORCE | |
| R59 | ❌ SUPERSEDED by R59b | |
| R59a | ❌ **WITHDRAWN** | Circular reasoning; blocked the morning drain. **Do not implement.** |
| R59b | IN FORCE | Recovery floor nets against P10 **generation**. |
| R60, R61, R62 | IN FORCE | |
| R63 | **RETIRED as a lever, 2026-08-10** | Drain deadline. The measurement is kept and published (`headroom_deadline_short_kwh`); the Max Export override is gone. See below. |
| R64 | IN FORCE | Rolling median on the overflow estimate (2026-07-28). |
| RD14c | IN FORCE | Saving sessions driven by the Octoplus **calendar** + native calendar triggers, not the lagging binary sensor (2026-07-28). |
| RD1–RD20 | IN FORCE | v30/v32 DC-coupled control layer — see Part 1 sections at the end. |
| RD21–RD28 | IN FORCE | v33 (2026-08-06): dawn reserve survives PV start; drain floor stops selling, not load-covering; one floor helper; Predbat branch self-heals PCS Remote Control; trigger/action equivalence enforced by test; **RD26 single point of truth** — dispatch intent defined once in `ha/sig_dispatch_intent_helpers.yaml`; **RD27 no mid-window handback** (RD4 'A' retired); **RD28 bank to tonight's need once no overflow is left**. |
| RD13a | IN FORCE | Manual override is ONE select (`input_select.sig_override`); the boolean is deleted (2026-07-28). |

## Goal

Minimise curtailment — PV that cannot be used because it exceeds the export cap
and the battery cannot absorb it — while ensuring the battery holds enough by
dusk to cover overnight load without importing at peak rates.

**Why this wording:** CM does exactly one thing. Predbat owns price, evening
export, saving sessions and the overnight reserve. CM has the wheel only inside
the curtailment window. Every past overreach (R56 evening drain, R59a's
charge-from-dawn) came from CM taking on a £-optimisation that was Predbat's.

Export cap = **3.68 kW** (DNO, hardware-enforced since the 2026-07-15 DC-coupled
swap). Earlier text saying 4 kW with software/SMA backstops is historical.

## Key Design Principle — worst case, and act early

**R25**: Headroom is **cheap to create early and impossible to create late**.
Once `PV − load > export_cap` there is no lever left: we export flat out at the
cap and the battery still fills from the excess. Therefore every decision is
biased toward creating headroom sooner, and toward the *worst-case* (p90)
forecast rather than the expected one.

**Why:** an over-drain costs one battery round-trip (~10%). An under-drain costs
the entire clipped surplus, and cannot be undone. The asymmetry is what justifies
the safety factor (R9), the p90 band, the pre-PV drain (R52), and the drain
deadline (R63).

**Removing this would:** reintroduce "wait and see" behaviour that looks correct
on marginal days and silently curtails on the days that matter — exactly what
R50's confidence blend did before R50a retired it.

**Geometry and Solcast have separate jobs** (resolved 2026-07-28).

| Question | Model | Why |
|---|---|---|
| **When** the overflow window opens/closes, and how much drain capacity is left | **Geometry** | The smooth curve is stable and monotone — exactly what timing needs. Feeds safe_time (R19), T_lockout and `max_sheddable` (R63), and the pre-PV drain start (R52). |
| **How much** overflow energy | **Solcast per-slot p90** (R53) | Keeps the forecast day-shape. Geometry as fallback only, below 4 detailed slots. |

**Why not geometry for the energy too**, despite it being the more conservative-
looking choice: geometry scales a *clear-sky* curve off the single highest p90
slot, producing a fictional day worse than the p90 forecast itself. Replaying 11
April/May fixtures, geometry over-predicts above-cap overflow on **11/11 days**,
mean error **15.71 kWh** against Solcast per-slot's **10.77 kWh**.

On 2026-04-28 geometry said **21.54 kWh** where the actual above-cap overflow was
**2.60 kWh** and Solcast said 7.56. That 8x over-estimate is what drove the ~9.5
kWh drain that left the battery at ~2% with 5.76 kWh of load still to cover. A
replay of that day at every starting SOC from 2.8% to 100% curtails **0.00 kWh** —
there was nothing to make room for at all.

Solcast p90 per-slot *is* the worst case, at every slot, and it keeps the shape.
That serves "protect against the maximum" better than a fiction that is worse
than the worst case.

**Why:** we are protecting against the *maximum*, and once PV − load exceeds the
export cap there are no levers left. The smooth `scale × sin(elev)` curve gives a
stable worst-case envelope. Solcast per-slot values move a lot cycle-to-cycle, and
driving the floor from them made the whole system jump around.

**Removing this would:** reintroduce a floor that chases per-slot noise, so the
drain decision changes every cycle and the headroom commitment is never stable.

**Known conflict — R53 currently contradicts this in code.** `_compute_overflow_band`
uses Solcast per-slot whenever `detailed` has ≥ 4 slots and only falls back to
geometry below that (`curtailment_plugin.py:909`), i.e. Solcast is primary in
practice. R53 was introduced in v20 to fix a specific failure: `actual_scale`
being extrapolated across the whole day (clear morning → cloudy afternoon →
16 kWh of phantom overflow). **That failure mode is already gone** — R43 was
replaced by R58 and `floor_scale = p90_scale` unconditionally
(`curtailment_plugin.py:1050`), so nothing extrapolates a live scale any more.
R53's original justification therefore no longer applies.

Restoring geometry as primary is a live behavioural change and is tracked as
follow-up work, not done inline. See *Open questions*.

## Safety

- **R1**: Export never exceeds DNO (4kW). SIG faults at 4.5kW. SMA backstop at 4.25kW.
- **R2**: On error, deactivate cleanly: release control (policy → Predbat) and clear
  read_only so Predbat resumes.
- **R3**: **read_only is the CM↔Predbat mutex** (v30). CM sets `base.set_read_only = True`
  only while it is *actively driving* a non-Predbat policy (Max Export / Hold Battery /
  Solar Charge Battery), i.e. inside the curtailment window. It clears read_only the
  moment it hands back — at the low-SOC handover (R4/RD4), at safe_time (R6/RD6), or on
  deactivation — so Predbat resumes owning the machine. In observe-only (policy-control
  gate off) CM never sets read_only. CM's *only* job is curtailment; Predbat owns
  price, evening export, saving sessions, and overnight reserve, so CM must not hold
  read_only outside its window.
- **R4**: Defer to Predbat charge windows when SOC < soc_keep and charge window active.

## Activation

- **R5**: Activate when BOTH conditions are true:
  1. `remaining_overflow > 0` — solar geometry curve (R9) predicts overflow.
  2. `solcast_remaining - load_remaining > (soc_max - soc_kw)` — total PV exceeds
     what is needed to fill the battery. If the battery won't reach 100% even with
     all the PV, the overflow energy is needed for charging — do not activate.
- **R6**: **Deactivate at safe_time (R19), handing the whole machine back to Predbat**
  (RD6). Once `now >= safe_time`, PV can no longer exceed the export cap, so there is
  no curtailment to manage — CM releases (read_only False, policy → Predbat) and
  Predbat owns the evening: excess export, saving sessions, and the drive to overnight
  reserve. **This supersedes R56** — CM must NOT stay active past safe_time to drain
  the battery to an overnight target; that is a £-optimisation and belongs to Predbat,
  not CM. Sundown (PV ≤ 0.1 after peak) remains only as a BACKSTOP for days where
  safe_time can't be computed. Pre-PV drain (R52/R62) is the one place CM acts outside
  the live-overflow window and STAYS with CM — it creates curtailment headroom, which
  Predbat cannot do.
- **R7**: No activation from per-slot forecast scan. Solar geometry and Solcast p90 only.
- **R8**: When inactive, Predbat manages normally.

## Scale — Worst-Case Clear Day

- **R42**: At activation, derive scale from Solcast p90 forecast:
  `scale = p90_peak_kw / sin(elevation at p90_peak_time)`
  This represents a near-perfect solar day — the worst case for overflow headroom.
- **R43**: `floor_scale = max(p90_scale, actual_scale)` — asymmetric use of actual_scale
  for the floor:
    - When `actual > p90` (day sunnier than forecast): use actual_scale. Bigger overflow
    estimate → lower floor → more drain → safer. Protects against the 10% of days that
    exceed the p90 forecast.
    - When `actual < p90` (day cloudier): keep p90_scale. Afternoon could still clear,
    and using a low actual_scale at peak hour would drop the floor (violating R11 spirit)
    and under-provision for a late-day p90 outcome.
  Previously the floor always used p90_scale. This under-estimated overflow on sunny
  days where actual PV exceeded p90, risking DNO breach.
  actual_scale also drives safe_scale (R21): cloudy day → earlier safe_time → earlier
  MSC handoff to recover battery; clear day → later safe_time (conservative).
- **R44**: Before today's peak is observed, use yesterday's scale as fallback if
  Solcast p90 is unavailable. Scale changes slowly day-to-day (~1° elevation per day).

## Floor — Solar Geometry Integral

- **R9** (v19 tapered cap): `remaining_overflow = ∫ max(0, scale × sin(elev(t))
    - effective_load(t) - DNO) dt` integrated from now to safe_time (R19).
  Evaluated each 5-minute plugin cycle.

  ```text
  buffer_kwh       = min(MAX_RESERVED_KWH, remaining_overflow)  # MAX_RESERVED_KWH = 1.8
  max_target_soc   = soc_max - buffer_kwh
  overflow_floor   = max_target_soc - remaining_overflow × OVERFLOW_SAFETY_FACTOR
  ```

  `OVERFLOW_SAFETY_FACTOR = 1.05` (was 1.2 until 2026-07-30). The tapered cap
  (R45) only binds when `remaining_overflow ≥ 1.8 kWh` (peak of day); near
  safe_time the buffer tapers toward 0, `max_target_soc` approaches soc_max, and
  the battery fills to ~100% before handoff to MSC.

  **Why 1.05 and not 1.2.** The factor multiplies an already-conservative input.
  Overflow is fed from the **p90** band, and overflow is an integral *above a
  threshold*, so forecast conservatism is amplified before the factor applies at
  all. Measured across the April fixture replay: a 13% generation over-forecast
  became a **36%** overflow over-forecast (3.36× leverage), and actual overflow
  never once exceeded the p90-derived estimate in 11 days. 1.2 on top of that
  reserved roughly **double** the headroom actually needed.

  Over-reserving is **not free** — it is paid for as a deeper pre-PV drain (R52)
  and the overnight import that follows. That is the cost being traded against
  curtailment risk, and it is a real observed cost, not a theoretical one.

  **Honest caveat on the evidence.** Those April fixtures were measured through
  the AC-coupled SMA, which clipped PV above the inverter ceiling and therefore
  *understated* actual overflow — flattering p90. The first DC-coupled day
  measured (19 Jul) showed only **16%** margin against p90 versus **56%** mean in
  April. 1.05 is a deliberate step toward the truth, not a settled number.

  **How to refine — use the meters, do NOT re-derive from fixtures.** Since
  2026-07-29 actual overflow is metered natively and exactly:

  ```text
  sensor.curtailment_overflow_power    template, max(0, pv - load - cap)
  sensor.curtailment_overflow_energy   Riemann integral of the above
  sensor.curtailment_overflow_daily    utility_meter, daily cycle
  ```

  The chain applies the clipping at native sensor resolution and only then
  integrates, so the daily total survives HA's hourly downsampling exactly.
  Reconstructing from 5-minute statistics instead understated a broken-cloud day
  by **63%**, and that data expires with the ~10-day recorder window.

  To retune: compare `sensor.curtailment_overflow_daily` (actual) against the
  daily max of `sensor.predbat_curtailment_overflow_p90` (forecast) across a few
  weeks of DC-coupled days. The factor should cover the worst observed
  `actual/p90` ratio with a little margin — cut it further if actual never
  approaches p90, raise it if any day exceeds p90.

  **The number that ultimately matters is neither of those.** It is whether we
  ever actually curtailed (SOC at max *and* export at cap) versus how much we
  imported overnight. Forecast calibration is only a proxy for that trade.

  **Implemented in:** `curtailment_plugin.py:OVERFLOW_SAFETY_FACTOR`,
  `curtailment_calc.py:required_headroom_kwh`.
  **Tested by:** `test_R9_overflow_safety_factor_is_1_05`.
- **R9a**: `effective_load(t) = max(base_load, loadml_forecast(t))` — the overflow
  integral MUST use Predbat's LoadML per-slot forecast with `base_load` (0.5 kW) as
  a floor. LoadML already learns regular daytime loads (DHW cycle, EV charging,
  cooking). Those absorb PV directly and reduce the overflow needing export headroom.
  Reason: with only the 0.5 kW flat constant, the formula overestimated overflow by
  1–2 kW × ~10 daylight hours on normal days, forcing unnecessary drain and lower
  sunset SOC. See also `feedback_use_loadml_for_floor.md`.
- **R10**: `floor = max(floor, effective_keep, reserve)` — never drain below
  household needs. `effective_keep` is normally `soc_keep` but can be relaxed
  to 0.5 kWh under R48 conditions.
- **R48**: Relaxed soc_keep on big-overflow mornings. When BOTH (a) the forecast
  overflow × safety_factor exceeds room available with base keep, AND (b) PV
  currently exceeds load by ≥ 0.5 kW, use `effective_keep = 0.5 kWh` instead
  of `soc_keep`. **Two-phase recovered latch** — battery must first be observed
  BELOW `soc_keep` this day (sets `_keep_drained_today = True`) before
  `_keep_recovered = True` can latch on SOC rising back to `soc_keep`. Without
  the drain-first guard, the latch fires at midnight rollover when battery is
  at 100% overnight, defeating R48 on every real morning.
  **Engagement latch** (`_r48_engaged_today`) — once R48's first-fire conditions
  are met today, latch on so subsequent cycles use relaxed keep regardless of
  pv_covering oscillation around the 0.5 kW threshold. Avoids effective_keep
  toggling 0.5 ↔ 1.5 kWh in cloudy mornings (5 toggles observed 2026-04-25
  06:11–09:58 BST before this fix). Engagement latch clears when
  `_keep_recovered = True` (drain cycle complete). All three flags persisted
  via state file; reset on day rollover.
- **R11** — ❌ **REMOVED 2026-07-28.** See History for the original text and the
  reasoning behind removal.

  **What it did:** clamped the overflow-derived floor with
  `overflow_floor = max(overflow_floor, previous_floor)`, so the floor could only
  ever rise within a day, bypassed only when `floor_scale` increased.

  **Why it was removed:**
  0. **Correction (2026-07-28):** reason 2 below asserted R43 was gone. It is
     not — `floor_scale = max(p90_scale, actual_scale)` is live at
     `curtailment_plugin.py:1414`, so the ratchet's bypass DOES still have a
     trigger. Reasons 1, 3 and 4 stand on their own and the removal is still
     correct: the mechanism contradicted its rationale, it locked the floor for
     a full day on 2026-07-28, and floor stability comes from stable inputs.
     But the removal was argued partly from a false premise — recorded here
     rather than quietly fixed.
  1. **Its rationale contradicted its mechanism.** It was justified as *"headroom
     already reserved cannot be reclaimed"*, but a *rising* floor means draining
     to a *higher* SOC — i.e. reserving *less* headroom. The stated intent would
     have required `min()`.
  2. **Its escape hatch no longer exists.** The bypass fired when `floor_scale`
     rose (R43). R43 is gone — `floor_scale = p90_scale` unconditionally — so the
     ratchet could never release, whatever the forecast did.
  3. **It caused a real failure.** On 2026-07-28 it locked the floor at 15.76 kWh
     (87%) from an early-morning moment when remaining overflow was 0.44 kWh, and
     held it there all day while p90 overflow rose to 12.28 kWh. Formula value was
     8.55 kWh (47%). No drain could fire until the persisted value was cleared by
     hand.
  4. **Whatever it was originally defending is handled elsewhere.** Floor
     stability now comes from the inputs being stable: `floor_scale` is p90-derived
     and does not jump, and R25 keeps the integral on the smooth geometry curve.

  **Removing this would (i.e. what we lose):** nothing identified. If a
  floor-instability failure reappears, fix it at the input that is unstable —
  do not reinstate a one-way clamp on the output.
- **R46**: Deactivation uses `safe_time`, not the forecast integral. Plugin goes
  Off only when `now >= safe_time` (solar geometry past overflow threshold) or
  when the battery-fill check fails. The LoadML-driven integral can under-
  estimate overflow (phantom afternoon load pushes predicted overflow to zero
  even while sun is still above threshold), which would cause premature
  deactivation and lose R45 protection during the last chunk of the overflow
  window. Activation still uses the integral (need forecast confidence to
  start draining the battery). R25/R19 solar geometry is ground truth.
- **R47**: Persist state `{date, peak_pv_kw, peak_pv_time, floor_ratchet,
  last_floor_scale}` to `curtailment_state.json` under `config_root`. Load
  on plugin init if date matches today; ignore if stale. Prevents restarts
  from losing observed peak_pv (and therefore actual_scale → safe_scale →
  safe_time) mid-day. Test environments without `config_root` skip
  persistence to avoid cross-test pollution.
- **R45** (v19): Reserved headroom = `min(effective_max_reserved, remaining_overflow)`.
  `MAX_RESERVED_KWH = 1.8` (10% of soc_max) — same ceiling as the previous
  hardcoded 90% cap. `effective_max_reserved` is normally `MAX_RESERVED_KWH`
  but may be reduced by R49 on confirmed-cloudy afternoons. The reservation
  tapers with `remaining_overflow`:
    - Peak overflow (`remaining ≥ effective_max_reserved`): buffer clamps at
    `effective_max_reserved`, target is `soc_max - effective_max_reserved`.
    Full CLS safety during the window where LoadML over-prediction could
    inflate real overflow.
    - Tail of overflow (`remaining < effective_max_reserved`): buffer =
    `remaining`, target rises toward 100%. Physical PV is already near
    DNO+load so LoadML surprise is bounded by the PV curve itself.
    - At safe_time (`remaining = 0`): buffer = 0, target = soc_max. Battery full
    before MSC handoff. Avoids the old trade-off of ending 92–95% on thin
    post-release tail days where MSC can't refill the 10% reserve from sparse
    evening PV.
- **R50** (v21 confidence-weighted overflow) — ⚠️ **DORMANT, not the live path.**
  Retired by R50a; code retained and re-enabled by setting
  `curtailment_confidence_high` < 1.0. Read R50a before changing anything here.
  The floor formula uses a
  confidence-weighted blend of three forecast bands instead of always-p90.
  Solcast publishes pv_estimate10 / pv_estimate (P50) / pv_estimate90 per
  slot, plus an `analysis.confidence` value (0..1). The plugin computes
  three overflow integrals using each band's scale, then blends them by
  confidence:

  ```text
  p10_scale = max(p10_peak / sin(elev_at_peak), actual_scale)   # R43 still applies
  p50_scale = max(p50_peak / sin(elev_at_peak), actual_scale)
  p90_scale = max(p90_peak / sin(elev_at_peak), actual_scale)

  overflow_p10 = ∫ max(0, p10_scale × sin(elev) − load − DNO) dt
  overflow_p50 = ∫ max(0, p50_scale × sin(elev) − load − DNO) dt
  overflow_p90 = ∫ max(0, p90_scale × sin(elev) − load − DNO) dt

  c = clamp(confidence, 0, 1)
  HIGH = input_number.curtailment_confidence_high   (default 0.85)
  LOW  = input_number.curtailment_confidence_low    (default 0.60)

  if c >= HIGH:           expected = overflow_p90        # pre-R50 behaviour
  elif c >= LOW:          t = (c − LOW) / (HIGH − LOW)
                          expected = (1−t)*p50 + t*p90
  else:                   t = c / LOW
                          expected = (1−t)*p10 + t*p50
  ```

  `expected` then substitutes for `remaining_overflow` in R9's floor
  formula. R45 buffer, R49 buffer reduction, R11 ratchet, R43 actual_scale
  promotion all still apply on top of the blended estimate.

  Why: at low confidence, the p90 forecast isn't trustworthy and committing
  to p90 drain wastes battery on round-trip losses. The blend leans toward
  pessimistic estimates when forecast quality is low.

  Reference incident: 2026-04-28. Plugin drained ~9.5 kWh on a forecast
  where Solcast reported confidence 0.69 and spread 25 kWh (P10=14, P50=31,
  P90=49). Day delivered ~5 kWh PV. Round-trip loss ~1.4 kWh + import cost.
  Battery hit 1.9%. Under R50 with c=0.69, expected ≈ 0.36 × p90 + 0.64 ×
  p50 — drains modestly, doesn't bottom-out battery.

  Default fallback: when Solcast doesn't expose `analysis.confidence`
  (test environments, data unavailable), treat as 0.9 → use overflow_p90
  exactly as pre-R50. R50 only changes behaviour when real confidence data
  is present and below HIGH threshold.

  Tunable thresholds via two input_number helpers (curtailment_confidence
  _high, curtailment_confidence_low) exposed on the dashboard. Constraint
  enforced in plugin: 0 ≤ low < high ≤ 1.

### R50a — confidence blend RETIRED as the live path (2026-07-28)

**R50 is superseded for the floor calculation. The live path returns to
`overflow_p90` per R7/R42/R43.** The blend code and both tunable helpers stay in
place; setting `input_number.curtailment_confidence_high` below 1.0 re-enables
R50 from the dashboard with no code change.

**Why R50 contradicted the core design.** R25: headroom is cheap to create early
and impossible to create late, so every estimate is biased to the worst case. An
over-drain costs one round-trip; an under-drain costs the whole clipped surplus
and cannot be undone. p90 is the worst-case band, and that asymmetry is the
entire basis of the floor.

> **Citation correction (2026-07-28, twice).** This section originally cited R7,
> R42 and R43. R7 *is* dead (superseded by R53) and citing it was wrong. R42 and
> R43 were then ALSO marked dead here — which was itself wrong, from reading a
> single assignment (`curtailment_plugin.py:1101`, the pre-PV path) and
> generalising. The post-PV path at `:1414` still runs
> `floor_scale = max(p90_scale, actual_scale)`; observed live the same evening
> with actual 11.9 against p90 8.5. **R42 and R43 are in force.** The conclusion
> holds on R25 and on R43's stated asymmetry; only the R7 citation was bad.

R50 inverted exactly that: on a low-confidence day it blends toward p10 — i.e.
assumes *no* overflow — which is the one assumption R25 forbids.

**Why its justification does not hold.** R50 cites 2026-04-28: drained ~9.5 kWh,
day gave ~5 kWh, battery hit 1.9%. Replaying that day's Solcast fixture through
the current floor formula:

```text
2026-04-28   overflow_p10 1.51   overflow_p90 7.56
             pure-p90 floor -> drain_above 7.21 kWh (39.9%)
             R59b          -> charge_below 4.00 kWh (22.1%, carried by soc_keep;
                              the recovery floor is 0 with a PV runway ahead)
             band [22.1%, 39.9%] -> drains to 39.9%, then Holds
```

The p90 estimate floors that day at **39.9%**, not 1.9%. So the p90 overflow
estimate cannot have caused the bottom-out — the floor formula already prevented
it. The true cause was elsewhere (most likely R48 relaxing `effective_keep` to
0.5 kWh, or the pre-PV target), which R50 does not address. R50 made the overflow
estimate pessimistic to fix a failure the floor was already handling.

Caveat: that is today's formula and constants replayed on April's forecast. April's
`effective_keep`/R48 state is unknown, so this shows the p90 estimate was not the
cause — not what was.

**What R50 actually cost.** Across the 11 April/May fixtures, all variants agree on
8 days (drain to ~3%). They diverge only on LOW-overflow days, where the blend says
*hold*. So R50's whole practical effect is "do not drain on marginal days" — exactly
the days where the forecast under-calls and the result is curtailment.

Observed 2026-07-27 and 2026-07-28, two consecutive clear days:

```text
              forecast overflow   actual
2026-07-27    2.35 kWh            ~13 kWh absorbed above the cap
2026-07-28    0.92 kWh (blended)  p90 said 13.03 kWh
```

On 2026-07-28 at 09:00, SOC 8.05 kWh with 10.03 kWh headroom against a p90 overflow
of 13.03 kWh — already short — and the blend still said Hold:

```text
R50 blend (c=0.35)   expected  0.92 -> drain_above 16.07 kWh (88.9%) -> HOLD
pure p90 (R7/R42/R43) expected 13.03 -> drain_above  0.64 kWh ( 3.6%) -> MAX EXPORT
```

**Over-drain protection does not need R50's pessimism.** It comes from two places,
neither of which is the overflow estimate being deliberately wrong:

1. **`drain_above` itself** — the p90 floor stops 2026-04-28 at 39.9%, as above.
   This is the protection that matters, because it limits how far we drain in the
   first place.
2. **The R59b recovery floor rising through the day** — as remaining generation
   shrinks the floor climbs, crossing SOC and forcing Solar Charge. Time-aware,
   so a day that starts bright and then under-delivers pulls us back up.

An earlier revision of this section claimed over-drain protection was "R59a's job,
and only R59a's". That was overstated on both counts: R59a's static floor was not
the only mechanism, and (per R59b) it bought its protection by blocking the morning
drain entirely. Note also the limit of mechanism 2 — Solar Charge can only bank PV
that actually arrives, so on a genuinely collapsed day it cannot fully recover a
battery that was over-drained at dawn. That is mechanism 1's job.

**Known consequence.** On marginal days p90 can put `charge_below` ABOVE
`drain_above` (e.g. 2026-05-02: 29.2% vs 15.3%) — the cross-over case. This is
more common under p90 than under the blend.

**Precedence on cross-over: DRAIN WINS. Curtailment defence beats deficit
insurance.** `compute_proposed_phase` charges to `min(charge_below,
drain_above)`, so on a cross-over day the charge target *is* `drain_above`: we
top up only to the headroom floor, and Drain fires above it.

*Why* — R25. Headroom is cheap early and impossible late: once `PV - load > DNO`
there are no levers left, so surrendering headroom to bank the evening reserve
buys insurance against a deficit that may not happen, at the price of
curtailment that then cannot be avoided. The asymmetry decides it — an
over-drained battery is recoverable (Solar Charge banks PV as it arrives, and
Predbat can grid-charge overnight if the deficit forecast holds), whereas
curtailed generation is gone. Note the limit of that recovery: Solar Charge can
only bank PV that actually arrives, so on a genuinely collapsed day it cannot
fully refill a battery over-drained at dawn — that is the pre-PV drain target's
job (R52/R62), not the phase logic's.

**Corrected 2026-07-30.** This paragraph previously read *"Charge wins, no
Drain — so it degrades safely"*, describing behaviour the code does not have and
inverting the actual rule. The `min()` and its test predate the correction; only
the prose was wrong. It caused a live misdiagnosis: the stale text was read back
as current behaviour and a code change was nearly made to "fix" logic that was
already correct. Doc-vs-code drift in the direction of the doc being *wrong
about a safety rule* is the most expensive kind — see the Charter.

**Implemented in:** `curtailment_calc.py:compute_proposed_phase`.
**Tested by:** `test_proposed_phase_cross_over_charges_to_lower_threshold`.

- **R52** (v22 pre-PV drain timing): activate the plugin BEFORE sunrise on
  confirmed-overflow days so we drain at full DNO rate while drain capacity
  is uncontested by PV. Two-stage drain:
    - Pre-PV: target = `soc_keep + buffer_pct × soc_max` (default 20%)
    - Post-PV: target = R50 floor (existing behaviour)

  Decision flow inside the existing "no PV yet" early return:

  ```text
  if input_boolean.gshp_ch_active is on:
      # Winter — protect overnight battery for heat pump load
      Off
  if overflow_p90 < 1 kWh:
      # No meaningful overflow forecast → no need to drain
      Off
  if SOC ≤ target_at_pv_start:
      # Already at/below pre-PV target
      Off

  pv_start_utc = compute_pv_start_time(p90_scale, ..., threshold=0.5 kW)
  drain_amount_kwh = SOC_now − target_at_pv_start
  drain_minutes = drain_amount / DNO × 60
  drain_start_utc = pv_start_utc − drain_minutes

  if now < drain_start_utc:
      Off (waiting; not enough time would have been wasted)
  else:
      Active, target = target_at_pv_start (pre-PV drain phase)
  ```

  After PV starts (`actual_pv ≥ 0.1`), normal flow resumes — R50 floor calc
  applies and battery drains further to the deeper R50 floor. The pre-PV
  drain only handles the FIRST stage (high SOC → target_at_pv_start).

  Why two-stage: pre-PV drain rate is 4 kW (DNO uncontested). Post-PV drain
  rate falls as PV ramps (PV uses DNO bandwidth). Splitting the drain target
  exploits this — coarse drain pre-PV, fine drain post-PV.

  Helpers:
    - `input_boolean.gshp_ch_active` — central heating active flag (manual
      toggle in pump room, or HA dashboard tile).
    - `input_number.curtailment_pre_pv_buffer_pct` — buffer above soc_keep
      (default 20, range 0-50).

  Reference incident: 2026-04-29. Plugin activated only at first PV (~05:12
  BST), drained from 70% → 24% during 05:12-08:12 BST. Should have started
  ~03:30 BST and finished pre-PV drain at PV start (~05:00 BST), with
  remainder draining post-PV — overall same total drain but no wasted
  capacity in the first 1.5 hours.

  Why the buffer (not drain to 0% pre-PV): if PV is delayed by clouds,
  battery has 3.6 kWh = 7h of base load buffer. Without it, plugin could
  drain to 0% then bleed via base load before sun arrives.

- **R49** (v20 dynamic buffer reduction): on confirmed-cloudy afternoons,
  scale `effective_max_reserved` down to `max(0.5, MAX_RESERVED_KWH × 0.7)`
  = 1.26 kWh. Reduction fires only when ALL hold:
    1. `minutes_now ≥ 14:00 local` — DHW typically done, peak likely past.
    2. `solcast_so_far > 10 kWh` — enough sunlight elapsed to make ratios
       statistically meaningful.
    3. `cumulative_ratio = SIG_DAILY_PV / (SOLCAST_TODAY − SOLCAST_REMAINING)
       < 0.9` — actual PV tracking ≥10% under forecast for the whole day.
    4. `recent_ratio = (Δ actual PV last 60 min) / (Δ solcast_so_far last
       60 min) < 0.95` — the most recent hour confirms the trend. Without
       this gate, the reduction would mis-fire when clouds clear after 15:00
       (cumulative still low, but afternoon will deliver).
  Why: Solcast over-forecasted today → the headroom we're reserving for an
  overflow that isn't materialising is wasted SOC. Reducing buffer raises
  max_target_soc by ~3%, letting the battery aim higher rather than ending
  the day with avoidable shortfall. The 0.7× factor (not 0.5×) and the 0.5
  kWh floor keep some safety margin against late-afternoon clearing. Reason
  for codifying: 2026-04-26 the day under-delivered on PV; with full 1.8
  kWh buffer the plugin held at ~93% target while battery was actually
  going to fill to 100% — user manually overrode to Charge. This rule lets
  the plugin decide automatically.
  PV history is kept in-memory only (rolling 75-min window) — after a plugin
  restart we wait one hour before recent_ratio is available. Cumulative
  ratio still works immediately on restart, but the gate requires both.
- **R12**: At safe_time, remaining_overflow = 0, floor = soc_max. Plugin deactivates.
- **R13**: Floor rises naturally each cycle as the integral shrinks (time passing,
  sin(elev) falling). Rises faster on cloudy days (actual peak < p90 → scale updates
  down → integral smaller → floor higher sooner).

## Control — Three Phases (HA automation, 5-second cycle)

Phase selection uses **Schmitt-trigger hysteresis**: the OUT transition (entering
Drain or Charge from Hold) requires SOC to exceed an outer threshold;
the IN transition (returning to Hold) only requires SOC to cross the target.
Drain and Charge therefore run **all the way to target**, not just to the
hysteresis edge — this avoids stopping short of target and re-entering on the
next minor SOC drift.

- **R14**: **Drain** (active when current_phase=Drain): export = DNO. SIG
  discharges to grid toward `target_kwh`. Exit to Hold when `SOC ≤ target`
  (drains all the way to target before yielding).
- **R15**: **Hold** (entry / steady state): export = min(excess, DNO). Battery
  absorbs overflow above DNO naturally.
    - Exit to Drain when `SOC > target + OUTER_THRESHOLD_KWH`
    - Exit to Charge when `SOC < target − OUTER_THRESHOLD_KWH`
- **R16**: **Charge** (active when current_phase=Charge): export = 0.
  Battery charges from sub-DNO PV toward `target_kwh`. Exit to Hold when
  `SOC ≥ target` (charges all the way to target).
- **R16a**: `OUTER_THRESHOLD_KWH = 0.18 kWh` (≈1% of soc_max). Sized to be
  robust to Sigen SOC 0.1% quantisation (~0.018 kWh), so SOC noise alone
  cannot pop us out of Hold. Tighter than the original 0.5 kWh design — the
  Schmitt run-to-target behaviour means tighter outer threshold no longer
  causes flap, because once Drain/Charge is engaged it commits to the target.
- **R17**: All active states use D-ESS mode. MSC only when off (R6).
- **R18**: HA automation (5-sec) handles real-time export control AND publishes live
  phase (Charge/Drain/Hold/Off, plus Manual Charge/Hold/Drain when override is set)
  to `input_text.curtailment_live_phase`. Plugin (5-min) computes floor, sets
  D-ESS mode, publishes Active/Off. Plugin sets live phase to Off on deactivation.
- **R38**: Plugin `export_target` sensor publishes:
    - `-2` when plugin is Off (signals MSC handoff to yaml).
    - `dno_limit_kw` when plugin is Active.
  The yaml uses this as the cap fed into the Hold/Drain `new_limit` calc and
  uses `< 0` as the Off detector. Plugin does NOT publish 0.0 to signal Charge:
  the yaml Hold path would interpret `export_cap=0` as "clamp Hold to 0",
  defeating Hold semantics. Charge/Hold/Drain phase selection is done in the
  yaml from SOC vs target (Schmitt-trigger, R14-R16) — plugin has no
  override on phase. Plugin's role is "publish the cap the yaml should
  enforce when it decides to export".

## Solar Geometry — Safe Time

- **R19**: Safe time = when `scale × sin(elev) < DNO + base_load`. No curtailment
  risk beyond this point. Computed each cycle from current scale.
  `base_load` = 0.5 kW (minimum household load that offsets PV before grid sees it).
- **R20**: Before today's actual peak is observed, safe_time is estimated from p90
  scale. Once actual peak seen and scale updates, safe_time recalculates.
- **R21**: Safe_time only moves later (more conservative) until actual peak is
  confirmed. Cannot move earlier until scale is updated downward from actual peak.

## Planning

- **R26**: on_before_plan reduces soc_keep on overflow days to morning_gap + margin.
  Uses tomorrow's Solcast p90 peak to determine if overflow is expected.
- **R27**: on_before_plan uses tomorrow's forecast window overnight (when today's
  solar < 1 hour remaining).
- **R28**: Overflow days should result in low morning SOC (max headroom for overflow).

## Tomorrow Sensor

- **R29**: Tomorrow sensor shows expected overflow energy (from p90 scale integral)
  and estimated safe_time. Available after today's PV is done. Shows "Pending" while
  waiting. Shows zeroed attributes when Inactive.
- **R30**: Tomorrow sensor uses same solar geometry calculation as live (R9/R19).
  Solcast p90 tomorrow peak for scale. Cached for 30 minutes.

## Floor Stability

- **R39** — ❌ **REMOVED 2026-07-28** with R11; it only restated the ratchet.
  Floor stability now comes from stable *inputs* (p90-derived `floor_scale`, the
  smooth geometry integral per R25), not from clamping the output.

## Testing

- **R34**: Integration tests run ACTUAL plugin.calculate() against CSV data with
  independent physics simulation. Algorithm bugs cannot hide in reimplemented logic.
- **R35**: Tests must provide Solcast p90 peak via MockBase sensor overrides.
  Scale derivation must be testable with known p90 inputs.
- **R36**: TDD — when a flaw is found, write a FAILING test first. Then fix the
  code. Never deploy a fix without a test that would have caught the bug.
- **R37**: Never break production code to make tests pass. If tests fail but
  production is correct, fix the tests.

---

## Proposed additions (2026-05-06, pure functions tested, plugin wiring deferred)

After investigating today's curtailment performance, two gaps identified
in R54. Pure helper functions added to `curtailment_calc.py` with full
unit-test coverage; plugin integration is a follow-up change.

### R59 — P10 recovery floor (lower bound on R54 floor)

The current R54 formula:

```text
target = max(min(curt_floor, effective_keep), reserve)
```

ensures we drain to *at least* `reserve`, but doesn't ensure we'll
actually recover to `overnight_target` by sundown on a worst-case (P10)
PV day. R55 sources `effective_keep` from *tomorrow's* morning gap,
not from *today's remaining* PV runway. So on a confirmed-overflow day
where R48 relaxes effective_keep to 0.5 kWh, we drain to 0.5 and
*assume* PV will refill — if the day delivers P10 instead of P50, we
end below overnight target.

**R59**: add a P10 recovery lower bound:

```text
p10_charging_potential = max(0, p10_pv_remaining_kwh - load_remaining_kwh)
p10_recovery_floor = max(0, overnight_target_kwh - p10_charging_potential)

target_soc = max(reserve, p10_recovery_floor, min(curt_floor, effective_keep))
```

Pure function `compute_p10_recovery_floor()` — passes seven unit
tests covering huge-runway / no-runway / partial / load>PV / zero-target /
today's actual data / combined-with-R54.

Behaviour:

- Sunrise (lots of P10 PV ahead): p10_recovery ≈ 0 → outer max yields
  inner min (no change vs current)
- Mid-afternoon (less ahead): rises, starts capping how low keep can go
- Sunset (P10 PV → 0): p10_recovery → overnight_target → forces SOC up
  to target by sunset (replaces R57's "drain to keep, hope PV refills")

### R59a — charge_below recovery nets against OVERFLOW, not raw PV (2026-07-27)

> ❌ **WITHDRAWN 2026-07-28 — superseded by R59b. DO NOT IMPLEMENT.**
> The reasoning below is circular: it assumes the no-charge policy is Hold in
> order to compute the threshold that *chooses* the policy. Under it the floor
> can only ever equal `overnight_target`, so Charge fires from dawn regardless of
> available PV — which blocked the morning drain on a 12.28 kWh-overflow day.
> Kept for the reasoning only. Jump to R59b.

**Defect in R59 as originally specified.** `p10_charging_potential` above
uses `p10_pv_remaining - load_remaining`, which assumes all PV net of load
reaches the battery. It does not. `charge_below` asks *"if I do NOT actively
charge, will I still make overnight_target?"* — and the no-charge policy is
**Hold**, which pins dispatch at `load + export_cap` and therefore serves the
export cap **before** the battery. Under Hold the battery only receives
`PV - load - export_cap`, i.e. the overflow.

Observed 2026-07-27 (live): `p10_pv_remaining=16.77`, `load_remaining=5.92`,
`overnight_target=7.07`, but `overflow_p10=0.0`. R59 computed
`7.07 - 10.85 → 0` → `charge_below` floored to 0.5 kWh (2.8%). The plugin sat
in `Hold Battery` for 6h25m from 05:51 at 9% SOC, crediting 10.85 kWh of PV
that Hold was exporting to the grid. Projection had it hitting the 2.8% drain
floor at 18:30 (P50) with a 5.7 kWh overnight load to import at 25.3p.

**R59a**: the recovery floor feeding `charge_below` uses the P10 **overflow**:

```text
charge_recovery_floor = max(0, overnight_target_kwh - p10_overflow_kwh)
charge_below          = max(charge_recovery_floor, soc_keep, DEEP_DISCHARGE_FLOOR_KWH)
```

On 2026-07-27 this gives `7.07 - 0.0 = 7.07` kWh (39.1%) — Charge fires below
that, which is the correct behaviour.

**Scope — R59a applies to `charge_below` ONLY.** The R54 drain target
(`compute_floor_with_source`) keeps the original `compute_p10_recovery_floor`.
Rationale: on a >7.7 kWh-overflow day `overflow_floor` drops below
`overnight_target`, so an overflow-netted recovery floor could raise the drain
target and strand headroom — violating R25/R52. `compute_drain_above()` never
took `p10_recovery` at all, so the Drain *threshold* is unaffected either way.

**Trigger condition** (why this went unseen since 2026-05-11): it needs
`p10_pv - load > overnight_target` while `p10_overflow ≈ 0` — a bright day whose
PV never clears the export cap. Overflow days (R59's design case) and overcast
days (P10 PV low, floor stays high) both mask it. Two existing unit tests
encoded the defect as correct and were corrected with R59a:
`test_p10_recovery_floor_huge_pv_runway` (20 kWh P10 PV over a day never nears
a 3.68 kW cap) and `test_p10_recovery_floor_partial_charging`.

### R59b — SUPERSEDES R59a: the recovery floor nets against GENERATION (2026-07-28)

**R59a is withdrawn.** Its argument was circular: it assumed the no-charge
policy is Hold in order to compute the threshold that *chooses* the policy.
Under that assumption the battery never charges, so the floor can only ever
equal `overnight_target` — and Charge fires from dawn regardless of how much
free PV is coming.

Overflow and generation are different quantities and the floor needs the second:

- **Overflow** = PV above `load + export_cap`. A *curtailment* quantity — how
  much we stand to waste.
- **Generation** = `p10_pv_remaining - load_remaining`. What can actually be
  used to refill the battery.

Observed 2026-07-28: `overflow_p90 = 12.28` kWh but usable surplus was
`16.77 - 5.92 = 10.85` kWh. `overflow_p10` was 0.0, so R59a drove `charge_below`
from 0.54 to 7.20 kWh at 06:01 where it sat flat all day. That **blocked the
morning drain on a day forecasting 12.28 kWh of overflow** — the exact inverse
of R25 (headroom is cheap early and impossible late). It is why the policy read
Hold when it should have read Max Export.

**R59b**: `charge_below` uses the same generation-netted recovery floor as the
R54 drain target:

```text
charge_recovery_floor = max(0, overnight_target - (p10_pv_remaining - load_remaining))
charge_below          = max(charge_recovery_floor, soc_keep, DEEP_DISCHARGE_FLOOR_KWH)
```

R59a's separate charge-side floor existed only to justify the overflow netting;
with that withdrawn the two must not diverge
(`test_charge_recovery_floor_matches_drain_side_recovery`).

**The Schmitt band supplies the timing — the floor must not.** The floor starts
near 0 on a bright morning (Hold: keep SOC low, preserve headroom for whatever
the afternoon brings) and *rises* as remaining generation shrinks, crossing SOC
and flipping Hold → Solar Charge by itself. Banking happens as late as it safely
can. Measured ramp on the 2026-07-27 numbers: `0.0 → 0.57 → 4.07 → 8.07` kWh
(`test_charge_recovery_floor_ramps_up_as_generation_runs_out`). That is the
2026-07-27 case handled correctly, without R59a's charge-from-dawn destroying
the afternoon headroom.

Where load exceeds remaining PV (overcast), the net is negative and the floor is
raised *above* `overnight_target` to cover the through-day deficit — so Charge
still fires on the low-overflow days RD17 was patching around.

**CM stays active for the whole curtailment window** rather than handing back
when overflow is momentarily zero. Predbat has no curtailment awareness, and PV
arriving in the afternoon still needs managing — only CM can do that. (This does
not change RD6, which hands back at `safe_time`, when the day's curtailment risk
is genuinely over.)

### R60 — effective export cap for overflow integral

The overflow integral asks "how much PV will exceed our export ability?"
and uses `dno_limit=4.0` as the export ceiling. But the voltage throttle
constrains real export below DNO whenever grid voltage rises. Reference:
on 2026-05-06 between 14:50 and 15:50 BST mean export was 2.92 kW —
27% under DNO. Forecast overflow using DNO=4.0 understates actual
curtailment by the same ratio.

**R60**: feed the overflow integral a smoothed effective DNO instead:

```text
effective_dno = compute_effective_export_cap(
    today_samples_kw,        # rolling 30-min cap readings during PV>load
    yesterday_avg_kw,        # persisted across days
    dno_kw=4.0,
    min_samples=10,
    hard_floor_kw=2.0,
)
```

Three-regime fallback:

1. ≥ min_samples today → today's mean (clamped [hard_floor, DNO])
2. else yesterday's daytime mean
3. else DNO (cold start, no persisted data)

**Why both regimes**: at 06:00 BST we have no today data, so use
yesterday's. By midday today's data dominates — yesterday is ignored.

**Why hard_floor**: a single bad voltage hour shouldn't predict
"no export at all" tomorrow. 2.0 kW floor preserves *some* DNO
contribution to the forecast.

Pure function `compute_effective_export_cap()` — passes eight unit tests
covering all three regimes plus clamps.

### Plugin wiring (done 2026-05-06)

Both R59 and R60 wired into `curtailment_plugin.py`:

- **State**: `_cap_samples` (deque, last 6 = 30 min), `_cap_samples_full_day`
  (list, full-day samples), `_yesterday_cap_avg` (float, persisted),
  `_effective_dno` (float, computed each cycle), `_p10_recovery_floor`
  (float, computed each cycle).
- **Sampling**: `voltage_throttle_filtered_cap` read each cycle. Filtered
  to `actual_pv > 0.5 kW` so idle hours don't dilute the daytime mean.
- **State persistence**: yesterday_cap_avg, cap_samples,
  cap_samples_full_day round-trip through `_load_state` / `_save_state`.
- **Day rollover** (`_reset_for_new_day`): rolls today's full-day mean
  into `_yesterday_cap_avg`, clears today's lists.
- **Today's overflow integral**: passes `self._effective_dno` to all three
  `_compute_overflow_band` calls in calculate() and
  `_publish_forecast_overflow`.
- **R54 floor formula** updated to:

  ```text
  floor = max(reserve, p10_recovery, min(overflow_floor, effective_keep))
  ```

- **Tomorrow's forecast** uses `compute_effective_export_cap` against
  `_cap_samples_full_day` (today's just-completed daytime mean) with
  yesterday fallback. `excess` now subtracts realistic exportable_kwh
  before comparison to headroom — was previously assuming all PV-load
  could exit (over-optimistic).
- **Diagnostic attributes** on `sensor.predbat_curtailment_phase`:
  `effective_dno_kw`, `p10_recovery_floor_kwh`, `yesterday_cap_avg_kw`,
  `cap_samples_today`. Tomorrow sensor adds `exportable_kwh` and
  `tomorrow_eff_dno_kw`.

All 138 curtailment tests pass (15 new + existing). Pure-function unit
tests cover the math; integration tests cover the floor formula change
through real-day CSV fixtures.

## Day-Shape Scenario Test Matrix (2026-05-08)

Every proposed change to charge_below / drain_above / phase logic must
be reasoned through these five canonical day shapes before merging.
Asymmetric days (sunny→cloudy, cloudy→sunny) are the critical guard
cases — naive blending and naive past-tracking ratios both fail there.

**Design choice 2026-05-11:** charge_below uses Solcast P10 (pessimistic)
remaining estimate directly. Reverts the 2026-05-08 P50 choice.

Rationale: once SOC crosses overnight_target the Hold/Drain logic exports
the surplus, so over-charging by a kWh or two costs at most one battery
round-trip (~10%). Under-charging costs the full overnight import bill
plus comfort risk. Asymmetric cost → choose the defensive quantile.

We do NOT apply a calibration ratio (last 30 min actual / solcast):

- The past doesn't predict the future on shape-changing days
  (sunny→cloudy invalidates a high morning ratio for the afternoon)
- Solcast already revises P10 through the day as actual conditions
  clarify; layering a ratio on top second-guesses Solcast's own
  time-aware model

For each day shape, document:

1. Expected SOC trajectory
2. Expected charge_below trajectory
3. Expected drain_above trajectory
4. Pitfalls (what the wrong logic would do)

### Scenario 1 — On-forecast day (Solcast P50 ≈ actual, P10 below)

- Profile: PV tracks Solcast P50 ±10% all day.
- charge_below: moderate early (P10 is conservative; floor reflects
  pessimistic outlook). Eases as P10 PV remaining shrinks toward
  sunset and actual PV is banked.
- drain_above: tracks `min(overflow_floor, effective_keep)`. On a
  bright day overflow_floor wins (low value); on a normal day
  effective_keep wins (= overnight target).
- Phase: morning Charge to charge_below as a safety buffer, then
  Hold/Drain as actual PV exceeds the P10 line.
- Pitfall: small extra round-trip on days that turn out P50+. Cost
  is ~10% of the over-charged slice — accepted as insurance.

### Scenario 2 — Under-forecast day (actual < Solcast)

- Profile: clouds materialise that Solcast didn't predict. Solcast P10
  remaining revises down through the morning to catch up.
- charge_below: P10 already pessimistic, so morning floor is already
  high enough to force-charge well before deficit becomes critical.
  As P10 revises down further the floor rises more, but most of the
  defensive charging happened earlier.
- drain_above: stays at curtailment-buffer floor — overflow probably
  won't materialise.
- Phase: morning Charge ensures SOC reaches target even on worst-case
  forecast.
- Pitfall: minimal — P10 is designed for this case.

### Scenario 3 — Over-forecast day (actual > Solcast)

- Profile: clearer than Solcast predicted. P10 remaining stays low
  (conservative) until Solcast revises up.
- charge_below: P10-based, so it stays elevated even as actual PV
  pours in. Some "wasted" defensive charging happens, but once SOC
  crosses target the Drain phase exports the surplus.
- drain_above: as actual overflow develops, overflow_floor lowers
  (R50 confidence weighting on the curtailment side handles this).
  drain_above tracks the overflow buffer requirement.
- Phase: morning Charge to P10 floor; Drain as overflow develops and
  SOC exceeds target.
- Pitfall: defensive charging cost ~10% on the over-charged slice.
  Accepted.

### Scenario 4 — Sunny morning, cloudy afternoon (front-loaded)

- Profile: clear sunrise → high PV early → clouds 11:00-13:00 → little
  afternoon. Solcast P10 should reflect this from sunrise.
- charge_below: starts elevated (P10 already accounts for afternoon
  clouds). Falls as morning PV banks.
- drain_above: drain target tracks overflow_floor. Drain may fire
  morning when battery fills from sunny-morning surplus.
- Phase: morning Charge to P10 floor; Hold/Drain midday; SOC already
  comfortable for afternoon clouds.
- Pitfall: a calibration ratio approach would say "ratio=1.3 morning,
  trust = scale up afternoon forecast" — wrong, the afternoon clouds
  are already in the forecast. Direct P10 avoids this trap.

### Scenario 5 — Cloudy morning, sunny afternoon (back-loaded)

- Profile: clouds sunrise → low PV early → clears 11:00 → high PV
  afternoon. Solcast P10 should reflect this from sunrise.
- charge_below: starts elevated (P10 won't promise the afternoon
  recovery — it assumes worst case). Triggers morning Charge from
  grid to guarantee target.
- drain_above: rises as Solcast says afternoon overflow likely;
  morning Hold ensures battery has room.
- Phase: morning Charge to P10 floor; Drain mid/late afternoon if
  overflow develops.
- Pitfall: a calibration ratio would say "ratio=0.3 morning, scale
  down afternoon forecast" — wrong, the afternoon sun is already in
  the forecast. Direct P10 avoids this trap.
- 2026-05-08 was this shape. With P10 we'd eager-charge morning →
  round-trip drain afternoon (cost ~£0.10-0.20 + battery wear). The
  alternative — P50-direct — gave 0 morning floor but accepted ending
  below overnight target if actual undershot P50. P10 chooses the
  defensive bet.

### Test Coverage Required

- `test_curtailment.py` pure-function tests: P10 with deficit / surplus /
  zero target / load > P10, plus regression guard that P50 is ignored. ✓
- Integration scenario tests: each of the 5 day shapes simulated end-
  to-end against expected SOC trajectory. **TODO**.
- Real-day CSV fixtures: capture 2026-05-08 (under-forecast cloudy)
  for regression. **TODO**.

### soc_keep floor (added 2026-05-08)

The published `charge_below` sensor is clamped to be ≥ `soc_keep`. Even
when forecast says we'll comfortably exceed overnight target without
intervention, charge_below should never tell the HA automation that
SOC below soc_keep is acceptable — soc_keep represents the minimum
acceptable SOC for comfort/safety, regardless of forecast.

This clamp is applied at publish time only — the R54 floor input
(`_p10_recovery_floor`) is not clamped, so R48's effective_keep
relaxation still works on big-overflow days (where intentionally
allowing SOC < soc_keep absorbs more PV). Two separate concepts:

- `_p10_recovery_floor`: pure forecast-derived recovery requirement
  (input to R54 outer max for drain target)
- Published `charge_below`: clamped to soc_keep (defines what the HA
  automation will force-charge to recover)

### Deep-discharge floor on charge_below (added 2026-06-04)

The soc_keep clamp above evaporates when `on_before_plan` (R26)
relaxes `best_soc_keep` toward 0 on sunny-tomorrow days. On those
days `charge_below = max(p10_recovery=0, soc_keep=0) = 0`, so
`charge_target = min(charge_below, drain_above) = 0`. With SOC = 0
the YAML stays in Hold and exports surplus PV to grid — leaving no
buffer for a load transient. Observed 2026-06-04: battery at 0%,
PV-load surplus 5 kW exporting, kettle (~3 kW) caused a sub-second
grid touch.

Symmetric fix: `compute_charge_below` floors at the same
`DEEP_DISCHARGE_FLOOR_KWH = 0.5` constant used by R54's drain target.

```text
charge_below = max(p10_recovery_floor, soc_keep, DEEP_DISCHARGE_FLOOR_KWH)
```

With SOC = 0 and `charge_below = 0.5`, `charge_target = 0.5` and the
phase flips to Charge: `export = 0`, all PV is directed to battery
until SOC reaches 0.5 kWh. A load transient at low SOC is now
absorbed by redirecting PV (already on-site) rather than from the
grid (round-trip via the export commitment).

Invariant: the system never reports a target SOC below
`DEEP_DISCHARGE_FLOOR_KWH` on either threshold, regardless of how
optimistic the forecast or how low `soc_keep` is.

### Why we accept this design's failure modes

The trade (P10 choice, 2026-05-11):

- **Best case (P10-or-worse day):** overnight target guaranteed,
  no expensive evening grid-fill.
- **Worst case (clearer than P10 day):** defensive charging that
  later round-trips out as Drain-phase export — ~10% efficiency
  loss on the over-charged slice + minor cycle wear.
- **Asymmetric cost calculus:** under-charging costs the full
  overnight import bill plus comfort risk; over-charging costs
  one round-trip on a small slice. Defensive bet wins.

Empirically on 2026-05-08 (cloudy morning, sunny afternoon shape):
P10 triggered ~6 kWh round-trip drain afternoon (£0.10-0.20 + cycle).
P50 would have skipped the morning charge but accepted ending below
overnight target on worse-than-P50 days. We prefer the round-trip
cost over the import-bill exposure.

### R61 — no-surplus drain hold (Option A, 2026-06-15)

The drain target (`effective_keep`) must never request draining below
the CURRENT SOC while PV is not covering load (no genuine surplus):

```text
effective_keep = apply_no_surplus_drain_hold(effective_keep, soc_kw, pv_covering)
# i.e. if not pv_covering: effective_keep = max(effective_keep, soc_kw)
```

**Why.** The R55 overnight target legitimately shrinks toward sunrise
(less battery needed as the morning nears). But `compute_morning_gap`
declares "sunrise" when PV crosses ~0.3 kW sustained — hours before PV
actually exceeds load. Draining to that collapsed target empties the
battery before PV relieves it. Observed 2026-06-15: plugin activated
05:11 BST with target ≈ 0.3 kWh, drained 7.6% → 2.7% to grid, then
imported at standard rate once empty; PV did not exceed load until
~08:44.

**Principle.** Draining exists to make room for *surplus* PV (R25/R52).
With no surplus there is nothing to make room for — hold at (at least)
current SOC. No Drain fires; the battery still covers load naturally in
Hold. Once `pv_covering` becomes true, normal drain-to-target resumes.

**Interaction with R52.** Pre-PV drain is an intentional pre-sunrise
drain on confirmed-overflow days; it uses a separate, separately-gated
path (`_pre_pv_drain_decision`) and does NOT flow through this guard.

Tests: `test_no_surplus_hold_dawn_collapse`,
`test_no_surplus_hold_target_above_soc_unchanged`,
`test_no_surplus_hold_surplus_allows_drain`.

Known residual (future work): the root cause — morning_gap's sunrise
boundary (PV ≥ 0.3 kW instead of PV ≥ load) — remains; the collapsed
pre-dawn overnight_target still feeds the published Off-path target and
R59's recovery input. Fix tracked in master-plan-jul-2026 Phase 1.5.

### R62 — forecast-driven pre-PV drain target (autonomous, 2026-07-07)

R52's pre-PV drain target was `soc_keep + PRE_PV_BUFFER_PCT% × soc_max` — a
static knob. On 2026-07-07 (63 kWh forecast @ 0.92 confidence, ~17-20 kWh
above the effective cap vs 17.6 kWh total battery headroom) it would have
stopped draining at 3.6 kWh, stranding ~3 kWh of headroom on exactly the day
it mattered. The plugin must size the pre-PV drain from the forecast itself —
no manual helper-tweaking the night before.

```text
legacy         = soc_keep + buffer_pct% × soc_max        (unchanged knob)
overflow_floor = max(0, (soc_max − min(1.8, overflow)) − overflow × 1.2)
target         = min(legacy, max(reserve,
                                 DEEP_DISCHARGE_FLOOR + dawn_load,
                                 overflow_floor))
```

- `overflow` = R50 confidence-blended overflow integral against the R60
  effective cap (both already computed pre-dawn by _publish_forecast_overflow).
  Low confidence shrinks overflow → target returns to legacy: the formula can
  only be MORE aggressive than R52, never less.
- `dawn_load` = forecast house load from PV-start until PV covers load — the
  R61 window where the battery still carries the house. Near-zero, never zero.
- Implemented in `compute_pre_pv_target()` (curtailment_calc.py).

**Companion fix (same date):** the pre-PV activation branch now stamps
`_effective_keep_kwh` / `_overflow_floor_kwh` with the pre-PV target and
clears `_p10_recovery_floor`. Previously publish() derived `drain_above` from
YESTERDAY EVENING'S values (e.g. 14.95 kWh after an R61 dusk hold), so the HA
automation would refuse to drain below yesterday's level — pre-PV drain fired
but silently did nothing.

Tests: `test_R62_pre_pv_target_*` (pure), updated `test_R52_pre_pv_drain_*`,
`test_R62_pre_pv_publish_thresholds_not_stale` (stale-leak regression).

Known open items (Phase 1.4): tomorrow-sensor `exportable = eff_dno ×
window` linearisation ignores the battery-absorb timing constraint
(display-only).

**R61 dusk behaviour — DECIDED intentional (2026-07-08).** The no-surplus
hold also stops the R56 late-afternoon drain once evening PV < load. Under
the flat 12p export tariff this is economically CORRECT: dusk drain earns
nothing over exporting tomorrow (flat rate), while tomorrow's overflow room
is R52/R62's job — decided pre-dawn with a fresher forecast. R56's "evening
kWh has higher grid value" rationale belonged to the old deemed-£0 tariff.
Do not "fix" the dusk asymmetry; revisit only if the export tariff becomes
time-of-use.

### RD14c — saving sessions run off their PLANNED times (2026-07-28)

**What:** the heartbeat forces `policy = 'Max Export'` while the **Octoplus
saving-sessions calendar** is `on` — **only when the select is not `Predbat`** —
with native `calendar` start/end triggers.

```yaml
- platform: calendar
  entity_id: calendar.octopus_energy_..._octoplus_saving_sessions
  event: start            # and a matching event: end
session_live: "{{ is_state('calendar...octoplus_saving_sessions', 'on') }}"
```

**O1 / sundown gate + latch (2026-08-04, IN FORCE).** Sundown requires the SUN to
be down, not merely PV to be low: `elevation < SUNDOWN_ELEV_DEG (8.0)`. Ten nights
of live transitions separate perfectly at the first handback — flap nights
**10.5 / 12.7 / 9.4 deg**, clean nights **5.4 / 3.8 / 2.9 / 3.2 / 4.4 / 4.2 / 2.5 deg**
(real site, lat 52.31N); no overlap, **4.0 deg** of gap, and 8.0 sits inside it so
every flap-triggering moment is blocked and every clean one permitted. Verified
2026-08-04, first night on the gate: ONE transition, 20:00:53 at 6.3 deg.
*(First derived at 55.86N — MockBase's hardcoded location, not `zone.home` — which
read ~3 deg high. 8.0 lands in the gap at both, so the deployed value was right by
luck. Re-tune from `zone.home`.)* At 11-15 deg the sun is still well up, so PV
under 100 W is a cloud — CM handed back, PV recovered, CM re-took the wheel, and
**every toggle of `read_only` forces a full inverter reset**
(`docs/customisation.md:38`). Elevation is monotonic through dusk, so this cannot
flap by construction; a dwell timer would only make it slower.
A same-day `_sundown_latched` (persisted in `curtailment_state.json`, cleared by
`_reset_for_new_day`) covers what the gate cannot: residual PV noise below the
gate, and midwinter, when peak elevation at the real site (~14 deg) clears 8.0
only briefly.
**The latch may only ARM below the gate** — otherwise one heavy afternoon storm
would latch CM off for the rest of a big-overflow day.
Tests: `test_sundown_gated_on_solar_elevation`, `test_sundown_latches_for_the_day`,
`test_sundown_latch_cannot_arm_while_the_sun_is_high`,
`test_sundown_latch_still_defers_to_a_live_session`.

**RD14-own (2026-08-04, IN FORCE) — CM OWNS a live joined session end to end.**
No **discretionary** handback while the session calendar is on. The heartbeat can
only force Max Export while CM holds the wheel AND the select is not `Predbat`
(RD14c), so *any* handback stops the sell — not just sundown. Guarded paths:
sundown (RD14c-sundown), **"no PV yet"** (peak never cleared 0.1 kW — a
winter-evening session), **`p90_scale < 0.5`** (Solcast missing/stale), and the
**R4 charge-window defer** (GSHP-gated, and winter is exactly when sessions run).
On the two forecast-less paths CM returns `active` with a **`hold`** override —
it must own the plant so the heartbeat can dispatch, but must not drive the
Schmitt off a forecast it has just declared unusable.
**Genuine fail-closed paths are NOT guarded** and must still refuse to act: no
`soc_max`, no location, no clock, unreadable plant SOC (A0).
Tests: `test_cm_owns_a_session_through_every_discretionary_handback`,
`test_r4_defer_does_not_release_the_wheel_mid_session`,
`test_sundown_defers_while_a_saving_session_is_live`.

**RD14c-sundown (2026-08-03, IN FORCE) — sundown must not hand back mid-session.**
`sundown = peaked and actual_pv < 0.1` also requires **no live joined session**.
The heartbeat can only force Max Export while CM holds the wheel and the select is
not `Predbat`, so deactivating during a session does not merely change who reports
the decision — **it stops the sell**. Live 2026-08-03: PV crossed 0.1 kW at
19:37:40, CM deactivated at 19:40:16 and disabled the heartbeat, and export went
3.7 kW → 0 with 20 minutes of the paid 19:00–20:00 window left (compounded by the
handback's `read_only -> False` write not taking, leaving no writer at all).
CM stays active and changes nothing else — the select stays where the Schmitt put
it and the heartbeat keeps dispatching off the calendar. **Do not fix this by
pinning the select to Max Export from the plugin:** that is what RD14c removed and
it caused the 5 min 46 s over-run at session end.
Implemented in: `curtailment_plugin` sundown test. Test:
`test_sundown_defers_while_a_saving_session_is_live`.

**RD14c-display (2026-08-03, IN FORCE) — the session dump must be REPORTED as the
policy in force.** Because the heartbeat forces Max Export without writing
`sig_dispatch_policy`, nothing downstream can see the dump: the select keeps the
pre-session policy and `intended_policy` published the plugin's own Schmitt wish.
Observed live 2026-08-03 19:13, mid-session — battery −3.84 kW, export 3.68 kW at
the cap, card reading "→ Hold Battery / Hold · surplus fits". The publish site had
documented the precedence `override > session > select` since RD13a but only ever
implemented the override rung; this is the 3dca0d06 defect one layer down.

`_publish_dispatch_policy` MUST mirror the heartbeat's effective-policy expression
term for term, **including the `!= Predbat` guard** (after handback Predbat owns
the machine and the heartbeat stands down, so the display must too), and MUST read
the **calendar** — the same entity the heartbeat reads. A display mirror is never a
second opinion. It publishes `session_dispatch` so the card can tell "the select
differs from reality because the heartbeat owns it" apart from a real
"not applied" fault.
Implemented in: `curtailment_plugin._is_session_dispatching`, `SIG_SAVING_SESSION_CALENDAR`.
Tests: `test_session_dump_is_published_as_the_effective_policy`,
`test_session_dump_respects_the_heartbeat_precedence_exactly`.

**Why the calendar, not the binary sensor or a hand-rolled window.** The
calendar is *"on when a saving session that the account has joined is active"* —
**joined-only**, so an un-joined session can never make us export for free — and
HA schedules `calendar` triggers to fire at the exact event boundary. No polling,
no `now()` window math, and no duplication between trigger and action.

An intermediate version of this requirement derived the window from the binary
sensor's `*_joined_event_start/end` attributes and compared `now()` to them. It
worked, but it hand-rolled what HA provides natively: two template triggers, the
window expression duplicated in trigger and action, and 60 s granularity from the
beat instead of exact firing. Superseded within the hour it was written — the
calendar entity is documented at
<https://bottlecapdave.github.io/HomeAssistant-OctopusEnergy/entities/octoplus/#saving-sessions-calendar>.

**Evidence (2026-07-28, the first session run under CM):**

```text
19:00:00  session starts
19:00:27  plugin cycle — sensor still 'off', correctly holds
19:00:57  Octopus publishes the sensor  (57 s late)
19:30:00  session ends
19:35:46  plugin released Max Export    (5 min 46 s late)
```

Start would have been ~5 min late; the end was **measured** at 5 m 46 s late,
dumping the battery at the cap well past the window. Roughly 15% of a 30-minute
session at each edge.

**Removing this would:** return both edges to the sensor's publish lag plus the
plugin's 5-minute cycle, losing ~10 minutes of every 30-minute session and
selling the battery outside the paid window.

**Trade-off:** none material. The calendar is a single source of truth for
"is a session live", and the native triggers remove the polling entirely.

**`| bool` is load-bearing.** HA renders `variables` to strings in some contexts,
and the string `"False"` is TRUTHY in Jinja — without the filter a session would
start correctly and then never release. Caught by
`test_rd14c_releases_at_the_planned_end`, not by review.

**Predbat guard.** A session must not override a handback: if the select says
`Predbat`, its mappers are enabled and forcing Max Export would put two writers
on the registers (the 2026-07-26 and 2026-07-28 failures).

**Implemented in:** `ha/sig_dispatch_heartbeat.yaml`; tests
`tests/test_yaml_heartbeat.py::test_rd14c_*`.

### R64 — smooth the overflow estimate (2026-07-28)

**What:** rolling **median** over a 30-minute trailing window, applied to
`overflow_p10/p50/p90` before they feed the floor. Raw values are published
alongside as sensor attributes.

**Why:** the Solcast-derived estimate is noisy cycle-to-cycle, and that noise
reaches the floor multiplied by `OVERFLOW_SAFETY_FACTOR`, chattering any
threshold SOC happens to be resting on. Median rather than mean because Solcast
revisions arrive as single-slot jumps: a median rejects a one-cycle spike
outright, a mean folds 1/N of it in.

**Evidence:** 2026-07-28, 09:00–14:40 live trace. The estimate fell 13.01 → 6.53
kWh — a genuine −6.48 kWh trend — but moved *up* on 27 of 59 steps, +3.77 kWh
total, path length 14.02 kWh: a **2.16x noise ratio**. Replayed through the
filter: noise **1.24x**, upward wobble **+0.75 kWh** (down 80%), net trend
preserved at −6.19 kWh.

**Direction is deliberate.** On a falling series — the normal shape of a day —
the median sits *above* the raw value (+0.29 kWh at the end of that trace). More
assumed overflow → lower floor → more drain. That is R25's safe direction: an
over-drain costs one round-trip, an under-drain costs the whole clipped surplus.

**Removing this would:** return the drain/hold decision to flapping at threshold
boundaries whenever SOC sits near a floor, and re-expose the floor to single-slot
forecast revisions. Note the previous damper for this was the R11 ratchet, which
was removed for unrelated and good reasons — do not reinstate that instead.

**Trade-off:** ~15 minutes of lag against genuine forecast change, in the
conservative direction. After a restart the history is empty and the raw value is
used until the window fills.

**Implemented in:** `curtailment_calc.py:smooth_overflow_samples`,
`curtailment_plugin.py` (`_overflow_history`, `OVERFLOW_SMOOTH_WINDOW_MIN`).

### R63 — drain deadline (2026-07-28) — **RETIRED as a lever 2026-08-10**

**Retired because its central claim is false as implemented.** The requirement
says "It can only fire EARLIER, never later". It cannot:

```text
Schmitt drains when:   needed > 0            (needed = required_headroom - headroom)
R63 fired when:        needed > sheddable    (sheddable >= 0 by construction)
```

`needed > sheddable >= 0` implies `needed > 0`, so **every state where R63 fired
was one where the ordinary drain was already running.** It was designed against
the *bare* energy test (`headroom < overflow + flat buffer`) — and the same
2026-07-28 change set replaced that with the safety-factored
`required_headroom_kwh()`, which closed the gap R63 existed for. The two landed
together and the redundancy went unnoticed.

**Its documented purpose was unreachable.** R63 was to outrank `no_drain`:

- via `overflow_fits`: needs `fits_margin >= early_buffer` (positive), while R63
  needs `needed > 0`, and `needed == -fits_margin`. Mutually exclusive.
- via `past_safe`: an evening state, while R63 is hard-gated on `lock_mins > 0`
  and lockout is a *morning* crossing. Never overlap.

**What it actually did** was drain through floors something else had
deliberately raised — the only states where the Schmitt was NOT already
draining:

| Floor | Consequence |
|---|---|
| dawn reserve (RD21) | 2026-08-08: ran the pack 7.2% -> 1.8% in the dark, 27 min before PV met load |
| `session_protect` | dumps premium-rate session energy at the ordinary export rate for headroom |

Neither was a decision anyone took.

**The conceptual error:** knowing you cannot make enough headroom before lockout
gives you no new action — you are already draining flat out. It is a
**diagnostic, not a lever**, and turning it into an override gave it the only
power an override has: to break floors. Kept as
`headroom_deadline_short_kwh` on the intended-policy sensor, which is the honest
input for the load-advice alert.

Pinned by `test_r63_never_forces_max_export_through_a_session_reserve` and
`test_deadline_shortfall_is_published_as_a_diagnostic`.

---

#### Original rationale (retained for the record)

### R63 — drain deadline: the trigger needs TIME, not just energy (2026-07-28)

**Defect.** The drain trigger (`SOC > drain_above`) reduces to a pure energy test:

```text
drain when:  headroom  <  OVERFLOW_SAFETY_FACTOR × remaining_overflow + buffer
```

It asks *"will the surplus fit?"* and never *"can I still make it fit in time?"*
Those come apart, because the rate at which we can shed is not constant — it is
the export cap **minus whatever PV is simultaneously refilling the battery**:

```text
shed_rate(t) = export_cap − max(0, PV(t) − load(t))
```

Worked on a clear July day (cap 3.68 kW), for 5 kWh of headroom:

| Time  | PV − load | shed_rate | time to shed 5 kWh |
|-------|-----------|-----------|--------------------|
| 06:00 | ~0 kW     | 3.68 kW   | 82 min             |
| 11:00 | 2.5 kW    | 1.18 kW   | 4h 14m             |
| 13:00 | 7.5 kW    | −3.8 kW   | never — filling    |

Once `PV − load > export_cap` the lever inverts: we export flat out at the cap
and the battery still charges from the excess. Call that crossing **T_lockout**
— it is the exact moment R25's "no levers" begins.

**Why the energy test fires too late.** Through the day `remaining_overflow`
shrinks (pushing the trigger away) while SOC rises (pulling it closer), so the
crossing drifts toward midday — the one window where acting achieves nothing.
The test can say "act now" precisely when it is already too late to act.

**R63**: gate the drain on achievability, not only on fit.

```text
headroom_needed = safety × remaining_overflow + buffer − current_headroom
max_sheddable   = ∫ shed_rate(t) dt   from now to T_lockout

if headroom_needed > max_sheddable:  → Max Export now, and latch
```

- **Integrate, don't sample.** `shed_rate` is falling continuously, so the
  instantaneous rate over-estimates what remains achievable.
- **T_lockout is the rising mirror of safe_time.** Same threshold
  (`dno + MIN_BASE_LOAD_KW`), same geometry model — `compute_pv_start_time`
  (R52) already computes the ascending crossing. Use geometry, not Solcast
  per-slot: R9 rejects per-slot forecast as too noisy, and this is the same
  decision.
- **Hysteresis, NOT a latch.** An earlier draft of this requirement said to
  latch on the reasoning "once behind, extra PV only makes it worse". That is
  wrong, and dangerously so: draining is precisely what clears the breach —
  `headroom` grows, so `headroom_needed` falls. A one-way latch would hold Max
  Export after the drain had succeeded and empty the battery. The feedback loop
  is self-correcting and must be left closed. Use a hysteresis band
  (`R63_HYST_KWH`) to stop chatter at the boundary: engage when
  `needed > sheddable`, disengage only once `needed < sheddable − hyst`.
- **It can only fire EARLIER, never later.** On days with slack it never
  triggers and behaviour is identical to today. That is what makes it safe to
  add to a live control path.

**Relationship to R62.** R62 (pre-PV drain) is already the dawn half of this
reasoning — it drains before sunrise precisely because `shed_rate` is at its
maximum there. R63 extends the same argument into the day, covering the case
where the forecast moves after the pre-PV decision was taken.

**Tension with R59b, deliberately surfaced.** Draining earlier and harder costs
round-trip and eats the evening reserve, which is what the R59b Overnight Floor
rising through the afternoon defends. The two floors then bracket the day:
*shed by this deadline* against *bank by that deadline*. When they cannot both
be met the floors invert, and that inversion is the honest signal that the day
is over-subscribed — surfaced on the dashboard rather than silently resolved.
Overnight need wins (importing at 25p is worse than curtailing free surplus).

---

## v30 — DC-Coupled Architecture (2026-07-18)

**Status: DIFF FOR REVIEW.** Interim executor (HA automations) is live and
validated; the plugin/mapper items below are NOT yet built. This section is the
spec to agree before that work.

### Context

Inverter swapped 2026-07-15: SigenStor EC 6.0 SP, DC-coupled, SIG owns PV at the
MPPTs (SMA retired). The whole 5-second software export-limit control is obsolete —
the inverter's own grid-limit register enforces the 3.68 kW G99 cap in hardware,
and DC-bus physics buffers PV into the battery natively. Control is now ONE lever:
Remote-EMS `PCS Remote Control` mode + `active_power_fixed_adjustment` (a signed AC
power setpoint). **R25's principle is UNCHANGED** (Andrew, corrected): once
PV − load > export cap there are no levers — the battery can only fill; all
room-making happens BEFORE overflow, judged at overflow start. Only the loss
changed (MPPT clip at ~31p/kWh, not fault risk) and the cap (4.0 → 3.68).

### Retired

- **R1** software DNO cap → hardware grid-limit register (`grid_export_limitation`).
- **R14–R16, R16a, R17, R18, R38** — the 5-second three-phase (Charge/Hold/Drain)
  export-limit automation → retired entirely. `curtailment_manager_dynamic_export_limit`
  and the voltage seek/throttle stack are OFF and to be removed.
- **R2** error→MSC restore → reframed as "release to EMS-MSC" (RD2).
- DNO constant 4.0 → 3.68 everywhere.

### Surviving (the brain — unchanged in purpose, new output)

R5 activation, R9/R9a/R10/R11 floor, R42–R50 scale/confidence/buffer, R52 pre-PV
drain, R19–R21 safe_time, R26–R30 planning/tomorrow, R47 state persistence,
R34–R37 testing. These still compute *how much room by when*. Their OUTPUT changes
from an export limit to **(a) a dispatch policy and (b) floor numbers** (RD9).

### New requirements

- **RD1 — Single writer.** Only `sig_dispatch_heartbeat` writes SIG control
  registers (Remote-EMS enable, control mode, export limit, dispatch). Everything
  else (guard, plugin, Predbat mapper, human) sets `input_select.sig_dispatch_policy`.
- **RD2 — Never app control; EMS-MSC is the rest state.** Remote EMS enable stays
  ON at all times. The resting/handover/overnight state is Remote-EMS control mode
  = `Maximum Self Consumption` (NOT Remote-EMS-off / app work mode). App work mode
  is set to MSC as a pure fallback. (Predbat also drives via Remote-EMS mode, so we
  must stay in Remote EMS to hand over cleanly.) **CHANGE from current live code**,
  where policy Off turns Remote EMS off → app mode; must become "set mode MSC, keep
  EMS on".
- **RD3 — Policy vocabulary** (display names locked 2026-07-18, `input_select.sig_dispatch_policy`):
  `Predbat` (=EMS-MSC rest / hand back — binary end state: not-CM-managed = Predbat
  in control) / `Max Export` (dispatch = cap+load) / `Hold Battery` (dispatch =
  max(PV,load): never absorb, sell surplus, cover load from battery) / `Solar Charge
  Battery` (dispatch = load: absorb PV surplus, zero export, cover house —
  grid-neutral). Future `Grid Charge` (negative dispatch) for the Predbat mapper.
  **CM vocabulary = Off / Hold / Full Export / Load Only** — these are the OLD THREE
  PHASES (Andrew, 2026-07-18): **Drain (R14)→Full Export, Hold (R15)→Hold, Charge
  (R16)→Load Only.** Old R16 "Charge" was export=0 + battery charging from sub-DNO PV
  toward target — exactly Load Only (dispatch=load absorbs the PV surplus). NOT a
  grid charge. The R14–R16 Schmitt-trigger phase selection survives verbatim; the
  plugin outputs a policy name instead of an export limit. **The CM never
  grid-charges** — grid charging is a price decision, always Predbat's; `Charge`
  (negative dispatch) exists solely for the Predbat mapper (RD7, Charging→Charge).
- **RD4 — NO software hard floor / no import-clamp (Andrew, 2026-07-18, "A").** The
  battery must always be able to cover house LOAD down to its own HARDWARE discharge
  cut-off — never forced to import while it holds charge. The heartbeat does NOT clamp
  dispatch. Low-SOC protection is delegated entirely to MSC: **PCS mode ignores the
  native cut-off (proven 2026-07-16 → 2.3%), so whenever SOC reaches the low-SOC
  handover point (`sig_low_soc_handover_pct`, default ~12%) the CM/guard HANDS TO MSC
  (policy → Predbat)** — the native cut-off then protects the pack while MSC keeps
  covering load. Handover, not clamp: battery covers load, no artificial import. This
  is the ONLY low-SOC threshold, and it behaves as a handover.
  (Also protected by plugin deactivation when the overflow forecast collapses, and by
  the safe_time/dusk handover — a PCS session must never persist into no-PV.)
- **RD5 — Sell floor = "stop SELLING", the only CM floor.** `Max Export` → `Predbat`
  at `sig_keep_floor_pct` (the drain/sell target). Below it the battery still covers
  house LOAD via MSC down to the hardware cut-off (RD4). The sell floor is a drain
  target, not a "freeze SOC" level. Plugin sets `keep_floor = its drain target`; the
  guard enforces it as a backstop.
- **RD10 — Sell floor resets on handback.** When the CM hands back (policy → Predbat
  at safe_time/dusk/deactivation), `keep_floor` resets to the overnight-reserve
  default (38%) so a later Predbat `Max Export` sells to the right overnight level,
  not the CM's stale overflow-drain target. There is no hard floor to reset (RD4).
- **NOTE — live-code delta:** the currently-deployed heartbeat still has the hard-floor
  CLAMP + `sig_hard_floor_pct`. Under RD4 that clamp is removed and replaced by the
  low-SOC→MSC handover in the coordinated RD4 implementation. Live clamp is safe-if-
  suboptimal (imports below 12%) until then; not urgent.
- **RD6 — Dusk handover.** Sun below horizon (and no active saving session) → policy
  `Off` → EMS-MSC for the night. Overnight safety MUST be hardware (native discharge
  cut-off), never dependent on the heartbeat being alive.
- **RD7 — Predbat owns price/evening/winter (handover).** Rewrite
  `predbat_requested_mode_action` to drive `input_select.sig_dispatch_policy`
  (Demand→Off, Charging→Charge, Discharging→Full Export) instead of the Command
  modes (which don't honour dispatch on this firmware). Predbat owns evenings,
  nights, non-overflow days, and **all price events including Octopus saving
  sessions** — Predbat natively models the session reward and plans the SOC
  trajectory (pre-session reserve + overnight) via its optimizer. The bespoke
  `sig_saving_session_planner` is **DISABLED 2026-07-18** (never worked for a
  session that ends before dusk — it set policy Off mid-daylight, handing to MSC
  while PV and export value remained, and the day's posture did not resume).
  Interim: **saving sessions handled MANUALLY** (set policy Full Export for the
  window) until Predbat owns them. The approach is replaced, not adapted, at
  handback.
- **RD8 — CM owns overflow daylight only.** On a forecast-overflow day, the plugin
  takes ownership for the daylight window (sets policy Hold/Full Export + floors,
  Predbat suppressed), and releases at day-end (RD6 dusk / safe_time) back to
  Predbat. Non-overflow days and winter: plugin leaves policy `Off`; Predbat owns
  throughout. This IS the old CM-day / Predbat-evening handover, preserved.
- **RD9 — Plugin automates the policy + floors (removes the human).** The plugin
  computes and SETS `input_select.sig_dispatch_policy` and the floor helpers
  (`sig_keep_floor_pct`, `sig_hard_floor_pct`) each cycle from the forecast:
  activation (R5) → posture (Hold vs Full Export vs Off); floor (R9/R50) →
  keep_floor. No daily human mode-switching. Load input MUST use
  `sensor.sigen_plant_total_load_power` (consumed_power now includes DC battery
  charging post-swap).

### RD13a — manual override is ONE select (2026-07-28)

**What:** `input_select.sig_override` — `Off` / `Max Export` / `Hold Battery` /
`Solar Charge Battery`. Override is active **iff the select is not "Off"**, and
its value IS the policy to hold. There is no boolean.

**Why:** the old design needed two actions in the right order — set
`sig_dispatch_policy`, then turn on `sig_manual_override`. Set the policy and
forget the toggle (or do them in the wrong order) and the plugin re-asserts its
own policy within ~5 minutes, silently discarding the manual choice. Hit live on
2026-07-28 at 19:04.

**Why no boolean:** it was redundant state derivable from the select, so the only
thing it could add was **divergence** — the select reading "Max Export" while the
boolean said off (plugin quietly back in control), or the reverse. A first attempt
at this kept both and bridged them with a `sig_override_control` automation: a
shim for a problem that only existed because of the second entity. Deleted.

**No `Predbat` option, by design.** Handing back is the plugin's decision (RD6
safe_time handback), not a manual mode; "Off" already means "you decide".
Offering Predbat would let a human park the machine somewhere the plugin then
has to fight.

**Precedence in the heartbeat:** manual override > live saving session > the
plugin's `sig_dispatch_policy`. A human holding a policy outranks the session
dump — they can see something the automation cannot.

**Removing this would:** return manual control to a two-entity dance whose
failure mode is silent (your choice vanishes ~5 minutes later with no
indication).

**Implemented in:** `curtailment_plugin.py` (`SIG_OVERRIDE_SELECT`),
`ha/sig_dispatch_heartbeat.yaml` (`override_choice` in the policy chain),
`ha/sig_manual_override_failsafe_off.yaml` (nightly return to "Off").
Tests: `test_override_is_the_select_alone_no_boolean`, `test_rd13a_*`.

- **RD13 — Manual override (SUPERSEDED by RD13a; was `input_boolean.sig_manual_override`).** When on (and the
  policy-control gate is on), the plugin keeps the single-writer machine LIVE — enables
  `automation.sig_dispatch_heartbeat` and holds Predbat read_only — but STOPS writing
  `input_select.sig_dispatch_policy` and the keep floor. The user sets the policy by
  hand and it sticks (the plugin no longer overwrites it each cycle). This is the fix
  for "if I change the dispatch policy it just changes back". It grabs control
  regardless of plugin_active (a failsafe manual-drive path) and never hands back while
  on; turning it off resumes automated policy control on the next cycle. Distinct from
  the gate (`sig_plugin_policy_control` off = plugin fully observe-only, Predbat free):
  this override drives manually WITHOUT surrendering the heartbeat/read_only machinery.

### Implementation order (gated on review of THIS section)

1. **Review + agree this diff** (Andrew). ← we are here
2. Deploy corrections: RD2 (EMS-MSC rest state), and confirm RD6/RD5 wording.
3. Plugin rework RD9 (set policy + floors; `total_load_power`). TDD, harness tests.
4. Predbat mapper RD7 (drive policy); retire bespoke saving-session planner; re-enable
   Predbat control staged.
5. Harness tests for heartbeat / guard / (retired session) + plugin. Remove dead
   automations (5-sec export limit, voltage stack).

### Resolved 2026-07-18 (Andrew)

- **RD3 Charge lever** → negative PCS dispatch, and it is **Predbat-only**; the CM
  never grid-charges.
- **RD7 saving sessions** → Predbat owns; bespoke planner DISABLED now (pre-dusk bug),
  manual until Predbat wired.

- **RD6 handover trigger** → **safe_time** (Andrew, 2026-07-18). The CM manages the
  overflow window and releases (read_only False, policy → Predbat) once
  `now >= safe_time` (R19, the solar-geometry end of overflow). Predbat then exports
  the evening excess and manages any saving session. This is exactly old R6. **Dusk
  (guard) is the BACKSTOP** release only — catches a still-active session/manual
  policy that safe_time didn't clear (e.g. CM never activated). Interim (Predbat
  mapper not yet wired): safe_time/dusk release → EMS-MSC, which holds/absorbs but
  does NOT export the evening — evening export + saving sessions are MANUAL until
  RD7 lands.

- **Governing principle (Andrew, 2026-07-18)** → **CM does one thing: minimise
  curtailment, £-aware. Predbat owns everything else.** Predbat has no mechanism to
  pre-position the battery against a DNO-cap clip, which is the entire reason CM
  exists. So CM's scope is the curtailment window ONLY: pre-PV drain (R52/R62, to
  create headroom) → real-time overflow management → release at safe_time (R6/RD6).
  Outside that window Predbat is in sole control (price, evening export, saving
  sessions, overnight reserve). `read_only` (R3) is the mutex that enforces this:
  set only while CM drives, cleared on every handback.

- **RD6 + read_only mutex IMPLEMENTED (2026-07-18):** plugin `calculate()` deactivates
  at safe_time (sundown = backstop); `_publish_dispatch_policy` sets/clears
  `base.set_read_only` on the driving/handback edges. Tests:
  `test_deactivate_at_safe_time*`, `test_read_only_*`.

### Still open

- **RD7 — Predbat mapper onto the policy select** (drive `sig_dispatch_policy` from
  `predbat_requested_mode`, gated by read_only / CM phase so mapper and CM never both
  write the select). Until this lands, `read_only=False` lets Predbat *plan* but not
  *act* (mapper off), so evening export + saving sessions stay MANUAL.

---

## v32 — Evening lifecycle + saving sessions (2026-07-20)

**Status: IMPLEMENTED (TDD).** Replaces the v31 early-handback (RD6 "deactivate at
safe_time" + the "overflow fits → hand back to Predbat" trigger), which released the
machine to EMS-MSC **while PV was still flowing**. MSC stops exporting at the cap and
banks the excess PV into the battery, which then round-trips back out later (~10%
loss). Observed live 2026-07-20 ~15:02: handed back at SOC 79%, PV 2.3 kW, `grid=0.0`,
battery charging. The plugin flapped Max Export ↔ Predbat and never held.

**Key physics (Andrew, 2026-07-20):** `Hold Battery` and `Max Export` are *physically
identical* whenever PV surplus ≥ cap — the heartbeat clamps both dispatch setpoints to
`load + cap`, so export pins at the cap and the excess PV charges the battery either
way (`sig_dispatch_heartbeat.yaml` line 57). They differ **only** when surplus < cap:
Max Export drains the battery to grid to fill the cap; Hold keeps the battery flat and
exports only the surplus. So Hold-vs-Max-Export is purely a *battery-drain* decision,
and the natural switch is **SOC reaching the floor** (the existing Schmitt).

- **RD11 — Deactivate only at sundown.** The plugin stays ACTIVE for the whole PV
  window and hands back to Predbat only at **sundown** (`peaked and actual_pv < 0.1`).
  `safe_time` and "overflow fits headroom" NO LONGER deactivate (they drive the Hold
  override, RD12). This restores the original "CM runs until PV≈0" design that RD6
  wrongly cut short. The observed peak (`_peak_pv`) PERSISTS through the evening (no
  evening reset) so `peaked` stays True and sundown fires reliably; `_reset_for_new_day`
  clears it at midnight, and pre-dawn is the "no PV yet" early return.
- **RD12 — Evening policy override (Hold gate).** Inside the active window the policy is:
  1. **Saving session live** → `Max Export` (dump the reserve at the cap, RD14b).
  2. **`overflow_fits`** (`battery_headroom − overflow_p90 ≥ early_buffer`, the
     ex-early-handback condition) OR **past safe_time** → `Hold Battery` — battery flat,
     export the surplus at the cap, never drain-to-grid, never MSC round-trip. Reuses
     the v31 `curtailment_early_handback_buffer_kwh` helper (default 1.5 kWh) as the
     Hold gate. Hysteresis `FITS_HYST_KWH = 0.5` prevents Hold↔Drain flap at the
     boundary (the v31 flap on 2026-07-20).
  3. **else** → the existing SOC-vs-band Schmitt makes room (Max Export drains to the
     curtailment floor / Hold / Solar Charge).
  The override is reset to neutral at the top of `calculate()` so the pre-PV drain and
  other early-return paths never inherit a stale Hold.
- **RD14 — Saving session (two things, pulls sessions back into CM until RD7).** Uses
  the existing `SIG_SAVING_SESSION` binary_sensor + `_get_session_reserve_kwh`
  (`duration × cap`):
    - **(a) Reserve ahead of a session** — while a session is UPCOMING (scheduled, not
    yet active), `session_protect_kwh = min(soc_max, overnight_target + session_reserve)`
    raises `drain_above` (via `compute_drain_above`) so CM does not drain the reserve
    away before the session. `compute_drain_above` is otherwise pure-curtailment (v31);
    `session_protect` is 0 on days with no session, so those days are unchanged. The
    reserve also feeds `charge_below` (recovery) as before.
    - **(a2) Name the driver (2026-08-03, IN FORCE).** `sensor.predbat_curtailment_drain_above`
    is titled "Headroom Floor (P90 overflow)" and publishes only P90 terms, so on a session
    day it showed 10.22 kWh next to `overflow_floor_kwh: 3.39` with nothing to account for
    the remaining 6.83. The floor was correct and unauditable. The sensor MUST publish
    `source` (the winning arm of `compute_drain_above`) plus `session_reserve_kwh`,
    `session_protect_kwh` and `session_start`; `intended_policy` MUST carry
    `drain_above_source` + `session_reserve_pct` so the Why This Mode card can REPORT the
    cause instead of re-deriving it, and `reason` names the session when it sets the floor.
    `compute_drain_above_source` is derived FROM `compute_drain_above` — never a second
    copy of the max (the `required_headroom_kwh` drift lesson).
    Implemented in: `curtailment_calc.compute_drain_above_source`,
    `curtailment_plugin._get_session_start`. Tests: `test_drain_above_source_mirrors_compute_drain_above`,
    `test_drain_above_publishes_its_source`, `test_why_this_mode_reports_session_reserve`.
    - **(b) Dump during the session** — while the session is LIVE, the RD12 override forces
    `Max Export` to sell the reserve at the cap; protection drops (reserve not re-added),
    so it drains to the overnight target. Resumes the lifecycle when the session ends.
    - **Scope note:** running CM into the evening overlaps saving-session hours, so this
    partially reverses "Predbat owns saving sessions" (RD7) until the Predbat mapper
    lands. Trade-off on an overflow day WITH an evening session: `drain_above` won't go
    below `overnight_target + reserve`, reducing curtailment headroom (possible extra
    midday clip) in exchange for having the reserve to sell at the peak — accepted
    (Andrew, 2026-07-20).

- **RD15 — Single drain floor `sig_drain_floor_pct` (default 2.8%, 2026-07-21).** ONE
  helper = the SOC below which CM stops selling the battery to grid. It replaces THREE
  coincident 5% floors that all did the same job: the heartbeat `sig_hard_floor_pct`
  (dispatch ≤ PV clamp), the plugin `sig_low_soc_handover_pct` (low-SOC → MSC handover),
  and the hardcoded `max(floor%, 5.0)` keep-clamp. All three now read/derive from
  `sig_drain_floor_pct`. Default **2.8% = the deep-discharge floor (0.5 kWh)**, so the
  pre-dawn drain can reach it; the helper can only ever RAISE the floor above 2.8%
  (`compute_drain_above` is hard-floored at 0.5 kWh). Correction to the earlier RD4
  rationale: the **hardware discharge cut-off is 0%** by design ("rails at device
  extremes"), so this software floor — NOT the BMS — is the operational protection.
  The old "PCS ignores the hardware cut-off" note is unverified (the cut-off was 0%,
  so nothing was overridden). `sig_hard_floor_pct` + `sig_low_soc_handover_pct` retired.
- **RD17 — Override allows Charge, suppresses Drain (v32.1, 2026-07-22).** The
  overflow-fits / past-safe override was a blanket `Hold`, which masked the evening-
  reserve Charge on a low-overflow (overcast) day: the plugin sat in Hold all day,
  exported the sub-cap surplus, and handed a near-empty battery to Predbat at dusk.
  Changed the override to **`no_drain`**: run the SOC-vs-band Schmitt but clamp
  `Drain→Hold` (Drain is a pointless round-trip once there's no curtailment risk).
  So `Charge` still fires when `SOC < charge_below` (the P10 recovery floor, which
  rises through the afternoon) — banking PV into the battery for the evening — while
  Drain stays suppressed. The pre-PV dawn wait keeps the **pure `hold`** override
  (no Charge: we just drained for headroom, nothing to bank). Observed 2026-07-22:
  overcast day, SOC stuck ~7.6% in Hold, would have entered evening empty.
- **RD18 — Single keep-floor helper range + write robustness (2026-07-22).**
  `sig_keep_floor_pct` (the plugin-written sell target the guard enforces) had range
  [10,60] while the plugin's intended keep is 2.8–95%. An out-of-range intended (e.g.
  77%) was stored clamped by HA while the plugin's dedup-cache held the unclamped
  value → the change-check skipped forever and the helper wedged (observed stuck at
  10% since pre-dawn 2026-07-22). Fix: widen the helper to [2,100] AND clamp the
  written value to that range in `_set_keep_floor` before the change-check, so the
  written value equals what HA stores. Added write/skip logging for live visibility.
- **RD19 — Pre-PV drain start-latch (v32.2, 2026-07-22).** The pre-PV drain timing
  gate (`now < drain_start_utc → wait`) was re-evaluated every cycle. But
  `drain_start_utc = pv_start − drain_minutes/60` and `drain_minutes = (soc−target)
  /dno×60`, so while draining at ~dno the drain window shrinks at ~60 min/h and
  `drain_start_utc` advances at the SAME rate as `now`. The comparison therefore
  hovers at equality and flips on any noise → the policy oscillates Max Export↔Hold
  (RD16 pre-PV Hold on the None branch). Observed 2026-07-22 04:27–05:47 BST (~6
  flips). Fix: the timing gate now gates the START only (`and not
  _pre_pv_drain_started`); once the drain begins it commits and runs to target
  (`soc ≤ target` is the clean exit → RD16 Hold). Latch clears at the day rollover.
- **RD20 — Sell floor tracks drain intent, not overflow_floor (v32.3, 2026-07-23).**
  The published sell floor (`sig_keep_floor_pct`, the guard's "stop Max Export at
  this SOC") was always set to `floor_kwh` (= `overflow_floor`). But `overflow_floor`
  RISES toward 100% as the forecast overflow winds down (R13), so on a low-overflow
  morning the sell floor climbed to ~68% while the plugin was Holding at 8% SOC —
  meaningless (nothing selling) and it would have under-sold a saving session
  (stopping the dump at 68% instead of the overnight reserve). Fix: use
  `overflow_floor` as the sell floor ONLY during a genuine curtailment drain
  (`_policy_override is None and schmitt == "Drain"`, which includes the pre-PV
  drain) so the big-overflow deep drain is unchanged; otherwise (session Max Export,
  Hold, Charge, no_drain) use the overnight reserve (`_overnight_target_kwh`, else
  the 38% RD10 default). Observed 2026-07-23: sell floor ramped 10%→55%→68% while
  Holding.
- **RD16 — Dawn-flap latch (2026-07-21).** Once the pre-PV drain fires today
  (`_pre_pv_engaged_today`), the "no PV yet" path must NOT hand back to Predbat when
  the drain completes while PV hasn't arrived. When the drain is done (`_pre_pv_drain
  _decision` returns None) but overflow is still forecast and `actual_pv < 0.1`, HOLD
  active (battery flat) instead of returning off. Reason: at dawn actual PV flickers
  across the 0.1 kW boundary, bouncing the plugin between the pre-PV path (off) and the
  main flow (active) — observed 2026-07-21 05:39–05:52 BST, ~4 policy/heartbeat toggles.
  The block only runs pre-dawn (peak not yet observed), so it never affects the evening.

---

## v33 — The dawn gap (2026-08-06)

- **RD21 — The dawn reserve must survive PV start.** The battery is not free of load
  duty at PV START; it is free at PV MEETS LOAD. Those are ~85 min apart in August and
  hours apart in winter. `compute_pre_pv_target` already reserved
  `DEEP_DISCHARGE_FLOOR_KWH + dawn_load_kwh` for exactly this, but that term lived only
  in the pre-PV path, so at PV start the phase ended and the reserve was discarded.
  Live 2026-08-06:

  | time | SOC | what happened |
  |---|---|---|
  | 04:00–05:30 | 35% → 5.5% | pre-PV drain, reserve honoured |
  | ~05:30 | 5.5% | PV START — pre-PV phase ends, reserve term vanishes |
  | 05:30–06:00 | → 2.5% | Schmitt drains on to `drain_above` = 2.8% |
  | 06:00–06:55 | → 1.3% | coasts on house load, importing |
  | ~06:55 | 1.3% | PV finally meets load, 85 min too late |

  Therefore `compute_drain_above` takes the hard-floor arm as a parameter
  (`floor_kwh`), and `_dawn_floor_kwh` supplies
  `max(DAWN_RESERVE_FRACTION x soc_max, DEEP_DISCHARGE_FLOOR_KWH + dawn_load_kwh)`
  until the gap closes. `DAWN_RESERVE_FRACTION` = 10%; the forecast `dawn_load` arm is
  what carries winter, when 10% is not enough.

  **Released on MEASUREMENT (`pv_kw >= load_kw`), never forecast, and the latch is
  one-way for the day.** Measurement is what makes over-reserving harmless: the reserve
  is returned the moment it is not needed, so the fraction needs no precision. One-way
  because a cloud at 11:00 must not re-arm a 10% floor and stop the drain mid-overflow —
  that is the dusk-flap failure (O1) arriving at dawn. Persisted in
  `curtailment_state.json`; cleared by `_reset_for_new_day`. Fails CLOSED: unreadable
  sensors hold the reserve (holding it wrongly costs ~27 min of drain against ~3 h of
  slack; releasing it wrongly imports the house through the dark).

  **Headroom cost is nil.** Releasing 10% → the drain floor is ~1.6 kWh, ~27 min at the
  3.68 kW cap, and crossover sits ~3 h ahead of overflow start.

- **RD22 — The drain floor stops SELLING, not load-covering.** The heartbeat clamp
  `dispatch = min(raw, pv)` below `sig_drain_floor_pct` applied to every policy, and is
  applied AFTER policy selection — so no policy, not even a human override, could use
  the battery below the floor. Live 2026-08-06 06:48, manual Hold at SOC 1.3%:
  `pv 0.311, load 0.359 → raw 0.359 → clamped to 0.311`, battery −0.003 kW (idle),
  import 0.027 kW. The battery held 0.235 kWh while the house imported.

  That is RD4's prohibition ("never forced to import while it holds charge") violated by
  a mechanism protecting against nothing: **the SIG simply imports at 0%** — there is no
  protection cliff (Andrew, 2026-08-06), which retires the RD15 note that the software
  floor is the operational protection. R5 (stop selling at the floor) and RD4 (keep
  covering load below it) are not in conflict; the clamp merely blocked both at once.

  Therefore the clamp is gated on `policy == 'Max Export'`. `policy` is the EFFECTIVE
  policy, so a session-forced Max Export is still clamped.

- **RD28 — Once no overflow is left, bank to tonight's need, then hold.** In the
  `no_drain` state the charge threshold is **not** `charge_below`. That is the P10
  recovery floor — a DEADLINE ("be at least this high now, or a P10 afternoon will not
  get you there") — and deferring to it is correct only while curtailment competes for
  the same kWh, because every kWh banked early is headroom lost at the peak.

  Once `overflow_p90` reaches 0 nothing competes. Deferring then earns an export credit
  and risks buying the same energy back overnight at import rates, which is the worse
  side of that trade.

  Live 2026-08-06 18:02 — `overflow_p90` 0.0, SOC 5.66 kWh, `overnight_target` 6.62,
  `charge_below` 5.15. SOC sat **above** `charge_below`, so the Schmitt said Hold and CM
  exported the surplus while 0.96 kWh short of tonight's need, on a P10 margin of 0.51
  kWh — about eight minutes of surplus. It took a manual Solar Charge override to bank it.

  `compute_no_overflow_charge_target` = `min(overnight_target, soc_max −
  required_headroom(overflow_p90))`. The `min` is the safeguard: "no overflow left" is a
  forecast and forecasts move, so while p90 is non-zero the target is clamped to leave
  room for it — via `required_headroom_kwh`, never a second expression (the 2026-07-28
  lesson where five spellings of that question let the weakest veto the strongest).

  **Gated on the measured condition, not on `safe_time`.** On 2026-08-06 overflow reached
  zero ~28 min before the predicted `safe_time`; gating on the time would have wasted
  that banking window. `safe_time` only ever predicted when this condition would arrive.

  Deliberately not `compute_proposed_phase` — that takes `min(charge_below, drain_above)`
  and would clamp the target back down when `drain_above` is low. Charge-then-Hold is the
  whole rule, so it is written plainly. No hysteresis is lost: the Schmitt deadband only
  applied to entering Drain, which this branch forbids (RD17, unchanged).

- **RD27 — No mid-window handback. RD4 "A" (low-SOC → MSC) is RETIRED.** While the
  plugin is ACTIVE, CM keeps the wheel at any SOC. **Three sites, not one** — the
  plugin's decision path, the plugin's acting path, and `sig_keep_floor_guard.yaml`.
  The guard was the last one live: its keep-floor branch wrote `Predbat`, which is a
  handback. It now writes **Hold Battery** — stop the SELL, not the ownership. The
  guard's DUSK branch still writes Predbat, correctly: that is a window-END release. Handing back is a window-END
  decision (RD6 safe_time / sundown) and nothing else.

  The old rule handed the policy select to Predbat below the drain floor — and
  handed it to **nobody**. `_release_to_predbat()` is not on that path, so it cleared
  `read_only` while leaving the three Predbat mappers **disabled** (`_set_writer(
  cm_driving=True)` runs a few lines earlier and stays). Predbat was un-muzzled with
  no write path, the heartbeat went inert in its Predbat branch, and the plant fell
  through to the SIG's own Maximum Self Consumption default.

  Live 2026-08-06: handed back at 04:50, still handed back at 09:00 — charging at
  3.5 kW with **zero export** against a 19.89 kWh overflow forecast, while surplus
  (3.54 kW) was still *under* the 3.68 kW cap and therefore fully exportable. ~2% of
  pack spent on headroom the peak then had to curtail. Unrecoverable.

  Nothing is lost by removing it: below the floor the Schmitt gives Charge (under
  `charge_below`) or Max Export, and RD22's **sell-only** clamp already stops the
  battery being sold. The handover existed solely to escape CM's own clamp, which is
  no longer something to escape. Safety lives in the executor, not in a second rule
  in the planner.

  **It was written in two places** — the decision side (which only shaped the
  published reason) and the acting side (which did the actual write). Removing only
  the first changed nothing; the failing test is what found the second.

- **RD26 — Single point of truth for dispatch intent.** RD25 made the two copies
  provably agree; this removes the second copy. `ha/sig_dispatch_intent_helpers.yaml`
  defines the effective policy and the setpoint **once**, as two template sensors:

  | sensor | is |
  |---|---|
  | `sensor.sig_effective_policy` | override > session > select |
  | `sensor.sig_dispatch_kw` | the inverter AC OUTPUT setpoint, incl. the RD22 sell-only clamp |

  Both the `stale_setpoint` trigger and the action variables now **read** them.
  Two sensors rather than one with attributes because the config-flow template
  helper exposes a state template only.

  **Fail-safe.** Consumers read the setpoint as `| float(-1)` and treat negative as
  "no opinion": the trigger will not fire and the dispatch write is skipped, so an
  unavailable sensor freezes the register rather than commanding the plant off a
  missing value. `-1` and not `0`, because **0 is a legitimate setpoint** (a Max
  Export clamped to zero PV asks for exactly that), so 0 cannot double as the error
  value. The ESS/import re-opens stay unconditional — they are permissive.

  **Deploy order matters:** helpers first, then the heartbeat. The reverse leaves the
  heartbeat reading `unknown` and holding the last setpoint until the helpers land.

  Guarded by `tests/test_yaml_dispatch_intent.py`:
  `test_heartbeat_defers_to_intent_sensors` fails if the arithmetic reappears in the
  automation, and `test_intent_sensors_match_reference` pins the values against an
  oracle verified equal to the pre-refactor formulas over the full matrix (0/240
  differ) on the day it landed. The heartbeat harness renders the helpers to supply
  the sensors, so the whole heartbeat suite exercises them end-to-end.

  Note the refactor rounds the setpoint to 2 dp one step earlier than before. The
  register write is unchanged — it was always `| round(2)`.

  **RD26a — trigger on the DERIVED sensor, never on its inputs.** Moving the value
  into a template sensor while leaving the triggers on `input_select.sig_override` /
  `input_select.sig_dispatch_policy` created a race: HA fires the state trigger on
  the input immediately, the sensor recomputes a moment later, and the run reads the
  OLD policy. Live 2026-08-06 11:42:13, from the trace — trigger
  `input_select.sig_override` "Off" → "Max Export", variables `policy: "Predbat"`,
  `dispatch_kw: 0.68`, took the Predbat branch, wrote nothing, finished in 12 ms.
  The override is the human's immediate lever and it did nothing for 47 s until the
  next `:00` beat, while the plant self-consumed 4.4 kW into the battery on an
  overflow day. Both state triggers are replaced by one on
  `sensor.sig_effective_policy`: a derived sensor cannot change before it has been
  computed, so ordering is correct by construction. `stale_setpoint` already
  references `sensor.sig_dispatch_kw` and is safe for the same reason.

- **RD25 — The `stale_setpoint` trigger and the action variables are ONE decision
  written twice, and must be tested as one.** HA gives a template trigger no access
  to the action's `variables`, so the dispatch logic is necessarily duplicated: once
  to decide "has the live setpoint drifted?", once to compute what to write. Three
  divergences shipped on 2026-08-06 alone — the RD22 clamp landing in the action copy
  only; a first regression test that passed anyway because at the live numbers the
  copies differed by less than the trigger's own 0.1 kW tolerance; and the trigger's
  policy ignoring `sig_override` entirely.

  That last one disarmed the fast corrector exactly when a human was driving: under a
  manual override with the select on handback, the ACTION drove Hold every beat while
  the TRIGGER computed the handback policy, failed its active-policy gate, and never
  fired. Only the 60 s beat wrote, so the open-loop setpoint trailed PV and the battery
  absorbed every sag (live 08:17 — commanded 1.45, PV 1.329, battery −0.135 kW; ±0.14
  kW oscillation about a mean of ~0).

  Two rules follow:

  1. **Policy precedence in the trigger MUST be the same decision as `policy` in the
     action:** override > session > select.
  2. **Never test the copies separately.** `test_trigger_and_action_can_never_diverge`
     renders BOTH over a 200-case matrix (select × override × session × PV/load/SOC)
     and asserts they agree on the policy, on the setpoint, and on whether the policy
     counts as active. An edit to one copy alone cannot pass it. This is the guard;
     the individual assertions are only documentation.

- **RD24 — Entering the Predbat branch must unwind PCS Remote Control, however it
  was entered.** The MSC handback was gated on the `policy_change` trigger alone,
  which assumes the only way into the branch is the policy select moving. It is not:
  the **override** also changes the EFFECTIVE policy. Live 2026-08-06 07:38 — select
  read `Predbat`, override set to `Hold Battery`, so the effective policy was Hold
  and the ACTIVE branch wrote PCS Remote Control + a 1.08 kW setpoint. Clearing the
  override returned the effective policy to Predbat without touching the select, so
  only `override_change` fired, the MSC write was skipped, and the plant was left
  exporting against a setpoint nobody owned — heartbeat inert, all three mappers
  disabled, Predbat read-only, battery at 1.4% discharging into it.

  Fix: `policy_change` **OR** live EMS mode == `PCS Remote Control`, on any trigger.
  Keying the self-heal on the MODE is what makes it safe and complete:
  `predbat_requested_mode_action` selects only `Maximum Self Consumption`,
  `Command Charging (Grid First)`, `Command Discharging (PV First)` — **never** PCS
  Remote Control, which only the heartbeat's active branch writes. So reverting it
  cannot stomp Predbat (the 2026-07-27 regression), and unlike simply adding
  `override_change` it also recovers after an HA restart, where no trigger fires at
  all. The `not MSC` guard stays — it is what prevents a write every minute.

- **RD23 — One drain floor, one owner.** The released floor is read from
  `input_number.sig_drain_floor_pct` (via `_drain_floor_kwh`), never a plugin-side
  constant. RD15 consolidated three coincident 5% floors into that helper; a twin
  constant would silently undo it. It is also the entity the heartbeat's sell-clamp
  reads, so a CM target below it is unreachable by construction. Fallback
  `DEFAULT_DRAIN_FLOOR_PCT` is deliberately the HIGHER plausible value — an unreadable
  helper should under-drain, not over-drain.

---

## PART 2 — HISTORY (NON-NORMATIVE)

Nothing in this part is in force. It is kept for the *reasoning* — why a
decision was made, and why it was later reversed. Check the status index in
Part 1 before citing anything here.

Nothing below this line is in force. It is kept for the *reasoning* — why a
decision was made, and why it was later reversed. Check the status index in
Part 1 before citing anything here.

## v20 Redesign Delta (2026-05-02) — HISTORICAL

> **⚠️ Superseded in part.** This section claimed "when in conflict, this section
> wins", which is how it came to contradict both the code and later layers. Its
> claims about R45 (removed — it is live) and R9's tapered cap (removed — it is
> present) are **wrong**. Its removal of R7 and replacement of R43 by R58 are
> **correct** and are reflected in the status index.
 Triggered by today's failure mode: clear morning + cloudy
afternoon caused the plugin to extrapolate `actual_scale` over the whole
day, predicting 16 kWh of overflow that wouldn't materialise (real
forecast: rain by 16:00). Plugin drained battery to 2.8% target and
"manage manually" was needed.

Goals of v20:

1. Use Solcast's day-shape forecast directly instead of clear-sky
   geometry from a single scalar. Solcast already knows about the
   afternoon clouds and rain.
2. Stop chasing 100% SOC at end of day. Drain to overnight need
   (= effective `soc_keep`) so end-of-day excess is exported in the
   evening (high grid value) rather than sitting at 100%.
3. Single drain-target rule: `target = min(curtailment_floor, soc_keep)`.
   Both are "drain to" levels; lower wins.
4. Plugin runs while PV > 0, not until safe_time. Evening drain to
   `soc_keep` happens through the late afternoon.
   > **SUPERSEDED by RD6/R6 (v30, 2026-07-18):** this evening-drain-by-CM is
   > exactly the overreach removed in v30. CM now releases at safe_time; the
   > evening drain to overnight reserve is Predbat's job. See R6 and RD6.

### Changed Goal

> Prevent grid export exceeding 4kW DNO limit while delivering enough SOC
> by sunset to cover overnight + tomorrow's morning gap. Excess above the
> overnight requirement is exported during the PV window (preferring
> evening for grid value).

### Triage of R1-R52

**Kept unchanged (✅):** R1, R2, R3, R4, R8, R14, R15, R16, R16a, R17, R18,
R26, R27, R28, R29, R30, R34, R35, R36, R37, R38, R44, R47, R48, R49.

**Amended (✏️):**

- **R5** — activation condition becomes "is there work to do?". Plugin
  is Active when `target_soc < soc_max` (i.e. drain target below full)
  AND there is PV (or pre-PV drain conditions per R52 hold). Drop the
  "battery won't reach 100% even with all PV" gate from old R5.
- **R6** — deactivate at `PV ≤ 0.1 kW` (effective sundown), not at
  `safe_time`. After overflow window, plugin continues running to drain
  toward `soc_keep` through the evening.
- **R7** — REMOVED — superseded by R53 (Solcast per-slot is the basis).
- **R9** — same formula shape; `remaining_overflow` is now sourced from
  R53 (Solcast per-slot integral) not solar geometry. Tapered-cap part
  removed (R45 superseded by R57). Result: `curtailment_floor =
  max(0, soc_max − remaining_overflow × OVERFLOW_SAFETY_FACTOR)`.
- **R9a** — strengthened. `effective_load(t) = max(base_load,
  smoothed_loadml(t))` where `smoothed_loadml = rolling_mean(loadml,
  60min)`. The unsmoothed LoadML noise was the v5 failure mode; smoothing
  it lets us safely use Solcast per-slot shape (R53) without re-breaking
  v5.
- **R10** — final clamp becomes
  `target = max(min(curtailment_floor, effective_keep), reserve)` where
  `effective_keep` is `soc_keep` after R26+R48 adjustment. soc_keep is
  no longer added to the `max` clamp directly — it's inside the `min`.
  Reason: on big-overflow days R48 already drops `soc_keep` to ~2.8%
  so the inner `min` correctly drains low. On normal days `soc_keep`
  caps the drain via the inner `min`.
- **R11** — ratchet still applies, but to the OVERFLOW component only.
  When overflow integral falls and `target` switches over to
  `effective_keep` (curtailment_floor exceeds keep), no ratchet on the
  keep component — it can rise/fall freely as Predbat plan changes.
- **R13** — keep concept; integral is now Solcast-shaped (R53).
- **R19** — safe_time now demoted from deactivation trigger to
  diagnostic. Defined as "first time `remaining_overflow_integral = 0`".
  Used for sensor display; not used for control.
- **R20, R21** — keep semantics, but only relevant for the safe_time
  diagnostic now (no functional consequence).
- **R39** — keep concept; integral reference updated to R53.
- **R42** — scale stops being structural. Kept only as a calibration
  knob feeding R58 (live recalibration of next ~30 min of slots).
- **R43** — REPLACED by R58. Old `floor_scale = max(p_scale,
  actual_scale)` collapsed p10/p50/p90 into one number whenever actual
  exceeded any band, destroying the spread that R50 needs.
- **R46** — REMOVED — its purpose (LoadML phantom
  underestimating overflow) is addressed at source by R9a smoothing
    - R53 Solcast slots. Deactivation rule moves to R6 (PV ≤ 0.1).
  > **Note (2026-07-28):** this triage lists R46 under *both* Amended and
  > Removed. Removed is correct.
- **R50** — operates on per-slot Solcast bands (`pv_estimate10` /
  `pv_estimate` / `pv_estimate90` summed per band), not three copies
  of `max(p_scale, actual_scale)`. Confidence blending unchanged.
- **R52** — pre-PV drain stays. Pre-PV target reformulated:
  `min(soc_keep + buffer, effective_keep)`. The two-stage mechanic
  (coarse pre-PV drain at full DNO, fine post-PV drain) is unchanged.

**Removed (❌):**

- **R7** — see above.
- **R12** — "at safe_time, floor = soc_max, plugin deactivates". Both
  parts gone: floor → effective_keep, plugin runs to PV ≤ 0.1.
- **R45** — tapered cap to 100% at safe_time. The "fill battery before
  MSC handoff" mechanism is exactly the behaviour we're removing.
  Replaced by R57.
- **R46** — see Amended.

### New Requirements

- **R53** (overflow integral source). The remaining-overflow integral
  uses Solcast per-slot pv_estimate kWh, integrated forward from now to
  end of PV. Form:

  ```text
  remaining_overflow = Σ_slots max(0,
                          solcast_slot_kwh
                          − effective_load_kwh(slot)
                          − dno_kwh_per_slot)
  ```

  Per band (R50): the same integral with `pv_estimate10` /
  `pv_estimate` / `pv_estimate90`. The clear-sky `scale × sin(elev)`
  model is no longer used inside the integral. Solcast already encodes
  the day-shape (cloud, rain, ramp), and discarding shape was the
  v18 failure mode.

- **R54** (single drain-target rule). At every plugin cycle:

  ```text
  target_soc = max(min(curtailment_floor, effective_keep),
                   reserve, DEEP_DISCHARGE_FLOOR_KWH)
  ```

    - `curtailment_floor` from R9 (Solcast-shaped via R53).
    - `effective_keep` is `soc_keep` after R26 (plan-time reduction)
    and R48 (live big-overflow relaxation latch).
    - `reserve` is the absolute physical floor (battery/inverter limit).
    - `DEEP_DISCHARGE_FLOOR_KWH = 0.5` — the drain target never
    falls below this regardless of `reserve` or overflow size.
    - `min` because both numbers are "drain TO this level"; lower wins.
    - `max` clamp guarantees we never request below `reserve` nor below
    the deep-discharge floor.

  Trade-off: when `effective_keep < curtailment_floor` (modest overflow
    - low overnight need), the rule drains slightly lower than curtailment
  strictly requires. Accepted in exchange for a single uniform rule
  across the day with no phase switch.

  **Deep-discharge floor (2026-05-19).** On an extreme-overflow day
  `curtailment_floor` (= `overflow_floor`) goes to 0 and R48 has relaxed
  `effective_keep` to 0.5 kWh. The inner `min(0, 0.5)` is 0, and with
  Predbat's `reserve` also 0 the drain target reaches absolute empty —
  observed live 2026-05-19 with the battery at 0.0% SOC. R48 deliberately
  relaxes keep to 0.5 (not 0); the inner `min` must not undo that. The
  `DEEP_DISCHARGE_FLOOR_KWH` (0.5 kWh ≈ 2.8% of soc_max) term in the
  outer `max` keeps a deep-discharge buffer. 0.5 kWh of headroom is
  negligible against a multi-kWh overflow (the battery is slammed full
  mid-day regardless) but protects the cell from a full bottom-out. This
  applies only to the drain target (`compute_drain_above` /
  `sensor.predbat_curtailment_drain_above`); the published `charge_below`
  is separately clamped to `soc_keep`.

- **R55** (overnight target sourced from morning gap).
  `effective_keep` is set in `on_before_plan` (R26) to
  `morning_gap + R55_MARGIN_KWH` where `morning_gap =
  compute_morning_gap(tomorrow_pv, tomorrow_load)` and
  `R55_MARGIN_KWH = 0.5`. R48 may further relax effective_keep on
  big-overflow days via the existing latch (down to 0.5 kWh).
  Published as a sensor (`sensor.predbat_curtailment_overnight_target`)
  for dashboard visibility.

- **R56** (plugin active while PV > 0) — ❌ **SUPERSEDED by R6/RD6.** CM must not
  own the evening drain; that is Predbat's £-optimisation. Kept for reasoning. The plugin is Active for the
  whole PV window (R52 pre-PV drain → through PV peak → through
  late-afternoon drain to `effective_keep`) until `pv_power ≤ 0.1 kW`.
  After PV stops, plugin deactivates and Predbat MSC takes over for
  overnight. Drain mode through the late afternoon will pull from
  battery to grid (round-trip cost) — accepted because evening kWh has
  higher grid value than midday curtailment, so net positive.

- **R57** (no 100% chase). Plugin never targets `soc_max` as the drain
  target. End-of-day SOC ≈ `effective_keep` on most days. Battery only
  reaches 100% if PV physically overcharges past the cap (e.g. a true
  no-load mid-day with battery already at `effective_keep`). R45
  superseded.

- **R58** (actual_scale as live calibration only). `actual_scale` is
  applied as a multiplier to the next 30 min of Solcast pv_estimate
  slots, capped at 1.5×. Beyond 30 min, Solcast slots are used as-is
  (preserving day-shape). Replaces R43's global override which
  collapsed p10/p50/p90 to a single value whenever actual exceeded p90.

  ```text
  if actual_scale > 0 and within next 30 min:
      slot_kwh_used = solcast_slot_kwh × min(1.5, actual_scale_ratio)
  else:
      slot_kwh_used = solcast_slot_kwh
  ```

  where `actual_scale_ratio = actual_pv_last_30min / solcast_last_30min`.

### Order of work (TDD)

For each item, write a FAILING test first (R36), then code, then
verify all existing tests still pass (R37 — never break production).

1. **R9a smoothing** (foundation for R53). Test: noisy LoadML with
   1 kW transient should not change the integral by more than 5%.
2. **R53 per-slot integral**. Test fixture: today's actual data
   (clear morning, rain afternoon). Old code returns ~16 kWh;
   new code should return < 2 kWh.
3. **R55 overnight target sensor**. Test: with mild overnight forecast
   `morning_gap = 4 kWh`, sensor publishes `4.5 kWh / 25%`.
4. **R54 single rule**. Test matrix from triage examples 1-4:
   target should be `min(curt, keep)` clamped above reserve.
5. **R57 / R45 removal**. Test: plugin never targets `soc_max` after
   `remaining_overflow → 0`. Target falls to `effective_keep`.
6. **R56 plugin active until PV=0**. Test: at 16:00 with overflow=0,
   plugin still Active and Drain mode if SOC > effective_keep.
   Plugin Off at PV=0.
7. **R58 actual_scale live calibration**. Test: `actual_scale=2.0`
   only multiplies next 30 min of Solcast slots; remaining-day shape
   preserved. Cap at 1.5× respected.
8. **R50 per-slot bands**. Test: p10 / p50 / p90 overflow integrals
   produce DIFFERENT values when fed Solcast bands with realistic
   spread (not collapsed by R43, which is removed).

### Items still flagged for discussion

- **R49** kept for now (user decision). Re-evaluate after R53 +
  R50-on-bands ship — if they fully address the "Solcast over-
  forecasted today" failure mode, R49 becomes redundant.
- **R48** kept for now (user decision). The relaxed-keep latch is
  what makes target=2.8% work on huge-overflow days under R54.
- **Round-trip loss in evening drain** (R56). Empirical question:
  on a no-overflow but high-SOC day, is evening drain from battery
  to grid actually net-positive? Worth instrumenting after deploy.

---
