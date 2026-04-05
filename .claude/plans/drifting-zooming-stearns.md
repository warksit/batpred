# Plan: Cold Weather SOC Keep Automation

## Context

On Mar 13, Mum's battery hit 0% SOC when Predbat predicted a minimum of ~12%. Root cause: LoadML (13 days old, 30-day time decay) had never seen 4.2°C overnight — the coldest in its training window was 5.8°C. The GSHP ran ~3 kWh more than predicted on the cold morning, draining the battery completely.

Rather than tuning ML parameters (which won't help until the model accumulates more cold-weather data), we'll create an HA automation that dynamically adjusts `best_soc_keep` based on the forecast overnight temperature. This acts as a safety net that:
- Doesn't interfere with LoadML learning (it's a SOC floor, not a load injection)
- Self-adjusts via a daily feedback loop (learns whether the per-degree factor is right)
- Naturally backs off as LoadML improves (better predictions → higher actual min SOC → review decreases the factor)

## Key Parameters (agreed with user)

- **Baseline `best_soc_keep`**: 3.5 kWh — gives exactly 5% effective keep on warm+sunny days after solar offset
- **`best_soc_keep_solar_offset`**: 2.6 kWh — keep current value (solar offset still serves its purpose)
- **`best_soc_keep_weight`**: 0.75 (increase from 0.5)
- **Per-degree factor**: 0.5 kWh/°C initial (learnable)
- **Temperature threshold**: 8°C

### Solar Offset Interaction

The `best_soc_keep_solar_offset` (2.6 kWh) reduces keep inside Predbat when total PV excess > 50% of battery capacity (plan.py:1055-1060). This is a daily total — it does NOT check whether solar is morning or afternoon. However, the keep penalty itself IS time-aware (applied at every 5-min simulation step), so the optimizer still avoids morning SOC dips even when it knows afternoon solar is coming.

**Formula:** `automation_sets = baseline + per_degree × max(0, threshold - forecast_low)`
**Predbat internally:** `effective = max(automation_sets - solar_offset, 0)` on sunny days

### Expected Effective Values

| Temp | Sunny? | Set Value | After Offset | Effective % |
|------|--------|-----------|-------------|------------|
| 10°C | Yes | 3.5 | 0.9 | **5%** |
| 10°C | No | 3.5 | 3.5 | 19% |
| 6°C | Yes | 4.5 | 1.9 | 11% |
| 6°C | No | 4.5 | 4.5 | 25% |
| 4°C | Yes | 5.5 | 2.9 | 16% |
| 4°C | No | 5.5 | 5.5 | 30% |
| 0°C | Yes | 7.5 | 4.9 | 27% |
| 0°C | No | 7.5 | 7.5 | 42% |

Mar 13 (4°C + sunny, 3 kWh prediction error) → effective keep 2.9 kWh (16%) — would have prevented 0%.

## Implementation

### Step 1: Create HA Helpers (via MCP)

Create 4 `input_number` helpers on Mum's HA:

| Helper | Min | Max | Step | Default | Purpose |
|--------|-----|-----|------|---------|---------|
| `input_number.cold_soc_baseline` | 0 | 10 | 0.1 | 3.5 | Baseline best_soc_keep (kWh) for normal temps |
| `input_number.cold_soc_per_degree` | 0 | 2 | 0.05 | 0.5 | Extra kWh per degree below threshold (learnable) |
| `input_number.cold_soc_threshold` | 0 | 15 | 0.5 | 8.0 | Temperature threshold (°C) — above this, use baseline |
| `input_number.cold_morning_min_soc` | 0 | 100 | 1 | 100 | Tracks actual morning min SOC % (reset nightly) |

### Step 2: Set Predbat parameters (via MCP, once-off)

- `input_number.predbat_best_soc_keep_weight` → 0.75

### Step 3: Create Automation — Set SOC Keep (runs 20:00-07:00 only)

**Trigger:** Time pattern every 30 minutes
**Condition:** Time is between 20:00-07:00 (covers overnight charging window; value persists in entity for morning)
**Action:**
1. Extract min temp from `sensor.predbat_temperature` `results` attribute for the window: now through next 10:00 UTC
2. Calculate: `soc_keep = baseline + per_degree × max(0, threshold - min_temp)`
3. Cap at 8 kWh max (~44% of battery)
4. Set `input_number.predbat_best_soc_keep` to the calculated value

**Temperature source:** `state_attr('sensor.predbat_temperature', 'results')` — dict with ISO datetime keys and °C values. Filter to keys between now and next 10:00 UTC, take min value.

### Step 4: Create Automation — Track Morning Min SOC (runs 07:00-10:00)

**Trigger:** Time pattern every 5 minutes
**Condition:** Time is between 07:00-10:00 (after cheap rate ends — min SOC during charging window is irrelevant)
**Action:**
1. Read `sensor.sigen_plant_battery_state_of_charge` current state
2. If current SOC < `input_number.cold_morning_min_soc`, update the helper

### Step 5: Create Automation — Morning Review + Reset (runs at 10:00)

**Trigger:** Time 10:00 daily
**Action:**
1. Read `input_number.cold_morning_min_soc` (actual min SOC %)
2. Read `input_number.cold_soc_per_degree` (current factor)
3. Read `input_number.predbat_best_soc_keep` (what was set)
4. Read `input_number.cold_soc_baseline` (baseline)
5. Determine if it was a "cold night" (soc_keep > baseline + 0.5)
6. Adjust `cold_soc_per_degree`:
   - If min_soc < 5%: increase by 0.1 (fast — avoid 0% is critical)
   - If min_soc > 20% AND cold night: decrease by 0.03 (slow — conservative)
   - Otherwise: no change
7. Clamp per_degree to [0.1, 2.0] range
8. Log the decision (HA logbook via `logbook.log` or notification)
9. Reset `input_number.cold_morning_min_soc` to 100

3 automations total (Set SOC Keep, Track Min SOC, Morning Review+Reset).

## How Learning Converges

1. **Cold night, SOC hits 0%:** factor increases 0.1 → next cold night keeps more battery
2. **Cold night, SOC stays at 20%:** factor was too aggressive, decreases 0.03
3. **Warm night:** automation sets baseline, no learning triggered
4. **LoadML improves:** ML predicts higher load → optimizer charges more → actual min SOC is higher even without the keep bump → morning review decreases factor
5. **Convergence:** factor settles at a value that compensates for LoadML's residual prediction error at cold temps

Asymmetric adjustment (increase 0.1 vs decrease 0.03) ensures the system is conservative — recovering from 0% SOC is worse than having 5% extra battery.

## Data Already Being Recorded

No additional recording needed — all inputs for analysis are already in HA long-term statistics:
- `sensor.sigen_plant_battery_state_of_charge` — state_class=measurement ✓
- `sensor.predbat_temperature` — state_class=measurement ✓
- `sensor.heat_pump_energy_meter_power` — state_class (has hourly stats) ✓
- All `input_number.*` helpers — recorded by HA automatically ✓

## Verification

1. Check helpers exist and have correct values
2. Manually test the SOC keep calculation with current temperature forecast
3. Verify `best_soc_keep` entity updates correctly
4. Monitor for 2-3 days across warm and cold nights
5. Check `morning_min_soc` is being tracked correctly
6. Verify morning review adjusts `cold_soc_per_degree` appropriately after a cold night

## No Batpred Code Changes Required

Everything is implemented via HA helpers and automations. Predbat entities modified:
- `input_number.predbat_best_soc_keep` — dynamically set by automation
- `input_number.predbat_best_soc_keep_weight` — once-off change from 0.5 → 0.75
- `input_number.predbat_best_soc_keep_solar_offset` — kept at current 2.6 (no change)
