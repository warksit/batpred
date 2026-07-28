# Master Plan — July 2026 (curtailment review → inverter swap → structural)

Swap date: **Tue 15 July** — 10 kW SIG → 6 kW hybrid, **whole 10 kWp array moves to SIG
MPPTs, SMA retired same day**, G99, new export limit **3.68 kW**. DC-coupled architecture
eliminates the two-brain CLS cascade and makes export limiting fail-safe by hardware;
curtailment manager becomes a yield optimiser (see inverter-swap-6kw.md).
Companion doc: [inverter-swap-6kw.md](inverter-swap-6kw.md) (detailed swap checklist).
Review findings F1-F9: see session 2026-07-04.

## Phase 0 — TODAY (Jul 4): restore git/live sanity  🔴

The working tree holds ~3 months of uncommitted deployed code + the undeployed
June-15 fix. Nothing else is safe to do until this is clean.

- [x] 0.1 Committed 2026-07-04: 84d9e4b0 (Option A → **R61** in REQUIREMENTS.md),
      f29dd9a1 (SIG mapper revert), af2049ae (kraken backport — synced to upstream/main
      incl. missing test files, octopus prefix fix), 7468bbe4 (skill + cspell)
- [x] 0.2 Full gate green 2026-07-04. NOTE: unit_test needs python3.11 (`ohme.py` imports
      typing.Self); system python3 is 3.9. Venv used: scratchpad/venv311 — deploy.sh (1.2)
      should pin the interpreter.
- [x] 0.3 Deployed 2026-07-04 ~21:50 BST: curtailment_plugin, curtailment_calc, kraken,
      kraken_auth_mixin, components (md5-verified). Addon restarted via supervisor API;
      plugin loaded + state restored cleanly.
- [x] 0.4 Pushed to origin (122a37b0..ef19eed7)

## Phase 1 — This week (Jul 5–8): tooling, comms, money fixes

Ordered so anything with lead time or daily cost comes first.

- [x] 1.1 **Installer comms — SENT 2026-07-08** (email to <solar@mciuk.com>, thread
      "6096827 MIDDLEMUIR", via Zoho): (1) Sigenergy written confirmation 6.0 SP +
      BAT 10.0 approved config, (2) 2-MPPT array combining flagged, (3) Modbus
      enable reminder, (4) volt-watt enabled at commissioning? (feeder sees
      253-258V at full export), (5) schematic to say 9.98 kWp per FIT cert
      (Octopus quote included). AWAITING RICKY'S REPLY — chase by Jul 11 if quiet.
      Answered already by user/schematic: SPCT-DH uses existing Sub1G wireless kit.
- [ ] 1.1b FIT metering: RESOLVED — bidirectional meter; Octopus FIT (Lesley) pre-approved.
      Post-works evidence list in inverter-swap-6kw.md (schematic, battery form, old/new
      meter readings + dates, net-reading photo). Capacity must stay **9.98 kWp** on all
      paperwork. No MCS needed. Ofgem approval is post-works via Octopus.
- [ ] 1.1c Forward Octopus's example schematic + battery form to the installer before
      swap day so they arrive ready to produce both.
- [ ] 1.2 **deploy.sh** in repo: refuses dirty tree, rsyncs the full file set,
      verifies remote md5s, restarts via supervisor API, tails predbat.log,
      tags `deployed/YYYY-MM-DD`. (Fixes the shoddy deploy; prerequisite for
      every later change.)
- [ ] 1.3 **DNO single source of truth**: create `input_number.dno_export_limit_w`
      (=4000 now). Rewire: apps.yaml `export_limit` → entity; YAML automation
      reads helper (update Jinja harness first, TDD); verify plugin path
      (get_arg already indirect-capable). Sweep for remaining hard-coded 4.0/4000.
      SMA Home Manager stays 4.25 until swap day.
- [ ] 1.4 **F4 fix — overflow-aware charge floor** (the 12p-export/29p-import
      economics): charge_below gains a `min(overnight_target, overflow_floor)`
      term so the battery charges toward the overnight reserve on low-overflow
      days but yields the space automatically on big-overflow days. TDD; agree
      exact formula before coding.
- [ ] 1.5 **F3 fix — morning_gap sunrise boundary**: sunrise = PV ≥ forecast load
      (not PV ≥ 0.3 kW), so overnight_target no longer collapses pre-dawn. TDD.
- [x] 1.6 F5 DONE (committed ebfbe686, NOT yet deployed): _log_once fallback audit,
      Solcast dataCorrect + date gates. Stale-phase watchdog automation LIVE in HA.
- [ ] 1.6b **R60 night-sample bias fix** (found 2026-07-08): the effective-DNO sampler
      collects voltage-throttle caps during OVERNIGHT Predbat export (throttle computes
      ~2.3 during brief 257 V peaks but is never enforced on Predbat's export; actual
      export ran 4.0 all night). Rolling 6-sample window filled with worst-moments →
      effective_dno 2.28 → today's overflow forecast inflated 24.7 vs 17.3. Fix: gate
      cap sampling on `self._actual_pv_kw > 0.5` (matches R60's documented daytime-PV
      intent). TDD, small.
- [ ] 1.7 Deploy ebfbe686 + 1.6b together after sundown (never mid-overflow-day);
      then evening /curtailment-review of Jul 8 (biggest day yet: 63 kWh fcst, started
      3.9%, ~25 kWh overflow — expect battery full ~14:00, some throttle loss, zero
      faults = win).

## Phase 2 — Pre-swap week (Jul 9–14): preparation + code freeze

- [ ] 2.1 **Code freeze from Jul 12** — no plugin/automation changes after this
      until post-swap validation done.
- [ ] 2.2 Full HA backup + copy of /addon_configs/6adb4f0d_predbat (incl. curtailment_state.json)
- [ ] 2.3 Snapshot entity registry: dump all sigen_*AND sma_*/Home Manager entity_ids + unique_ids to repo file
- [ ] 2.4 Record current SIG installer settings (hard limit 4.5, protection thresholds)
- [ ] 2.5 Omada: DHCP reservation for the NEW unit's MAC (get MAC from installer if possible; else do on install day)
- [ ] 2.6 **Prepare the config flip set as one commit, deploy on swap day** (see
      inverter-swap-6kw.md): apps.yaml pv_today → sigen_plant_daily_pv_energy (reverses
      #3597), pv_power → SIG PV sensor, inverter_limit/rates → 6000 W, export_limit →
      helper; curtailment_plugin.py SIG_DAILY_PV constant → sigen_plant_daily_pv_energy
      (third-party sensor dies with the SMA — silently breaks R49/R58 otherwise)
- [ ] 2.7 Write down swap-day runbook order (from inverter-swap-6kw.md) where Mum/you can follow it on the day

## Phase 3 — Swap day (Jul 15)

Follow [inverter-swap-6kw.md](inverter-swap-6kw.md). Highlights:

- [ ] 3.1 Before installer starts: disable curtailment enable, voltage automations,
      fault alerts; Predbat read-only
- [ ] 3.2 Installer: head unit swap + **strings to MPPTs + SMA decommission**, hard
      export limit **3.68**, G99 protection, CT pairing
- [ ] 3.3 **Acceptance tests before installer leaves**: Modbus reads (signs!), PV visible
      DC-side, EMS mode write, export limit write verified at meter,
      **export-limit enforcement test** (battery near full + sun → MPPT curtails, no fault),
      load-drop transient test (kettle off while exporting — no CLS-style fault)
- [ ] 3.4 Entity continuity: edit existing TypQxQ config entry (don't re-add);
      diff entity list vs 2.3 snapshot; fix any _2 duplicates; strip dead SMA entities
- [ ] 3.5 Deploy the 2.6 config flip set via deploy.sh
- [ ] 3.6 Set `input_number.dno_export_limit_w` → **3680** (soft lever ~3.5 initially);
      energy dashboard solar source → SIG PV sensor

## Phase 4 — Post-swap (Jul 15–22): staged re-enable + retune

- [ ] 4.1 Day 0-1: Predbat read-only, watch sensors (signs, SOC, PV, DC charging visible) — verify sane
- [ ] 4.2 Re-enable Predbat control; verify one overnight charge window (~6 kW max AC — windows lengthen)
- [ ] 4.3 **Leave voltage_seek + throttle OFF** — MPPT/volt-watt response should make them
      redundant; watch a sunny day before deciding retire vs retune
- [ ] 4.4 Re-enable curtailment; manual Charge→Hold→Drain cycle. Note: Drain has AC headroom
      only pre/post PV peak now (PV saturates the 6 kW bus midday) — pre-PV drain is the lever
- [ ] 4.5 **Re-measure**: DC charge curve near full (old AC-coupled curve void), max DC
      PV→battery rate, inverter_loss (DC coupling more efficient); revisit MAX_RESERVED_KWH
- [ ] 4.6 pv_calibration restarts from empty history (same effect as #3597) — expect a few
      days clamped; don't chase it
- [ ] 4.7 Watch first big overflow day end-to-end — "curtailed" now means MPPT-clipped
      energy, the yield-loss metric

## Phase 5 — Structural (late July onward, calm days)

- [ ] 5.1 **Git-checkout deploy**: make /addon_configs/6adb4f0d_predbat a clone of
      the fork; update = git fetch + reset + restart. Supersedes deploy.sh.
- [ ] 5.2 **HA "Update Predbat" button**: script/shell_command triggering 5.1's
      update script. Verify predbat auto-update (select.predbat_update) is pinned OFF.
- [ ] 5.3 **Rebase curtailment-manager onto latest upstream tag** (fork surface on
      core is now small: predbat.py hooks, plugin_system.py, components.py kraken,
      octopus.py). Full test gate, deploy via 5.1.
- [ ] 5.4 **Restore direct SIG control** (project_sig_architecture_revert decision):
      re-apply HEAD's SIG execute/inverter paths against the NEW inverter, retire
      the mapper automation. Do this only after 4.x fully stable.
- [ ] 5.5 **Curtailment manager v-next for the DC-coupled world**: rewrite R25 (hardware
      now enforces the limit); likely retire the 5-sec YAML automation and the voltage
      throttle stack; plugin becomes a yield optimiser (pre-PV drain + overnight target
      as the main levers). Big simplification — design doc first.
      **Top design theme (from Jul 7-8 night)**: THREE actors drain the battery without
      knowing about each other — manual override, Predbat's export plan, and the plugin.
      They coordinated by luck (compatible tariff incentives); Jul 5 showed the opposite
      (all idle at 2%). v-next should make ONE actor own the SOC trajectory.
      Also: with volt-watt enabled, the inverter continuously discovers the feeder's
      real export ceiling — can replace R60's outside-in effective_dno estimation.
      Economics ground-truth: drain = insurance at ~2p/kWh premium (round-trip on a
      wrong forecast) vs ~31p/kWh payout (19p FIT + 12p export on avoided clipping).

## Unknown-unknowns backlog (review 2026-07-07; #1 watchdog + #4 fallback audit DONE)

- [ ] UU2 **What does the current DNO agreement actually permit?** (User's task.) The
      swap exists because 10 kW couldn't get G99 — implying current basis is G98/3.68,
      while we run 4.0 soft. If confirmed 3.68, drop the export limit NOW (~2% revenue).
- [ ] UU3 **BMS truth + capacity fade**: integrate battery_power over one full drain
      (measured kWh 90→5% vs BMS claim); daily deep cycling parks the battery where SOC
      estimation is worst. Read SigenStor warranty duty-cycle terms.
- [ ] UU6 **P&L the curtailment manager**: one week of daily kWh-cycled × losses × wear
      vs clipping avoided × 31p. Marginal days may be losses dressed as diligence.
- [ ] UU7 **Mum runbook**: one-page laminated "turn it off" card (curtailment_manager_enable
      off + what normal looks like). Bus factor is 1.
- (UU5 CT-loss test and UU8 March mode-table retest are already on the swap checklist.)
- [ ] 5.6 F6 refactor: consolidate floor derivation into one pure derive_targets()
      function; F7 remove_pre_pv_drain_decision side effects; F8 fix stale comment.
- [ ] 5.7 Plugin dir restructure (plugins/curtailment_manager/, plugins/cold_weather/) →
      upstream PR of plugin system → long-term: stock Predbat + drop-in plugins.
- [ ] 5.8 Autumn: re-enable LoadML when GSHP CH season returns (existing note).
