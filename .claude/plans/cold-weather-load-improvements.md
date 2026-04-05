# Cold Weather & Load Prediction Improvements

## Context

Separate from v8 curtailment (implement after). Today's curtailment failure was partly caused by LoadML overestimating midday load (3.5kW predicted vs ~0.5kW actual between DHW cycles). Two related improvements:

## 1. Change `days_previous` to `[1,2,3,4,5,6,7]`

**Current:** `[7]` — uses only same day last week. One outlier (e.g., cooking day) directly poisons next week's prediction. No data for modal filter to work with.

**Change to:** `[1,2,3,4,5,6,7]` with equal weights. Mum's days are all the same (retired, 79). With `load_filter_modal: true`, the modal filter strips the 1-in-7 cooking outlier. The other 6 normal days (27-36 kWh) set the prediction.

**File:** `apps.yaml` on Mum's HA (`/addon_configs/6adb4f0d_predbat/apps.yaml`)

```yaml
days_previous:
  - 1
  - 2
  - 3
  - 4
  - 5
  - 6
  - 7

days_previous_weight:
  - 1
  - 1
  - 1
  - 1
  - 1
  - 1
  - 1
```

## 2. Fix cold weather boost formula

**Current:** `keep_boost = prediction * 0.5` — ignores what LoadML already knows about GSHP. With `days_previous: [1..7]`, LoadML averages ~5 kWh/day GSHP into its prediction. The cold weather boost adds 2.5 kWh on top even when tonight matches the average.

**Fix:** Revert to difference-based boost: `boost = max(0, prediction - rolling_avg)`. Only boost by the EXTRA above what LoadML has learned. If tonight is average cold, boost = 0 (LoadML handles it). If 3 kWh colder than average, boost = 3.

**Keep:** alert_keep injection (strong penalty) + weight boost (so optimizer obeys).

**File:** `apps/predbat/cold_weather_plugin.py` — `on_before_plan()`

```python
# Replace:
keep_boost = prediction * 0.5

# With:
rolling_avg = self._rolling_avg()
keep_boost = max(0.0, prediction - rolling_avg)
```

## 3. Load ratio for curtailment (optional, after v8)

Track cumulative actual load vs expected load (base + cold_weather + DHW) during the day. If actual is much lower than LoadML predicted → LoadML was poisoned by outlier → trajectory should use lower load → more conservative floor.

Build expected load from known components:
- Base: 0.6 kW constant
- GSHP: cold weather prediction
- DHW: ~3 kWh at 12:00-14:00 BST (consistent daily)

This is a refinement for after v8 is working.

## Verification

1. Check `sensor.predbat_best_soc_keep` — boost should be near 0 on average-temperature nights
2. Check LoadML prediction smoothness over a week
3. `/soc-keep-review` after 7 days
