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

## Pre-Deploy Checks

- `pre-commit run --all-files`
- `cd apps/predbat && python3 tests/test_curtailment.py`
- `cd coverage && python3 ../apps/predbat/unit_test.py --quick`
