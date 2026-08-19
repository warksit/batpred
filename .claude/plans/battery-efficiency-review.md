# Battery efficiency: measure, then set the Predbat losses

**Status:** measuring. Sensors live since 2026-08-19. **Review from ~2026-08-26,
decide ~2026-09-09.**

## Why this exists

The question was whether PV should be exported directly rather than cycled through
the battery. That needs a number for what the round trip costs, and we did not have
one — two hand calculations over the same fortnight gave 3.85% and 2.0%, differing
only in how the endpoints were handled.

Two of my own claims were wrong before we measured, both in the same direction
(overstating the cost of storing):

- *"the round trip costs ~10%"* — no. `get_diff()` (`prediction.py:64`) nets the DC
  bus **before** applying `inverter_loss`, and `inverter_hybrid` is `true`, so
  PV→battery never crosses the inverter. Only the battery-side losses are marginal.
- *"the configured 1.4%/1.4% is optimistic, real is 4.5–6%"* — no, measured **1.94%**.
  The ~3.2% figure in memory is a pack-side load-sensor artefact, not conversion loss.

That is why this is now instrumented rather than argued.

## Where it stands

| | |
|---|---|
| Measured round trip (whole battery life, 456 kWh) | **1.94%** |
| Predbat configured (`battery_loss` 1.4% + `battery_loss_discharge` 1.4%) | 2.78% |
| Same maths without the stored-energy (delta-stored) term (do not quote this) | 5.78% |
| `metric_battery_cycle` | restored to **3.0 p/kWh** |
| `combine_charge_slots` | restored to **on** |

Sensors: `sensor.battery_round_trip_loss`, `sensor.battery_throughput_since_baseline`.
Baseline in `input_number.battery_eff_base_{charge,discharge,stored}_kwh`.
Record: `apps/predbat/ha/battery_efficiency_sensors.yaml`.

## The review — do this in a few days

1. **Read `sensor.battery_throughput_since_baseline` FIRST.** Below 50 kWh the loss
   sensor reports `unknown` by design. Treat it as indicative under ~200 kWh, solid
   past ~500 kWh. At ~15 kWh/day that is roughly **a week for indicative, five weeks
   for solid**.
2. **Read `sensor.battery_round_trip_loss`.** Sanity-check it against the 1.94%
   whole-life figure — a large divergence means something changed, not that the
   sensor is smarter.
3. **Then set `battery_loss` and `battery_loss_discharge`** to half the measured
   round-trip figure each (they compound, so 2 × 1.75% ≈ 3.5% total). Via
   `input_number.predbat_battery_loss` / `..._battery_loss_discharge`.
4. **Verify discriminatingly:** `output.py:1508-1509` computes the adjusted rates
   from these, so pull `mcp__predbat__get_plan` before and after and confirm the
   adjusted import/export rates move by the expected amount. Predbat's own log line
   rounds losses to whole percent (`battery_loss(1%)`), so it cannot verify a change
   from 1.4% to 1.75% — use `mcp__predbat__get_config`.

## The open decision: what to set now

Andrew asked whether to just pick **3.5%** now rather than wait.

**Recommendation: leave the losses at 1.4% / 1.4% until the sensor has data.**

The reasoning is that the two knobs do different jobs and should not both be used
for the same purpose:

- `battery_loss` / `battery_loss_discharge` model **physics**. Measured is 1.94%;
  configured 2.78% is already mildly pessimistic, and pessimistic is the safe
  direction — it makes Predbat marginally *less* keen to cycle, which is what we
  want. Raising them to 3.5% total is a further ~0.7 point of pessimism.
- `metric_battery_cycle` models **preference** — wear, and a dislike of pointless
  round trips. It is now 3 p/kWh, and because `battery_cycle` accumulates
  `abs(battery_draw)` in **both** directions (`prediction.py:1118`), that is ~6p per
  kWh actually round-tripped. Against a physical cost of ~0.23p/kWh (1.94% × 12p),
  that knob is already doing ~25× the work of the physics.

So the anti-cycling thumb is already firmly on the scale via `metric_battery_cycle`.
Adding 3.5% to the losses puts a second thumb on the same side, and then neither
number means what it says — which is exactly the state we just spent a day getting
out of.

**If a single number is wanted now anyway**, 3.5% total (1.75% each) is defensible
as "measured 1.94%, rounded up for uncertainty and high-rate losses" — it is not
crazy, just unmeasured. What should NOT happen is 3.5% *each* (7% round trip); that
is 3.6× the measured value and would suppress genuine peak arbitrage.

## Known gap

This is a single blended figure across all charge/discharge rates, and efficiency is
load-dependent. If the split matters, bin by average power over matched-SOC spans —
design sketched in `~/.claude/plans/mellow-dazzling-platypus.md`, not built.
