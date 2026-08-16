# Parked findings

Things noticed while doing something else. **Not** work orders — Andrew pulls
from this list. Per `CLAUDE.md` rule 3, findings go here silently instead of
being appended to replies.

Format: `- [date] finding — why it might matter — evidence`

## Awaiting a discriminating observation (deployed, NOT verified)

- **[2026-08-14] RD41 — session reserve as a charge target (`ac8b04cf`).**
  Deployed 11:57 and confirmed LOADED: `session_charge_target_kwh` appears in the
  published attributes from 12:02 (absent before the reload; HA keeps null
  attributes, so the key's presence is the discriminator). **Not behaviourally
  verified** — no session was armed on the 14th, and without one RD41 is a no-op
  by construction, so today proves nothing about the control path.
  **Success =** on the next session day, `session_charge_target_kwh` is non-null,
  it sits at `min(session_protect, overflow_floor)` while headroom is still owed,
  and CM flips to Solar Charge once the clamp lifts — with PV still to come, not
  at the break-even minute. **Failure =** SOC stalls below the reserve through the
  afternoon again, or Charge fires while `headroom_short_kwh` is still positive
  (that would be the clamp failing, i.e. RD41 eating the p90 drain).

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

- **[2026-08-16] Octopus legacy entities: one already dead, the rest go Jan 2027.**
  ADR 0004 renamed Saving Sessions -> power down, Free Electricity -> power up;
  legacy entities are removed **January 2027**. On v19.0.0 the legacy
  `binary_sensor...octoplus_saving_sessions` is ALREADY gone — it reads
  `unavailable, restored: true`. CM still reads it at
  `curtailment_plugin.py:175` (`_get_session_reserve_kwh`),
  `sig_keep_floor_guard.yaml:129`, `sig_saving_session.yaml:34,38`, so the
  session reserve is **blind today**: `session_need_kwh` is null because the
  source no longer exists, not because there is no session. It computed 7.78 kWh
  on 12 Aug. Next real saving session, CM dumps but does not prepare — and RD41's
  charge target has nothing to act on.
  The legacy CALENDAR is still alive but goes the same way, and we key off it at
  `sig_dispatch_intent_helpers.yaml:54` (the Max Export forcing),
  `sig_dispatch_heartbeat.yaml:103,107` and `curtailment_plugin.py:181`. When it
  is removed, `is_state(..., 'on')` is permanently false and CM **silently stops
  forcing Max Export during saving sessions** — no error, no log.
  ONE migration fixes all of it: move to `...octoplus_power_down` and to the
  `power_down_events` `joined_events` list (which is also where
  `octopoints_per_kwh` lives, so the power-up/power-down discrimination lands in
  the same change — see the RD14c note below). Needs the dispatch-intent harness.

- **[2026-08-15] The fault alert cannot see a DEAD inverter, only a dead meter.**
  SIG unreachable from ~15:48 (ARP FAILED on 192.168.5.145, 100% ping loss, TCP
  502 closed) and `automation.sig_inverter_fault_alert` never fired — its
  `last_triggered` was still 13 Aug 05:55 nearly 90 minutes in. The triggers key
  on the meter-dead signature (`grid_*` stale while `pv`/`battery` update); when
  the whole integration goes `unknown` there is nothing left to compare, so no
  trigger fires. The site is blind to the more serious of the two failures.
  Needs an availability trigger (`to: unavailable`/`unknown` on a core SIG
  entity, `for:` a few minutes) rather than another value comparison.

- **[2026-08-15] The heartbeat keeps beating into an unreachable plant.**
  `sig_dispatch_heartbeat` was still firing every minute (last 17:08) with all
  its target entities `unknown`, and `current: 1` — an instance in flight. It
  neither backs off nor reports. Harmless in itself, but it means "the heartbeat
  is running" tells you nothing about whether anything is being written.

- **[2026-08-14] `session_reserve_is_reachable` measures PV the pack cannot
  receive.** It sums `max(0, pv - load)` — gross surplus — but in Hold the pack
  only receives the *overflow* (`pv - load - cap`). On 12 Aug that is ~44 kWh of
  "available" PV against 11.5 kWh actually absorbable. Not what bit that
  afternoon (the reserve was armed by 14:20), but it is what released the floor
  to 0.18 kWh at 07:17 for the deep morning drain, and the arithmetic is wrong
  either way. `curtailment_calc.py:269`.

- **[2026-08-14] `estimate_session_end_kwh` clamps at a floor nothing enforces.**
  The clamp exists "because the keep-floor guard stops the sell there", but the
  guard defers to a live session (RD14-own), so the dump runs straight through
  it. Live 12 Aug 19:00: published `session_end_soc_pct` **39.3%** (exactly
  `overnight_target_pct`, i.e. the clamp), unclamped model 33.0%, actual end
  **31.2%**. The card was most optimistic at the one moment you would act on it.
  `curtailment_calc.py:595`. Either drop the clamp for a live session or make the
  guard actually hold it.

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
