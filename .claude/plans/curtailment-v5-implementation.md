## ✅ COMPLETE — Implement Curtailment Manager v5 — Iterative Target SOC
Committed c12bb5b (2026-03-17). Not yet deployed. Needs real-world test on high-PV day.

### Context
Read these files first:
- `.claude/modelling/curtailment_model_v5.py` — the validated algorithm (THIS IS THE SPEC)
- `.claude/projects/-Users-andrew-code-batpred/memory/curtailment-redesign.md` — full context
- `.claude/projects/-Users-andrew-code-batpred/memory/sig-control.md` — SIG inverter details
- `apps/predbat/plugin_system.py` — plugin lifecycle (on_init, on_update hooks)
- `apps/predbat/execute.py` — how Predbat sets inverter modes (especially SIG path)
- `apps/predbat/plan.py` — where best_soc_keep and charge_limit_best are set
- `apps/predbat/inverter.py` — adjust_ems_mode(), adjust_charge_immediate(), adjust_export_immediate()
- `apps/predbat/tests/test_execute.py` — test patterns for execute tests

### Algorithm (from v5 model)
Every Predbat cycle (5 min):
1. remaining_overflow = sum of max(0, (pv_forecast - load_forecast) - DNO_LIMIT) * slot_hours for all FUTURE slots
2. target_soc = battery_max - remaining_overflow (clamped 0..100%)
3. If SOC > target: force export (D-ESS) to drain toward target, capped at DNO limit
4. If SOC < target: charge from PV toward target, export remainder
5. If PV excess > DNO limit: export DNO limit, battery absorbs overflow
6. If PV < load: battery covers load (self-consumption, always allowed)

Activation: any day where remaining_overflow > 0 (any future slot with excess > DNO limit)
Deactivation: not needed — target_soc naturally reaches 100% as overflow consumed

### Implementation Plan

#### Step 1: TDD — Write tests first
Create `apps/predbat/tests/test_curtailment.py`:
- Test the PURE ALGORITHM functions (no Predbat dependencies)
- Use the 6 CSV validation files listed in curtailment-redesign.md
- For each day, verify:
  - Zero curtailment (main goal)
  - Dawn target SOC matches expected values from the model results table
  - Battery reaches 100% by end of day (or close)
  - Export never exceeds DNO limit (4kW) in any slot
- Also test edge cases:
  - No overflow day → plugin stays inactive (target = 100%)
  - Battery starts full → immediate drain
  - Battery starts empty → charges from PV first

Register the test in unit_test.py following the existing pattern.

#### Step 2: Pure algorithm module
Create `apps/predbat/curtailment_calc.py`:
- `compute_remaining_overflow(pv_forecast, load_forecast, dno_limit, minutes_now, forecast_minutes, step_minutes)` → float kWh
- `compute_target_soc(remaining_overflow, battery_max_kwh)` → float kWh
- `should_activate(remaining_overflow)` → bool
- These functions take Predbat's minute-indexed forecast dicts (pv_forecast_minute_step, load_minutes_step) directly
- No HA dependencies, no side effects — pure functions

#### Step 3: Plugin class
Create `apps/predbat/curtailment_plugin.py`:
- Class `CurtailmentPlugin` with PREDBAT_PLUGIN = True
- `__init__(self, base)` — store reference to PredBat instance
- `register_hooks(self, plugin_system)` — register on_update
- `on_update(self)`:
  1. Check if enabled (via apps.yaml config `curtailment_enable`)
  2. Read DNO limit from config (`curtailment_dno_limit`, default 4.0)
  3. Call curtailment_calc functions with self.base.pv_forecast_minute_step, self.base.load_minutes_step
  4. If active: override self.base.charge_limit_best and/or self.base.best_soc_keep
  5. Log status to HA sensor for dashboard visibility

Key question: should the plugin override charge_limit_best (telling Predbat what SOC target to aim for), or should it directly call adjust_ems_mode() on the inverter? The former is cleaner (works WITH Predbat), the latter gives more control but fights Predbat.

The plugin should work WITH Predbat by setting charge_limit_best. Predbat's execute.py already handles the SIG mode switching (MSC, D-ESS, Grid First) based on charge_limit vs current SOC. The plugin just needs to tell Predbat the right target.

### Config (apps.yaml)
```yaml
curtailment_enable: True
curtailment_dno_limit: 4.0
```

### Test Data Files
Six validation days (15-min CSV, semicolon-delimited):
- /Users/andrew/code/mum-energy/data/sma/daily_15min/Energy_Balance_2025_06_19.csv (65kWh peak, Watts)
- /Users/andrew/code/mum-energy/data/sma/daily_15min/Energy_Balance_2025_07_12.csv (68kWh peak, kW)
- /Users/andrew/code/mum-energy/data/sma/daily_15min/Energy_Balance_2025_07_09.csv (45kWh cloudy, kW)
- /Users/andrew/code/mum-energy/data/sma/daily_15min/Energy_Balance_2025_07_15.csv (23kWh poor, kW)
- /Users/andrew/code/mum-energy/data/sma/daily_15min/Energy_Balance_2025_07_01.csv (39kWh moderate, kW)
- /Users/andrew/code/mum-energy/data/sma/daily_15min/Energy_Balance_2025_05_30.csv (47kWh, kW)

Battery: 18.08 kWh, max charge/discharge 5.5kW, DNO limit 4kW.

### Expected Results (from validated model)
| Day        | PV   | Overflow | Dawn Target | Curtailed | End SOC |
|------------|-----:|--------:|-----------:|----------:|--------:|
| Jun 19     | 66   | 13.0    | 28%        | 0         | 93%     |
| Jul 12     | 68   | 15.5    | 14%        | 0         | 92%     |
| Jul 9      | 45   | 3.6     | 80%        | 0         | 91%     |
| Jul 15     | 23   | 0.0     | OFF        | 0         | 83%     |
| Jul 1      | 39   | 1.3     | 93%        | 0         | 87%     |
| May 30     | 47   | 3.8     | 79%        | 0         | 92%     |

### DO NOT
- Touch Flux, tariff rates, or export pricing — curtailment is tariff-agnostic
- Modify execute.py or plan.py core logic
- Add HA automations — this is a Predbat plugin only
- Over-engineer: the algorithm is ONE formula, recalculated each cycle
