# Predbat v8.46.4 → v9.3.7 upgrade runbook (Middlemuir)

Prepared 2026-10-11. Branch: `cm-on-v9.3.7` = upstream tag `v9.3.7` + our 183
commits. **Not yet run.** Everything under "Facts" was checked on the day; re-check
the ones marked ⟳ before starting, because they go stale.

## Method

Let Predbat's own updater install the stock v9.3.7 files (it verifies each download
against GitHub checksums), then put our files back on top with the deploy script.

The updater overwrites `plugin_system.py` and `predbat.py` with stock. Between that
and step 4 the plugins are broken: `soc_keep_publish_plugin` loads as the empty
base class (the PR #4319 bug) and CM's `on_before_plan` hook never runs. Keep that
gap to a couple of minutes.

## Facts

- Box: add-on 1.7.1, Python 3.12.3, `aiohttp` 3.14.3 (v9 needs ≥ 3.12). aarch64. ⟳
- `switch.predbat_auto_update` is **off**. `update.predbat_version` offers v9.3.7. ⟳
- Box files match `cm-on-latest-predbat` @ `d03e8fbc` byte for byte (the six below). ⟳
- Runtime files that differ from the stock tag — exactly these six, nothing else:

  | File | vs v9.3.7 |
  |---|---|
  | `plugin_system.py` | modified (discovery fix + `on_before_plan` hooks/priority) |
  | `predbat.py` | modified (+8 lines: calls the hooks) |
  | `curtailment_plugin.py`, `curtailment_calc.py` | ours |
  | `cold_weather_plugin.py`, `soc_keep_publish_plugin.py` | ours |

- Startup `check_install` only **warns** on a modified file. It fails and
  re-downloads the release only if a manifest-listed file is missing or empty.
  So two "size/SHA mismatch" warnings in the log after step 4 are expected.
- The updater does not delete files it does not know about; the plugin files,
  `apps.yaml`, `curtailment_state.json` and the logs stay.
- Gates on `cm-on-v9.3.7`: pre-commit, curtailment (443), requirements,
  registration, soc-keep, host contract, 12 YAML harnesses, upstream quick suite.

## When

Evening, after sundown: policy `Predbat`, CM phase Off, no saving session within the
next hour, not inside a cheap-rate charge window. Not on a day with a free session.

## Steps

1. **Pre-flight (read only).**
   - `predbat.status` is not `Read-Only`; `input_select.sig_dispatch_policy` = `Predbat`.
   - Re-check the ⟳ facts.
   - `git switch cm-on-v9.3.7 && git status` is clean and pushed.

2. **Back up the box.**

   ```
   ssh hassio@100.110.70.80 'sudo tar czf /addon_configs/predbat-pre-v9-$(date +%Y%m%d).tgz -C /addon_configs 6adb4f0d_predbat && ls -la /addon_configs/*.tgz'
   ```

   About 113 MB; 14 GB free.

3. **Run the core update to v9.3.7** (HA: `update.predbat_version` → Install, or
   `select.predbat_update` → the `v9.3.7 …` option). Predbat restarts itself. Wait
   for `predbat.py` on the box to read `THIS_VERSION = "v9.3.7"`.

4. **Put our six files back, at once.**

   ```
   .claude/bin/deploy apps/predbat/plugin_system.py apps/predbat/predbat.py \
     apps/predbat/curtailment_plugin.py apps/predbat/curtailment_calc.py \
     apps/predbat/cold_weather_plugin.py apps/predbat/soc_keep_publish_plugin.py
   ```

   It refuses on a dirty tree or unpushed commits, verifies md5 on the box, writes
   `DEPLOYED_SHA` and triggers the reload.

5. **Verify — each line is a pass/fail.**
   - Log: `Initialising plugin class (name-based): ColdWeatherPlugin`, `CurtailmentPlugin`,
     `SocKeepPublishPlugin` — the last must NOT read `PredBatPlugin`.
   - Log: `Curtailment: restored state from …`, no `Traceback`.
   - `predbat.status` attribute `version` = `v9.3.7`; a plan completes
     (`Info: Completed run status …`). Note how long the plan took.
   - `sensor.predbat_curtailment_phase` updated within the last cycle.
   - Log: `Octopus:` saving-session lines still appear (the `octopus_saving_session`
     regex in apps.yaml must still resolve).
   - Inverter: `predbat.status` matches what the plant is doing; the three mapper
     automations are `on`; EMS mode is one Predbat would set.

6. **Settle the three defaults that change.** Decide, do not inherit:
   `calculate_second_pass` (now on), `calculate_pv90_plan` (new, on),
   `car_charging_plan_smart` (now on; no car here). Both planning switches cost
   compute — if the plan time in step 5 is uncomfortable on the HA Green, turn
   `performance_tweaks` on to expose them and switch one off.

## Rollback

```
ssh hassio@100.110.70.80 'sudo tar xzf /addon_configs/predbat-pre-v9-YYYYMMDD.tgz -C /addon_configs && sudo touch /addon_configs/6adb4f0d_predbat/predbat.py'
```

That restores every v8.46.4 file including its `manifest.yaml`. The extra v9-only
files are left behind and are ignored by v8. Then confirm the version reads
v8.46.4 and the three plugins load. `git switch cm-on-latest-predbat` locally so
the deploy script and the box agree again.

## After a good upgrade

- Next morning: one curtailment window with CM driving on v9. Watch for the new
  "settings changed outside Predbat" detector (web UI → metrics) reacting to the
  heartbeat's writes — not checked in advance, needs the box.
- `cm-on-latest-predbat` becomes the rollback branch; `cm-on-v9.3.7` is the deployed
  one. `cm-on-upstream-main` (4 commits past the tag) can be deleted.
- Things v9 does natively that we patched round: free sessions (null code, joined
  zero-reward, horizon from now). `octopus_free_electricity` in apps.yaml can stay.
