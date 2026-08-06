# SIG / CM control — maintenance posture (from 2026-08)

**Status:** architecture frozen while CM is stable. Written after CM was
judged “working pretty well, edge fine-tuning only,” with autumn approaching
(Predbat dominant).

Related: `apps/predbat/ha/README.md` (ownership table),  
`.claude/plans/sig-control-python-migration.md` (**proposal, deferred** — not the current path),  
`origin/sig-control-v2` (historical Predbat EMS driver; **not** a CM Hold/Max Export solution).

---

## Freeze (default)

Do **not** start a structural rewrite of the live control path (Python executor,
delete heartbeat, merge `sig-control-v2` wholesale, new arbiter entity) unless:

- multi-writer / mutex defects burn **multiple** days again, **or**
- overflow season shows the HA layer is still the dominant failure mode,

**and** any PCS-in-Python move has an explicit park-if-Predbat-dies answer.

Edge fine-tuning of floors, activation, and cards is fine. New architecture is not.

Revisit the migration plan at earliest **next high-overflow season**, not as an
autumn project.

---

## What *is* worth doing (simple maintainability)

1. **Prefer plan-side CM off** over clever handback. Tighter activation on dull
   days means fewer mutex transitions. Autumn/winter: Predbat should own most hours.
2. **One owner table** — `apps/predbat/ha/README.md`. If a change violates it, reject it.
3. **No dual maths** — dispatch formula lives only in `sig_dispatch_intent_helpers.yaml`
   (RD26). `test_yaml_dispatch_intent.py` enforces this.
4. **Repo YAML is source of truth** — commit first, deploy full config; never
   live-only JSON edits for control automations.
5. **Golden tests** before/after control edits (heartbeat, intent, requested_mode,
   curtailment). Especially: Predbat-idle heartbeat quiet; clean handback.
6. **Prune dead automations** from live HA when unused (see ha/README legacy list).
   Do not re-enable them “just in case.”
7. **Optional later:** firmware discharge cut-off = keep floor (small win) — only
   if keep-floor guard is still painful. Skip if quiet.

---

## What *not* to do this autumn

| Idea | Why not |
|---|---|
| Full Python Hold executor + delete HA layer | High cost/risk; CM barely running in shoulder season |
| Merge `sig-control-v2` as the CM solution | EMS/MSC model cannot express export-priority Hold; different problem |
| New arbiter / more automations | More handover surface |
| Refactors that touch the enable/disable writer mutex | Highest historical bug density (2026-08-06) |
| Structural control changes mid-overflow day | CLAUDE.md: land in low-stakes windows only |

---

## Seasonal focus (Predbat-heavy)

As CM arms less often, maintenance shifts to the **Predbat path**:

- Three mappers complete and correct for charge / freeze / discharge / demand.
- After a CM day: handback leaves ESS limits open, EMS in MSC, no stranded PCS
  (heartbeat self-heal + `_neutralise_predbat` rules still apply).
- Smoke after any CM change: policy stays Predbat for a quiet period with no
  unexpected plant writes.

CM edge-tuning is still allowed; treat unexpected handback/mutex behaviour as
stop-and-audit (two surprises = audit, not more patches).

---

## Recovered branch note

`sig-control-v2` is on origin (Mar 2026): Predbat EMS modes + rates + discharge
cut-off via `adjust_ems_mode` / `soc_limits_block_solar`. Useful reference for a
**future Predbat-half** driver port; **not** a substitute for Max Export / Hold
Battery / Solar Charge (those need PCS + live setpoint). Do not land it casually
on the live CM stack.
