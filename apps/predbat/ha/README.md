# HA automations — SIG / curtailment control

**Repo YAML is the source of truth.** Never change a control automation in HA
without updating and committing the matching file here first, then deploy with
full `config:` replacement (not `python_transform` — it reorders `variables`).

See also: `.claude/memory/sig-control-maintenance.md` (maintenance posture),
`.claude/CLAUDE.md` (Writer Ownership, deploy checks).

---

## Who may write plant registers

Master policy surface: `input_select.sig_dispatch_policy`
(and `input_select.sig_override` for human override of that policy).

| Policy (effective) | Sole plant writer | Must not write plant |
|---|---|---|
| **Predbat** | The three Predbat mappers (below). Heartbeat **parks** on entry (Remote EMS ON + MSC) and **self-heals** — on any trigger, if it finds the plant in `PCS Remote Control` it writes MSC (RD24). Otherwise it writes **nothing**. | Heartbeat active/dispatch branches |
| **Max Export** | `sig_dispatch_heartbeat` only | All three Predbat mappers (disabled while CM drives) |
| **Hold Battery** | `sig_dispatch_heartbeat` only | Same |
| **Solar Charge Battery** | `sig_dispatch_heartbeat` only | Same |

### Predbat mappers (plant writers when policy = Predbat)

| Automation | Writes | Driven by |
|---|---|---|
| `predbat_requested_mode_action` | EMS control mode, `grid_import_limitation` | `input_select.predbat_requested_mode` |
| `predbat_max_discharging_limit_action` | `ess_max_discharging_limit` | `input_number.discharge_rate` |
| `predbat_max_charging_limit_action` | `ess_max_charging_limit` | `input_number.charge_rate` |

### Intent / helpers (not plant writers)

| Piece | Role |
|---|---|
| `curtailment_plugin` | Sets policy + floors; may park EMS→MSC; enables/disables writer roles. Does **not** own the PCS setpoint loop. |
| `sig_dispatch_intent_helpers.yaml` | **Single** copy of dispatch maths (RD26). Heartbeat must not re-inline it. |
| `sig_keep_floor_guard` | Policy / floor guard only — must not become a second plant writer. |

**Rule:** if a change needs a second copy of a formula, or a new automation
enable in the mutex, stop and simplify instead.

Disabling a mapper stops **future** writes; it does **not** clear registers
already written. The writer that changed a register must change it back
(see CLAUDE.md Writer Ownership).

---

## Active control files (keep)

| File | Role |
|---|---|
| `sig_dispatch_heartbeat.yaml` | Sole plant writer for non-Predbat policies |
| `sig_dispatch_intent_helpers.yaml` | Dispatch maths — single source of truth |
| `predbat_requested_mode_action.yaml` | Predbat → EMS mode |
| `predbat_max_charging_limit_action.yaml` | Predbat → ESS max charge |
| `predbat_max_discharging_limit_action.yaml` | Predbat → ESS max discharge |
| `sig_keep_floor_guard.yaml` | Keep-floor / dusk policy guard |
| `sig_manual_override_failsafe_off.yaml` | Override safety |
| `sig_*_alert*.yaml`, `sig_voltage_protect.yaml`, `sig_saving_session.yaml` | Alerts / protect / sessions |
| `big_overflow_load_advice.yaml` | Advice only |

### Not an automation

| File | Role |
|---|---|
| `mum-apps.yaml` | Mirror of the Predbat **addon** config on the box (`/addon_configs/6adb4f0d_predbat/apps.yaml`). Different deploy path to everything above: edit the box copy, mirror it back here in the same change. `mcp_secret` is redacted — this fork is public. Tracked since 2026-08-17, after an entity rename silently disabled saving sessions in an untracked config. |

## Legacy / dormant (do not re-enable for control)

| File | Why dormant |
|---|---|
| `curtailment_manager_dynamic_export_limit.yaml` | SMA-era export-limit phases |
| `curtailment_stale_phase_watchdog.yaml` | Never deployed; park-on-death idea |
| `voltage_seek_controller.yaml` | Voltage seek into CM export path |
| `voltage_throttle_filter_asymmetric_rate_limit.yaml` | Voltage throttle into filtered cap |

Prune from live HA when confirmed unused; keep in git history.

---

## Golden checks (run before / after control changes)

```bash
cd apps/predbat
python3 tests/test_yaml_heartbeat.py
python3 tests/test_yaml_dispatch_intent.py   # maths only in intent helpers
python3 tests/test_yaml_requested_mode.py
python3 tests/test_yaml_curtailment.py
```

Behaviour that must stay true:

1. **policy = Predbat** → heartbeat does not drive PCS dispatch (park/self-heal MSC only as designed).
2. **Active CM policy** → mappers are not the plant writers; heartbeat re-opens ESS/import limits so a prior freeze cannot lock the battery.
3. **Handback** → no stranded PCS Remote Control / zero ESS limits after return to Predbat.

Live verify with an automation **trace** and a discriminating condition, not a casual state read (CLAUDE.md).

---

## Deploy

1. Edit YAML in this directory and commit.
2. Run the matching Jinja harness(es); they must fail for the right reason first when pinning a bug.
3. Apply to HA with full config replacement.
4. Confirm with a discriminating observation (trace + known input → expected write).
