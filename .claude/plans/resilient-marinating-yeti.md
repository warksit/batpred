# Fix curtailment export windows being discarded by optimizer

## Context
On sunny days, battery hits 100% during peak solar and excess PV gets curtailed (SMA 3.5kW cap). The optimizer should pre-emptively drain the battery before solar peak so there's headroom to absorb excess.

Curtailment windows ARE being created (9.4kWh, 06:35-09:00, target 48%) but the optimizer discards them because exporting at 0p/kWh shows no **cost** improvement — only **metric** improvement (via `metric_curtailment_cost`).

The `best_soc_keep_solar_offset` is a red herring — keep is already reduced to 1.6 kWh but the optimizer bottoms at 18% (3.25 kWh) which is above keep, so the penalty is zero. The optimizer charges overnight at cheap rates because that's economically optimal without curtailment awareness.

## Root cause
`plan.py:1861` in `optimise_export()`:
```python
if (metric <= off_metric) and (metric <= best_metric) and ((cost + min_improvement_scaled) <= off_cost):
```

This requires **both** metric AND cost improvement. The curtailment cost penalty is inside `metric` (via prediction.py simulation), but exporting at 0p/kWh increases raw `cost` (battery losses). So the cost guard rejects the window.

We already added a `curtailment` flag to bypass the cost guard, but the window is injected into `self.export_window` (input windows) before the optimizer. The optimizer then evaluates it via `optimise_export()` which tries `[100.0 (off), 99.0 (freeze), 0.0 (full export)]` — when it compares "off" vs "export at 48%", the cost is worse for exporting, so it picks "off" (disables the window).

## Fix approach

The cost guard bypass we added checks `window.get("curtailment", False)` but the `window` variable in `optimise_export()` is the original window dict. Let me verify the flag survives through the optimizer.

### Step 1: Verify the curtailment flag flows through
- The window dict with `"curtailment": True` is added to `self.export_window`
- `optimise_export()` at line 1706: `window = export_window[window_n]` — this should have the flag
- Confirm with a debug log

### Step 2: Check if metric actually improves for curtailment export
The metric comparison `metric <= off_metric` may fail if:
- The curtailment cost in the simulation isn't large enough
- Battery losses from exporting exceed the curtailment savings
- The `off` scenario (100%) already has the curtailment penalty but battery stays full and absorbs some

Need to add debug logging to see the actual metric/cost values for off vs export options.

### Step 3: Implementation
In `fetch.py`, add debug log for the solar offset value (minor fix).

In `plan.py:optimise_export()`, add logging for curtailment windows to see what metrics the optimizer computes:
```python
if window.get("curtailment", False) and self.debug_enable:
    self.log("Curtailment export eval: limit={} metric={} cost={} off_metric={} off_cost={} cost_ok={}".format(
        this_export_limit, dp2(metric), dp2(cost), dp2(off_metric), dp2(off_cost), cost_ok))
```

Temporarily enable `debug_enable` to see these logs, then fix based on findings.

### Step 4: If metric doesn't improve enough
The curtailment cost (21.5p/kWh × 9.4kWh = 202p) should be a large penalty. If the "off" scenario already shows curtailment in its metric, both options have similar curtailment — meaning the export window placement isn't actually reducing curtailment in the simulation.

This could happen if:
- The export window time (06:35-09:00) doesn't actually drain the battery enough before solar peak (09:00-15:00)
- The simulation with export at 48% still reaches 100% SOC before peak solar
- Need to check the prediction simulation to ensure the forced export actually creates headroom

## Files to modify
- `apps/predbat/fetch.py:2155` — add `Info:` debug log for solar offset value
- `apps/predbat/plan.py` — add debug logging in `optimise_export()` for curtailment windows
- Temporarily enable `debug_enable` on Mum's HA to capture optimizer decisions

## Verification
1. Deploy with debug logs
2. Enable debug mode: set `switch.predbat_debug_enable` to true
3. Wait for plan recompute
4. Check predbat.log for curtailment export eval lines
5. Compare metric for off (100%) vs export (48%) — the export should have significantly lower metric
6. If not, investigate why the simulation doesn't show curtailment reduction
7. Disable debug mode after investigation
