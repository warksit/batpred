---
name: Curtailment v8 design decisions
description: Key design decisions and learnings from v6→v7→v8 evolution, validated from Mar 28+31 real data
type: project
---

## v8 Curtailment Manager Design (2026-03-31)

**Activation**: SOC trajectory simulation ("will battery reach 95%?") replaces per-slot overflow (which missed overflow on correctly-forecast days because LoadML averaged load masked intermittent excess).

**Floor**: `soc_max * 0.95 - net_charge` from trajectory. Bidirectional — drops when PV ahead, rises when behind. No ratchet, no cap. Rises naturally through afternoon as future overflow shrinks → pulls SOC up → battery reaches 95% → release to 100%.

**Key asymmetry**: floor too low = safe (charges back from PV). Floor too high = SIG fault.

**Validated thresholds (Mar 28, 120 slots)**:
- Mean PV < 2kW: zero spikes above 4.5kW (safe to release)
- Mean PV 2-4kW: spikes to 6-10kW (overflow risk)
- Max/mean ratio up to 3.9x

**SIG charge curve** (rated 8.8kW):
- 95%: 80% = 7.0kW (can absorb overflow)
- 96%: 67% = 5.9kW (OK)
- 97%+: 32% = 2.8kW (can't absorb)

**Physics constraint**: AC-coupled SMA — overflow (PV-load-DNO) charges battery, can't prevent. Floor determines START point; overflow adds on top.

**LoadML issue**: per-slot averages unreliable (DHW cycles, cooking outliers). Totals are roughly right. Trajectory uses totals.

**Tariff**: Octopus Cosy (cheap 04-07, 13-16, 22-00; peak 16-19; standard rest).

**Why v6/v7 failed**:
- v6: instantaneous PV scaling (5-min snapshot) thrashed floor, 51 transitions
- v7: per-slot overflow (PV-LoadML) always 0 despite actual overflow; oscillated 20 times
- Both: Off→MSC transition during PV caused export spikes → SIG fault
