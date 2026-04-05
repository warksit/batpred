---
name: Cold Weather SOC Keep Automation
description: HA automations on Mum's system that dynamically adjust best_soc_keep based on overnight forecast temperature to prevent battery depletion during cold GSHP mornings
type: project
---

## Cold Weather SOC Keep Automation (deployed 2026-03-13, updated 2026-03-16)

### Problem
On Mar 13, battery hit 0% when LoadML (13 days old) had never seen 4.2°C overnight — GSHP used ~3 kWh more than predicted. best_soc_keep is a soft penalty, not a hard floor, so it needs to be set high enough that the optimizer charges sufficiently during cheap rate.

### Architecture
3 HA automations + 4 input_number helpers. No Predbat code changes.

### Helpers
| Entity | Value | Purpose |
|--------|-------|---------|
| `input_number.cold_soc_baseline` | 2.0 kWh | Daytime/warm-weather keep (~11%) |
| `input_number.cold_soc_per_degree` | 0.75 kWh/°C | Extra keep per degree below threshold (learnable) |
| `input_number.cold_soc_threshold` | 8.0°C | Below this, cold bump applies |
| `input_number.cold_morning_min_soc` | 100% | Tracks actual morning min SOC (reset daily) |

### Predbat Settings
- `best_soc_keep_weight` = 0.75 (was 0.5)
- `best_soc_keep_solar_offset` = 2.6 (unchanged)

### Automations
1. **`automation.cold_soc_keep_set_overnight`** — triggers at 20:00
   - Reads `sensor.predbat_temperature` results attribute
   - Finds min temp from now to next 10:00 UTC
   - Sets `best_soc_keep = min(baseline + per_degree × max(0, threshold - min_temp), 8.0)`
   - Runs once (not repeatedly) — avoids lowering keep as coldest hours pass

2. **`automation.cold_soc_keep_track_morning_min`** — triggers every 5min, 07:00-10:00
   - Resets `best_soc_keep` back to baseline (daytime value)
   - Tracks lowest SOC seen in `cold_morning_min_soc`

3. **`automation.cold_soc_keep_morning_review`** — triggers at 10:00
   - If min_soc < 5%: increase per_degree by 0.1 (fast — avoid 0% is critical)
   - If min_soc > 20% AND was a cold night: decrease per_degree by 0.03 (slow)
   - Clamps per_degree to [0.1, 2.0]
   - Logs decision to HA logbook
   - Resets morning min tracker to 100

### Formula Examples (with current params)
| Night Min | Keep Set | After Solar Offset | Effective % |
|-----------|----------|-------------------|-------------|
| 10°C (warm) | 2.0 | 0 | ~0% (baseline) |
| 6°C | 3.5 | 0.9 | 5% |
| 4°C | 5.0 | 2.4 | 13% |
| 2°C | 6.5 | 3.9 | 22% |
| 0°C | 8.0 (cap) | 5.4 | 30% |

### Observed GSHP Correlation (Mar 6-16)
- Warm nights (7-8°C): GSHP uses 3-4 kWh morning (07-13 UTC)
- Cold nights (2-4°C): GSHP uses 5-7 kWh morning
- 0.75 kWh/°C tracks well against actual consumption

### Learning Loop
- Asymmetric: fast increase (+0.1) on failure, slow decrease (-0.03) on excess
- Naturally backs off as LoadML improves (better predictions → higher min SOC → review decreases factor)
- Warm nights don't trigger learning (only cold nights where keep > baseline + 0.5)
