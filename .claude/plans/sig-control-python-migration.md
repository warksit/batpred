# SIG control — move actuation into Python, delete the HA automation layer

**Status:** proposal, **deferred**. Written 2026-08-06. Not the current path —
CM is stable with edge fine-tuning only; autumn is Predbat-dominant. See
`.claude/memory/sig-control-maintenance.md` and `apps/predbat/ha/README.md`.
Revisit only if multi-writer pain returns (earliest: next high-overflow season).
`sig-control-v2` was recovered on origin (Predbat EMS half only — not Hold/PCS).

**For reviewers:** this document is self-contained. §1–§5a are background and evidence,
§6 is the proposal, §7 separates verified fact from assumption, §8 records what was tried
and discarded. If you disagree, §7 and §11 are where to push.

**Principal known risk — please scrutinise.** Today, if Predbat dies, the HA heartbeat
keeps running and the plant stays driven. Under this proposal the plant would sit on its
last setpoint under `PCS Remote Control` indefinitely with nothing to park it. That is a
**safety regression** and the proposal does not currently solve it; see §11 Q4. The
dormant `curtailment_stale_phase_watchdog.yaml` was exactly this idea and was never
deployed. A deliberate single-automation watchdog may be the right exception — but it
would reintroduce one HA writer, so it needs designing, not bolting on.

**Author's caveat on provenance.** The evidence in §3 comes from one intense day of live
debugging by the author of this document, who also caused several of the defects listed.
The measurements are from HA history and automation traces and are reliable; the
*diagnosis* in §4 is the author's own and has not been independently reviewed. That is
what this review is for.

---

## 1. The system as it stands

A SigenStor EC 6.0 SP (18.08 kWh, DC-coupled) at a UK site with a **3.68 kW DNO export
limit** and a **6.6 kW inverter AC rating**. Predbat (a Home Assistant app) plans battery
charge/discharge against forecasts and tariffs. A site-specific **Curtailment Manager
(CM)** plugin runs inside Predbat and takes the wheel during the solar-overflow window to
minimise curtailment.

Five things can write inverter control registers today:

| Writer | Kind | Registers |
|---|---|---|
| `sig_dispatch_heartbeat` | HA automation, 1-min beat + event triggers | EMS mode, export/import limits, both ESS limits, the dispatch setpoint |
| `predbat_requested_mode_action` | HA automation | EMS mode, import limit |
| `predbat_max_discharging_limit_action` | HA automation | ESS max discharge |
| `predbat_max_charging_limit_action` | HA automation | ESS max charge |
| `curtailment_plugin._park_ems_msc` | Python | EMS mode |

The first is "CM driving"; the middle three are "Predbat driving". **The mutex between
them is enabling and disabling automations** (`curtailment_plugin._set_writer`,
`curtailment_plugin.py:2880`). Handing over therefore means changing *which software
writes*, as a multi-step sequence.

## 2. Vocabulary (site convention — do not interchange)

- **Clipped** — PV exceeds the **inverter's** AC capacity (6.6 kW).
- **Curtailed** — PV export exceeds the **DNO** limit (3.68 kW), so the MPPTs back off.

Being DC-coupled, battery charging does not consume AC output capacity, so the two limits
bind independently. Everything in this document concerns **curtailment**; clipping is a
separate loss not addressed here.

## 3. Evidence — 2026-08-06

Eight defects in one day, on a day forecast at ~20 kWh of overflow. Selected, with
measurements:

| # | Defect | Evidence |
|---|---|---|
| 1 | A sell-clamp fix landed in one of **two** copies of the dispatch maths | Found by reading deployed JSON, not by tests |
| 2 | The regression test for #1 **passed against the broken YAML** | The two copies differed by 0.048 kW; the trigger's own tolerance is 0.1 kW |
| 3 | The `stale_setpoint` trigger ignored the manual override, disarming the fast corrector whenever a human was driving | Battery discharged to **536 W** under Hold through a PV sag 08:15–08:20, mean −0.118 kW |
| 4 | Mid-window handback existed in **three** places (plugin decision path, plugin acting path, keep-floor guard) | First fix changed only the published reason; second found by a failing test; third found by grep |
| 5 | Handback cleared `read_only` but left the three mappers **disabled** — nobody drove | 04:50→09:00 the plant ran on its own default, charging 3.5 kW with **zero export** while surplus (3.54 kW) was still under the 3.68 cap. ~2% of pack spent on headroom the peak then curtailed |
| 6 | A refactor to remove duplication introduced an **ordering race** | Trigger fired on `input_select.sig_override`; action read the derived sensor, which had not recomputed. Trace shows `policy="Predbat"`, `dispatch_kw=0.68` — both stale — branch taken, nothing written, 12 ms. Manual override did nothing for **47 s** |
| 7 | The keep-floor guard wrote `policy = Predbat`, cancelling manual overrides ~3 min after being set | Guard fired 11:38:25.79; select changed 11:38:25.85 |
| 8 | A published card stated a threshold the code was not using | Card read "charge if below 2.8%"; `compute_proposed_phase` uses `min(charge_below, drain_above)` = 1.0% |

## 4. Diagnosis

These are not eight problems. Every one is a consequence of **control authority being
distributed across writers with no single owner and no atomic transition**:

- the same rule implemented N times, and N−1 edited (#1, #4)
- transitions that move *some* of the state (#5, #7)
- a mutex made of automation enables, which can half-complete (#5)
- ordering races once a value is derived but its inputs are still the trigger (#6)
- no single entity answers "who is driving and why" (#5, #8)

**Why the HA layer exists at all:** CM's dispatch uses an **absolute power setpoint**
(`number.sigen_plant_active_power_fixed_adjustment` under `PCS Remote Control`). That is
open-loop — it must be recomputed whenever PV or load moves. Predbat's plan loop is
5-minutely, so the fast loop was pushed *out* of Predbat into HA automations. Defects
#1, #2, #3 and #6 are all artefacts of that fast loop.

**The counter-model:** Predbat controls GivEnergy entirely in Python — compute → setter →
`write_and_poll_value` → read back → retry (`inverter.py:1990`). One process, one writer,
closed loop, no intermediary, and *verification the automations do not have*.

## 5. Definition of Hold (agreed with Andrew, 2026-08-06)

This is the policy that determines whether the fast loop can be eliminated, so it is
stated precisely.

**Keep the battery level where it is, and move it only when the alternative is worse.**

| Condition | Behaviour | Why the level may move |
|---|---|---|
| PV > load + cap | export at cap; battery absorbs `pv − load − cap` | **curtailing** is worse |
| load < PV ≤ load + cap | export `pv − load`; battery **flat** | — |
| PV < load | battery covers `load − pv`; no import | **importing** is worse |
| at the drain floor | import covers the shortfall | deep-discharge is worse |

Holding the level is the objective; **"export before charge" is the mechanism, not the
aim.** The battery is a buffer for the house and for un-exportable PV, and never a source
of export.

One line: `inverter_AC_output = min( max(pv, load), load + cap )` — which is exactly the
formula in production today, checked against all three regimes.

**Consequence:** charging is a *last resort*. Maximum Self Consumption's premise is the
opposite — charge before export — so **MSC cannot express Hold.** This is conceptual, not
a matter of tuning.

## 5a. Modbus evidence

Source: `TypQxQ/Sigenergy-Local-Modbus`, cloned to
`/Users/home/Documents/code/Sigenergy-Local-Modbus` (sibling of batpred, so it does not
pollute the repo; refresh with `git -C … pull`). It is a faithful transcription of
Sigenergy's Modbus spec; mode *semantics* beyond the names live in Sigenergy's own
Appendix 6, which is not in that repo.

Writable plant registers relevant here (`modbusregisterdefinitions.py`):

| Addr | Name | Type | Notes |
|---|---|---|---|
| 40001 | `plant_active_power_fixed_target` | **S32**, gain 1000, kW | **Signed** — one register for charge and discharge |
| 40005 | `plant_active_power_percentage_target` | S16, [-100.00, 100.00] % | alternative to absolute kW |
| 40029 | `plant_remote_ems_enable` | U16 | 0/1 |
| 40031 | `plant_remote_ems_control_mode` | U16 | the nine modes below |
| 40032 / 40034 | `plant_ess_max_{charging,discharging}_limit` | kW | |
| 40038 / 40040 | `plant_grid_point_maximum_{export,import}_limitation` | kW | **grid connection point — the DNO cap** |
| 40042 / 40044 | `plant_pcs_maximum_{export,import}_limitation` | kW | "takes effect globally" |
| 40046 | `plant_backup_soc` | %, [0,100] | |
| 40047 | `plant_charge_cut_off_soc` | %, [0,100] | **firmware charge ceiling** |
| 40048 | `plant_discharge_cut_off_soc` | %, [0,100] | **firmware drain floor** |

Read-only reflections of the cut-offs exist at 30085/30086 — do not confuse them with the
writable setpoints at 40047/40048.

`RemoteEMSControlMode`: `PCS_REMOTE_CONTROL=0 · STANDBY=1 · MAXIMUM_SELF_CONSUMPTION=2 ·
COMMAND_CHARGING_GRID_FIRST=3 · COMMAND_CHARGING_PV_FIRST=4 ·
COMMAND_DISCHARGING_PV_FIRST=5 · COMMAND_DISCHARGING_ESS_FIRST=6 · RESERVED=7 · V2G=8`.

**Two conclusions:**

1. **The firmware drain floor is real** — 40048 is writable with the right range. A2 is
   verified, not assumed.
2. **No mode expresses Hold.** None of the nine holds the level with export priority, so
   Hold retains a computed setpoint. This is now grounded in the register map rather than
   inferred from mode names.

**Opportunity not previously known:** `plant_charge_cut_off_soc` (40047) is writable, so a
headroom *ceiling* can be firmware-enforced. That bears directly on the A1 ceiling
decision deferred on 2026-07-31 — the plant could enforce "do not fill above X%" with no
software in the loop. Out of scope here; worth a separate look.

## 6. Proposal

Actuation moves into Python, in the standard Predbat inverter driver. The HA automation
layer is deleted.

### 6.1 How each policy is expressed

```
Max Export   PCS Remote Control, setpoint 6.6 (FLAT — no tracking)
             + grid_export_limitation = DNO cap        hardware clamps export
Hold         PCS Remote Control, setpoint min(max(pv,load), load+cap)
Solar Charge PCS Remote Control, setpoint = load
             (alt: Command Charging (PV First) + grid_import_limitation = 0)
Drain floor  ess_discharge_cut_off_state_of_charge = floor%     FIRMWARE
Predbat      its existing EMS modes, written directly by the driver
```

### 6.2 Hold needs a live executor — and it must be a dedicated task

**An earlier draft of this document claimed Predbat's existing 15 s `update_time_loop`
could serve as the Hold executor. That was wrong, twice over.** Recording the correction
because a reviewer should see the reasoning was tested, not assumed.

**Wrong 1 — the 15 s loop is not a control loop.** It runs every 15 s
(`predbat.py:1642`) but only acts when `update_pending` is set, and the only thing that
sets it is `watch_event`, which *also* sets `plan_valid = False`
(`userinterface.py:376-382`). The resulting path is the full cycle —
`load_user_config` → `validate_config` → `update_pred(scheduled=False)` →
`create_entity_list` (`predbat.py:1695-1701`), i.e. fetch, the multi-threaded window
search, execute and publish. Putting PV/load on the `watch_list` would attempt a **full
re-plan on every PV change**; the `prediction_started` guard would drop most and the rest
would peg the CPU.

**Wrong 2 — the regime argument only held in steady state.** The original claim was that
the peak regime tracks `load + cap` (slow), so the stakes and the tracking demand are
inversely correlated. That is true *within* a regime and false *across* one. Broken
cloud, PV 8.5 → 2.0 kW in seconds:

```
before   peak regime, setpoint = load + cap = 4.08, battery absorbing ~4.4 kW
after    correct setpoint = max(pv, load) = 2.0
until rewritten: plant targets 4.08 with 2.0 kW of PV
                 -> battery DISCHARGES ~2.1 kW
```

The error scales with the PV swing and is not self-limiting. This is the 536 W sag
mechanism (#3) an order of magnitude larger, and it defeats Hold's entire purpose.

**Therefore Hold requires a live executor** — reacting to PV/load changes in roughly the
time the current heartbeat does (its `stale_setpoint` template trigger re-evaluates on
every PV/load state change and fires immediately past a 0.1 kW divergence).

**This does not require an HA automation.** A Predbat component can spawn a long-lived
asyncio task independent of both the 60 s component tick and the 5-min plan loop —
`sigenergy.py:2374` does exactly this (`asyncio.ensure_future(self._mqtt_listener_loop())`).
The design becomes:

```
plan loop (5 min)     sets INTENT: policy + parameters (floors, cap)
executor task (live)  sole writer of the dispatch register; reconciles it
                      against PV/load/SOC with the existing 0.1 kW deadband
HA WebSocket          feeds the executor via a lightweight callback that does
                      NOT set plan_valid = False (i.e. not watch_event)
```

Same intent/actuator separation as before, but wholly inside one Python process: one
writer, verified writes, no HA automations. It is more work than reusing an existing
loop — a real task with lifecycle, error handling and a single-owner rule for the
register — and the plan should be costed accordingly.

### 6.3 What this removes

| Today's failure mode | Why it cannot recur |
|---|---|
| Two copies of the dispatch maths | One implementation, in Python |
| Writer-role swap half-completing | No writer role — one process always writes |
| Dead zone with nobody driving | The driver is never disabled |
| Trigger/derived-value race | No HA triggers involved |
| Guard cancelling a manual override | Floor is firmware; guard deleted |
| Stranded registers after a transition | Driver reconciles and reads back |

## 7. Claims — verified vs assumed

**Reviewers: this is the section to attack.**

| # | Claim | Status | Basis |
|---|---|---|---|
| V1 | `write_and_poll_value/_option` work with any `number/select/switch` entity | **Verified** | `inverter.py:1990,2048` — inverter-agnostic |
| V2 | Python can already write SIG plant registers | **Verified** | `curtailment_plugin.py:2816` does it in production |
| V3 | The 15 s loop is a full-re-plan trigger, **not** a control loop | **Verified** | `predbat.py:1642,1695-1701`; `watch_event` sets `plan_valid=False` (`userinterface.py:376`). **Retracts an earlier claim that it could host the Hold executor** |
| V4 | HA state changes reach Predbat sub-second | **Verified** | `ha.py:557` → `trigger_watch_list`. Note the existing path forces a re-plan; the executor needs a lighter callback |
| V10 | A component can run a long-lived async task | **Verified** | `sigenergy.py:2374` `asyncio.ensure_future(self._mqtt_listener_loop())` |
| V5 | Today's Hold formula matches §5 | **Verified** | Checked against all three regimes; live 12:00 export 3.60 at cap, battery +4.56 |
| V6 | The drain-floor register exists and is currently 0.0 | **Verified** | `number.sigen_plant_ess_discharge_cut_off_state_of_charge` |
| V7 | `sigenergy.py` is a cloud/MQTT client, not a register driver | **Verified** | 6 s rate limit, no cut-off SOC, no EMS mode |
| V8 | A writable firmware discharge floor exists | **Verified** | `plant_discharge_cut_off_soc` addr 40048, HOLDING, [0,100.0] (§5a) |
| V9 | No EMS mode expresses Hold | **Verified** | All nine `RemoteEMSControlMode` values enumerated (§5a) — none holds level with export priority |
| A1 | A Python executor task can track PV as well as the current HA heartbeat does | **Assumed** | Mechanism verified (V10); achievable latency untested. **Step 2 tests it.** If it cannot, Hold keeps an HA executor and only the mappers go |
| A2 | The plant *obeys* the cut-off floor in practice under our modes | **Assumed** | Register exists and is writable (V8); behaviour under PCS Remote Control untested. **Step 1 tests it** |
| A3 | The lost branch is recoverable | **Assumed** | Andrew says it is on another machine |
| A4 | Deleting the mappers does not break Predbat's own control | **Assumed** | Predbat expresses intent via helpers; the driver would consume them directly. Needs review |
| U1 | Whether `Command Charging (PV First)` is a better Solar Charge than a setpoint | **Unknown** | Never used. Not on the critical path |
| U2 | Rebase cost of the recovered branch | **Unknown** | 1093 commits ahead of `origin/main` |

## 8. Alternatives considered and discarded

- **Keep HA automations, add a single arbiter entity.** Was the previous draft. Fixes
  duplication but keeps two actuators, the enable/disable mutex and the trigger races.
  Strictly worse than removing the layer.
- **`ess_max_charging_limit = 0` for Hold.** Wrong — blocks absorption at the overflow
  peak, which is the one thing that must not happen (§5 row 1).
- **`MSC + grid_export_limitation` for Hold.** Wrong — MSC charges before exporting, so
  the battery fills early, losing both headroom and export revenue (§5).
- **`sigenergy.py` (cloud API) as the control path.** 6 s rate limit, no access to cut-off
  SOC, EMS mode or the setpoint; requires VPP onboarding which disables app control.

## 9. Steps

**Step 1 — firmware drain floor.** Set `ess_discharge_cut_off_state_of_charge` to the
drain floor. Deletes `sig_keep_floor_guard` entirely: its 3-minute lag, RD27's third copy
of the mid-window handback, and defect #7. Small, independent, valuable regardless of the
rest. Tests A2.

**Step 2 — build and prove the executor task.** A dedicated asyncio task that owns the
dispatch register and reconciles it against PV/load with the existing 0.1 kW deadband,
fed by a lightweight WebSocket callback (not `watch_event` — that forces a re-plan).
Measure against the current heartbeat on the case that matters: a **PV swing across
`load + cap`** on a broken-cloud day. Log commanded setpoint, `battery_power` and
`grid_export_power`. Success = battery excursions no worse than the heartbeat achieves
today. Tests A1. **Do not proceed past this on assumption** — if the task cannot match
the heartbeat, Hold keeps an HA executor and only Step 5.1 (the mappers) proceeds.

**Step 3 — recover `sig-inverter-control`.** Andrew pushes it; assess rebase cost. Adopt
rather than rewrite; fall back to `.claude/plans/sig-refactor.md` +
`sig-mode-table-tests.md` + `tests/inverters/sig/__init__.py` as spec if recovery fails.
Independent of 1–2.

**Step 4 — repoint the driver at the plant.** `templates/sigenergy_sigenstor.yaml`: map
rates and charge/discharge services at real `number.sigen_plant_*` / `select.sigen_plant_*`
entities instead of `input_number.charge_rate` / `input_select.predbat_requested_mode`.
`config.py:1835`: set `has_reserve_soc: True` (the template already names the cut-off SOC
at line 73; the flag is the only thing disabling it). Revisit `has_target_soc`.

**Step 5 — delete the automation layer**, in order:
1. the three `predbat_*_action` mappers → removes the second actuator, `_set_writer`,
   `_neutralise_predbat`, `_release_to_predbat`, the enable/disable mutex
   (`curtailment_plugin.py:2803-2913`)
2. `sig_dispatch_heartbeat.yaml` — after Step 2 passes
3. `sig_keep_floor_guard.yaml` — floor is firmware; dusk release becomes a plan decision
4. already-dormant files: `curtailment_manager_dynamic_export_limit.yaml`,
   `curtailment_stale_phase_watchdog.yaml`, `voltage_seek_controller.yaml`,
   `voltage_throttle_filter_asymmetric_rate_limit.yaml`, and the orphan duplicate under
   `ha_automations/`

CM keeps what it is for — the plan (floors, forecasts, policy) — and expresses it through
Predbat's control surface rather than a parallel one.

## 10. Verification

- Revive `apps/predbat/tests/inverters/sig/__init__.py` (per-mode expected write sequence)
  and wire it into the runner; nothing imports it today.
- New: write-verify test — for each policy, assert the driver writes exactly the expected
  register set and reads it back.
- Keep passing: `test_curtailment`, `test_yaml_*` (until their automations are deleted),
  `unit_test --quick`.
- Live, after each step: a **discriminating** check — conditions under which old and new
  behaviour differ — plus actual register values. A state read at a non-discriminating
  moment is not evidence (this error was made twice on 2026-08-06).
- Land every step in a low-stakes window, never mid-curtailment.

## 11. Open questions for a reviewer

1. **Is A1 sound?** The regime-transition case is now the *design* case, not an edge
   case (§6.2). Can an in-process asyncio task genuinely match an HA template trigger for
   latency, given Predbat also runs a heavy 5-minute plan in the same process? What
   happens to executor latency *during* a plan run?
2. **A4** — does anything in Predbat rely on the mapper automations existing, rather than
   on the helpers they read?
3. Should **Solar Charge** use `Command Charging (PV First)` + `grid_import_limitation = 0`
   instead of a setpoint? It would remove one more tracked value (U1).
4. Is there a case for keeping **one** HA automation as a watchdog — something that parks
   the plant safely if Predbat dies? Today nothing does this; the dormant
   `curtailment_stale_phase_watchdog.yaml` was that idea and was never deployed.
