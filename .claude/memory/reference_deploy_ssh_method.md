---
name: SSH deploy method for Mum's HA
description: The exact SSH command that works to copy files to Mum's HA — use this FIRST, skip scp/tee/docker which all fail
type: reference
---

## Deploy command (use this, skip everything else)

Files on Mum's HA are owned by root. The `hassio` SSH user cannot write directly. The ONLY method that works:

```bash
ssh hassio@100.110.70.80 "sudo cp /dev/stdin /addon_configs/6adb4f0d_predbat/FILENAME.py" < /Users/andrew/code/batpred/apps/predbat/FILENAME.py
```

## What does NOT work (don't try these)

- `scp` — permission denied (root-owned files)
- `scp -O` — same
- `cat file | ssh "cat > path"` — permission denied
- `cat file | ssh "tee path"` — permission denied
- `docker exec` — no docker socket access from hassio user

## Deploy path

Remote: `/addon_configs/6adb4f0d_predbat/` (flat directory, no `apps/predbat/` subdirectory)

## After deploying plugin-only files

`curtailment_plugin.py` is not in Predbat's watched file list. Touch a watched file to trigger restart:

```bash
ssh hassio@100.110.70.80 "sudo touch /addon_configs/6adb4f0d_predbat/plugin_system.py"
```
