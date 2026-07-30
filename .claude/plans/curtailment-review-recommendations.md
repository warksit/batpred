# Curtailment Manager — Architecture Review & Simplification Recommendations

**Date:** 2026-07-30 (rev 3 — exec summary + dashboard UX)  
**Status:** Recommendations only. No code until you choose the open questions below.

---

## Executive summary (read this)

### What CM should do
Keep enough empty battery so midday surplus PV is not wasted, then hand control back to Predbat. Over-draining costs overnight import; under-draining costs curtailed solar. That trade-off is the whole product.

### What's wrong
The idea is simple; the code is a pile of incident patches: dual formulas, many latches, ownership spread across 6 entities, a 2k-line requirements doc that disagrees with itself, and possibly **two plugin instances both writing policy** if both file trees exist on the box.

### What to do (short path)

| When | What | Why |
|------|------|-----|
| **Now (A0)** | Fix dual-load if present; fail closed on bad sensors; alerts; measure curtailment vs overnight import | Safe, may explain flaps, no control-law change |
| **Next (A1)** | One floor formula + fix dawn_load; **own deploy** after a ceiling decision | Fixes known 06:15 jump / overshoot — *is* a control change |
| **Then (B)** | Reconstruct peak PV after deploy; one ownership state machine | Stops mid-day deploy chaos + writer bugs |
| **Later (C)** | Split giant `calculate()`; shrink requirements; delete dead paths (R50 etc.) carefully | Maintainability |
| **Only if still awful** | Thin rewrite of pure `decide()` | Optional |

### Dashboard ("Why This Mode") — separate UX debt
Your screenshot is a good example of why this is hard to run live:

- **% and kWh mixed** on every line (13% vs 1.9–3.0 kWh band; 12.8% · 2.31 kWh; 10.3% (1.87 kWh)…)
- **Wall of words** restating the same decision three ways
- **Not at-a-glance**: you should see in 1 second: mode, SOC vs band, why

Target shape (illustrative — not coded yet):

```
Hold · 13%
Band  11% ──●──────── 17%     (or kWh-only if you prefer one unit)
Why   headroom short 1.2 kWh · no_drain off
```

One unit system (prefer **%** for the band card; put kWh in a detail expand). Card must **mirror** plugin `reason`, never re-derive (Charter). Treat as **A0-adjacent UX** once the decision string is clean — not a reason to delay dual-load check.

### Decisions locked (2026-07-30)

| Topic | Choice |
|-------|--------|
| First work | **A0 + full diagnostics UX** |
| Live HA | **mums_middlemuir_homeassistant** |
| Authority during A0 | **Repo + HA + deploy/restart Predbat** (no further ask for A0 scope) |
| Why This Mode units | **% SOC only** at a glance; kWh in expand/detail only |
| UX depth | **Full diagnostics pass**: card + reason string + clean redundant CM diagnostics (no re-derive; card must not show ×1.2 if code is 1.05) |
| Dual-load if found live | **Investigate and report only — no delete on the box yet** (repo fix for `plugin_system` skip-if-loaded is still in scope as defence) |
| Pre-PV legacy ceiling | **Decide after A0 baseline** (not in A0) |
| After A0 | **Stop and report** — wait before A1 / B |

### A0 scope (explicit)

**In:**
1. Verify dual-load on box (logs/files); report; no live file delete yet  
2. `plugin_system`: skip if `plugin_name` already loaded (defence in depth)  
3. Fail closed on missing/defaulted SOC (hold, publish loudly)  
4. Invariant alerts (minimal set)  
5. A/B visibility if missing (curtailment condition minutes, overnight import)  
6. Why This Mode: at-a-glance % band; plugin publishes clean attributes; card reports only  
7. Diagnostics pass: remove/hide redundant noise on CM view  
8. Deploy/restart as needed for A0  

**Out of A0:**
- Floor formula unification / dawn_load / ceiling (A1)  
- Ownership state machine (B)  
- Deleting dual tree on the live box (until you say so)  
- R50 / Tier 2 deletions (C)

---

## 1. Executive diagnosis (detail)

You are right: **the premise is simple and the implementation is not.**

**The real job (one sentence):**

> Before and during the PV overflow window, keep battery headroom ≥ worst-case remaining overflow so PV above `load + DNO` is absorbed rather than curtailed — then hand the machine back to Predbat.

**Everything else is either:**
- a **necessary trade-off** against overnight import (failure mode B), or
- **accidental complexity** from months of incident-driven patches, dual formulas, layered overrides, and a requirements document that outgrew the code.

| Failure | Cost |
|--------|------|
| **A. Under-drained** | Curtailed PV (lost generation) |
| **B. Over-drained** | Overnight import at peak rates + wasted cycle |

**Verdict:** Stabilize and measure first; hold live control changes to their own deploys. Do not add another latch.

---

## 2. What is actually essential

Strip to the physics and you get a small machine:

```
1. Estimate remaining overflow energy (p90, Solcast per-slot)
2. Compute drain floor: soc_max − required_headroom(overflow)
3. Compute charge floor: overnight need netted against P10 remaining PV
4. charge_target = min(charge_floor, drain_floor)   # curtailment wins on cross-over
5. Schmitt band on SOC → Charge / Hold / Drain
6. Own the inverter only inside the window; one writer; release at sundown
```

**R25 remains the design north star:** headroom is cheap early, impossible late. That asymmetry justifies p90 and acting early — it does *not* justify five ways of saying “does the surplus fit?”.

Supporting pieces that *are* load-bearing (keep in some form):

| Piece | Why keep |
|-------|----------|
| Pre-PV drain on big-overflow mornings | R25: create headroom before lockout |
| Hardware DNO 3.68 kW (SIG MPPT) | Software export cap is obsolete |
| Single-writer ownership (heartbeat vs Predbat mappers) | Real, repeated outages without it |
| Manual override as one select | RD13a — dual boolean+select was a bug factory |
| Overflow meters (2026-07-29) | First honest ground truth for A |
| Pure calc module + plugin I/O split | Testable core — good architecture |

---

## 3. Complexity inventory (evidence)

| Artifact | Size / measure | Signal |
|----------|----------------|--------|
| `curtailment_plugin.py` | **2,817 lines** | Orchestrator + state + sensors + ownership |
| `calculate()` | **~630 lines; ruff McCabe = 49** (ratchet pin as of 2026-07-28; currently passes). A looser AST branch count ~76 is **not** McCabe — do not use it for the ratchet. | Single function owns the day |
| `curtailment_calc.py` | **1,369 lines** | Mostly clean pure functions |
| `REQUIREMENTS.md` | **2,091 lines** | Charter + status index + history archaeology |
| `test_curtailment.py` | **6,852 lines / 241 tests** | Calc covered; loop failures not |
| HA automations under `ha/` | **17 YAML files** | Control split across plugin + many automations |
| Interesting instance fields | **~50** | Latches, engaged flags, dual floors, history |
| Named constants in plugin | **~50** | Many tunables, some dormant |
| Dual copies | root (30 Jul) vs `plugins/` (25 Jul) | ~610 / ~249 line diffs; discovery scans **three** dirs |

**Complexity tooling:**
- `.flake8` has `max-complexity = 15` but **flake8 is not in pre-commit**.
- Pre-commit runs **ruff C901 on CM files only**, pinned at **49** (today’s `calculate()`).
- Tier 1 target “no function above 15” is measured against **ruff McCabe**, matching `.flake8`. Enabling the existing flake8 complexity limit **only for the four CM files** (plugin + calc × root, once dual tree is gone) is nearly free once the ratchet is lowered by extractions.

---

## 4. Root causes (why it keeps getting harder)

### 4.1 Incident-driven layering, not redesign

Almost every RD16–RD20 and R48/R61/R63 is a **latch for a flap or edge case** observed on one day. Each latch is locally correct and globally opaque. The control law is no longer “SOC vs band”; it is “SOC vs band, unless override, unless R63, unless pre-PV hold, unless session, unless no_drain, unless manual…”.

### 4.2 Dual paths for the same quantity (seams)

Documented live behaviour (2026-07-30) in rebuild context:

1. **pre-PV target** (`compute_pre_pv_target`) applies `min(legacy, floor_driven)`  
2. **post-PV floor** (`compute_floor_with_source` / R9 path) does **not**  
→ Floor jumped **0.80 → 1.87 kWh at 06:15 with forecast flat**, then Solar Charge bought back the dawn drain.

**Important correction:** the `min(legacy, floor_driven)` ceiling is **deliberate R62 design**, not accidental drift. The docstring states the R62 formula may only ever be *more aggressive* than R52’s static value, “so cloudy/uncertain mornings behave exactly as before.” The 06:15 jump is that ceiling **coming off at the pre-PV → post-PV transition**. See item **0.3** — do not “unify by deleting the ceiling” without choosing which side wins.

`dawn_load` is today inside a `max()` (`DEEP_FLOOR + dawn_load` as one arm) rather than applied after the floor choice — so when `overflow_floor` dominates, dawn load has no effect and the drain overshoots. See item **0.3b** for the exact restructure.

### 4.3 Ownership is a convention, not a state machine

Writer role is distributed across:

`read_only` · heartbeat enable · 3 Predbat mappers · override select · policy select

Failures (real): mappers disabled but registers left clamped; handback left `read_only` on; CM re-took writers at night on bad data; guard watched `policy` while `override` drove. Rebuild context §5 is accurate: **one transition function + verify by reading back**.

### 4.4 Doc/code drift is structural, not accidental

- Status index (2026-07-28) was built *from code* because prose was wrong.  
- Body text still contradicts the index in places (R6 “deactivate at safe_time” vs v32 “safe_time → Hold override”; R11 removal text still claims R43 is gone in one bullet while the index says R43 is live).  
- `test_requirements_implemented.py` checks markers exist, **not semantics**.  
- Docstring on `calculate()` still mentions R11 ratchet and safety factor 1.25.

When the doc is trusted and wrong, people “fix” correct code (nearly happened on cross-over precedence 2026-07-30).

**Fix that sticks:** each of ≤20 surviving normative rules gets a `test_r<NN>_semantics` test; the checker asserts the test exists (item **0.7**).

### 4.5 Tests pass while the plant misbehaves

241 tests cover pure calc. Real faults live in:

- ownership / handback  
- bad sensor defaults (`soc_kw` → 0.0 on failed read → “battery empty”)  
- seams between paths  
- deploy wiping in-memory state  

There is **no closed loop**: commanded policy ≠ actual registers is not asserted.

### 4.6 Duplicate plugin trees — possible **live double-writer** (escalated)

`plugin_system.discover_plugins` scans **three** dirs in **fixed** order (`plugin_system.py:104–108`):

1. `apps/predbat/` (same dir as predbat files) — **newer**, 30 Jul  
2. `apps/predbat/plugins/` — **stale**, 25 Jul  
3. `apps/predbat/../plugins` — parent plugins  

Scan order is deterministic, not “whichever wins by luck.”

**What actually happens:**

1. Root copy loads first → constructs `CurtailmentPlugin` A → `register_hooks()` **appends** A’s `on_update` / `on_before_plan` callbacks.  
2. `plugins/` copy loads second → constructs instance B → **overwrites** `self.plugins["curtailment_plugin"] = B` → `register_hooks()` **appends** B’s callbacks.  
3. Overwriting the dict entry does **not** unregister A’s hooks (`self.hooks` are append-lists).  

**Result:** both instances remain live on every hook cycle — ~610 lines of plugin diff and ~249 of calc diff apart — both computing floors and both potentially writing the same `sig_dispatch_policy` / ownership controls. `get_plugin()` returns the **stale** B instance; hooks fire **both**.

This is not merely a footgun. It is a **plausible live double-writer** and a candidate contributor to unexplained flaps.

**Deploy note:** production is often flat-copied to `/addon_configs/`, so the live box *may* only have one tree. That must be **verified on the box**, not assumed from this repo. Regardless, `plugin_system` must refuse a second load of the same `plugin_name` so deleting the tree is not the only defence.

### 4.7 Dead / dormant weight still in the hot path

- R50 confidence blend: dormant but code + helpers + tests remain (**21** plugin refs, **6** calc refs, two HA helpers become orphans on delete)  
- p10/p50 overflow integrals always computed for diagnostics  
- Voltage-throttle effective DNO sampling, R49 buffer reduction, R58 calibration  
- Saving-session planning half still in plugin while dispatch moved to heartbeat  

Each adds branches and mental load even when “not the live path.” **Deletions are Phase C**, with REQUIREMENTS status change + new semantic test — not Phase A drive-bys (see Tier 2).

### 4.8 No loss function in code or metrics

Rebuild context §2 is the most important unfixed design gap. Without daily:

- minutes (or kWh) in curtailment condition  
- overnight import kWh  
- optionally: cycle count / drain depth  

…tuning debates have no arbiter and recurrence is guaranteed.

### 4.9 State lost on every deploy

`_peak_pv`, R64 overflow smooth history, cap samples, and various latches reset on process restart. Mid-day deploy/debug is unreliable. Identified in rebuild Q7; previously had no Tier item — now **1.1**.

---

## 5. Recommendations (ranked)

### Tier 0 — Stabilize and measure (behaviour-neutral first)

| # | Action | Why | Behaviour on a normal day |
|---|--------|-----|---------------------------|
| **0.0** | **Dual-load / double-hook** — (a) **verify on the box** which `curtailment_plugin.py` paths exist under `/addon_configs/` (and whether logs show two “Successfully loaded plugin: curtailment_plugin” / two “Initialising plugin class” lines); (b) **delete or archive** `apps/predbat/plugins/curtailment_*.py` in the repo; (c) **fix `plugin_system.load_plugin`**: if `plugin_name` already in `self.plugins`, log a warning and **skip** (do not construct, do not `register_hooks`). Optionally also skip if any hook list already has a callback from that name. | Possible live double-writer; flaps | Neutral once only one instance runs (may *stop* a latent race) |
| **0.1** | *(folded into 0.0)* Dual tree cleanup is part of 0.0, not a softer “footgun” item. | — | — |
| **0.2** | **Fail closed on missing inputs.** If SOC / critical sensors are unavailable or defaulted, **hold position, change nothing, publish loudly**. Never treat missing SOC as 0. | Night re-take outage 2026-07-29 | Neutral when sensors healthy |
| **0.5** | **Invariant alerts** (HA or plugin): (a) CM active outside window; (b) two writers enabled; (c) commanded policy ≠ EMS mode for N minutes; (d) curtailment condition true; (e) overnight import > threshold after a drain day. | Closes the loop tests miss | Alert-only |
| **0.6** | **Publish A and B daily** from existing meters: overflow daily + curtailment-condition minutes + overnight import. Dashboard “Why This Mode” reports plugin `reason` only. | Objective function; baseline before control changes | Neutral |
| **0.7** | **Shrink REQUIREMENTS Current Spec** to ~15–20 normative rules that match code. For each surviving rule: `Implemented in:` + **`test_r<NN>_semantics`** that asserts behaviour (not a marker string). Extend `test_requirements_implemented.py` so **missing semantic test = fail**. Body that contradicts the status index is deleted or fixed the same day. | Doc hazard; makes 0.7 stick | Neutral (doc/tests) |
| **Docstring** | Fix `calculate()` docstring drift (R11, safety 1.25). | Trust | Neutral |

**Explicitly not in the first deploy:** item **0.3** (control change) — see Phase A split.

---

### Item 0.3 — Floor unification (separate deploy; answer first)

**Do not ship as “delete the legacy ceiling.”**

#### Decision required before coding

| Option | Meaning | Effect |
|--------|---------|--------|
| **A. Ceiling everywhere** | `min(legacy, floor_driven)` applies pre-PV **and** post-PV | Cloudy mornings stay conservative all day; big-overflow days may under-drain post-PV if legacy is high |
| **B. Ceiling nowhere** | Floor is always pure overflow/recovery math | Removes 06:15 jump; cloudy mornings may drain more aggressively than R52 ever did (A↔B trade **without** measurement if done blind) |
| **C. Ceiling pre-PV only** *(today)* | Keep intentional R62 asymmetry | Accept 06:15 discontinuity **or** smooth the transition another way |

**Recommended default to analyse (not yet commit):** prefer **B only if** DC-coupled meters + a few cloudy mornings show legacy is redundant given p90 + charge_below; otherwise prefer **A** (ceiling as optional arg, default on) so cloudy behaviour does not silently regress. **C is status quo** and may remain temporarily with dawn_load fixed alone.

Whatever is chosen: **unify with the ceiling as an argument** to one function (plan’s own “one quantity, one definition”), and **pin with a test**.

#### 0.3a — One floor function

```text
compute_drain_floor(
    overflow_kwh, soc_max, reserve, max_reserved_kwh, safety_factor,
    dawn_load_kwh=0.0,
    legacy_ceiling_kwh=None,   # None = no ceiling; set for pre-PV if Option C/A
) -> floor_kwh
```

Pre-PV and post-PV call the **same** function; differences are arguments, not separate expressions.

#### 0.3b — dawn_load restructure (spell the formula)

**Today (double-count risk if “just add” naively):**

```text
floor_driven = max(reserve, DEEP_FLOOR + dawn_load, overflow_floor)
target       = min(legacy, floor_driven)   # pre-PV only
```

If you add `dawn_load` to the chosen target **and** leave `DEEP_FLOOR + dawn_load` inside the max, the deep-floor branch **double-counts**.

**Correct restructure:**

```text
core   = max(reserve, DEEP_FLOOR, overflow_floor)
target = core + max(0.0, dawn_load_kwh)
if legacy_ceiling_kwh is not None:
    target = min(legacy_ceiling_kwh, target)
```

- `dawn_load` is **always additive after** the core floor choice.  
- It is **never** an arm of the `max()`.  
- Tests: (1) overflow dominates → target = overflow_floor + dawn_load; (2) deep floor dominates → target = DEEP_FLOOR + dawn_load (once, not twice); (3) ceiling binds when provided.

#### 0.3 deploy rules

- Own commit/deploy after Phase A baseline is running on the overflow meter.  
- REQUIREMENTS status note for R62/R52 relationship updated to match the chosen option.  
- Semantic tests pin the option.

---

### Tier 1 — Structural simplification of the decision core

Goal: replace “latched override soup” with an **explicit small state machine** and one formula.

| # | Action | Why |
|---|--------|-----|
| **1.1** | **State survival across deploy.** Reconstruct `_peak_pv` (and peak time if possible) from HA history on startup when state file is empty/stale for today. Persist overflow smooth window if cheap. Prefer reconstruct over ever-growing latch sets. | Kills mid-day deploy confusion; belongs **before** ownership state-machine work |
| **1.2** | Ownership state machine: `acquire_cm()` / `release_predbat()` / `apply_manual(policy)` — full mapper chain, neutralise Predbat, park EMS, policy, `read_only`, **then verify** key entities. | Repeated 07-26…07-30 |
| **1.3** | Decision state machine (Idle / PreDrain / Manage / HoldOnly / Released) mapping today’s overrides. | Opacity of override soup |
| **1.4** | Split `calculate()` along phase boundaries (one extraction per commit); lower ruff pin from 49 toward 15; optionally enable flake8 C901 on CM files only. | Edit risk |

```
States:  Idle | PreDrain | Manage | HoldOnly | Released
Events:  big_overflow_morning | pv_arrived | overflow_fits | sundown | fault | manual
```

| State | Behaviour |
|-------|-----------|
| Idle | Predbat owns writers |
| PreDrain | Max Export toward **one** floor (pre-PV) |
| Manage | Schmitt Charge/Hold/Drain on band |
| HoldOnly | No drain (fits or past safe_time); Charge still allowed for evening reserve |
| Released | Predbat owns; CM observe-only |

Map today’s overrides into states:

| Today | Becomes |
|-------|---------|
| `_policy_override = hold` (pre-PV wait) | PreDrain complete → HoldOnly until PV |
| `no_drain` | HoldOnly |
| R63 `max_export` | Manage with forced Drain (or temporary PreDrain urgency) |
| Manual override | Parallel “user owns policy” mode; **writer role still follows CM-active** |
| Session dump | Heartbeat-only (already RD14c); plugin only plans reserve energy |

---

### Tier 2 — Delete or freeze features (Phase C only)

Be ruthless **and** disciplined. Per `feedback_check_before_removing` / R25 history (drain removed twice and restored twice):

**Every Tier 2 deletion requires:**
1. REQUIREMENTS status change (IN FORCE → REMOVED/SUPERSEDED) with Why / Removing this would  
2. A **new** semantic test asserting the post-deletion behaviour  
3. **Not** merely deleting the old tests  

| Candidate | Recommendation | Notes |
|-----------|----------------|-------|
| R50 confidence blend | **Delete** in Phase C | ~21 plugin + 6 calc refs; two HA helpers orphan; large blast radius — **not Phase A** |
| Geometry energy as dual primary | Energy = Solcast p90 only; geometry for **timing** only | One sentence in requirements + semantic test |
| R49 cloudy buffer reduction | Evaluate with meters; freeze or remove with status change | |
| R58 live calibration | Same | |
| Effective DNO (R60) | Keep only if throttle still bites post-swap | |
| R48 multi-latch keep relaxation | Re-express as one rule if possible | |
| Dual HA YAML paths (`ha/` vs `ha_automations/`) | One versioned path | |
| `curtailment_manager_dynamic_export_limit` | Confirm still needed post-v30; retire if heartbeat owns export | |

---

### Tier 3 — Rebuild shape (only if Phase C still feels like archaeology)

A rebuild rewrites the **CM decision core**, not Predbat. Hard budgets **by concern** (so they are not abandoned):

| Concern | Budget | Today (approx) |
|---------|--------|----------------|
| Pure `decide()` core | ≤ ~400 lines | buried in `calculate()` |
| Ownership / writers | ≤ ~200 lines | scattered `_set_writer`, release, neutralise |
| Publish / sensors | ≤ ~400 lines | large `publish()` + diagnostics |
| Pure calc | ≤ ~600 lines | 1,369 (trim dormant) |
| Normative requirements | ≤ 20 | 2,091-line doc with history |
| Tests | calc + ownership integration + invariant properties + `test_r*_semantics` | calc-heavy |

Sensor publishing and ownership are not going away — **do not** set a single “≤800 line plugin” number that silently fails. Split the budget per concern.

**Start with the loss function** (rebuild context Q1–Q2):

```
score = w_A * curtailment_kwh + w_B * overnight_import_kwh + w_C * extra_cycles
```

Derive `OVERFLOW_SAFETY_FACTOR` and `MAX_RESERVED_KWH` from measured A/B over DC-coupled weeks — not April AC-coupled fixtures.

**Proposed core API (sketch):**

```python
@dataclass
class CmInputs:
    soc_kwh: float | None          # None = unavailable
    soc_max: float
    overflow_p90_kwh: float
    load_remaining_kwh: float
    p10_pv_remaining_kwh: float
    overnight_target_kwh: float
    actual_pv_kw: float
    peaked: bool
    now_utc: datetime
    safe_time_utc: datetime | None
    manual_policy: str | None

@dataclass
class CmDecision:
    state: str                     # Idle|PreDrain|Manage|HoldOnly|Released
    policy: str                    # Predbat|Max Export|Hold|Solar Charge
    drain_above_kwh: float
    charge_below_kwh: float
    reason: str                    # single string, dashboard mirrors it
    cm_driving: bool
```

One pure function: `decide(inputs, prev_state) -> CmDecision`.  
Plugin: read HA → `decide` → apply ownership + publish `reason` only (dashboard never re-derives).

---

### Tier 4 — What not to do

1. **Do not add another latch** for the next flap without first asking: “is this a dual-formula seam or missing fail-closed?”  
2. **Do not re-tune from April fixtures** (AC-coupled understated overflow). Use meters.  
3. **Do not expand CM into Predbat’s job** (evening £ optimisation, session £ dump beyond reserve planning).  
4. **Do not keep History text readable as current** — the Charter already forbids this; enforce it.  
5. **Do not break production to make tests pass (R37)** — and do not let green tests mean “safe to deploy” without invariant checks.  
6. **Do not mid-day deploy-debug** without accepting state reset — unless 1.1 is done.  
7. **Do not delete Tier 2 features** without REQUIREMENTS status change + new semantic test.  
8. **Do not ship 0.3 with footgun cleanups** — cannot attribute the next day’s outcome.

---

## 6. Suggested programme (practical sequence)

### Phase A0 — Footguns + baseline (1–2 days) — **no intentional control change**

- **0.0** verify on box → fix `plugin_system` skip-if-loaded → remove dual tree from repo  
- **0.2** fail-closed on bad SOC  
- **0.5** minimal invariant alerts  
- **0.6** A/B daily sensors (if not already complete) — overflow meter already exists; ensure curtailment-condition + import are visible  
- Docstring drift fix  
- Let the overflow meter + A/B run for a **baseline window** before any floor change  

### Phase A1 — Floor control change **alone** (own deploy)

- Answer ceiling question (A / B / C) in writing in REQUIREMENTS  
- **0.3** one floor function + **0.3b** dawn_load formula as spelled above  
- Semantic tests pin ceiling policy and dawn_load cases  
- Attribute next-day A/B metrics to this deploy only  

### Phase B — Ownership + observability hardening (3–7 days)

- **1.1** reconstruct `_peak_pv` from HA history (ahead of 1.2)  
- **1.2** ownership state machine + verify  
- Integration tests: handback sequence, dual-writer forbidden, missing SOC holds, dual plugin load skipped  

### Phase C — Decompose + freeze/delete (1–2 weeks, low risk per commit)

- **1.4** extract `calculate()`; lower complexity ratchet  
- **0.7** collapse REQUIREMENTS + `test_r*_semantics` gate  
- Tier 2 deletions one at a time (R50 last among the large ones, with full ref cleanup)  

### Phase D — Optional thin rebuild of `decide()`

- Only if Phase C still feels like archaeology  
- Swap plugin to call new pure core behind a flag  
- Replay against overflow meters + a week of DC-coupled history before cutover  

---

## 7. Answers to the rebuild-context questions

| # | Question | Recommendation |
|---|----------|----------------|
| 1 | Objective function? | Explicit: minimise curtailment kWh, then overnight import, then extra cycles. Write weights once. |
| 2 | What is measured? | Curtailment-condition minutes + overflow meter + overnight import — not p90 calibration alone. **Baseline before 0.3.** |
| 3 | Where does ownership live? | One transition module; verify registers after transition. |
| 4 | Missing data? | Hold position; change nothing; scream. |
| 5 | Seams? | One floor function; **ceiling is an explicit argument** after a written decision (not silent deletion). |
| 6 | Drain target aim? | `core = max(reserve, DEEP_FLOOR, overflow_floor)` then **`+ dawn_load`** (never double-count). |
| 7 | Survives deploy? | **1.1:** reconstruct `_peak_pv` from HA history; persist only day-key + irreducible state. |
| 8 | Doc/code agreement? | ≤20 rules; each has `Implemented in:` + **`test_r*_semantics`**; checker fails if test missing. |

---

## 8. Bottom line

**CM is overcomplicated relative to its charter**, not because the physics is hard, but because:

1. The loss function is unstated and unmeasured  
2. The same questions have multiple code paths  
3. Ownership is distributed  
4. Every incident added a latch instead of removing a seam  
5. The requirements document became a second, conflicting system  
6. **(New)** Discovery can register **two live CM instances** with different code on the same hooks  

The path out:

> **One overflow estimate → one floor (ceiling policy explicit) → one small state machine → one writer → measure A and B → delete everything that does not serve that — with dual-load fixed first and control changes deployed alone.**

---

## 9. Decision for the user

**Adopt this plan** with the following confirmed stance:

| Item | Stance |
|------|--------|
| Dual-load | **0.0** — verify on box, fix `plugin_system`, remove dual tree |
| 0.3 ceiling | **Answer A/B/C first**; unify with ceiling as arg; own deploy after baseline |
| dawn_load | `target = max(reserve, DEEP_FLOOR, overflow_floor) + dawn_load` then optional ceiling |
| Semantic tests | Part of **0.7** — required gate |
| State survival | **1.1** in Tier 1, before ownership SM |
| Phase A | Split: A0 footguns+baseline, then A1 control |
| R50 / Tier 2 | Phase C only; REQUIREMENTS + new semantic test |

**Still open (needs a user call before A1):**

- Ceiling policy: **A** (everywhere), **B** (nowhere), or **C** (pre-PV only, status quo)  

Recommended analysis order: gather cloudy-morning A/B from meters under current C; then choose A or B deliberately. Do not default to B without that look.

**Overall sequence:** A0 → measure → A1 → B (1.1 then 1.2) → C → D only if needed.
