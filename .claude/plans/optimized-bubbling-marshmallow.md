# Curtailment Manager — Predbat Plugin

## Context

On high-solar days, Mum's 18kWh SIG battery fills to 100% while PV still exceeds the 4kW DNO export limit. The SMA curtails excess energy, wasting it. The curtailment manager solves this by controlling battery charging rate so the battery reaches 100% exactly when PV excess drops below the DNO limit, maximising export and avoiding curtailment.

**Layer 1 (DONE):** HA automation `curtailment_manager_dynamic_export_limit` handles fast (~5s) response, adjusting `grid_export_limitation = min(PV - load, cap)` when SIG is in Discharge ESS First mode.

**Layer 2 (THIS TASK):** A Predbat **plugin** that calculates WHEN to activate and WHAT export target to use, based on forecast look-ahead. Publishes a sensor that the HA automation reads.

## Why a Plugin (not core modifications)

- **Zero Predbat file changes** — survives all Predbat updates without merge conflicts
- Predbat's plugin system auto-discovers `*_plugin.py` files, not in PREDBAT_FILES list
- Plugin gets full access to Predbat internals via `self.base` (forecasts, SOC, inverter data)
- `on_update` hook fires every Predbat cycle (after execute_plan)
- Plugin toggles `set_read_only` to suppress Predbat's normal inverter control when active
- Config via HA input helpers (no CONFIG_ITEMS/APPS_SCHEMA changes needed)

## Files

### New: `apps/predbat/curtailment_plugin.py`
Single file containing the entire curtailment manager.

### HA Configuration: Input helpers (create via MCP or YAML)
- `input_boolean.curtailment_manager_enable` — master enable
- `input_number.curtailment_manager_threshold` — min curtailment kWh to activate (default 1.0, range 0-20, step 0.5)
- `input_number.curtailment_manager_dno_limit` — DNO export limit kW (default 4.0, range 0-10, step 0.1)

### Update: HA automation `curtailment_manager_dynamic_export_limit`
Change template to read from Predbat sensor instead of hardcoded 4kW cap.

## Algorithm

Each Predbat cycle (5 min), using `self.base.pv_forecast_minute_step` and `self.base.load_minutes_step`:

**Data format:** Dict mapping minute offset (0, 5, 10, ...) → kWh per 5-min step. Convert to kW: `value * 12`.

1. **Activation check:** Sum forecasted curtailment (PV excess above DNO limit) across all forecast steps. If total > threshold, activate.

2. **Calculate reservation:** Sum `max(0, excess_kw - dno_limit) / 12` for all FUTURE steps. This is battery capacity that must stay free.

3. **Target SOC:** `target_soc_kwh = soc_max - reserved_kwh`, clamped to `[reserve, soc_max]`.

4. **Phase determination** (margin = 0.5 kWh):
   - `ACTIVE` — PV excess > DNO limit NOW, or SOC > target + margin. Export target = DNO limit. Battery buffers automatically.
   - `HOLD` — SOC within margin of target. Export target = -1 (auto: HA automation tracks PV-load). Battery idle.
   - `CHARGE` — SOC < target - margin. Export target = 0. Battery charges from all solar.

5. **Publish sensors** via `self.base.dashboard_item()`:
   - `predbat.curtailment_phase` — "off", "active", "charge", "hold"
   - `predbat.curtailment_export_target` — kW value or -1 for auto
   - `predbat.curtailment_target_soc` — target SOC %
   - `predbat.curtailment_reserved_kwh` — reserved battery capacity
   - `predbat.curtailment_total_kwh` — total forecasted curtailment

6. **Inverter control** (when active):
   - Set EMS mode → "Command Discharging (ESS First)" via `self.base.call_service_wrapper("select/select_option", ...)`
   - Set grid_import_limitation → 0 via `self.base.call_service_wrapper("number/set_value", ...)`
   - Set charge_limit → 100% (allow solar) via same pattern
   - Set `self.base.set_read_only = True` to suppress Predbat's normal control on next cycle

7. **Deactivation** (transition active → off):
   - Restore grid_import_limitation → 100
   - Restore grid_export_limitation → dno_limit
   - Set `self.base.set_read_only = False`
   - Normal Predbat resumes on next cycle

## Plugin Structure

```python
from plugin_system import PredBatPlugin

# SIG entity names (Mum's system)
SIG_EMS_MODE = "select.sigen_plant_remote_ems_control_mode"
SIG_EXPORT_LIMIT = "number.sigen_plant_grid_export_limitation"
SIG_IMPORT_LIMIT = "number.sigen_plant_grid_import_limitation"
SIG_CHARGE_LIMIT = "number.sigen_plant_ess_charge_cut_off_state_of_charge"

# HA input helper names
HA_ENABLE = "input_boolean.curtailment_manager_enable"
HA_THRESHOLD = "input_number.curtailment_manager_threshold"
HA_DNO_LIMIT = "input_number.curtailment_manager_dno_limit"

PREDICT_STEP = 5
SOC_MARGIN_KWH = 0.5

class CurtailmentPlugin(PredBatPlugin):
    def __init__(self, base):
        super().__init__(base)
        self.was_active = False

    def register_hooks(self, plugin_system):
        plugin_system.register_hook("on_update", self.on_update)

    def on_update(self):
        # 1. Read config from HA helpers
        # 2. Calculate curtailment plan
        # 3. Publish sensors
        # 4. Apply inverter control (if active and not read_only by user)
        # 5. Toggle read_only for next cycle
```

## Key Methods

**`calculate()`** — Pure calculation, returns (phase, target_soc_kwh, reserved_kwh, total_kwh, export_target_kw):
- Iterates forecast steps 0..forecast_minutes
- `pv_kwh = self.base.pv_forecast_minute_step.get(minute, 0)`
- `load_kwh = self.base.load_minutes_step.get(minute, 0)`
- `excess_kw = (pv_kwh - load_kwh) * 12`
- `dno_limit_kwh = dno_limit_kw / 12`
- Current PV excess for phase check: step 0 values

**`publish(phase, data)`** — Publishes sensors via `self.base.dashboard_item()`:
- Uses `self.base.prefix + ".curtailment_phase"` etc. for entity naming

**`apply(phase, export_target)`** — Writes SIG entities and manages read_only:
- Uses `self.base.call_service_wrapper()` for HA service calls
- Only writes when values change (tracks last-written state)
- On activation: sets read_only True (takes effect next cycle)
- On deactivation: restores entities, sets read_only False

## read_only Toggle Logic

- Plugin sets `self.base.set_read_only = True` directly (bypasses expose_config to avoid inverter reset)
- Also updates HA entity: `self.base.set_state_wrapper("switch.predbat_set_read_only", "on", ...)`
- On the activation cycle: Predbat already ran execute_plan (wrote demand mode), plugin overwrites with curtailment mode. Brief double-write, SIG handles last-wins.
- On subsequent cycles: execute_plan sees read_only → skips inverter control. Plugin writes curtailment settings cleanly.
- On deactivation: Plugin sets read_only False. Next cycle, Predbat resumes normal control.
- **User-visible:** `switch.predbat_set_read_only` shows ON during curtailment, `sensor.predbat_curtailment_phase` explains why.

## HA Automation Update

Update `automation.curtailment_manager_dynamic_export_limit` template:
```yaml
# Replace hardcoded min(PV-load, 4):
{% set target = states('sensor.predbat_curtailment_export_target') | float(-1) %}
{% if target >= 0 %}
  {{ target }}
{% else %}
  {{ [states('sensor.sigen_plant_pv_power')|float(0) - states('sensor.sigen_plant_consumed_power')|float(0), 4.0] | min | max(0) }}
{% endif %}
```

## Safety

- `grid_import_limitation = 0` prevents grid import during curtailment
- SIG CLS only triggers when battery discharge + PV export exceeds SIG installer limit (4.5kW); SMA curtails at 4kW at meter, so safe
- Export target never exceeds DNO limit
- Deactivation always restores all entities
- If plugin crashes, Predbat's normal inverter reset on next cycle restores safe state
- Plugin checks that read_only wasn't set by user BEFORE it was set by curtailment (don't clear user's read_only)

## Deployment

1. Create HA input helpers via MCP
2. Write `curtailment_plugin.py` to local repo
3. Deploy to Mum's HA: `scp curtailment_plugin.py hassio@100.110.70.80:/addon_configs/6adb4f0d_predbat/apps/predbat/`
4. Update HA automation template via MCP
5. Predbat auto-discovers plugin on next restart/file change
6. Enable via `input_boolean.curtailment_manager_enable`
7. Monitor `sensor.predbat_curtailment_*` sensors

## Verification

1. **Initial deploy with enable=off:** Plugin loads, publishes "off" phase sensor, does nothing else
2. **Enable in read-only:** Manually set Predbat read_only first, then enable curtailment. Check sensors calculate correctly without writing to inverter.
3. **Full test on sunny day:** Enable curtailment, disable manual read_only. Monitor:
   - Phase transitions as PV ramps up/down
   - Battery SOC tracks target
   - Export stays at/below DNO limit
   - No SMA curtailment when battery has capacity
4. **Deactivation test:** Disable curtailment_manager_enable. Verify Predbat resumes normal control within 1 cycle.
