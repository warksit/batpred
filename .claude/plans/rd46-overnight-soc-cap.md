# RD46 — stop Predbat carrying an overnight reserve that CM dumps at dawn

**Status:** designed, verified, NOT built. Next RD number is **RD46** (RD45 is the
highest in `REQUIREMENTS.md`).

## The problem

Andrew, 2026-08-20: Predbat planned `FrzChrg` 22:00–00:00 and a top-up at 04:00,
importing at 12.42p to hold ~45% SOC into a morning where CM drains to ~1% for
curtailment headroom and exports it at 12p. He overrode the slots to Demand by hand
and asked for a permanent fix.

Root cause: **Predbat plans headroom for the p50 forecast; CM defends the p90.**
Predbat's 45% dawn SOC *is* its headroom plan (9.9 kWh, enough for a 54.65 kWh day).
CM will drain to ~1% (18 kWh of room) because it defends the 65.4 kWh p90. Predbat
does not know CM is about to take the wheel, so it buys a reserve that is then dumped.

Tonight's waste was ~1.4 kWh ≈ **1p**. The reason to fix it is winter, when Predbat
grid-charges several kWh in a cheap window and CM dumps it — and, worse, the reserve
eats the headroom CM then has to drain harder to recover.

## Verified mechanism (read the code before changing this)

`input_number.predbat_best_soc_max` — currently `0` (disabled). In
`plan.py:1392-1393`:

```python
if self.best_soc_max > 0:
    loop_soc = min(loop_soc, self.best_soc_max)
```

`loop_soc` seeds the charge-target candidate list, so the cap **does** suppress
active charges (`Chrg`) and hold-charges (`HoldChrg`).

**It does NOT suppress a charge freeze.** A freeze is `charge_limit == reserve`
(`execute.py:678`), and in `optimise_charge_limit` the freeze candidate is appended
independently of `loop_soc`:

```python
while loop_soc > self.reserve and not freeze_only:
    ...                                    # candidates capped by best_soc_max
else:
    if allow_freeze and (self.reserve not in try_socs):
        try_socs.append(self.reserve)      # freeze — NOT capped
```

I originally proposed this cap believing it would kill the freeze. It will not.
Verify this by test before assuming otherwise.

## The design

CM writes `best_soc_max` = its **live** `overnight_target_kwh`, refreshed every
cycle, cleared at takeover.

- **Why the overnight target, not a low fixed % (e.g. 10%):** the target is *defined*
  as the energy the night consumes, so it **self-liquidates** — buy exactly that and
  it is gone by dawn, giving CM its headroom anyway. A 10% cap buys under 1 kWh more
  headroom at takeover (out of 18 kWh) but blocks Predbat from buying cheap overnight
  energy for the house's own load: on a winter night at 20% SOC with 8 kWh of load
  that is ~55p of avoidable 25.32p import.
- **Why live, not static:** `compute_morning_gap` measures from *now* to sunrise, so
  the target shrinks through the night — 6.76 kWh at 22:00, ~2 kWh by 04:00. That is
  what kills the 04:00 top-up; a static value would not.
- **Clear it at takeover — this is the dangerous part.** `best_soc_max` caps *every*
  charge window in the plan. Left set, Predbat will not plan to fill the battery from
  PV, which is catastrophically wrong on an overflow day.

Anchors:
- write helper: mirror `_set_predbat_export_floor` (`curtailment_plugin.py:2821`) —
  write-if-changed, guards the write storm that wedged `sig_keep_floor_pct` on
  2026-07-22. Add `PREDBAT_SOC_MAX_HELPER = "input_number.predbat_best_soc_max"`
  next to `PREDBAT_SOC_MIN_HELPER` (line 330).
- value source: `self._overnight_target_kwh` (set at line 1046,
  `_refresh_overnight_target` line 986).
- set/clear points: `_set_writer(cm_driving=True)` (line 3856) clears it to 0;
  `_release_to_predbat()` (lines 3799, 3883) sets it. Follow RD34's shape:
  `_set_predbat_export_floor(0.0 if self._dawn_released else dawn_floor)`.

## Tests to write first

1. **The cap suppresses a charge but NOT a freeze** — pin the verified behaviour so a
   future reader does not repeat my wrong inference.
2. Cap = live overnight target, and it **shrinks** across successive cycles.
3. **Cleared at takeover** — assert `best_soc_max` is 0 once CM drives. This is the
   one that prevents the catastrophic failure; watch it fail first.
4. No write when unchanged (write-storm guard).
5. Nothing written while CM is not acting / plugin off.

## Ruled out — do not revisit without new evidence

- **CM toggling `switch.predbat_set_charge_freeze`** to kill the freeze. That config
  item carries `reset_inverter: true`, so every toggle forces a full inverter reset to
  defaults — twice a day is the trap the charter already records for `set_read_only`.
- **Lowering `metric_battery_cycle` to kill the freeze.** It will not. Predbat values
  leftover battery at `rate_min_forward` = 12.42p, the same as the cheap-window rate,
  so freezing is *exactly* neutral even at a zero penalty and the tie breaks
  arbitrarily. I claimed otherwise on 2026-08-21; that was wrong.

## Residual after RD46

The 22:00–00:00 freeze (~0.56 kWh) survives. It is neutral-to-correct in Predbat's own
terms — it preserves battery for the 25.32p window — and only looks wasteful because CM
dumps the reserve. Accept it, or revisit only with a mechanism that does not toggle
`set_charge_freeze`.

## Separate, related: `metric_battery_cycle` is too high

Not part of RD46, but found in the same investigation.

- `docs/customisation.md:116-124` is explicit that this is a **wear** cost
  (capital ÷ throughput ÷ cycles), **not** an energy-loss proxy — losses are already
  modelled by `battery_loss` / `inverter_loss`. The May 2026 note sized it from
  round-trip efficiency, which double-counts. The efficiency work belongs to the loss
  settings, not this one.
- Documented range is **0–2** (`configuration-guide.md:76`). It is currently **3.0**.
- 3p implies `3 × 36.16 kWh/cycle × 6000 cycles = £6,509` at full amortisation.
- Usage since the 2026-07-15 swap: 485 kWh in 38 days = 26.8 equivalent full cycles ≈
  **258/year**. Against a 6,000-cycle LFP rating that is 23 years — the battery dies of
  calendar age with over half its cycles unused, so marginal wear is well below full
  amortisation.
- **Blocked on one datum:** what the storage portion cost. Absent that, **1.5p** is the
  defensible value (half the amortised figure, per the docs' own advice, inside range).

## Efficiency sensors — status

`sensor.battery_throughput_since_baseline` was 29.04 / 50 kWh on 2026-08-22, so
`sensor.battery_round_trip_loss` still reads `unknown` (by design). The lifetime
counters already give a converged answer: **1.98%** round-trip loss (485.12 charge,
467.59 discharge, 7.92 stored) against 1.94% three days earlier. That figure sizes
`battery_loss` / `battery_loss_discharge` — see
`.claude/plans/battery-efficiency-review.md` — and does **not** size
`metric_battery_cycle`.
