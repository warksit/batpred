# SIG Inverter Swap: 10 kW → 6 kW hybrid + full PV migration (G99) — 2026-07-15

Scope (confirmed 2026-07-04):

- Inverter head unit 10 kW → **SigenStor EC 6.0 SP**; battery packs stay (2× BAT 10.0, 18.08 kWh usable)
- **Whole array moves from SMA to SIG MPPTs** (6 kW unit accepts 2× DC oversizing, OK)
- **SMA (Sunny Boy) retired**, same day
- New DNO export limit: **3.68 kW** (G99), enforced by SIG SPCT-DH smart power sensor at the supply point

Installer schematic (MCI, 2026-04-30): `docs/middlemuir/schematic-6kw-dc-coupled-2026-04-30.pdf`

- Confirms: generation meter on SIG AC output (bidirectional netting works); DC strings →
  inbuilt DC isolator → inverter; export CT at Henley blocks by supplier meter
- Array per drawing: 2 × (7-string + 8-string) Ben-Q 330 W Sunforte = 30 panels = **9.90 kWp**

**Questions for MCI — EMAILED 2026-07-08, Ricky replied 2026-07-10** (thread
"6096827 MIDDLEMUIR" → <solar@mciuk.com>). Status: Ricky has forwarded the technical
questions (volt-watt reference/knee, Modbus, MPPT plan) **to Sigenergy — answers
pending; chase Mon Jul 14 if nothing** (install Wed Jul 15):

- [x] ~~Sigenergy written confirmation: EC 6.0 SP + 2× BAT 10.0~~ — Ricky sent a
      **supplier compatibility sheet listing BAT 10.0 with the 6.0 SP** (Jul 10).
      Third-party distributor doc, not Sigenergy's own letter — acceptable; keep the
      email as the warranty evidence trail. Sigenergy may still confirm directly.
- [ ] Array combining onto the 6.0 SP's **2 MPPTs** (10 kW unit had 4) — with Sigenergy
- [ ] Modbus enable reminder — with Sigenergy/Ricky for install day
- [ ] Volt-watt enabled at commissioning + can it reference SPCT (meter-end) voltage,
      or knee set for the ~6 V run offset? (cable data emailed Jul 10) — with Sigenergy
- [ ] Schematic to state **9.98 kWp** per FIT certificate — unanswered, re-raise on the day
- [x] SPCT-DH comms: ANSWERED — same Sub1G wireless kit as today (still the known
      weak link — 4001_3 faults — but no new install variable)

## What the DC-coupled architecture changes (context for every decision below)

- **Single brain**: SIG controls PV at the MPPT. Overvoltage/export-limit response = ramp PV
  down or divert to DC battery charge, not trip. The SIG-trip → SMA-uncontrolled → CLS-fault
  cascade is structurally eliminated.
- **Fail-safe by hardware**: export limit is enforced natively (charge battery → curtail MPPT).
  R25's premise ("no levers once PV-load > DNO") inverts. Curtailment manager's role becomes
  yield optimisation (make battery room pre-peak so overflow is stored + exported at 12p
  instead of curtailed), not fault prevention.
- **DC charging bypasses the 6 kW AC limit**: PV→battery is DC-side. AC output (load + export)
  caps at 6 kW; PV→battery charging doesn't count against it.
- **Midday drain becomes impossible**: during peak PV the 6 kW AC output is saturated by PV
  (load + 3.68 export ≈ 4.2–5.7 kW from PV alone) — battery discharge has no AC headroom.
  Draining must happen BEFORE PV ramps (R52 pre-PV drain becomes the primary lever).
- **More efficient**: PV→battery skips AC double conversion — re-measure inverter_loss.

## FIT generation metering (19p/kWh) — RESOLVED: bidirectional meter + Octopus email

**Bidirectional FIT meter** (confirmed 2026-07-04): positioned at the inverter AC
terminals, grid-charge energy flows through in reverse and nets off the generation
register. Net reading ≈ true PV generation − battery round-trip losses.

**Octopus FIT (Lesley, ~2026-07-04) has pre-approved the approach.** Key rulings:

- Not a deemed→metered change — FIT export status is negotiated; generation payments
  already FIT-only; export stays metered on the non-FIT 12p tariff. Works disturb neither.
- Ofgem approval is **post-works** (Octopus notifies); rarely rejected if readings
  verifiable and **panel capacity stays 9.98 kWp** — paperwork must state 9.98, not "10".
- **No MCS certificate needed** for inverter swap or re-siting strings (only for
  added panels/batteries).

**Post-works evidence Octopus requires** (collect on swap day):

- [ ] New single-line schematic: panels, inverter, generation meter, battery (Octopus
      sent an example — installer produces this)
- [ ] Battery form (attached to Octopus email) — installer fills, or Lesley can from schematic
- [ ] Old generation meter: removal date + closing reading (**photograph before removal**)
- [ ] New generation meter: install date + opening reading
- [ ] **Photo of new meter showing a net value reading** — if Emlite (preferred model),
      both arrows point right; installer to double-check Emlite installer instructions
- [ ] Submit the lot to Octopus FIT after works → they notify Ofgem

Two small economics notes (not blockers):

- PV routed via battery loses ~10% round-trip **and** that loss comes off the FIT
  generation register too → each stored-then-discharged kWh nets ~19p × loss ≈ 2p extra
  cost vs direct export. Slightly favours export-over-store at the margin.
- Every Predbat grid-charge cycle's round-trip loss also debits the generation register
  (charge 10 in, discharge 9 out → net −1 kWh of generation credit ≈ 19p). Winter
  grid-charge economics are ~2p/kWh worse than the raw rate spread suggests — worth
  reflecting in metric_battery_cycle at some point.

Economics context: clipped kWh loses 19p generation + 12p export ≈ **31p**. Worst-case
full-battery afternoon ≈ 10–15 kWh ≈ **£3–4.50/day** — the post-swap curtailment
manager's job. Store-vs-export is indifferent for the 19p (paid on generation either
way); only clipping forfeits it.

## Before the swap (week of Jul 7)

- [ ] **Commit + deploy all outstanding work** (Option A etc.) so live == git before hardware changes.
- [ ] **Installer/Sigenergy requests (lead time!)**:
    - [ ] Enable local Modbus TCP + "Remote EMS controlled by HA" on the new unit
    - [ ] Hard export limit: set exactly **3.68 kW** (G99 compliance device); confirm which
        export-limitation scheme the G99 application declares
    - [ ] **Volt-watt / P(U) droop: confirm ENABLED + knee settings** (EN 50549 profile).
        This is what makes voltage events graceful (reduce AC output → surplus to battery
        DC → MPPT backs off only when battery full). If left disabled, the only voltage
        response is the hard G99 trip stages — trip-not-throttle, today's failure mode
        minus the cascade. Ask for the configured V thresholds and droop slope.
    - [ ] **Volt-watt knee must allow for the 128 m internal run** (measured 2026-07-10:
        ~0.39 Ω loop, ~1.6 V/kW — inverter terminals read 253 while the SPCT/cutout read
        247 at 3.8 kW export; supply point is comfortably in spec, feeder is NOT the
        problem). A knee at 253-at-inverter would needlessly throttle; terminals
        legitimately run ~6 V above the cutout at full export. Statutory point is the
        cutout; equipment limit is the ~262 V trip.
    - [ ] Does the SPCT-DH expose supply-point **voltage** to the new unit / TypQxQ?
        (Nothing in HA today — only inverter-terminal voltage. Meter-end voltage would
        let us monitor cutout compliance directly.)
    - [ ] String plan: 10 kWp across the new unit's MPPTs — voltage/current windows per string (installer's homework, but ask)
    - [ ] Grid CT: confirm Sub1G wireless kit pairs to the new unit (known first-check on faults)
    - [ ] New unit model/firmware → check TypQxQ integration compatibility (issues page)
    - [ ] What happens to the SMA + Home Manager hardware (decommission plan)
- [ ] **Full HA backup** + copy of `/addon_configs/6adb4f0d_predbat/` (incl. `curtailment_state.json`)
- [ ] **Snapshot entity registry**: all `sigen_*` AND `sma_*`/Home Manager entity IDs + unique_ids to a repo file
- [ ] Record current SIG installer settings (hard limit 4.5, protection thresholds) for deliberate comparison
- [ ] **DHCP reservation** for new unit's MAC (new MAC — update Omada, don't assume same-IP survives)
- [ ] Prepare (in repo, not deployed) the **config flip set** — one commit, deploy on swap day:
    - [ ] apps.yaml: `pv_today` → `sensor.sigen_plant_daily_pv_energy` (reverses #3597 fix — SMA third-party sensor dies)
    - [ ] apps.yaml: `pv_power` → SIG native PV sensor
    - [ ] apps.yaml: `export_limit` → `input_number.dno_export_limit_w` (single source of truth, Phase 1)
    - [ ] apps.yaml: `inverter_limit` / rate maxima → 6000 W
    - [ ] curtailment_plugin.py: `SIG_DAILY_PV` constant → `sensor.sigen_plant_daily_pv_energy`
        (currently third_party_inverter_energy — goes to 0 after migration, silently breaking
        R49 buffer-reduction and R58 calibration)
    - [ ] Verify `SIG_PV_POWER` (`sensor.sigen_plant_pv_power`) still reads total plant PV post-migration

## Install day (Jul 15)

- [ ] Before installer starts: disable `curtailment_manager_enable`, voltage_seek, voltage
      throttle automations, SIG fault alerts; Predbat read-only
- [ ] Installer: swap head unit, move strings to MPPTs, decommission SMA, set hard limit 3.68,
      G99 protection, CT pairing
- [ ] **Acceptance tests before installer leaves**:
    - [ ] Modbus reads: plant SOC / PV power / grid power sane (check sign conventions!)
    - [ ] PV production visible on SIG DC side, per-MPPT if exposed
    - [ ] Write test: EMS mode switch sticks; export limit write verified at meter
    - [ ] `switch.sigen_plant_remote_ems_controlled_by_home_assistant` works
    - [ ] **Export-limit enforcement test**: battery near full + sunny → confirm MPPT curtails
        (export pinned ≤ 3.68, no fault) — this is the whole point of the migration
    - [ ] Load-drop transient: kettle on → off while exporting — no CLS-style fault
- [ ] Entity continuity: edit existing TypQxQ config entry (don't delete/re-add); diff vs
      snapshot; fix `_2` duplicates; remove dead SMA entities from energy dashboard
- [ ] Deploy the prepared config flip set (apps.yaml + plugin constant) via deploy.sh
- [ ] Set `input_number.dno_export_limit_w` → **3680** (software lever ~3.5 initially for margin)
- [ ] Energy dashboard: solar production source → SIG PV sensor

## After swap — staged re-enable (Jul 15–22)

1. Predbat read-only, watch sensors a few hours (PV/SOC/grid signs, DC charging visible)
2. Re-enable Predbat control; verify one charge window (~6 kW max AC — but check whether
   overnight grid-charge is DC-side limited differently)
3. **Leave voltage_seek + throttle OFF** — the volt-watt/MPPT response should make them
   redundant. Watch voltage behaviour on a sunny day before deciding to retire vs retune.
4. Re-enable curtailment manager; manual Charge→Hold→Drain cycle first. Note: Drain only
   has AC headroom pre/post PV peak now.
5. Re-measure: DC charge curve near full (old AC-coupled curve — 8.8 kW rated, 7 @95%,
   2.8 @97% — is void), inverter_loss (DC coupling more efficient), max DC PV→battery rate.
6. pv_calibration restarts from empty history (same effect as #3597) — expect a few days
   of clamped calibration; don't chase it.
7. Watch first big overflow day: `sensor.curtailment_overflow_energy` semantics change —
   "curtailed" now literally means MPPT-clipped energy, which is the yield-loss metric.

## Follow-on (Phase 5 of master plan)

- **Curtailment manager v-next for DC-coupled world**: rewrite R25; likely retire the 5-sec
  YAML automation (hardware enforces the limit); plugin becomes a yield optimiser whose main
  lever is pre-PV drain + overnight target. Big simplification.
- Retire/remove: SMA Home Manager config, voltage throttle stack (if step 3 confirms),
  mapper-automation SMA references.

## Peak-load analysis — 10 kW → 6 kW discharge cap (done 2026-07-08)

Question: how much extra import would the 6 kW unit have caused on the biggest-use day
(Christmas Day 2025, 44.9 kWh imported — confirmed biggest of Dec by Octopus HH backfill)?

**Answer: zero.** Confirmed by Sigen app Power Metrics Chart for 2025-12-25 (user
screenshot) + Octopus HH backfill (`octopus_energy:electricity_..._previous_accumulative_consumption`):

- Lunch peak 12:00–13:30: LOAD maxed ~7–7.5 kW in brief oven-cycling bursts (mostly
  4–6 kW). The 11.5 kW grid draw (10.3 + 8.6 kWh in the 11:00/12:00 UTC meter hours) was
  load + **battery CHARGING at 4–6 kW** — Sigen AI recharged through the peak at standard
  rate. Battery was NOT flat (evening import ≈ 0 proves it had charge at 16:00).
- **Battery discharge never exceeded ~4.5 kW all day** — the heaviest recorded day never
  used even 6 kW of the 10 kW unit's discharge. The brief load>6 moments coincided with
  battery charging, so grid served them identically under either inverter.
- Load >6 kW never breaks anything: **house load is grid passthrough**, not limited by the
  inverter. The 6 kW cap only decides which kWh come from battery vs grid.
- Summer evidence (5-min stats, Jul 6): daily 7–10 kW load peaks are seconds-long
  (kettle-class); no 5-min slot mean ever exceeded 6 kW.
- Cosy tariff: the 13:00–16:00 cheap window covers most Christmas-lunch load anyway;
  Predbat (unlike Sigen AI in 2025) will also enter the morning full.
- Correction to an earlier claim: AC charging does NOT share the 6 kW with house load
  (load is grid-side). The 6 kW port limit applies to charge rate and to
  discharge-serving-(load+export) combined — e.g. can't export 3.68 while covering
  3 kW load from battery simultaneously.

## Open questions

- ~~Max DC PV→battery charge rate?~~ **Battery can absorb the full 10 kWp DC-side**
  (confirmed 2026-07-04). So MPPT clipping ONLY happens when the battery is full —
  the yield problem reduces entirely to "have room during the peak". Clip rate when
  full ≈ PV − export − load ≈ 9 − 3.68 − 0.5 ≈ **~5 kW** → each full-battery hour at
  peak costs ~5 kWh × **31p (19p FIT generation + 12p export) ≈ £1.55/hr**. That's the
  economic case for the post-swap curtailment manager, served by pre-PV drain +
  overnight target.
- **FIT metering under DC coupling — see BLOCKER section at top.**
- Does TypQxQ expose per-MPPT/DC PV sensors on this model?
- Installer's answer on G99 export-limitation scheme + final hard-limit value.
- Verify on swap day: DC charge at ~10 kW actually observed (readback), and whether the
  near-full taper throttles DC charging early (the new charge-curve measurement, 4.5).
