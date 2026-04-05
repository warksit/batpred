---
name: Check commit history before removing code
description: Before proposing to remove or simplify existing code, check WHY it was added — don't reintroduce solved problems
type: feedback
---

Before removing or simplifying existing curtailment code, ALWAYS check the commit that added it (git log, commit message) to understand the problem it solved.

**Why:** Recommended removing the v8.1 charge rate (target_charge_rate / HA automation) and replacing with binary export=0/DNO — which was exactly the v8.0 approach that v8.1 was created to fix. The charge rate prevents dumping all PV to battery and filling too fast. Had the full context in memory and commit history but didn't check.

**How to apply:**
1. Before proposing to remove/replace code: `git log --oneline | grep <feature>` and read the commit message
2. Ask: "would this change reintroduce the problem the original code solved?"
3. If the current problem is in a different area (e.g., activation), fix THAT area, don't undo the original design
4. When the user's question reveals a symptom, trace the root cause before proposing changes — the nearest code to the symptom is often not the right thing to change
