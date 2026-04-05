---
name: Curtailment thinking model
description: Don't think in per-slot excess vs DNO. Think in SOC trajectory and total energy balance. Per-slot averages miss spikes.
type: feedback
---

Do NOT think about curtailment as "will per-slot PV-load exceed DNO?" — this fails because averaged values miss spikes (validated: mean 3kW can spike to 10kW).

DO think: "will the battery reach 95%?" — answered by SOC trajectory simulation using total energy (which IS accurate even when per-slot isn't).

**Why:** Per-slot overflow was always 0 on a correctly-forecast 38kWh day (Mar 31) because LoadML's averaged DHW/GSHP load masked the intermittent excess. The trajectory correctly predicted the battery would fill.

**How to apply:** When discussing overflow, activation, or floor — always frame in terms of trajectory/totals, never per-slot excess vs DNO threshold.
