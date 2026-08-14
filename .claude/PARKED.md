# Parked findings

Things noticed while doing something else. **Not** work orders — Andrew pulls
from this list. Per `CLAUDE.md` rule 3, findings go here silently instead of
being appended to replies.

Format: `- [date] finding — why it might matter — evidence`

## Awaiting a discriminating observation (deployed, NOT verified)

- **[2026-08-13] Intended-freeze alert suppression (`99614c22`).** Deployed and
  structurally confirmed live (stop guard sits between the variables block and
  the notify). **Not behaviourally verified.** Needs a pre-dawn hour where
  `input_select.predbat_requested_mode` is actually **Freeze Charging** or
  **Freeze Discharging** while policy is **Predbat**. Checked 14 Aug: overnight
  modes were Demand/Discharging only, so the precondition never occurred — the
  alert staying silent proves nothing yet.
  **Success =** the automation trace shows the run stopping at the guard, with no
  notify. **Failure =** a "Meter Communication Fault" push at ~05:55 with
  `meter_dead: false` in the same trace.

## Open

- **[2026-08-12] The saving-session reserve is not SOC-aware.**
  `session_reserve_is_reachable` compares remaining P10 PV against a deficit
  measured from the *drain floor*, never against current SOC. As
  `minutes_to_session` shrinks, remaining PV shrinks with it, so the reserve can
  arm late in the day on an already-full battery, where it has nothing to
  protect and only blocks the afternoon drain. `curtailment_calc.py:223`.

- **[2026-08-12] The tracking band and the overflow advice disagree by design.**
  Both consume the same pair (`pv_actual_kwh`, `pv_expected_p50_kwh`) but gate at
  3.0 kWh and 1.0 kWh respectively. So the card can say "too early to judge the
  band" while asserting "✓ fits" — and the *load-bearing* claim (fits ⇒ no action
  needed) runs on the *looser* gate. Backwards on risk. Options: align the gates,
  gate the verdict on the band being nameable, or mark the advice provisional.

- **[2026-08-12] Overnight reserve will ratchet through autumn.**
  `compute_morning_gap` runs on "PV below 0.3 kW", not the clock. Those shoulders
  widen far faster than sunset/sunrise move: the window went **10.5 h → 12.0 h in
  one week** (astronomical night grew only 0.4 h). Legitimate — the pack really
  must cover it — but it is multiplied by the ×1.10 safety, so worth a look
  before October rather than a surprise at a 60% overnight reserve.

- **[2026-08-12] `sig_keep_floor_guard` writes selects but cannot swap the writer
  role.** Its dusk branch can leave up to 5 minutes with nobody driving.
  (Pre-existing; recorded in REQUIREMENTS.md "Known gap".)

## Done

- [2026-08-12] Double-counted discharge efficiency in the overnight target —
  fixed, RD39, `5f37ad45`.
- [2026-08-12] `classify_forecast_tracking` parameters named `*_scale` while the
  caller passes energies — fixed, `a76d9e40`.
- [2026-08-13] RD36/RD38 first real dawn — **VERIFIED 14 Aug**: pack drained to
  3.5% and daily grid import was **0.000 kWh**. That was the criterion named in
  advance (anything material would have meant the pv-covers-load crossing ran
  early and the thinner reserve failed to cover it).
- [2026-08-13] RD39 still holding 14 Aug: `morning_gap_kwh` == `morning_gap_load_kwh`
  (6.04 = 6.04), target 6.64 kWh / 36.7% — no efficiency double-count.
- [2026-08-13] Structural guards: pre-commit hook installed (blocks commits on a
  failed gate, caught 3 real issues on day one), pre-push hook runs 15 suites
  (`72d8d64d`), orphan-test guard (`baca17df`). Both hooks copied to
  `.claude/hooks/`; `.git/hooks` is untracked so a fresh clone needs re-install.
