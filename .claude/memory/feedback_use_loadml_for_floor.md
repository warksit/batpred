---
name: Use LoadML for floor calculation
description: Floor calculation should use LoadML (Predbat's load forecast), not base load or spike factors
type: feedback
---

Use LoadML for the curtailment floor calculation, not base load (0.5kW) or spike factors.

**Why:** LoadML is Predbat's best load estimate. The slight underestimate from load averaging (Jensen's inequality at the DNO threshold margin) is within the 10% headroom (SOC_CAP_FACTOR = 0.90). Using base load would be an extreme pessimistic assumption that ignores all load forecasting. The real safety comes from the headroom margin, not from worst-casing the load.

**How to apply:** When computing overflow or floor in curtailment_calc.py, always use load_forecast (LoadML) as-is. Don't substitute min_base_load for the trajectory/floor calculations. min_base_load is only for the solar geometry release threshold (DNO + base_load = safe export level).
