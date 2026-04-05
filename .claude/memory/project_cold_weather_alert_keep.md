---
name: Cold weather plugin v2 — alert_keep mechanism
description: Cold weather plugin now uses all_active_keep injection + weight/keep boost (deployed 2026-03-26), needs monitoring on cold mornings
type: project
---

Replaced the weak `best_soc_keep` boost (weight=0.5, ignored by optimizer) with three-tier approach. Deployed 2026-03-26.

**Why:** Battery hit 1.6% on 2026-03-26 morning despite best_soc_keep=3.03kWh. The optimizer penalty at weight=0.5 during cheap rate (7.5p/kWh) was less than the cost of charging, so the optimizer correctly chose not to charge. The four-hour ramp in prediction.py further weakened the penalty for near-term GSHP load.

**How to apply:** Monitor on cold mornings. Check `sensor.predbat_cold_weather_prediction` attributes.

## Three mechanisms

1. **`all_active_keep` injection** (04:00-08:00, tapered): Forces `keep_minute_scaling=10.0` in prediction.py, bypasses four-hour ramp. Tapers linearly from full keep% at 04:00 to 0% at 08:00 (remaining GSHP load decreases).

2. **`best_soc_keep` boost** (all day): `base_keep + prediction * 0.5`. Gives the optimizer a meaningful all-day floor. E.g., prediction=5.8 → keep=2.0+2.9=4.9kWh.

3. **`best_soc_keep_weight` boost** (all day): `base_weight + prediction/6.0 * 2.0`, capped at 5.0. Makes the keep penalty actually bite. E.g., prediction=5.8 → weight=0.5+1.93=2.4.

## Verification

- **Baseline failure**: 2026-03-26, prediction=5.8kWh, battery hit 1.6% (0.29kWh)
- **Success**: Battery stays above ~5% during 04:00-08:00 GSHP window
- **Sensor**: `sensor.predbat_cold_weather_prediction` — check `alert_keep_pct`, `alert_keep_kwh`, `keep_weight`
- **Log**: grep for "Cold weather: alert_keep=" in predbat.log
- **First confirmed run**: alert_keep=34.7%->0%, soc_keep=2.0->4.9kWh, weight=0.5->2.4

## Also fixed this session

- HA automation `predbat_max_discharging_limit_action`: wrong entity `sensor.sigen_inverter_ess_rated_discharging_power` → `sensor.sigen_plant_ess_rated_discharging_power`
- Exposed `best_soc_keep_weight` in plugin context (predbat.py)
