# Batpred Project Instructions

## Curtailment Manager

Before modifying ANY curtailment file (curtailment_plugin.py, curtailment_calc.py, tests/test_curtailment.py, or the HA automation `curtailment_manager_dynamic_export_limit`):

1. **Read `apps/predbat/REQUIREMENTS.md` first** — it contains the definitive requirements (R1-R30)
2. **Check every change against the requirements** — do not remove, weaken, or bypass any requirement
3. **If a requirement seems wrong**, discuss with the user before changing it. Update REQUIREMENTS.md if agreed.
4. **R25 is the key design principle**: once PV-load > DNO, we have NO control levers. All management (drain/charge/hold) must happen BEFORE overflow. Never remove the drain mechanism.

## Stock Predbat files (Charter — Working practices)

**Do not edit upgrade-overwritten stock Predbat modules** (`config.py`, `plan.py`, `fetch.py`, `predbat.py`, etc.) for site behaviour. A Predbat update overwrites them and silently undoes the patch. Put site logic in our tree: `curtailment_*`, plugins, `ha/*.yaml`, HA entities. Prefer expert mode / existing switches over patching stock config gates.

## TDD for Curtailment

When a flaw is found: **write a failing test FIRST**, then fix the code. Never deploy a fix without a test that would have caught the bug. Never break production code to make tests pass (R36/R37).

**Watch it FAIL, and fail for the right reason.** A test that has never failed has
proved nothing. If it passes the moment you write it, you have not pinned the
behaviour — you have written a description.

**Never let the failure mode into the allowed set.** Assert the SPECIFIC expected
value, not a set of acceptable ones. Broken twice on 2026-08-05: the new
forecast-tracking metric was shipped with

```python
assert attrs["tracking_band"] in ("below p10", "p10-p50", "p50-p90", "above p90", "unknown")
```

`"unknown"` IS the failure mode, so the test passed on the exact defect it existed
to catch, twice, through two deploys. It was only found by reading the live value.
**If you cannot name the value it should be, you do not understand it well enough
to ship it.**

**The rig is not production — check the shapes.** Four rig-fidelity gaps surfaced in
one day (2026-08-05), each hiding a real defect: MockBase's lat/lon is 55.86N when
`zone.home` is 52.31N; plant SOC was absent so every rig hit the A0 fail-closed
hold; `_peak_pv` was set without `_peak_pv_time`, so `actual_scale` computed as 0;
and `_make_p90_sensors` supplied only `pv_estimate90`, so the other two bands were
always 0. A green test on an unfaithful rig is not evidence. When adding a
diagnostic, **read the live value after deploying** — that is the only check that
caught any of these.

## Key Curtailment Lessons (2026-04-05)

- **Activation = "is there a problem?"** (excess > headroom). **Floor = "what's the solution?"** (how much to drain). Keep them separate.
- Use **TOTALS** (Solcast remaining, LoadML sum) not per-slot overflow. Per-slot shape only for TIMING (overflow window, release fraction).
- **Tomorrow sensor is simple**: excess vs headroom. Floor is live's job.
- Never break production for tests (R37). If tests fail but production works, fix tests.
- Every deploy resets plugin state (_peak_pv, cache). Be aware during live debugging.
- **Do not arbitrarily remove features** without checking REQUIREMENTS.md. The drain mechanism was removed twice and had to be restored both times.
- **Discuss before coding** when the approach is uncertain. Don't iterate through 10 broken deploys mid-day.

## Diagnosing ownership — use `predbat.status`, NOT the read_only switch

**`switch.predbat_set_read_only` lags Predbat's internal state by hours.** On
2026-08-03 CM wrote `read_only -> False` at 20:16:06, Predbat went Read-Only →
**Demand** by 20:20:30, and the switch entity still read `on` at 20:30 (unchanged
since 20:05:23). Apparent "changes" at 22:56 / 14:55 on other days are the entity
being reconciled, not the mode changing.

Reading the switch instead of `predbat.status` cost a long live misdiagnosis on
2026-08-03 — it looked like CM's write path was broken. It is not.

Also: **every change to that switch forces a full inverter reset to defaults**
(charge/discharge disabled, rates to full, reserve to default —
`docs/customisation.md:38`, `config.py` `reset_inverter_force: True`). It is
designed as an occasional human mode switch, not a high-frequency mutex. See
plan §11 O3/O4.

## HA Automation Version Control

The HA automation `curtailment_manager_dynamic_export_limit` is stored in:
`apps/predbat/ha/curtailment_manager_dynamic_export_limit.yaml`

**Never change the automation in HA without first updating and committing this file.**
After changing in HA, pull the updated config and commit it so the file stays in sync.

## Pre-Deploy Checks

- `pre-commit run --all-files`
- `cd apps/predbat && python3 tests/test_requirements_implemented.py` — every IN FORCE requirement has code behind it (catches R16a-class drift)
- `cd apps/predbat && python3 tests/test_curtailment.py`
- `cd apps/predbat && python3 tests/test_yaml_curtailment.py` — Jinja harness for the curtailment HA automation YAML
- `cd apps/predbat && python3 tests/test_yaml_voltage_seek.py` — Jinja harness for voltage_seek_controller YAML (catches variable-order bugs under StrictUndefined)
- `cd apps/predbat && python3 tests/test_yaml_heartbeat.py` — heartbeat dispatch + the ESS/import limit re-open (regressed twice; see below)
- `cd apps/predbat && python3 tests/test_yaml_inverter_fault_alert.py` — fault-alert diagnosis (clamped battery vs meter fault)
- `cd apps/predbat && python3 tests/test_yaml_requested_mode.py` — Predbat→SIG mode mapper
- `cd apps/predbat && python3 tests/test_yaml_dhw_meter.py` — GSHP DHW cycle meter (delta template + the once-per-day / sustain guards)
- `cd apps/predbat && python3 tests/test_plugin_host_contract.py` — fails if the Predbat build lacks the `on_before_plan` host API
- `cd apps/predbat && python3 tests/test_soc_keep_publish.py` — effective `best_soc_keep` sensor (read by `/soc-keep-review`)
- `cd coverage && python3 ../apps/predbat/unit_test.py --quick`
- **Commit before deploying** — always `git commit` before `scp`/deploy so deployed code is always in git history

## HA Automation Edits — MANDATORY workflow

Editing an HA automation Jinja via MCP `python_transform` can silently
reorder the `variables` dict. HA evaluates variables top-down, so a
reordered dict can leave templates referencing Undefined values, falling
through every branch silently. This caused a 2-hour controller outage on
2026-05-06.

Before any HA automation edit (`ha_config_set_automation`):

1. Edit the YAML file in `apps/predbat/ha/` first, in repo
2. Run the matching Jinja harness — it MUST pass with new behaviour asserted
3. Apply the change to HA via `config:` (full replacement), NOT
   `python_transform` (which reorders dict keys)
4. After deploy, query the live entity and verify it produces an
   expected value for a known input — never just check that the
   automation is "on" or "running"

## Writer Ownership (SIG control) — learned the hard way 2026-07-28

**Exactly one writer enabled is NOT sufficient.** Predbat writes PLANT registers
via THREE mapper automations, and disabling them stops further writes but leaves
everything they already wrote in place:

| Automation | Writes | Driven by |
|---|---|---|
| `predbat_requested_mode_action` | EMS control mode, `grid_import_limitation` | `input_select.predbat_requested_mode` |
| `predbat_max_discharging_limit_action` | `ess_max_discharging_limit` | `input_number.discharge_rate` |
| `predbat_max_charging_limit_action` | `ess_max_charging_limit` | `input_number.charge_rate` |

Rules:

1. **The writer that changed a register changes it back.** `_neutralise_predbat()`
   sets Predbat's own INPUTS back to neutral so Predbat's own mappers unwind the
   registers — before the chain is disabled. Never enumerate registers in CM; we
   missed two that way.
2. **Setting mode to MSC does NOT clear the ESS clamps** — those hang off the RATE
   helpers, not the mode.
3. **The heartbeat re-opens all three registers on every active-policy run** as a
   backstop, and mirrors the rate helpers (writing only the register leaves the
   helper at 0, so the next change re-clamps).
4. **Under manual override the writer role still follows the policy select** —
   override means the user owns the POLICY, not that the role goes stale.

Symptom of getting this wrong: SOC flat for hours under an active policy, battery
power 0.000, dispatch commanded but nothing moving. Happened 2026-07-26 (fixed by
5bbdedeb) and again 2026-07-28 (the v8.46.4 port predated that commit, so
redeploying the heartbeat from the ported file dropped it).
