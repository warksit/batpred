# Curtailment Manager — Executive Summary & Rebuild Context

**Written 2026-07-30.** Companion to `apps/predbat/REQUIREMENTS.md`. That document
says *what the system does*; this one says *what it is for, what we actually know,
what is broken, and why it keeps being hard to build*. Read this first, then the
requirements.

---

## 1. What CM is for

CM does **one** thing: minimise curtailment, £-aware. Predbat owns everything else
— price arbitrage, evening export, saving sessions, the overnight plan. CM takes
the wheel only inside the curtailment window: pre-PV drain → real-time overflow
management → handback at `safe_time`.

The hardware cannot export more than the DNO cap. Any PV above `load + cap` must
go into the battery or be thrown away. CM's job is to make sure there is room in
the battery at the moment that surplus arrives.

---

## 2. The loss function — the thing that is nowhere in the code

There are exactly **two** failure modes and they are opposed:

| | cause | cost |
|---|---|---|
| **A. Under-drained** | battery too full when surplus arrives | PV curtailed — generation lost outright |
| **B. Over-drained** | battery too empty at dusk | overnight import at ~25p having exported at ~15p, plus a wasted cycle |

**Every tunable trades A against B.** `OVERFLOW_SAFETY_FACTOR`,
`MAX_RESERVED_KWH`, `OVERNIGHT_SAFETY_PCT`, the Schmitt band, the pre-PV target —
all of them.

This is the single most important thing that is not written down anywhere in the
system, and its absence explains most of the difficulty. Because the loss function
is unstated:

- We tune against **forecast calibration** (is p90 accurate?) which is only a
  *proxy* for the thing we care about.
- Nothing measures A or B, so no change can be evaluated.
- Arguments about constants have no arbiter and recur indefinitely.

**A rebuild should start here.** If we never curtail and import every morning, the
reserve is too large — regardless of how well-calibrated the forecast is.

---

## 3. Physical system (verified 2026-07-30)

| | |
|---|---|
| Inverter | SigenStor EC 6.0 SP, **DC-coupled** (SMA retired 2026-07-15) |
| Battery | 18.08 kWh usable |
| Inverter AC ceiling | 6.6 kW |
| DNO export cap | **3.68 kW**, hardware-enforced by SIG-managed MPPTs |
| Integration | **local Modbus TCP, 192.168.5.145:502** — not cloud |
| Predbat | v8.46.4, branch `cm-on-latest-predbat` |

Consequences that matter:

- DC coupling means clipped PV reaches the battery without passing the AC limit.
  **All pre-swap (April) measurements are therefore not comparable** — see §7.
- Modbus is local, so an internet outage should not break it. On 2026-07-29 it
  did break (`Failed to connect to 192.168.5.145:502`), which implicates the
  router/LAN, not the internet.
- Hardware discharge cut-off is **0% by design**. The software floor, not the
  BMS, is the operational protection.

---

## 4. Architecture as built

```
curtailment_plugin.py    strategy: computes floors, picks a phase, drives selects
curtailment_calc.py      pure functions (testable, no I/O)
        │
        ├── input_select.sig_dispatch_policy    ← CM's intent
        ├── input_select.sig_override           ← manual, OUTRANKS policy
        └── switch.predbat_set_read_only        ← CM↔Predbat mutex
                    │
     automation.sig_dispatch_heartbeat  ← sole register writer while CM drives
     automation.predbat_*_action  x3    ← sole register writers while Predbat drives
     automation.sig_keep_floor_guard    ← backstop, stops a sell at the reserve
```

**Exactly one writer must be enabled.** The heartbeat and the three Predbat
mappers are mutually exclusive; `read_only` is the mutex.

---

## 5. Control ownership — the most regression-prone part of the system

This has broken repeatedly (2026-07-26, 07-28, 07-29, 07-30). The invariant is
**distributed across six entities** with no single enforcement point:

`read_only` · heartbeat enable · three mapper enables · override select · policy select

Failure modes seen, all real:

- Mappers disabled but registers left where they were → SOC flat for hours.
- Handback cleared the policy but left `read_only` on → Predbat planned, could not act.
- CM re-activated at night on bad data and re-took the writer chain.
- The guard watched one layer (`policy`) while another (`override`) was driving.

**Design lesson:** ownership is currently a *convention* maintained by several
independent code paths agreeing. It should be a single explicit state machine with
one transition function, and the transition should be *verified* (read the
registers back), not assumed.

---

## 6. The decision CM makes

Two thresholds and a Schmitt band:

```
SOC < charge_below   → Solar Charge     (bank PV for the evening reserve)
SOC > drain_above    → Max Export       (sell down to make headroom)
between              → Hold
```

- `drain_above` = headroom floor, from **p90 overflow** (R9/R42/R43).
- `charge_below` = overnight floor, from **P10 generation** netted against the
  overnight need (R59b).
- Charge target is `min(charge_below, drain_above)` — so on a cross-over day
  **Drain wins**. Curtailment defence beats deficit insurance (R25: headroom is
  cheap early, impossible late; an over-drained battery refills, curtailed
  generation does not).
- `OUTER_THRESHOLD_KWH = 0.18` — Schmitt deadband on Drain *entry* only.

### Seams — where it goes wrong

The system has several boundaries where the same quantity is computed twice by
different code, and the two disagree:

1. **pre-PV target vs post-PV floor.** `compute_pre_pv_target` applies a
   `min(legacy, floor_driven)` ceiling that the post-PV floor
   (`compute_floor_with_source`) does not have. On 2026-07-30 the floor jumped
   **0.80 → 1.87 kWh at 06:15 with the forecast flat**, purely from crossing this
   seam — and Solar Charge then bought back what the dawn drain had just sold.
   **This is the live, unfixed bug.**
2. **`dawn_load` is a floor term, not compensation.** It appears only inside
   `max(reserve, DEEP_FLOOR + dawn_load, overflow_floor)`. When `overflow_floor`
   dominates it has *no effect*, so the drain hits target and then coasts below it
   on house load (2026-07-30: 6.05% → 4.10%). It should be **added** to the chosen
   target so we arrive *on* the floor.
3. **Historic:** `required_headroom_kwh` was computed by three different
   expressions that drifted; now unified. R49's buffer was used in one place and
   the constant in another. Same class of defect.

**Design lesson:** any quantity that two paths need must have exactly one
definition, and differences must be *arguments*, not separate expressions.

---

## 7. What is measured — and the trap in the old evidence

### New, as of 2026-07-29 (native HA, exact, permanent)

```
sensor.curtailment_overflow_power    template  max(0, pv − load − cap)
sensor.curtailment_overflow_energy   integration (Riemann)
sensor.curtailment_overflow_daily    utility_meter, daily cycle
```

Why this shape: the clipping is applied at **native sensor resolution** and only
then integrated, so the daily total survives HA's hourly downsampling exactly.

**This matters more than it sounds.** HA keeps 5-minute statistics for only ~10
days, then hourly forever. Reconstructing overflow from hourly means understates
it badly, because overflow is a convex function of power:

| | 26 Jul (broken cloud) | 19 Jul (clear) |
|---|---|---|
| from 5-minute data | 6.51 kWh | 15.87 kWh |
| from hourly means | 2.44 kWh (**37%**) | 14.88 kWh (94%) |

The error is worst on exactly the variable days that drive tuning.

### The trap: all pre-swap evidence is biased

The 11 April fixtures were measured through the **AC-coupled SMA**, which clipped
PV above the inverter ceiling — so measured "actual overflow" was *understated*,
which *flattered* p90.

```
                    actual/p90    implied margin in p90
April (11 days)     0.641 mean    56%
April worst day     0.79          27%
19 Jul (DC-coupled) 0.862         16%   ← much tighter
```

A recommendation to cut the safety factor to 1.05 was built on the April numbers,
withdrawn when the DC-coupled day contradicted them, then adopted anyway as a
deliberate user decision with the caveat recorded.

**Do not re-derive tuning from the April fixtures.** Use the meters.

### Still not measured — and these are the ones that matter

- **Did we actually curtail?** No derating flag exists on the SIG, so magnitude is
  unobservable. The *condition* is not: `SOC ≥ 99% AND export ≥ cap − 0.05 AND
  battery charge ≈ 0`. Minutes in that state per day = failure mode A.
- **Overnight import** = failure mode B. `sensor.sigen_plant_daily_grid_import_energy`
  already exists.
- **Belief at decision time** — what the plugin thought at the moment it drained.
  Continuous series are the wrong shape for this; a snapshot is exact.

---

## 8. Changes made 2026-07-29/30 (all committed, all deployed)

| commit | change |
|---|---|
| `a188a2ac` | `state_class` on numeric diagnostics (they were unrecoverable after the recorder window); floor components promoted to sensors |
| `da47e527` | the two Predbat rate mappers versioned into the repo + guarded |
| `428e86c7` | **the charging mapper referenced an entity that does not exist** — it had *never once* written `ess_max_charging_limit`. Stock config from `docs/inverter-setup.md`; the doc's own errata note was not applied at setup |
| `91445f39` | `OVERFLOW_SAFETY_FACTOR` 1.2 → **1.05**, with the refinement path documented |
| `ff4123eb` | keep-floor guard now acts on the **effective** policy and clears both layers; REQUIREMENTS.md cross-over precedence corrected |

Also: `input_number.curtailment_manager_enable` was cycled off/on during the
2026-07-29 outage recovery; ownership was manually restored and verified.

---

## 9. Recurring failure patterns — why this is hard to build

These are the actual obstacles. A rebuild should be judged on whether it removes
them.

1. **Doc and code drift, and the doc is trusted.** R16a was *required* for months
   with **zero** implementation (its home, a 5-second HA automation, was retired
   by v30 and nobody noticed). R42/R43 were marked replaced while still running.
   The cross-over paragraph stated the **opposite** of the code's actual safety
   rule and caused a live misdiagnosis on 2026-07-30 — correct code was nearly
   "fixed" to match wrong prose. `test_requirements_implemented.py` now checks a
   marker exists, but **not** that the semantics match.

2. **The tests pass while the system misbehaves.** 240+ tests are green and the
   detection mechanism for real faults has been the user looking at a phone. The
   tests cover *calculation*; the faults live in the *loop between calculation and
   inverter* — ownership, staleness, seams, bad reads.

3. **No closed loop at all.** Nothing asserts that commanded state equals actual
   state. Four invariants would have caught most of this year's incidents:
   commanded policy ≠ register state; curtailment condition detected; overnight
   import > 0; CM active outside its window.

4. **Bad data is substituted, not refused.** On a failed read Predbat defaults
   `soc_percent` to **0.0** — "battery empty", the input that drives charging.
   During the 2026-07-29 outage CM read `SOC=0.0kWh`, computed a degenerate
   100% floor, and re-took the writer chain at night. **On an unreadable input the
   correct behaviour is to change nothing.**

5. **Parts of the control chain were never in the repo.** Two automations that
   write plant registers existed only in HA — unversioned, unreviewed, untested.
   That is exactly how a dead entity reference survived indefinitely.

6. **Three naming conventions coexist** for the same physical quantity
   (`sigen_plant_ess_rated_charging_power`, `sigen_inverter_ess_rated_charge_power`,
   …). One mapper used a fourth combination that does not exist.

7. **~~All state is lost on deploy.~~ CORRECTED 2026-08-03 — mostly solved.**
   `7cdba1c0` added `curtailment_state.json` (same-day guard, atomic
   tmp + `os.replace`), which persists and restores `peak_pv_kw`,
   `peak_pv_time`, `pv_history` (R49), `cap_samples` / `yesterday_cap_avg`
   (R60), `last_floor_scale`, and the day latches. Verified live across the
   2026-08-03 19:19 deploy: `restored state from /config/curtailment_state.json
   (peak=7.68kW, pv_history=15 entries)`.
   **Still not persisted:** `_overflow_history`, the R64 rolling-median input
   (`deque(maxlen=24)`), so a restart degrades the median to short-history
   behaviour — conservatively, per
   `test_overflow_smoothing_degrades_safely_on_short_history` — until the 30-min
   trailing window refills.
   *This entry was written 2026-07-30, after the fix had already shipped, and
   was still believed on 2026-08-03 — it nearly deferred a deploy for a reason
   that no longer existed. Exactly the §4.4 doc-drift hazard this document warns
   about, committed by this document.*

8. **Layered precedence with unaware consumers.** `override > session > policy`.
   The keep-floor guard was written against `policy` alone, so under a manual
   override the floor silently did not exist.

---

## 10. Questions a rebuild must answer

1. **What is the objective function, explicitly?** Cost of curtailed kWh vs cost
   of imported kWh vs cycle cost. Write it down; make every parameter derive from
   it.
2. **What is measured to evaluate it?** Curtailment minutes and overnight import,
   not forecast calibration.
3. **Where does ownership live?** One state machine, one transition function,
   verified by reading registers back.
4. **How does the system behave on missing data?** Proposal: hold position,
   change nothing, and say so loudly.
5. **How are seams eliminated?** Should there be a pre-PV path at all, or one
   continuous floor function evaluated with a time argument?
6. **What is the drain *target* actually aiming at?** The floor now, or the floor
   at the moment PV meets load, plus the load consumed on the way?
7. **What survives a deploy?** Which state is genuinely necessary, and can it be
   reconstructed rather than persisted?
8. **How is doc/code agreement enforced** beyond "a marker exists"?

---

## 11. Live state at time of writing (2026-07-30 ~10:30)

- CM enabled, phase Active, `safe_time` 18:00, single writer (heartbeat), override Off.
- `OVERFLOW_SAFETY_FACTOR` 1.05 live and verified (`floor=2.2 kWh` where 1.2 gave 0.16).
- Overnight 29→30 Jul: drained to 4.1%, **0.01 kWh import**, no flapping.
- 29 Jul day total: **34.92 kWh exported, 0.01 kWh imported**, no curtailment observed.
- Overflow meter recording; **30 Jul is its first complete day**.

### Known-unfixed

1. The **pre-PV / post-PV seam** (§6.1) — dominant cause of the 06:15 Solar Charge.
2. **`dawn_load` not additive** (§6.2) — ~2 percentage points of overshoot.
3. `predbat.soc_kw` was ~1.8 kWh stale after the outage; confirm it cleared.
4. No invariant/alerting layer exists (§9.3).
5. `_overflow_history` (R64) is the only in-memory state not in
   `curtailment_state.json` — see §9.7 and review item 1.1.

### Corrected since writing (2026-08-03)

- §9.7 "all state is lost on deploy" was **already false when written** — see
  the entry. Persistence shipped in `7cdba1c0`.
- The **session dump was invisible** to every consumer: RD14c moved dispatch to
  the heartbeat, which forces Max Export off the calendar without writing the
  policy select, so `intended_policy` published the plugin's wish while the
  battery exported at the cap. Fixed 2026-08-03 (`cbe946f7`); the publish site's
  own `override > session > select` comment had described the missing rung for
  weeks. Another instance of §9.8 "layered precedence with unaware consumers".
