# Parked findings

Things noticed while doing something else. **Not** work orders — Andrew pulls
from this list. Per `CLAUDE.md` rule 3, findings go here silently instead of
being appended to replies.

Format: `- [date] finding — why it might matter — evidence`

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
