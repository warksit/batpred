# Curtailment Ceiling Override in execute.py

## Context

The prediction code correctly models ceiling-based discharge during charge hold windows (fixed in previous commits). But the **control code** (execute.py) doesn't know about the ceiling — during "Hold charging", it sets `discharge_rate=0`, preventing the battery from discharging even when the ceiling says SOC should be lower. Result: battery sits at 100% importing from grid while PV < load, instead of discharging to create headroom for future solar.

## Key Findings

### execute.py charge hold flow (lines 176-262)

```
if is_freeze_charge(...):           # line 176
    ...freeze logic...
    status = "Freeze charging"
else:                                # line 197
    target_soc = ...                 # line 200
    if soc_percent >= target_soc:    # line 201 — HOLD PATH
        status = "Hold charging"
        ...disable_charge_window or adjust_charge_window...
        ...adjust_pause_mode or adjust_discharge_rate(0)...  ← SUPPRESSES DISCHARGE
    else:                            # CHARGING PATH
        status = "Charging"
        inverter.adjust_charge_window(...)
    if not set_discharge_during_charge:  ...suppress discharge...
    isCharging = True                # line 245
    self.isCharging_Target = ...     # line 246
```

- `isCharging=True` → later code (line 516+) sets charge targets via `adjust_charge_immediate`
- `disabled_charge_window=True` → line 516 block is skipped
- For SIG inverters: "Demand" mode happens when charge window is disabled and no force export

### max_soc_ceiling

- Computed in `plan.py:generate_curtailment_export_windows()` (line 859)
- Dict with **relative** minute keys (0, 5, 10, ...) mapping to max SOC in kWh
- Currently local variable only — NOT stored on `self`
- Need to store with **absolute** minute keys for execute.py lookup

## Plan

### Step 1: Write failing tests in test_execute.py

Add `max_soc_ceiling=None` parameter to `run_execute_test()` (line 208). In setup, set `my_predbat.max_soc_ceiling = max_soc_ceiling or {}`.

**Test A — ceiling_hold_override**: SOC 10kWh, ceiling 5kWh → should NOT hold, should allow discharge
```python
run_execute_test(
    my_predbat, "ceiling_hold_override",
    charge_window_best=charge_window_best,
    charge_limit_best=charge_limit_best,  # target=100%
    set_charge_window=True, set_export_window=True,
    soc_kw=10,
    max_soc_ceiling={720: 5.0},           # absolute minute key
    assert_status="Demand (ceiling)",
    assert_discharge_rate=1000,
    assert_pause_discharge=False,
)
```

**Test B — ceiling_hold_no_override**: SOC 5kWh, ceiling 8kWh → normal hold
```python
run_execute_test(
    my_predbat, "ceiling_hold_no_override",
    charge_window_best=charge_window_best,
    charge_limit_best=charge_limit_best2,  # target=50%
    set_charge_window=True, set_export_window=True,
    soc_kw=5,
    max_soc_ceiling={720: 8.0},
    assert_status="Hold charging",
    assert_pause_discharge=True,
    assert_discharge_rate=1000,
    assert_reserve=0,
    assert_soc_target=100,
    assert_immediate_soc_target=50,
)
```

### Step 2: Store max_soc_ceiling on self

- **predbat.py** (~line 478): `self.max_soc_ceiling = {}`
- **plan.py** (after line 866): `self.max_soc_ceiling = {m + self.minutes_now: v for m, v in max_soc_ceiling.items()}`

### Step 3: Modify execute.py hold charging path

At line 201, split the hold block with a ceiling check:

```python
if self.set_soc_enable and self.soc_percent >= target_soc:
    # Check curtailment ceiling
    ceiling_kw = getattr(self, "max_soc_ceiling", {}).get(self.minutes_now, None)

    if ceiling_kw is not None and self.soc_kw > ceiling_kw + 0.1:
        # SOC exceeds ceiling — skip hold, allow discharge
        status = "Demand (ceiling)"
        self.log("Curtailment ceiling override: SOC {:.1f}kWh > ceiling {:.1f}kWh".format(self.soc_kw, ceiling_kw))
        inverter.disable_charge_window()
        disabled_charge_window = True
        # Do NOT suppress discharge, do NOT set isCharging
    else:
        status = "Hold charging"
        ... existing hold logic unchanged ...
```

**Critical**: `isCharging = True` must be set for freeze path AND non-ceiling hold/charge paths, but NOT for ceiling override. Restructure so:
- Freeze path: sets `isCharging = True` explicitly
- Non-freeze, non-ceiling: sets `isCharging = True` (as now)
- Ceiling override: skips `isCharging` entirely

### Step 4: Verify

1. `python3 ../apps/predbat/unit_test.py --quick` — all tests pass
2. `pre-commit run --all-files` — no lint errors
3. Commit, merge to mum, deploy
4. Check Predbat logs for "ceiling override", verify battery discharges

### Files to modify
1. `apps/predbat/tests/test_execute.py` — add parameter + 2 tests
2. `apps/predbat/predbat.py` — init `self.max_soc_ceiling = {}`
3. `apps/predbat/plan.py` — store ceiling with absolute keys
4. `apps/predbat/execute.py` — ceiling check in hold path

---

# Optimizer Charges From Grid When Solar Would Fill Battery For Free

## Investigation Summary (2026-03-10)

### What happened

On a day with 31.5 kWh Solcast forecast, 18 kWh battery, and 0p export rate:

| Time | SOC | Event |
|------|-----|-------|
| 00:00 | 51% | Overnight discharge begins |
| 04:06 | 37% | Minimum SOC reached |
| 04:15-06:00 | 37% | Predbat held charge (freeze), no grid import |
| 06:00-06:52 | 37%→52% | **Grid charged ~2.7 kWh at 14.09p = ~38p** |
| 06:55 | 52% | Cheap rate ends, demand mode |
| 07:00-09:15 | 52%→47% | Morning load, weak solar |
| 09:30-12:43 | 47%→100% | **Solar filled battery to 100%** |

The 2.7 kWh imported at cheap rate was wasted — solar would have charged the battery from 47% to 100% regardless.

### Why `best_charge_limit` kept rising

The optimizer re-evaluates every ~10 minutes. During the cheap window:
- Midnight: 22% → 04:00: 33% → 06:00: 41% → 06:30: 47% → 06:37: 55%

Two factors drove the ratchet:
1. **Solcast reduced forecast** from 31.5 to 27.2 kWh at 06:31 (early morning pessimism, later revised back up to 30.6 kWh)
2. **PV10 weighting** made the optimizer pay for insurance against low-solar scenarios

### Root cause: PV10 penalty + 0p export

The optimizer uses `compute_metric()` (plan.py:1329-1365) which combines normal and PV10 (10th percentile) scenarios:

```python
if metric10 > metric:
    metric_diff = metric10 - metric
    metric_diff *= self.pv_metric10_weight  # default 0.5
    metric += metric_diff
```

In the PV10 scenario (~40-60% of Solcast forecast), having extra battery from grid charging means:
- **Higher end-of-forecast SOC** → more `battery_value10` credit (~43p via `soc × rate_min`)
- **Fewer expensive peak imports** in evening → lower `cost10` (~86p at 43p/kWh peak)

Combined PV10 benefit of charging: ~129p × 0.5 weight = **~64.5p**, which exceeds the **38p** direct import cost.

**With 0p export, there's no natural counterbalance.** Solar exported at 0p earns nothing in the metric (`metric -= export_rate × energy = 0`). With a positive export rate (e.g., 15p), displaced solar would earn 15p/kWh, making wasteful imports clearly unprofitable. At 0p, the optimizer can't "see" the waste.

### Key code paths

| File | Lines | Purpose |
|------|-------|---------|
| `plan.py` | 1329-1365 | `compute_metric()` — combines cost, battery_value, PV10, self_sufficiency |
| `plan.py` | 1367-1668 | `optimise_charge_limit()` — tries SOC targets from max→0, picks lowest metric |
| `plan.py` | 384-456 | Global optimization loop — price threshold sweep |
| `prediction.py` | 914-938 | ECO mode — excess solar automatically charges battery |
| `prediction.py` | 1037-1043 | Curtailment penalty (only above `export_limit`) |
| `prediction.py` | 567-580, 945-952 | Ceiling mechanism for curtailment avoidance |

### Relevant settings

| Setting | Default | Effect |
|---------|---------|--------|
| `pv_metric10_weight` | 0.5 | Weight of PV10 pessimistic scenario |
| `metric_battery_value_scaling` | 1.0 | Scale factor for end-of-forecast battery value |
| `metric_self_sufficiency` | **0.0** | Per-kWh penalty for grid imports (none by default!) |
| `metric_battery_cycle` | small | Per-kWh penalty for battery cycling |
| `metric_min_improvement` | 0.0 | Min metric improvement to change charge target |

## Proposed Fix

### Solar displacement penalty in `compute_metric()`

When the simulation shows the optimizer both importing to battery AND exporting solar, the imported energy displaced free solar. Penalise by the gap between import cost and export revenue per displaced kWh.

**Add after line 1345 in `plan.py`, before PV10 weighting:**

```python
# Solar displacement penalty: when grid charging coexists with solar exports,
# the imported energy displaced free solar. Penalise by the gap between
# what we paid (rate_min) and what the displaced solar earned (rate_export_min).
if import_kwh_battery > 0 and export_kwh > 0:
    displaced = min(import_kwh_battery, export_kwh)
    raw_rate_min = self.rate_min_forward.get(self.minutes_now + end_record, self.rate_min)
    export_import_gap = max(raw_rate_min - self.rate_export_min, 0)
    metric += displaced * export_import_gap
    metric10 += displaced * export_import_gap  # Apply to both so PV10 diff isn't distorted
```

### How it works

- **0p export**: penalty = displaced × rate_min (~14p/kWh) — strong preference for solar
- **5p export**: penalty = displaced × 9p/kWh — moderate preference
- **14p+ export**: penalty = 0 — no change (exports are valuable, no solar waste)

Applied to **both** `metric` and `metric10` so the PV10 difference (`metric10 - metric`) is unchanged — PV10 risk assessment isn't distorted. The penalty directly increases the absolute cost of any scenario with solar displacement.

### Why `min(import_kwh_battery, export_kwh)` is safe

`export_kwh` includes forced discharge exports, not just solar. But forced discharge happens at high export rates where `max(rate_min - rate_export, 0) = 0`, so the displacement penalty naturally won't trigger.

### Files to modify

1. **`apps/predbat/plan.py`** — 6 lines added to `compute_metric()`
2. **`apps/predbat/tests/test_compute_metric.py`** — displacement test cases
3. **`apps/predbat/tests/test_curtailment.py`** — integration test: high solar + 0p export

### Test cases

1. No displacement with high export rate (rate_export >= rate_min → penalty = 0)
2. Full displacement with 0p export (penalty = displaced × rate_min)
3. No displacement when no battery imports (import_kwh_battery = 0)
4. No displacement when no exports (export_kwh = 0)
5. Partial displacement (rate_export between 0 and rate_min)

### Verification

1. `python3 apps/predbat/unit_test.py --quick` — existing tests pass
2. `pre-commit run --all-files` — formatting OK
3. Deploy to Mum's HA, verify on next sunny day: lower overnight charge target when solar forecast > battery capacity
