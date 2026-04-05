# SIG Freeze Mode Issues (discovered 2026-03-11)

## Two problems

### 1. Optimizer choosing FrzExp inappropriately

On 2026-03-11, plan showed FrzExp at 08:35-09:00 with export_rate=0p. Only 0.26 kWh of curtailment expected at midday. The optimizer chose freeze export to avoid ~6p of curtailment cost (0.26 * 21.5p), but this causes grid imports at 28p when PV < load during the freeze window — a net loss.

The optimizer doesn't account for the fact that on SIG, freeze blocks battery from serving load (causing imports). On GivEnergy/Fox, freeze still allows battery to serve load.

**Possible fixes:**
- Disable freeze export/charge for `soc_limits_block_solar` inverters entirely
- Or make the optimizer aware that freeze on SIG = grid import cost for any load > PV

### 2. SIG freeze implementation is wrong

**Freeze Export** (`adjust_export_immediate` with freeze=True, inverter.py:2505):
- Sets EMS to "Maximum Self Consumption" with discharge_floor = `reserve` (0%)
- Should set discharge_cut_off = current SOC% AND charge_cut_off = current SOC%
- Currently: charge_cut_off stays at whatever it was, discharge_cut_off = 0% (not frozen at all)

**Freeze Charge** (`adjust_charge_immediate` with freeze=True, inverter.py:2464):
- Sets EMS to "Maximum Self Consumption" with discharge_floor = `soc_percent`
- This sets discharge_cut_off correctly but doesn't touch charge_cut_off

**What "freeze" means on SIG:**
- charge_cut_off = current SOC% → blocks solar charging battery (but also blocks solar path through inverter)
- discharge_cut_off = current SOC% → blocks battery discharging (including serving load)
- EMS = Maximum Self Consumption

**Fundamental SIG limitation:**
On GivEnergy/Fox, "freeze" pauses charge/discharge while solar still flows through to serve load. On SIG, the only levers are charge_cut_off and discharge_cut_off, which block EVERYTHING:
- discharge_cut_off = SOC% → battery can't serve load → grid imports when PV < load
- charge_cut_off = SOC% → solar can't charge battery (intended) but this is `soc_limits_block_solar` behavior

There is NO SIG EMS mode that means "hold SOC but still let battery serve load." Maximum Self Consumption without limits is the closest — but that's just Demand mode (battery charges from PV, serves load).

**Confirmed by testing:** User manually set both charge_cut_off and discharge_cut_off to 29% (current SOC). When PV dropped below load, system imported from grid at 28p instead of using battery.

### Decision needed
- Should freeze modes be disabled entirely for SIG? (safest, loses some optimization flexibility)
- Or is there a creative SIG EMS mode combination that achieves freeze?
- Or should the optimizer simply never pick freeze (limit=99) when `soc_limits_block_solar` is True?
