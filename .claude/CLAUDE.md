# Batpred Project Instructions

## Curtailment Manager

Before modifying ANY curtailment file (curtailment_plugin.py, curtailment_calc.py, tests/test_curtailment.py, or the HA automation `curtailment_manager_dynamic_export_limit`):

1. **Read `apps/predbat/REQUIREMENTS.md` first** — it contains the definitive requirements (R1-R30)
2. **Check every change against the requirements** — do not remove, weaken, or bypass any requirement
3. **If a requirement seems wrong**, discuss with the user before changing it. Update REQUIREMENTS.md if agreed.
4. **R25 is the key design principle**: once PV-load > DNO, we have NO control levers. All management (drain/charge/hold) must happen BEFORE overflow. Never remove the drain mechanism.

## TDD for Curtailment

When a flaw is found: **write a failing test FIRST**, then fix the code. Never deploy a fix without a test that would have caught the bug. Never break production code to make tests pass (R36/R37).

## Key Curtailment Lessons (2026-04-05)

- **Activation = "is there a problem?"** (excess > headroom). **Floor = "what's the solution?"** (how much to drain). Keep them separate.
- Use **TOTALS** (Solcast remaining, LoadML sum) not per-slot overflow. Per-slot shape only for TIMING (overflow window, release fraction).
- **Tomorrow sensor is simple**: excess vs headroom. Floor is live's job.
- Never break production for tests (R37). If tests fail but production works, fix tests.
- Every deploy resets plugin state (_peak_pv, cache). Be aware during live debugging.
- **Do not arbitrarily remove features** without checking REQUIREMENTS.md. The drain mechanism was removed twice and had to be restored both times.
- **Discuss before coding** when the approach is uncertain. Don't iterate through 10 broken deploys mid-day.

## HA Automation Version Control

The HA automation `curtailment_manager_dynamic_export_limit` is stored in:
`apps/predbat/ha/curtailment_manager_dynamic_export_limit.yaml`

**Never change the automation in HA without first updating and committing this file.**
After changing in HA, pull the updated config and commit it so the file stays in sync.

## Pre-Deploy Checks

- `pre-commit run --all-files`
- `cd apps/predbat && python3 tests/test_curtailment.py`
- `cd apps/predbat && python3 tests/test_yaml_curtailment.py` — Jinja harness for the curtailment HA automation YAML
- `cd apps/predbat && python3 tests/test_yaml_voltage_seek.py` — Jinja harness for voltage_seek_controller YAML (catches variable-order bugs under StrictUndefined)
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
