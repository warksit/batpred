---
name: Always deploy ALL files together
description: Never deploy a single .py file to Mum's HA — always deploy the full set of changed files from the current branch
type: feedback
---

ALWAYS deploy ALL changed .py files together when pushing to Mum's HA. Never deploy just one file.

**Why:** This has now caused TWO incidents (2026-03-10 and 2026-03-12). Deploying a single file creates a mismatch between files from different branches, crashing Predbat. The user has documented this rule multiple times and I keep breaking it.

**How to apply:** When deploying to Mum's HA, ALWAYS use the full file list deploy command. Never use a single `cat file | ssh tee` for just one file. Even if the change is only in one file, deploy all 10: config.py, execute.py, fetch.py, inverter.py, output.py, plan.py, predbat.py, prediction.py, web.py, web_helper.py.

## Plugin files and restart trigger

`curtailment_plugin.py` is NOT in Predbat's watched files list (`PREDBAT_FILES`). Deploying it alone does NOT trigger a Predbat restart. After deploying plugin files, you MUST touch `plugin_system.py` to force a restart:

```bash
ssh hassio@100.110.70.80 "sudo touch /addon_configs/6adb4f0d_predbat/plugin_system.py"
```

Remote deploy path is `/addon_configs/6adb4f0d_predbat/` (flat, NOT `apps/predbat/` subdirectory).
