# Parked findings

Things noticed while doing something else. **Not** work orders — Andrew pulls
from this list. Per `CLAUDE.md` rule 3, findings go here silently instead of
being appended to replies.

Format: `- [date] finding — why it might matter — evidence`

## Live behaviour

- **[2026-08-17] FIXED same day (`81c5112d`), NOT yet verified live.** RD41's
  session charge target did not reach the decision on an overflow-fits day: RD28's
  `no_drain` branch computed its target from the overnight reserve alone, so
  `charge_below` published 60.5% while the Schmitt compared SOC against 6.93 kWh
  and answered Hold. Fixed by taking `max(no_overflow_target, session_charge_target)`.
  **Success =** on the next overflow-fits day with a joined session, CM flips to
  Solar Charge Battery while SOC is below the published `session_charge_target_pct`,
  instead of sitting in Hold. **Failure =** policy stays Hold with SOC below that
  line. Cannot be told apart tonight — the branch only runs inside the curtailment
  window, and there is no session armed.

## Awaiting data (measuring, not yet conclusive)

- **[2026-08-19] Battery round-trip loss: CHECK THE NEW SENSORS AND REFINE.**
  Built `sensor.battery_round_trip_loss` + `sensor.battery_throughput_since_baseline`
  (+ three `input_number.battery_eff_base_*` holding the baseline). Repo record:
  `apps/predbat/ha/battery_efficiency_sensors.yaml`.
  **Baseline set 2026-08-19 at charge 456.08 / discharge 429.72 / stored 17.538 kWh.**
  **When to look:** the sensor reports `unknown` below 50 kWh throughput; treat it as
  indicative under ~200 kWh and solid past ~500 kWh. At ~15 kWh/day of charge that is
  roughly **two weeks for indicative, five weeks for solid** — so review from early
  September, and again in late September.
  **What we think now:** whole-life figure at creation was **1.94%** loss (stored-term
  corrected; the naive ratio said 5.78% on a 97%-full battery — a 3.8 point error, so
  never quote the naive one). Predbat is configured at **2.78%** (`battery_loss` 1.4%
  + `battery_loss_discharge` 1.4%), i.e. slightly PESSIMISTIC about storage.
  **Decision taken:** leave the loss settings alone. 1.94% vs 2.78% is inside the
  uncertainty while throughput is this small, and the error points the safe way —
  it makes Predbat marginally less keen to cycle, which is the direction we want.
  Correcting them downward would make it MORE willing to cycle.
  **What to do at review:** if the sensor settles well below 2.78% with >500 kWh
  behind it, consider lowering `battery_loss`/`battery_loss_discharge` to match — but
  only alongside a deliberate view on `metric_battery_cycle`, since the two knobs
  push the same decision in opposite directions.
  **Plan with the review steps and the open 3.5% decision:**
  `.claude/plans/battery-efficiency-review.md`.
  **Known gap:** this is a single blended figure across all charge/discharge rates.
  Efficiency is load-dependent. If that matters, bin by average power over
  matched-SOC spans (the analyser design in
  `~/.claude/plans/mellow-dazzling-platypus.md`, not built).

## Security

- **[2026-08-17] The Predbat MCP bearer token is committed to a PUBLIC repo.**
  `.mcp.json` is tracked and pushed to `origin/cm-on-latest-predbat`, and
  `gh repo view warksit/batpred` reports `"visibility":"PUBLIC"`. The token is the
  same value as `mcp_secret` in the box `apps.yaml`. Found while scanning apps.yaml
  for credentials before mirroring it into the repo (the mirror redacts it; this
  copy does not). **Mitigation already in place:** the endpoint is
  `http://100.110.70.80:8199`, a Tailscale CGNAT address, so the token is only
  usable by someone already on the tailnet — exposure, not an open door.
  **Needs a decision:** rotate `mcp_secret` on the box and in `.mcp.json`, then
  untrack `.mcp.json` (`.gitignore` + `git rm --cached`). Purging it from history
  needs a force-push, which is a bigger call. Not actioned — rotating breaks the
  running MCP config, so it is Andrew's to time.

- **[2026-08-17] No API keys in apps.yaml.** Checked while mirroring: `ha_key`,
  `solcast_api_key` and `axle_api_key` are all commented-out placeholders
  (`'xxx'` / `'xxxx'` / `"xxxxxxx"`). Solar forecasts come from the Solcast HA
  integration's sensors via `re:`, so no cloud key is needed. `mcp_secret` is the
  only real credential in the file. Recorded so the next audit does not re-derive it.

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

- **[2026-08-22] RD46 designed, not built — Predbat carries an overnight reserve CM
  dumps at dawn.** Full design, verified mechanism and the two ruled-out approaches:
  `.claude/plans/rd46-overnight-soc-cap.md`. Andrew asked for this after overriding
  the 22:00-00:00 slots to Demand by hand on 2026-08-20. Also holds the
  `metric_battery_cycle` finding (currently 3.0, documented range 0-2, blocked on the
  battery's capital cost; 1.5p is the defensible fallback).




- **[2026-08-20] Clearing a manual override drops the plant onto a STALE select for
  up to one CM cycle.** RD13a has CM stand off `sig_dispatch_policy` entirely while
  an override is held — the override IS the policy, so CM must not fight it. Correct,
  but it leaves the select holding whatever was last written, which can be days old.
  Live this morning: the select still read `Predbat` from the 2026-08-18 handback, so
  every time the override was cleared the effective policy fell to `Predbat`, the
  heartbeat parked to MSC, and the **pre-dawn drain paused** — on a day with 15.9 kWh
  at risk. It looked like "reverting to Predbat" and was in fact a stale value with a
  ~5 min repair time (measured: 72 s once given a real gap, 08:20:57 -> 08:22:09).
  Four attempts in a row lasting 7-17 s never gave CM a cycle to fix it, so it looked
  permanent.
  **Fix shape:** have CM keep the select in sync while manual is held (write it to the
  intended policy without acting on it), or write it on the cycle the override
  releases. The first is better — it means the select is never stale, so the fall-back
  target is always current. Needs care not to re-create the RD13a fight it was written
  to prevent: writing the select is not the same as driving from it.
  Not done: this is control-path code and it surfaced mid-drain.

- **[2026-08-16] Octopus legacy entities — RESOLVED 2026-08-17.** ADR 0004 renamed
  Saving Sessions -> Power Down; the legacy binary sensor AND calendar are both
  gone on v19.0.0 (the calendar went too, sooner than this note predicted). All
  three consequences landed and are fixed:
  - `apps.yaml: octopus_saving_session` matched nothing, so `auto_config` deleted
    the arg and Predbat skipped the whole saving-session block — no auto-join, no
    saving rate in the plan (`ad6ae8c8`).
  - `curtailment_plugin` read the dead sensor and calendar, so `session_need_kwh`
    published null and RD41 had nothing to act on (`0aab0125`, `c5b1d811`).
  - `sig_saving_session.yaml` triggered off the dead sensor; deleted rather than
    repointed, because it pins the select to Max Export — the thing RD14c removed
    (`81c5112d`).
  Verified end to end on the 17 Aug 18:00 session: joined at 13:00, priced into
  the plan at 12.625 p/kWh, the discrimination sensor and all three window sensors
  flipped on at 18:00:00 and off at 19:00:00, and the dump ran the full hour.
  Guarded by `test_plugin_reads_only_window_sensors_this_file_publishes` and
  `test_yaml_mum_apps.py`, both watched failing first.

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

## Answered

- **[2026-08-20] RD7: Predbat's export path DOES fire — the 2026-08-18 session proves it.**
  With CM stood down and `sig_dispatch_heartbeat` DISABLED, the only possible writer was
  Predbat's mapper chain, and it ran the whole 18:00-19:00 Power Down unaided:
  session on 18:00:00.48 -> `requested_mode` Discharging 18:01:19.95 -> EMS
  `Command Discharging (PV First)` 18:01:20.21 (270 ms) -> `predbat.status` Exporting ->
  released 19:00:51. **SOC 74.8% -> 52.7%, about 4.0 kWh from the pack** in the paid hour.
  **This reverses the standing assumption.** The 2026-07-27 note ("Freeze charging took
  effect, two Exporting windows did not") is best explained by CM holding the wheel that
  day: the three Predbat mappers are disabled whenever CM drives, so Predbat's export
  windows had no write path. Not a broken export path — a muzzled one. A hypothesis, but
  it fits both observations and there is no competing one on offer.
  **Implication:** CM's saving-session handling is redundant on any day CM is not already
  driving for curtailment. That is the RD45 direction, and it means RD44's session floor
  may guard a path that need not exist. Do NOT act on one session's evidence — but stop
  treating "Predbat cannot export" as a known constraint, because it is not one.
  **Process note:** I left this open for two days having written that only the session
  window itself would tell us, then never looked at the window. Andrew asked.

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
