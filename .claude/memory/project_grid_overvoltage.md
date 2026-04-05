---
name: Grid overvoltage issue
description: Local grid voltage rises during export causing SIG/SMA trips — SMA limits raised, SIG pending
type: project
---

Grid voltage on the lane rises when multiple houses export, causing overvoltage faults.

**SMA**: Stratford Energy visited and increased the acceptable grid voltage range (installer setting). No longer trips on overvoltage.

**SIG**: Still tripping on grid overvoltage (Level I, alert code 1011_1). Installer is Ricky. He needs to raise the SIG's overvoltage threshold to match the SMA setting. Waiting for next fault with voltage data as evidence.

**Voltage sensor**: Enabled 2026-04-02 in HA to capture exact voltage readings when SIG faults. Entity TBD — check after next fault.

**SMA export limit**: Set to 4.25kW (2026-04-02) to prevent cascade: when SIG trips on overvoltage, SMA was exporting uncontrolled at 8kW+. When SIG recovered it saw the breach and CLS faulted. The 4.25kW limit caps SMA output during SIG downtime.

**Why:** The cascade (overvoltage → SIG trip → uncontrolled SMA export → CLS fault on SIG recovery) is more damaging than the original overvoltage. The SMA limit prevents the cascade while waiting for Ricky to fix the SIG threshold.

**How to apply:** When reviewing SIG faults, check if "Environmental Abnormality" correlates with high grid voltage. If so, it's the overvoltage issue, not a curtailment manager problem.
