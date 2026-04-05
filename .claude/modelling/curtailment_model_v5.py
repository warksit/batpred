#!/usr/bin/env python3
"""
Curtailment plugin model v5 — iterative target SOC.

Every slot:
  1. remaining_overflow = sum of max(0, forecast_excess - DNO) for future slots
  2. target_soc = battery_max - remaining_overflow (clamped to 0..100%)
  3. If SOC > target: export from battery (+ PV) to bring SOC toward target
  4. If SOC < target: charge from PV to bring SOC toward target
  5. Always: if PV excess > DNO, absorb overflow into battery

Target SOC naturally rises as overflow is absorbed through the day.
No phases, no floor, no deactivation time — just one rule, recalculated each slot.

Battery: 18.08 kWh, max charge/discharge 5.5kW
DNO limit: 4kW. Financials: 19p gen, 12p export, 24.5p import.
"""

import re

def parse_value(val):
    val = val.strip().strip('"')
    val = val.replace(',', '')
    try:
        return float(val)
    except:
        return 0.0

def parse_time(time_str):
    match = re.search(r'(\d{2}):(\d{2})', time_str)
    if match:
        h, m = int(match.group(1)), int(match.group(2))
        return h + m / 60.0
    return None

def read_csv(filepath, watts=False):
    rows = []
    with open(filepath, 'r') as f:
        lines = f.readlines()
    for line in lines[1:]:
        parts = line.strip().split(';')
        if len(parts) < 7:
            continue
        time_h = parse_time(parts[0])
        if time_h is None:
            continue
        load = parse_value(parts[3])
        pv = parse_value(parts[6])
        if watts:
            load /= 1000.0
            pv /= 1000.0
        rows.append((time_h, pv, load))
    return rows

def aggregate_30min(rows_15min):
    slots = []
    i = 0
    while i + 1 < len(rows_15min):
        t1, pv1, load1 = rows_15min[i]
        t2, pv2, load2 = rows_15min[i + 1]
        avg_pv = (pv1 + pv2) / 2
        avg_load = (load1 + load2) / 2
        slots.append((t2, avg_pv, avg_load))
        i += 2
    return slots

def fmt(h):
    hours = int(h)
    mins = int(round((h - hours) * 60))
    return f"{hours:02d}:{mins:02d}"

def compute_remaining_overflow(slots, dno_limit, slot_hours=0.5):
    """For each slot index, total overflow kWh from that slot to end of day."""
    n = len(slots)
    remaining = [0.0] * (n + 1)
    for i in range(n - 1, -1, -1):
        t, pv, load = slots[i]
        excess = pv - load
        overflow = max(0, excess - dno_limit) * slot_hours
        remaining[i] = remaining[i + 1] + overflow
    return remaining

def simulate_iterative(slots, start_soc_pct=0.40, battery_kwh=18.08,
                       dno_limit=4.0, max_charge_rate=5.5, max_discharge_rate=5.5,
                       slot_hours=0.5):
    """
    Iterative target SOC approach.

    Each slot:
    - target_soc = battery_kwh - remaining_overflow (clamped to [0, battery_kwh])
    - If excess >= dno_limit: export dno_limit, absorb overflow into battery
    - If 0 < excess < dno_limit:
        - If SOC > target: export excess + discharge battery toward target (capped at dno_limit)
        - If SOC < target: charge from PV toward target, export remainder
    - If excess < 0: battery covers load (self-consumption, always allowed)
    """
    battery_max = battery_kwh  # target 100%
    soc = battery_kwh * start_soc_pct
    n = len(slots)

    remaining_overflow = compute_remaining_overflow(slots, dno_limit, slot_hours)
    total_overflow = remaining_overflow[0]

    results = []
    totals = {
        'export_kwh': 0, 'curtailed_kwh': 0, 'import_kwh': 0,
        'pv_generated_kwh': 0, 'battery_full_time': None,
        'min_soc': soc, 'min_soc_time': 0, 'end_soc': 0,
    }

    for idx, (time_h, pv, load) in enumerate(slots):
        excess = pv - load
        export = 0.0
        curtailed = 0.0
        charge = 0.0
        discharge = 0.0
        grid_import = 0.0

        # Target SOC: leave room for remaining overflow
        future_overflow = remaining_overflow[idx + 1] if idx + 1 <= n else 0
        target_soc = max(0, min(battery_max, battery_max - future_overflow))

        remaining_cap = max(0, battery_max - soc)
        max_charge_slot = min(max_charge_rate, remaining_cap / slot_hours) if remaining_cap > 0.01 else 0
        available_soc = max(0, soc)
        max_discharge_slot = min(max_discharge_rate, available_soc / slot_hours) if available_soc > 0.01 else 0

        # How far are we from target?
        soc_above_target = max(0, soc - target_soc)
        soc_below_target = max(0, target_soc - soc)

        if excess >= dno_limit:
            # HIGH PV: export DNO limit, absorb overflow into battery
            export = dno_limit
            overflow = excess - dno_limit
            charge = min(overflow, max_charge_slot)
            curtailed = max(0, overflow - charge)

        elif excess > 0:
            # MODERATE PV: excess > 0 but < DNO limit
            if soc > target_soc + 0.1:
                # SOC above target — export PV + discharge battery toward target
                # How much to discharge this slot to move toward target?
                drain_wanted = min(soc_above_target / slot_hours, max_discharge_rate)
                # Total export = PV excess + battery discharge, capped at DNO
                discharge = min(drain_wanted, max_discharge_slot)
                total_export = excess + discharge
                export = min(total_export, dno_limit)
                # If we limited by DNO, reduce discharge accordingly
                if excess + discharge > dno_limit:
                    discharge = max(0, dno_limit - excess)
                    export = dno_limit
                    # Remaining excess charges battery
                    leftover = excess - (export - discharge)
                    if leftover > 0:
                        charge = min(leftover, max_charge_slot)
            elif soc < target_soc - 0.1:
                # SOC below target — charge from PV toward target
                charge_wanted = min(soc_below_target / slot_hours, max_charge_rate)
                charge = min(charge_wanted, excess, max_charge_slot)
                after_charge = excess - charge
                export = min(after_charge, dno_limit)
            else:
                # SOC ≈ target — export PV excess
                export = min(excess, dno_limit)

        else:
            # DEFICIT: PV < load — battery covers deficit (self-consumption, always)
            load_deficit = -excess
            discharge = min(load_deficit, max_discharge_slot)
            grid_import = max(0, load_deficit - discharge)

        # Update SOC
        soc += charge * slot_hours - discharge * slot_hours
        soc = max(0, min(battery_max, soc))

        # Track min SOC
        if soc < totals['min_soc']:
            totals['min_soc'] = soc
            totals['min_soc_time'] = time_h

        # Accumulate totals
        totals['export_kwh'] += export * slot_hours
        totals['curtailed_kwh'] += curtailed * slot_hours
        totals['import_kwh'] += grid_import * slot_hours
        totals['pv_generated_kwh'] += (pv - curtailed) * slot_hours

        if totals['battery_full_time'] is None and soc >= battery_max - 0.05:
            totals['battery_full_time'] = time_h
        totals['end_soc'] = soc

        results.append({
            'time': time_h, 'pv': pv, 'load': load, 'excess': excess,
            'soc': soc, 'soc_pct': soc / battery_kwh * 100,
            'target_soc': target_soc, 'target_pct': target_soc / battery_kwh * 100,
            'export': export, 'curtailed': curtailed,
            'charge': charge, 'discharge': discharge,
            'grid_import': grid_import,
        })

    totals['potential_pv_kwh'] = sum(pv * slot_hours for _, pv, _ in slots)
    totals['self_consumption_kwh'] = totals['pv_generated_kwh'] - totals['export_kwh']
    return results, totals, total_overflow

def simulate_msc(slots, start_soc_pct=0.40, battery_kwh=18.08, max_soc_pct=0.90,
                 dno_limit=4.0, max_charge_rate=5.5, max_discharge_rate=5.5):
    """MSC baseline — 90% max."""
    max_soc = battery_kwh * max_soc_pct
    soc = battery_kwh * start_soc_pct
    slot_hours = 0.5

    results = []
    totals = {
        'export_kwh': 0, 'curtailed_kwh': 0, 'import_kwh': 0,
        'pv_generated_kwh': 0, 'battery_full_time': None,
        'min_soc': soc, 'min_soc_time': 0, 'end_soc': 0,
    }

    for time_h, pv, load in slots:
        excess = pv - load
        export = charge = discharge = grid_import = curtailed = 0.0

        remaining_cap = max(0, max_soc - soc)
        max_charge_slot = min(max_charge_rate, remaining_cap / slot_hours) if remaining_cap > 0.01 else 0

        if excess > 0:
            charge = min(excess, max_charge_slot)
            after_charge = excess - charge
            export = min(after_charge, dno_limit)
            curtailed = max(0, after_charge - dno_limit)
        else:
            avail = max(0, soc)
            msc_max = min(max_discharge_rate, avail / slot_hours) if avail > 0.01 else 0
            discharge = min(-excess, msc_max)
            grid_import = max(0, -excess - discharge)

        soc += charge * slot_hours - discharge * slot_hours
        soc = max(0, min(max_soc, soc))

        if soc < totals['min_soc']:
            totals['min_soc'] = soc
            totals['min_soc_time'] = time_h

        totals['export_kwh'] += export * slot_hours
        totals['curtailed_kwh'] += curtailed * slot_hours
        totals['import_kwh'] += grid_import * slot_hours
        totals['pv_generated_kwh'] += (pv - curtailed) * slot_hours

        if totals['battery_full_time'] is None and soc >= max_soc - 0.05:
            totals['battery_full_time'] = time_h
        totals['end_soc'] = soc

        results.append({
            'time': time_h, 'pv': pv, 'load': load, 'excess': excess,
            'soc': soc, 'soc_pct': soc / battery_kwh * 100,
            'target_soc': max_soc, 'target_pct': max_soc / battery_kwh * 100,
            'export': export, 'curtailed': curtailed,
            'charge': charge, 'discharge': discharge,
            'grid_import': grid_import,
        })

    totals['potential_pv_kwh'] = sum(pv * 0.5 for _, pv, _ in slots)
    totals['self_consumption_kwh'] = totals['pv_generated_kwh'] - totals['export_kwh']
    return results, totals

def print_table(results, title, show_range=(4.5, 21.5)):
    print(f"\n{'=' * 120}")
    print(f"  {title}")
    print(f"{'=' * 120}")
    print(f"{'Time':>5} | {'PV':>5} {'Load':>5} {'Excs':>5} | {'Chrg':>5} {'Disc':>5} {'SOC%':>5} {'Tgt%':>5} {'SOC':>6} | {'Exprt':>5} {'Crtld':>5}")
    print('-' * 120)
    for r in results:
        if show_range[0] <= r['time'] <= show_range[1]:
            crt = f"{r['curtailed']:>5.1f}" if r['curtailed'] > 0.01 else '    -'
            disc = f"{r['discharge']:>5.1f}" if r['discharge'] > 0.01 else '    -'
            chrg = f"{r['charge']:>5.1f}" if r['charge'] > 0.01 else '    -'
            print(f"{fmt(r['time']):>5} | {r['pv']:>5.2f} {r['load']:>5.2f} {r['excess']:>5.1f} | "
                  f"{chrg} {disc} {r['soc_pct']:>5.1f} {r['target_pct']:>5.0f} {r['soc']:>6.2f} | "
                  f"{r['export']:>5.2f} {crt}")

def print_summary(totals, name, battery_kwh=18.08):
    gen_rate, export_rate, import_rate = 0.19, 0.12, 0.245
    revenue_gen = totals['pv_generated_kwh'] * gen_rate
    revenue_exp = totals['export_kwh'] * export_rate
    cost_imp = totals['import_kwh'] * import_rate
    net = revenue_gen + revenue_exp - cost_imp

    print(f"\n  --- {name} ---")
    print(f"  PV generated:     {totals['pv_generated_kwh']:>6.1f} kWh  (curtailed: {totals['curtailed_kwh']:.1f} kWh)")
    print(f"  Export:           {totals['export_kwh']:>6.1f} kWh")
    print(f"  Import:           {totals['import_kwh']:>6.1f} kWh")
    print(f"  Self-consumption: {totals['self_consumption_kwh']:>6.1f} kWh")
    full = fmt(totals['battery_full_time']) if totals['battery_full_time'] else 'NEVER'
    print(f"  Battery full at:  {full}")
    print(f"  Min SOC:          {totals['min_soc']/battery_kwh*100:.0f}% ({totals['min_soc']:.1f} kWh) at {fmt(totals['min_soc_time'])}")
    print(f"  End SOC:          {totals['end_soc']/battery_kwh*100:.0f}% ({totals['end_soc']:.1f} kWh)")
    print(f"  Revenue:  gen £{revenue_gen:.2f} + export £{revenue_exp:.2f} - import £{cost_imp:.2f} = £{net:.2f}")

# ============= MAIN =============
days = [
    ("June 19 — 65 kWh peak", "/Users/andrew/code/mum-energy/data/sma/daily_15min/Energy_Balance_2025_06_19.csv", True),
    ("July 12 — 68 kWh peak", "/Users/andrew/code/mum-energy/data/sma/daily_15min/Energy_Balance_2025_07_12.csv", False),
    ("July 9  — 45 kWh cloudy", "/Users/andrew/code/mum-energy/data/sma/daily_15min/Energy_Balance_2025_07_09.csv", False),
    ("July 15 — poor day", "/Users/andrew/code/mum-energy/data/sma/daily_15min/Energy_Balance_2025_07_15.csv", False),
    ("July 1  — 40 kWh day", "/Users/andrew/code/mum-energy/data/sma/daily_15min/Energy_Balance_2025_07_01.csv", False),
    ("May 30  — 47 kWh day", "/Users/andrew/code/mum-energy/data/sma/daily_15min/Energy_Balance_2025_05_30.csv", False),
]

for day_name, filepath, watts in days:
    print(f"\n{'#' * 120}")
    print(f"#  {day_name}")
    print(f"{'#' * 120}")

    rows = read_csv(filepath, watts=watts)
    slots = aggregate_30min(rows)

    total_pv = sum(pv * 0.5 for _, pv, _ in slots)
    total_load = sum(load * 0.5 for _, _, load in slots)

    all_results = []

    # A) MSC baseline
    results_a, totals_a = simulate_msc(slots)

    # B) Iterative target SOC — check if any overflow exists
    remaining_overflow = compute_remaining_overflow(slots, 4.0)
    total_overflow = remaining_overflow[0]

    initial_target = max(0, 18.08 - total_overflow)
    print(f"  PV: {total_pv:.0f} kWh | Load: {total_load:.0f} kWh | Excess: {total_pv - total_load:.0f} kWh")
    print(f"  Expected overflow: {total_overflow:.1f} kWh | Initial target SOC: {initial_target:.1f} kWh ({initial_target/18.08*100:.0f}%)")

    print_table(results_a, f"{day_name}  —  A) MSC baseline (90% max)")
    print_summary(totals_a, 'A) MSC baseline')
    all_results.append(('A) MSC baseline', totals_a))

    if total_overflow > 0:
        results_b, totals_b, _ = simulate_iterative(slots)
        print_table(results_b, f"{day_name}  —  B) Iterative target SOC (100% target)")
        print_summary(totals_b, 'B) Iterative target SOC')
        all_results.append(('B) Iterative', totals_b))
    else:
        print(f"\n  Plugin OFF — no overflow forecast")
        all_results.append(('B) Plugin OFF', totals_a))

    # Comparison
    print(f"\n  {'COMPARISON':=^95}")
    print(f"  {'Scenario':<35} {'Crtld':>6} {'Export':>7} {'Import':>7} {'Full@':>6} {'EndSOC':>7} {'Net £':>7}")
    print(f"  {'-'*89}")
    for label, t in all_results:
        full = fmt(t['battery_full_time']) if t['battery_full_time'] else 'never'
        net = t['pv_generated_kwh']*0.19 + t['export_kwh']*0.12 - t['import_kwh']*0.245
        end_pct = f"{t['end_soc']/18.08*100:.0f}%"
        print(f"  {label:<35} {t['curtailed_kwh']:>5.1f}  {t['export_kwh']:>6.1f}  {t['import_kwh']:>6.1f}  {full:>5}  {end_pct:>6}  £{net:>5.2f}")

    if total_overflow > 0:
        t = all_results[1][1]
        base_net = totals_a['pv_generated_kwh']*0.19 + totals_a['export_kwh']*0.12 - totals_a['import_kwh']*0.245
        t_net = t['pv_generated_kwh']*0.19 + t['export_kwh']*0.12 - t['import_kwh']*0.245
        saved = totals_a['curtailed_kwh'] - t['curtailed_kwh']
        print(f"\n  Iterative: saves {saved:.1f} kWh curtailment, +£{t_net - base_net:.2f}/day vs MSC")
    print()
