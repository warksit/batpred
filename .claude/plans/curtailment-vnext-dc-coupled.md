# Curtailment Manager v-next — DC-coupled EC 6.0 SP design

Drafted 2026-07-15 23:30, swap night, from live lever-test evidence. Status: DESIGN FOR
DISCUSSION (CLAUDE.md: discuss before coding). No code until validation gates pass.

## Why the old design is dead

The old automation's physics: SMA pushed PV onto the AC bus regardless; capping SIG grid
export forced the surplus into the battery ("passive absorption"). Every phase was an
export-limit value. Post-swap the SIG owns the PV at the MPPT: capping export just throttles
the MPPT (proven Jul 15: export=0 + D-ESS → PV tracks load, battery idle, yield lost).
R25's premise inverts: the enemy is no longer DNO breach (hardware enforces 3.68 always);
it is a FULL BATTERY at peak (MPPT clip = 31p/kWh incl FIT).

## Proven control vocabulary (Jul 15 lever tests)

| Action | Mechanism | Evidence |
|---|---|---|
| Absorb (PV→battery first) | MSC mode (native or remote) | default behaviour |
| Drain / export battery | Remote EMS on + PCS Remote Control + `active_power_fixed_adjustment` = +kW | export observed 22:47 (amplitude gate V2 below) |
| Grid-charge battery | same, negative dispatch | −3 → 3.5 kW import, instant |
| Kill switch | `grid_export_limitation` = 0 | stopped rogue export instantly, twice |
| Grid cap | `grid_export_limitation` = 3.68 | works; ALSO required for PV export |
| Hold (export PV, don't absorb) | **UNPROVEN** — candidate: `ess_charge_cut_off_state_of_charge` ≈ current SOC | gate V3 |

Registers proven inert: `ess_max_charging_limit` / `ess_max_discharging_limit` (writes
rejected, not enforcing). Registers proven perishable: `grid_export_limitation` reverted
to 0 once (trigger unidentified — gate V4).

## Design principles (new requirements, to fold into REQUIREMENTS.md after discussion)

1. **R25 stands (corrected 2026-07-15, Andrew)**: once PV − load > export limit (3.68),
   there are NO levers — export is pinned by PV, drain has no AC headroom, and refusing to
   fill = MPPT throttle. During overflow the battery CAN ONLY FILL. All management (drain /
   hold / room-making) must complete BEFORE overflow starts; required room is judged AT
   overflow start. Only the consequence changed: arriving with too little room now costs
   MPPT clipping (~31p/kWh incl FIT) instead of fault risk.
2. **Single-writer doctrine**: exactly ONE automation writes SIG registers. Predbat's
   requested-mode mapper routes through it; the Sigen app manual modes are human override —
   detect divergence (commanded vs observed power) and alert rather than fight.
3. **Perishable registers**: every commanded state is re-written on a heartbeat (30–60 s)
   while active. Loss of heartbeat (Predbat/HA death) must decay to the safe state.
4. **Safe state** = native self-consumption + Remote EMS off + export_limitation 3.68.
   The stale-phase watchdog reverts to this, not to "export = DNO".
5. **Remote EMS sessions are opened, used, closed** — never left on idle (remote-MSC
   behaved oddly Jul 15; retest as V5 now the manual-dispatch confound is known).

## VALIDATED Jul 16 morning — the phase model collapses to one continuous lever

V2 passed textbook (PCS Remote Control + dispatch 3.0 → PCS output exactly 3.0, PV
surplus auto-charged battery). Then the morning play: **dispatch 4.15 (= 3.68 + load) →
grid pinned at −3.68 exactly (grid-limit register clamps), battery buffers the
difference bidirectionally.** One static register state runs the whole day:

- PV < dispatch: battery tops up → morning drain-export / dusk export-down
- PV > dispatch: battery absorbs the excess → absorption with full export channel
- battery full: grid limit + MPPT clip (same as MSC) — the R25 failure mode, planned
  away by entering the day empty
Executor design therefore: **continuous control, not state machine** —
`dispatch = export_limit + load_estimate`, heartbeat-rewritten, with SOC guards:
(a) evening keep floor: SOC ≤ floor → dispatch = load only (stop drain);
(b) overnight: exit to native MSC (safe state).
The Charge/Hold/Drain phases and split thresholds become internal *decisions about the
dispatch value*, not modes. Plugin keeps: overflow forecasting, overnight targets, keep
floors. Automation sheds: all phase/EMS-mode switching.
Watch-point: terminals 253.8 V at full export (cutout ≈247-248 per cable offset) —
volt-watt knee unknown (Ricky); unexplained export sag below 3.68 = knee engaging.
Sensor semantics change: `consumed_power` now includes DC battery charging — plugin
must switch load input to `total_load_power` (also fix solar dashboard Home tile +
battery arrow).

## IMPLEMENTED 2026-07-18 — policy-driven session architecture (interim executor)

Master control surface: **`input_select.sig_dispatch_policy`** (Off / Full Export / Hold / Load Only). Everything sets the policy; ONE automation writes registers.

- **`automation.sig_dispatch_heartbeat`** — sole writer of Remote EMS enable + control mode + export limit + PCS dispatch. Triggers: 1-min beat, policy change, stale-setpoint (>0.3 kW dev, 10 s). Off → release to app MSC if we own it (Remote EMS on + PCS). Active → open session + write. Dispatch: Full Export = cap+load; Hold = max(PV,load); Load Only = load. **Hard-floor LIVE CLAMP**: SOC ≤ `sig_hard_floor_pct` (12) → dispatch ≤ PV (never discharge below floor, any policy). Verified live: policy Hold at SOC 8.9% opened session, battery flat, exported PV surplus.
- **`automation.sig_keep_floor_guard`** — sets policy only. Full Export → Load Only at `sig_keep_floor_pct` (38, overnight reserve); Load Only → Off when PV>1.5 kW/10 min.
- **`automation.sig_saving_session_planner`** (id 1784355397820) — joined Octopus session → Full Export; end → Off. Keep floor leaves overnight reserve. "Keep enough to full-export the session AND leave enough for overnight" = export earns bonus down to keep floor; keep floor = the reserve. Pre-session SOC reservation is the plugin's future job.

Helpers: `sig_dispatch_policy`, `sig_keep_floor_pct` (38), `sig_hard_floor_pct` (12), `dno_export_limit_w` (3680).

## DEFERRED — Predbat handover (the mapper), NOT yet done

`automation.predbat_requested_mode_action` maps `input_select.predbat_requested_mode` →
`select.sigen_plant_remote_ems_control_mode` using **Command Charging/Discharging modes** —
which DO NOT WORK for dispatch on this firmware (only PCS Remote Control honours the dispatch
register). Predbat ALSO drives via Remote EMS mode, so the resting/handover state is NOT the
app work mode — it is Remote EMS + Predbat's mode. Currently the mapper is **OFF** (disabled
Jul 16), so no conflict, but Predbat is a passenger. **Rewrite the mapper to drive
`input_select.sig_dispatch_policy`** (Demand → Off/release, Charging → a Charge policy [new],
Discharging → Full Export) so Predbat requests sessions through the single writer. This is
the winter/evening-ownership path and the last structural piece. Needs a **Charge policy**
added to the select (negative dispatch or Command Charging Grid First for cheap-window grid
charging) — the old `charge_below`/P10-recovery role.

## Phase model — SUPERSEDED by the above, kept for reference

Plugin (5-min, forecasting, floors, thresholds — all reusable) decides phase; a slim
executor automation applies it:

- **Off**: safe state. (Hardware enforces 3.68 — no real-time export management needed.
  The 5-second dynamic-export-limit automation RETIRES.)
- **Charge/Absorb**: safe state too (MSC absorbs natively). Distinct phase only for
  bookkeeping/thresholds.
- **Hold** (morning export-priority): a mechanism WILL exist (Andrew, 2026-07-15) — rank
  candidates by test:
    - **V3a (new, likely best): Command Discharging (PV First) + dispatch ≈ 3.68 + load** —
    PCS outputs PV to grid first, battery tops up the shortfall. Gives constant full
    export AND battery drain through the morning ramp in one register state, converging
    to pure PV export as PV reaches the cap. Hold + pre-overflow Drain fused.
    - **V3b: charge cut-off SOC pinned to current SOC** (release = 100). Watch for
    throttle-instead-of-export (old `soc_limits_block_solar` cousin).
    - V3c: app TOU schedule / other Sigen-native features (manual fallback, not
    automation-friendly).
- **Drain** (pre-PV / post-peak / saving sessions): Remote EMS session + PCS Remote
  Control + positive dispatch = min(need, 3.68 + load), heartbeat, close session on exit.
- **Predbat export slots** (night export at 12p): SAME mechanism — the requested-mode
  mapper's "Discharging" translation must become a PCS dispatch session, else Predbat's
  night export silently does nothing post-swap. This is as important as the curtailment
  phases and shares the single writer.

## Validation gates (tomorrow, daylight, ~30 min total) — before ANY code

- **V1**: native MSC morning — battery charges from PV, and PV above charge saturation
  exports up to 3.68 (not clipped). Passive observation.
- **V2**: dispatch amplitude at SOC > 40%: +3 → ~3 kW PCS output (the Jul-15 1 kW cap is
  believed to be the app-manual-dispatch confound; confirm).
- **V3a**: Command Discharging (PV First) + dispatch 3.68+load in morning sun → constant
  export at cap, battery discharging only the PV shortfall (verify battery power ≈
  dispatch − PV, and that it tapers to ~0 as PV reaches the cap).
- **V3b**: charge cut-off SOC = current SOC in MSC with sun → PV exports instead of
  charging; set 100 → absorption resumes. (Old unit's `soc_limits_block_solar` caveat:
  watch that it exports rather than throttles.)
- **V4**: export_limitation persistence: read hourly through the day; identify what (if
  anything) resets it. Heartbeat design depends on the answer.
- **V5**: remote-MSC idle session for 10 min, clean of manual dispatch — reproduce or
  clear the Jul-15 anomaly.

## Implementation order (after gates)

1. REQUIREMENTS.md: R25-v2 + single-writer + heartbeat + safe-state requirements (discuss
   diffs with Andrew first).
2. New executor YAML (`sig_control_executor`): phase in → register session out, heartbeat,
   divergence alarm. Jinja harness tests FIRST (mandatory workflow), incl. heartbeat and
   decay-to-safe cases.
3. Rework `predbat_requested_mode_action` mapper onto the executor (night export = dispatch
   session). Harness tests.
4. Plugin: phase names/thresholds survive; floor logic survives; retire per-phase export
   arithmetic; wire Hold to V3 lever.
5. Retire: `curtailment_manager_dynamic_export_limit` (5-sec loop), voltage seek/throttle
   stack (volt-watt + hardware limit make it redundant — confirm on first sunny day),
   `sig_voltage_protect`.
6. Stale-phase watchdog: retarget to safe state.
7. Staged enable: executor manual-driven first (one full day), then Predbat control, then
   curtailment phases.

## Open items riding along

- Volt-watt knee config at inverter (Ricky/Sigenergy — cable-run offset email already sent)
- Charge curve near full: re-measure DC charge taper (old AC curve void)
- inverter_loss re-measure (DC coupling)
- REQUIREMENTS.md R-numbers to retire/rewrite list (do during step 1)
