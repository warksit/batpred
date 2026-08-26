# Predbat and curtailment: it already prices it, it just cannot see it

**Status: DEFERRED PROPOSAL, not the current path.** Nothing here is agreed work.
Written 2026-08-26 after Andrew: *"The more we tweak CM, the more I wish Predbat
understood curtailment cost."* The investigation that afternoon turned up an
answer that changes what that wish would actually cost to grant.

## The thought

**Predbat already prices curtailment, and prices it correctly.**

It models the export cap end to end — config (`config.py:2229` `export_limit`,
`:2221` `inverter_limit`), loaded per-inverter (`inverter.py:488-491`), aggregated
before planning (`execute.py:822-823` → `:843-844`, called from `predbat.py:836`),
and carried into the simulation (`prediction.py:159-160`, scaled `:535-536`).

In the forward simulation it **clips and discards**: after the battery decision,
AC export above `export_limit` is removed from `pv_ac` and added to
`clipped_today` (`prediction.py:1074-1081`). Inside a forced-export window the
clamp runs earlier and, if `inverter_can_charge_during_export`, diverts the
over-cap surplus into the battery (`:838-876`).

`clipped_today` (`prediction.py:688, 1063, 1081`, published `:670`, consumed only
by `output.py:1442, 1597, 1624`) carries **no metric term** — and does not need
one. Clipped PV never reaches the export branch, so the metric simply loses
`export_rate × energy` (`prediction.py:1158-1160`). **Curtailed energy costs you
exactly the export revenue you would have earned, and that is what the model
sees.** The economic loss is captured; only the physical quantity goes unscored.

So the gap is not the cost model. It is two other things.

### 1. Risk posture — the real one

Predbat plans against a historical weighted-average / modal-filtered load forecast
(`fetch.py:398-446, 527-566`) with a PV10 pessimistic scenario weighted by
`pv_metric10_weight`. **There is no P90 load scenario anywhere in stock Predbat**
(confirmed by full-repo grep).

CM defends the p90. Predbat plans the p50. On a day the median says "fits",
Predbat never *sees* the clipping coming — so there is no cost to avoid and no
reason to make room. That is the entire reason CM exists.

Conceptually this is a small change upstream: run the clipping check against a
pessimistic band, not the median. It is a **risk-posture** change, not a new cost
model — which is a far easier thing to argue for and to review.

### 2. The terminal-value bug — pushes the wrong way

`plan.py:1326-1331` values energy left in the pack at `end_record`:

```
rate_min = rate_min_forward.get(...) / inverter_loss / battery_loss + metric_battery_cycle
battery_value = soc * metric_battery_value_scaling * max(rate_min, 1.0, rate_export_min)
```

`rate_min_forward` (`fetch.py:1798-1820`, fed `self.rate_import` at `:1903`) is the
minimum future **import** rate. Live on the box 2026-08-26: a stored kWh scored
**16.13 p/kWh** against **12.0 p** for exporting the same kWh.

Two distortions:

- **Loss direction inverted.** `/inverter_loss/battery_loss` treats it as
  replacement cost, but discharge losses are already applied in-simulation
  (`prediction.py:811`, `get_diff` `:64-70`). True delivered worth is
  `12.4 × 0.96 × 0.986 = 11.77p`, not 13.13p — roughly 2.7 p/kWh of pure error.
- **Cycle penalty refunded.** `+ metric_battery_cycle` gives back the 3.0p charged
  on the way in (`prediction.py:1118`, `plan.py:1350`) for anything still in the
  pack at the horizon.

The `rate_export_min` branch (`:1328`) is effectively dead: it subtracts
`rate_min`, making it an arbitrage *spread* (−7.8p here), so `max()` never selects
it. **The model never values leftover battery at what it could be sold for.**

That matters most on exactly the days CM cares about, where surplus above the
overnight need can only ever be exported, never self-consumed.

## Why this reframes the CM work

Nearly all of CM's recent surface area exists because Predbat cannot **see** the
risk, not because it cannot **price** it:

| Tweak | What it really compensates for |
|---|---|
| RD46 (cap the overnight charge) | Predbat banks a reserve CM will dump — terminal-value bias |
| RD47 (grade the drain floor) | CM's own p90 defence over-preparing |
| RD48/RD49 (hold the wheel, don't bank) | Predbat banking PV with no buyer — terminal-value bias |

Fix the risk posture and the valuation, and most of that becomes redundant: the
drain-depth tuning, the useful ceiling, the handback boundary. They are scaffolding
around a blind spot.

## The evidence we already hold

- **`sensor.curtailment_overflow_daily`**, running since 2026-07-29 — 26+ days of
  realised above-cap overflow. Median ~9 kWh, max 17.04.
- **2026-08-26, a worked example of the churn:** PV 31.69 kWh, realised overflow
  **4.36 kWh**. CM drained the pack 84.2% → 22.3% (**11.2 kWh dumped**), creating
  **14.1 kWh of headroom for a 4.36 kWh need** — about 3× over-prepared. The pack
  was then low enough that Predbat bought **4.33 kWh** back that afternoon. Dump at
  12p in the morning, buy it back at 12.42p after lunch: near-neutral on rate, a couple of
  percent on round-trip, pure churn — and CM manufactured the import it then
  profited from.
- **2026-08-24**, the banking case: surplus 5.25 kW going entirely into the pack
  with the 3.68 kW export cap idle, ~7.5 kWh with no buyer.

That is a real dataset for an upstream argument, not an anecdote.

## Suggested sequencing (if ever taken up)

1. **The loss-direction fix first** — small, isolated, demonstrable, and the same
   shape as PR #4319 which is already in flight and proves the path works.
2. **Then the p90 clipping check**, with the overflow meter data as evidence. A
   bigger ask; make it only once (1) has landed and built credibility.
3. **Only then** consider retiring CM scaffolding, one requirement at a time, each
   with its own discriminating check.

## Cautions

- `plan.py` and `prediction.py` are stock modules. The charter forbids editing them
  for site behaviour — any fix here is an **upstream PR or configuration**, never a
  local patch.
- `metric_battery_value_scaling` (live 1.0) can tip the store/export comparison from
  the dashboard, but it is **global**: lowering it also discourages holding charge
  overnight when the battery genuinely does displace 25.32p import. Blunt
  instrument; do not reach for it as a shortcut.
- `prediction_kernel.py:491` blanks `predict_clipped_best` — if the C kernel is
  active, the clipping figure reads zero regardless of reality. Check before
  quoting it as evidence.
- Upstream review latency is real: #4319 sat a month on a green CI and one
  unresolved comment. Plan for that, and do not let CM regress while waiting.
