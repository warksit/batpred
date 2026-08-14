# Batpred — working charter

Organised by **moment**, not by incident. At any decision point one block applies;
read that block, not the file. If you are about to write a reply, edit code,
diagnose a symptom, or claim something is done — the checklist is below.

Everything here was paid for with a live failure. Nothing is generic advice.

---

## Before you REPLY

1. **Count the asks.** Two sentences usually means two asks. Answer every part or
   ask about every part. Explaining away the half you did not understand is the
   move that loses the actual issue.
2. **A question is not an instruction.** "Should we X?" invites thought, not
   deployment. Answer it and stop.
3. **A reported symptom IS an instruction**, even phrased as an observation.
   "The % doesn't show" is a bug report.
4. **Findings go to `.claude/PARKED.md`, not the reply.** Raise one mid-task only
   if it blocks the ask. No "while I was there I noticed". Andrew pulls that list.
5. **Am I reporting THEIR issue as fixed, or mine?** If the reply contains work
   nobody asked for, delete it and finish the ask.

## Before you touch CODE

1. **Scope line first**: `Scope: fixing X. Not touching Y, Z.` If you cannot write
   it, you do not understand the request — ask one question instead.
2. **Read what already exists** — the function, its callers, the helper you are
   about to duplicate, the tests you are about to extend. Most defects here come
   from writing before reading, not from writing badly.
3. **Enumerate the sites.** grep the *concept* (entity, threshold, policy name),
   not the code in front of you. If a quantity legitimately lives in two places it
   belongs in the **Duplicate-logic index** in `REQUIREMENTS.md`, with a test that
   renders every copy and asserts they agree.
4. **Read `REQUIREMENTS.md` before any curtailment file** (`curtailment_*.py`,
   `tests/test_curtailment.py`, `ha/*.yaml`). Requirements are R1-R64 + RD1-RD40;
   the **Status index** table beats the prose. Do not remove or weaken one without
   agreeing it first.
5. **Disagree in one sentence, then comply.** Not a redesign.

## Before you DIAGNOSE

1. **Read memory first** — grep
   `~/.claude/projects/-Users-home-Documents-code-batpred/memory/`. The warning
   that would have prevented the 2026-08-11 overnight misdiagnosis was already
   written down, and unread.
2. **But read it for TRAPS and METHOD, never for current values.** Memory numbers
   rot: a window recorded as 11 h had moved to 12.0; a "fixed ~95 W standby"
   turned out to be a converter curve with no standby at all. Verify any number
   before relying on it, then correct the file.
3. **Check freshness before concluding anything is broken.** Read `last_reported`
   FIRST, and compare against the BOX clock, not yours — a deploy was called
   broken twice on 2026-08-11, and again on 08-12 by reading a remote timestamp
   against a local clock.
4. **Prefer a trace over a state read.** Three defects on 2026-08-06 were
   invisible in state and obvious in the trace.
5. **Two surprises in one subsystem = stop patching, start auditing.** The mental
   model is wrong; there are not simply two bugs.

## Before you say it is DONE

Never call work done before **all four**: pre-commit + full test suite + deploy +
a **discriminating** check.

- **Discriminating** means conditions under which old and new behaviour differ.
  If today's conditions cannot tell them apart, the honest report is "deployed,
  not yet verified — needs X".
- **Blocked sub-goals are the FIRST line, not a caveat inside a success report.**
- Say what is true: "committed, not deployed", "deployed, unverified".
- **Commit before deploying**, always, so deployed code is in git history.

---

## Site facts you cannot derive

- **R25 — the key design principle.** Once PV−load > DNO cap there are NO control
  levers. All management (drain/charge/hold) must happen BEFORE overflow. The
  drain mechanism has been deleted twice and restored twice. Never remove it.
- **Never edit upgrade-overwritten stock Predbat modules** (`config.py`, `plan.py`,
  `fetch.py`, `predbat.py`) for site behaviour — an update silently undoes it.
  Site logic lives in `curtailment_*`, plugins, `ha/*.yaml`, HA entities. Hence
  `switch.predbat_expert_mode` stays On rather than un-gating `set_charge_freeze`.
- **Diagnose ownership from `predbat.status`, NEVER `switch.predbat_set_read_only`.**
  The switch lags Predbat's internal state by HOURS (cost a long misdiagnosis
  2026-08-03), and every change to it forces a full inverter reset to defaults.
- **Predbat writes PLANT registers via THREE mapper automations**, not one:
  `predbat_requested_mode_action`, `predbat_max_discharging_limit_action`,
  `predbat_max_charging_limit_action`. Disabling them stops further writes but
  leaves everything already written in place — **the writer that changed a
  register changes it back** (`_neutralise_predbat()` before disabling the chain).
  Setting mode to MSC does NOT clear the ESS clamps; those hang off the RATE
  helpers. Symptom of getting it wrong: SOC flat for hours, battery power 0.000,
  dispatch commanded, nothing moving.
- **Clipped ≠ curtailed.** Clipped = PV over the inverter's 6.6 kW AC capacity.
  Curtailed = export over the 3.68 kW DNO cap. Never interchange them.
- **Plan values are kWh per slot, not kW.**
- **Every deploy resets plugin state** (`_peak_pv`, caches). Expect it when
  debugging live.

## Pre-deploy gate

**These are enforced by git, not by memory** — install once per clone:

```
pre-commit install                    # lint/format/spell, blocks the commit
cp .claude/hooks/pre-push .git/hooks/ # runs the suites below, blocks the push
```

`.git/hooks` is not tracked, so a fresh clone needs both again. Bypass a push
deliberately with `--no-verify`; there is no bypass worth using routinely.

If running by hand anyway, run each as its OWN step and read its exit code — a
piped `grep`/`tail` reports the pipe's status, not the test's (this let a failed
gate commit through on 2026-08-13).

```
pre-commit run --all-files
cd apps/predbat && python3 tests/test_all_tests_registered.py   # no orphaned tests
cd apps/predbat && python3 tests/test_requirements_implemented.py
cd apps/predbat && python3 tests/test_curtailment.py
cd apps/predbat && python3 tests/test_yaml_*.py          # one per changed automation
cd coverage && ./venv/bin/python ../apps/predbat/unit_test.py --quick
```

`unit_test` **must** use `coverage/venv/bin/python` (built by `source setup.csh`).
Bare `python3`/`python3.11` fails on a missing `protobuf` in `test_gateway.py` —
that is an interpreter gap, NOT a regression. Check which interpreter you ran
before investigating a failure, and never stash the working tree to "prove" it.

## Deploy — PUSH before you deploy

**Committed is not enough. Push first.** The pre-push hook is where the test
suites run, so deploying from a local commit skips them entirely — untested code
onto a live battery controller. Requiring the push makes the tests unskippable
rather than a thing to remember.

Use the script; it enforces this and will refuse otherwise:

```
.claude/bin/deploy              # standard curtailment set
.claude/bin/deploy --dry-run    # check the gates, copy nothing
```

It refuses on a dirty tree, refuses on unpushed commits, verifies each file's
md5 on the box after copying, writes the deployed commit to `DEPLOYED_SHA`
(so "what is actually running?" is answerable without md5-ing every file), and
touches `plugin_system.py` — plugin files are not watched, so nothing loads
without it. Deploy ALL changed .py together.

Bypass exists (`DEPLOY_FORCE=1`) and should stay unused.

## HA automation edits — mandatory workflow

`python_transform` can silently reorder the `variables` dict, and HA evaluates
variables top-down, so a reorder leaves templates reading Undefined and falling
through every branch. This caused a 2-hour controller outage on 2026-05-06.

1. Edit the YAML in `apps/predbat/ha/` first, in repo
2. Run the matching Jinja harness — it must pass with the NEW behaviour asserted
3. Apply via `config:` (full replacement), **never** `python_transform`
4. Verify the live entity produces an expected value for a known input — not just
   that the automation is `on`

## Tests: the three ways a green test lies

1. **Never ran** — written, never registered. Check the PASS count CHANGES.
2. **Never reached its subject** — the production call site does not exist, or the
   precondition never fired. Assert the precondition, not just the outcome.
3. **The failure mode is inside the allowed set** — `assert x in (good, bad)`, or
   `assert "CLAMPED" not in msg` passing on a worse wrong answer. **Assert the
   specific expected value.** If you cannot name it, you do not understand it well
   enough to ship it.

Watch it FAIL, and fail for the RIGHT reason. When a change makes an old test
fail, first ask whether the FIXTURE was lying — impossible dates, absent state,
abundant PV that stops the branch being reached, positional indexing that
silently re-points.

## Frozen — do not start

Architecture is frozen while CM is stable; edge fine-tuning only. Do not begin a
structural rewrite (Python PCS executor, delete heartbeat, wholesale
`sig-control-v2` merge) unless multi-writer defects burn multiple days again.
**No structural refactor of the live control path on a high-stakes day** — land it
pre-dawn or after the curtailment window.

- Ownership table + deploy + golden checks: `apps/predbat/ha/README.md`
- Freeze detail: `.claude/memory/sig-control-maintenance.md`
- Deferred proposal (NOT the current path): `.claude/plans/sig-control-python-migration.md`

---

**Assumed, not listed as rules:** DRY, no positional indexing into fixtures, read
before you edit, don't mask exit codes, name things for what they hold. These are
standards to meet unprompted. Writing them as rules implies the default is
otherwise — and a rule you must remember to read cannot save you anyway.
