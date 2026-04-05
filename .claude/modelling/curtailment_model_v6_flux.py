#!/usr/bin/env python3
"""
Curtailment plugin model v6 — iterative target SOC + Octopus Flux.

Compares two tariff setups:
  A) Current: FIT gen 19p + Octopus Outgoing export 12p flat + ~24.5p import flat
  B) Flux:    FIT gen 19p + Flux TOU export + Flux TOU import

Flux rates (actual):
  Import: 02-05 16.60p | 05-16 27.67p | 16-19 38.73p | 19-02 27.67p
  Export: 02-05  4.34p | 05-16  9.76p | 16-19 27.70p | 19-02  9.76p

For each tariff, models V5 curtailment + optional Flux forced battery export 4-7pm.
Battery: 18.08 kWh, max charge/discharge 5.5kW, DNO limit 4kW.
"""

import re

BATTERY_KWH = 18.08
DNO_LIMIT = 4.0
MAX_CHARGE_RATE = 5.5
MAX_DISCHARGE_RATE = 5.5
SLOT_HOURS = 0.5

# FIT generation rate (separate contract, always applies)
FIT_GEN = 0.19

# Current tariff: Octopus Outgoing (flat)
CURRENT_EXPORT = 0.12
CURRENT_IMPORT = 0.245

# Flux tariff: time-of-use
FLUX_IMPORT = {
    'cheap':    0.1660,  # 02:00-05:00
    'day':      0.2767,  # 05:00-16:00
    'peak':     0.3873,  # 16:00-19:00
    'evening':  0.2767,  # 19:00-02:00
}
FLUX_EXPORT = {
    'cheap':    0.0434,  # 02:00-05:00
    'day':      0.0976,  # 05:00-16:00
    'peak':     0.2770,  # 16:00-19:00
    'evening':  0.0976,  # 19:00-02:00
}


def parse_value(val):
    val = val.strip().strip('"').replace(',', '')
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
    if h is None:
        return 'never'
    hours = int(h)
    mins = int(round((h - hours) * 60))
    return f"{hours:02d}:{mins:02d}"


def is_flux_window(time_h):
    """4pm-7pm (16.0 <= time < 19.0)"""
    return 16.0 <= time_h < 19.0


def flux_period(time_h):
    """Return the Flux TOU period for a given time."""
    if 2.0 <= time_h < 5.0:
        return 'cheap'
    elif 5.0 <= time_h < 16.0:
        return 'day'
    elif 16.0 <= time_h < 19.0:
        return 'peak'
    else:
        return 'evening'


def flux_import_rate(time_h):
    return FLUX_IMPORT[flux_period(time_h)]


def flux_export_rate(time_h):
    return FLUX_EXPORT[flux_period(time_h)]


def compute_remaining_overflow(slots, dno_limit, slot_hours=0.5):
    n = len(slots)
    remaining = [0.0] * (n + 1)
    for i in range(n - 1, -1, -1):
        t, pv, load = slots[i]
        excess = pv - load
        overflow = max(0, excess - dno_limit) * slot_hours
        remaining[i] = remaining[i + 1] + overflow
    return remaining


def simulate_v5(slots, start_soc_pct=0.40):
    """V5 iterative target SOC — original curtailment-only model."""
    soc = BATTERY_KWH * start_soc_pct
    remaining_overflow = compute_remaining_overflow(slots, DNO_LIMIT, SLOT_HOURS)
    n = len(slots)

    results = []
    totals = {
        'export_kwh': 0, 'curtailed_kwh': 0, 'import_kwh': 0,
        'pv_generated_kwh': 0, 'battery_full_time': None,
        'min_soc': soc, 'min_soc_time': 0, 'end_soc': 0,
        'flux_export_kwh': 0, 'fit_export_kwh': 0,
        'import_cost': 0, 'export_revenue': 0,
    }

    for idx, (time_h, pv, load) in enumerate(slots):
        excess = pv - load
        exp = charge = discharge = grid_import = curtailed = 0.0

        future_overflow = remaining_overflow[idx + 1] if idx + 1 <= n else 0
        target_soc = max(0, min(BATTERY_KWH, BATTERY_KWH - future_overflow))

        remaining_cap = max(0, BATTERY_KWH - soc)
        max_charge_slot = min(MAX_CHARGE_RATE, remaining_cap / SLOT_HOURS) if remaining_cap > 0.01 else 0
        available_soc = max(0, soc)
        max_discharge_slot = min(MAX_DISCHARGE_RATE, available_soc / SLOT_HOURS) if available_soc > 0.01 else 0

        soc_above_target = max(0, soc - target_soc)
        soc_below_target = max(0, target_soc - soc)

        if excess >= DNO_LIMIT:
            exp = DNO_LIMIT
            overflow = excess - DNO_LIMIT
            charge = min(overflow, max_charge_slot)
            curtailed = max(0, overflow - charge)
        elif excess > 0:
            if soc > target_soc + 0.1:
                drain_wanted = min(soc_above_target / SLOT_HOURS, MAX_DISCHARGE_RATE)
                discharge = min(drain_wanted, max_discharge_slot)
                total_export = excess + discharge
                exp = min(total_export, DNO_LIMIT)
                if excess + discharge > DNO_LIMIT:
                    discharge = max(0, DNO_LIMIT - excess)
                    exp = DNO_LIMIT
                    leftover = excess - (exp - discharge)
                    if leftover > 0:
                        charge = min(leftover, max_charge_slot)
            elif soc < target_soc - 0.1:
                charge_wanted = min(soc_below_target / SLOT_HOURS, MAX_CHARGE_RATE)
                charge = min(charge_wanted, excess, max_charge_slot)
                after_charge = excess - charge
                exp = min(after_charge, DNO_LIMIT)
            else:
                exp = min(excess, DNO_LIMIT)
        else:
            load_deficit = -excess
            discharge = min(load_deficit, max_discharge_slot)
            grid_import = max(0, load_deficit - discharge)

        soc += charge * SLOT_HOURS - discharge * SLOT_HOURS
        soc = max(0, min(BATTERY_KWH, soc))

        if soc < totals['min_soc']:
            totals['min_soc'] = soc
            totals['min_soc_time'] = time_h

        exp_kwh = exp * SLOT_HOURS
        imp_kwh = grid_import * SLOT_HOURS
        totals['export_kwh'] += exp_kwh
        totals['curtailed_kwh'] += curtailed * SLOT_HOURS
        totals['import_kwh'] += imp_kwh
        totals['pv_generated_kwh'] += (pv - curtailed) * SLOT_HOURS

        # Financial tracking with TOU rates
        e_rate = export_rate(time_h)
        i_rate = import_rate(time_h)
        totals['export_revenue'] += exp_kwh * e_rate
        totals['import_cost'] += imp_kwh * i_rate
        if is_flux_window(time_h):
            totals['flux_export_kwh'] += exp_kwh
        else:
            totals['fit_export_kwh'] += exp_kwh

        if totals['battery_full_time'] is None and soc >= BATTERY_KWH - 0.05:
            totals['battery_full_time'] = time_h
        totals['end_soc'] = soc

        results.append({
            'time': time_h, 'pv': pv, 'load': load, 'excess': excess,
            'soc': soc, 'soc_pct': soc / BATTERY_KWH * 100,
            'target_soc': target_soc, 'target_pct': target_soc / BATTERY_KWH * 100,
            'export': exp, 'curtailed': curtailed,
            'charge': charge, 'discharge': discharge,
            'grid_import': grid_import, 'flux': is_flux_window(time_h),
        })

    totals['potential_pv_kwh'] = sum(pv * SLOT_HOURS for _, pv, _ in slots)
    totals['self_consumption_kwh'] = totals['pv_generated_kwh'] - totals['export_kwh']
    return results, totals


def simulate_v6_flux(slots, start_soc_pct=0.40, min_overnight_soc_pct=0.10):
    """
    V6: iterative target SOC + Flux forced export 4-7pm.

    During Flux window (4-7pm):
    - Force battery discharge to maximise export up to DNO limit
    - PV excess still exports as normal
    - Battery tops up export to DNO limit if PV excess < DNO
    - Curtailment protection still active (absorb overflow if PV > DNO + battery headroom)

    The curtailment algorithm adjusts: during Flux window, we WANT low SOC
    (we're being paid 29p to export), so target_soc accounts for both
    remaining overflow AND Flux export opportunity.

    After Flux window: battery may be low. 2-5am cheap import at 17p refills.
    """
    soc = BATTERY_KWH * start_soc_pct
    remaining_overflow = compute_remaining_overflow(slots, DNO_LIMIT, SLOT_HOURS)
    n = len(slots)

    # Calculate how much Flux export we can do (kWh available in 4-7pm window)
    # This informs pre-Flux target SOC planning
    flux_slots = sum(1 for t, _, _ in slots if is_flux_window(t))
    max_flux_export_kwh = flux_slots * SLOT_HOURS * DNO_LIMIT  # theoretical max

    results = []
    totals = {
        'export_kwh': 0, 'curtailed_kwh': 0, 'import_kwh': 0,
        'pv_generated_kwh': 0, 'battery_full_time': None,
        'min_soc': soc, 'min_soc_time': 0, 'end_soc': 0,
        'flux_export_kwh': 0, 'fit_export_kwh': 0,
        'import_cost': 0, 'export_revenue': 0,
        'overnight_import_kwh': 0,
    }
    min_soc_kwh = BATTERY_KWH * min_overnight_soc_pct  # Don't drain below this

    for idx, (time_h, pv, load) in enumerate(slots):
        excess = pv - load
        exp = charge = discharge = grid_import = curtailed = 0.0

        future_overflow = remaining_overflow[idx + 1] if idx + 1 <= n else 0
        curtailment_target = max(0, min(BATTERY_KWH, BATTERY_KWH - future_overflow))

        remaining_cap = max(0, BATTERY_KWH - soc)
        max_charge_slot = min(MAX_CHARGE_RATE, remaining_cap / SLOT_HOURS) if remaining_cap > 0.01 else 0
        available_soc = max(0, soc - min_soc_kwh)
        max_discharge_slot = min(MAX_DISCHARGE_RATE, available_soc / SLOT_HOURS) if available_soc > 0.01 else 0

        if is_flux_window(time_h):
            # FLUX WINDOW: maximise export at 29p/kWh
            # Priority: export up to DNO limit from PV + battery

            if excess >= DNO_LIMIT:
                # PV alone exceeds DNO — same as curtailment logic
                exp = DNO_LIMIT
                overflow = excess - DNO_LIMIT
                charge = min(overflow, max_charge_slot)
                curtailed = max(0, overflow - charge)
            elif excess > 0:
                # PV excess < DNO: export PV + discharge battery to fill up to DNO
                battery_export_wanted = DNO_LIMIT - excess
                discharge = min(battery_export_wanted, max_discharge_slot)
                exp = excess + discharge
                # Don't charge during Flux — we want to export, not store
            elif excess >= -DNO_LIMIT:
                # PV < load but not by much: cover load from battery, export remainder to DNO
                load_deficit = -excess
                # First cover load from battery
                discharge_for_load = min(load_deficit, max_discharge_slot)
                remaining_discharge = max(0, max_discharge_slot - discharge_for_load)
                # Then export from battery
                discharge_for_export = min(DNO_LIMIT, remaining_discharge)
                discharge = discharge_for_load + discharge_for_export
                exp = discharge_for_export
                grid_import = max(0, load_deficit - discharge_for_load)
            else:
                # Large deficit — cover what we can
                load_deficit = -excess
                discharge = min(load_deficit, max_discharge_slot)
                grid_import = max(0, load_deficit - discharge)

            # Use curtailment target as floor during Flux to protect against
            # post-Flux curtailment (if PV is still high after 7pm)
            # But generally Flux window is late enough that curtailment risk is low

        else:
            # NON-FLUX: standard v5 curtailment logic
            target_soc = curtailment_target
            soc_above_target = max(0, soc - target_soc)
            soc_below_target = max(0, target_soc - soc)

            if excess >= DNO_LIMIT:
                exp = DNO_LIMIT
                overflow = excess - DNO_LIMIT
                charge = min(overflow, max_charge_slot)
                curtailed = max(0, overflow - charge)
            elif excess > 0:
                if soc > target_soc + 0.1:
                    drain_wanted = min(soc_above_target / SLOT_HOURS, MAX_DISCHARGE_RATE)
                    discharge = min(drain_wanted, max_discharge_slot)
                    total_export = excess + discharge
                    exp = min(total_export, DNO_LIMIT)
                    if excess + discharge > DNO_LIMIT:
                        discharge = max(0, DNO_LIMIT - excess)
                        exp = DNO_LIMIT
                        leftover = excess - (exp - discharge)
                        if leftover > 0:
                            charge = min(leftover, max_charge_slot)
                elif soc < target_soc - 0.1:
                    charge_wanted = min(soc_below_target / SLOT_HOURS, MAX_CHARGE_RATE)
                    charge = min(charge_wanted, excess, max_charge_slot)
                    after_charge = excess - charge
                    exp = min(after_charge, DNO_LIMIT)
                else:
                    exp = min(excess, DNO_LIMIT)
            else:
                load_deficit = -excess
                discharge = min(load_deficit, max_discharge_slot)
                grid_import = max(0, load_deficit - discharge)

        soc += charge * SLOT_HOURS - discharge * SLOT_HOURS
        soc = max(0, min(BATTERY_KWH, soc))

        if soc < totals['min_soc']:
            totals['min_soc'] = soc
            totals['min_soc_time'] = time_h

        exp_kwh = exp * SLOT_HOURS
        imp_kwh = grid_import * SLOT_HOURS
        totals['export_kwh'] += exp_kwh
        totals['curtailed_kwh'] += curtailed * SLOT_HOURS
        totals['import_kwh'] += imp_kwh
        totals['pv_generated_kwh'] += (pv - curtailed) * SLOT_HOURS

        e_rate = export_rate(time_h)
        i_rate = import_rate(time_h)
        totals['export_revenue'] += exp_kwh * e_rate
        totals['import_cost'] += imp_kwh * i_rate
        if is_flux_window(time_h):
            totals['flux_export_kwh'] += exp_kwh
        else:
            totals['fit_export_kwh'] += exp_kwh

        if totals['battery_full_time'] is None and soc >= BATTERY_KWH - 0.05:
            totals['battery_full_time'] = time_h
        totals['end_soc'] = soc

        results.append({
            'time': time_h, 'pv': pv, 'load': load, 'excess': excess,
            'soc': soc, 'soc_pct': soc / BATTERY_KWH * 100,
            'target_soc': curtailment_target, 'target_pct': curtailment_target / BATTERY_KWH * 100,
            'export': exp, 'curtailed': curtailed,
            'charge': charge, 'discharge': discharge,
            'grid_import': grid_import, 'flux': is_flux_window(time_h),
        })

    totals['potential_pv_kwh'] = sum(pv * SLOT_HOURS for _, pv, _ in slots)
    totals['self_consumption_kwh'] = totals['pv_generated_kwh'] - totals['export_kwh']

    # Calculate overnight refill need: if end SOC < 80%, we'd want to refill
    # at 2-5am (17p) to have enough for next day
    target_overnight = BATTERY_KWH * 0.40  # assume we want 40% by morning
    shortfall = max(0, target_overnight - totals['end_soc'])
    totals['overnight_import_kwh'] = shortfall
    totals['overnight_import_cost'] = shortfall * IMPORT_CHEAP

    return results, totals


def simulate_msc(slots, start_soc_pct=0.40, max_soc_pct=0.90):
    """MSC baseline — 90% max, no curtailment management."""
    max_soc = BATTERY_KWH * max_soc_pct
    soc = BATTERY_KWH * start_soc_pct

    results = []
    totals = {
        'export_kwh': 0, 'curtailed_kwh': 0, 'import_kwh': 0,
        'pv_generated_kwh': 0, 'battery_full_time': None,
        'min_soc': soc, 'min_soc_time': 0, 'end_soc': 0,
        'flux_export_kwh': 0, 'fit_export_kwh': 0,
        'import_cost': 0, 'export_revenue': 0,
    }

    for time_h, pv, load in slots:
        excess = pv - load
        exp = charge = discharge = grid_import = curtailed = 0.0

        remaining_cap = max(0, max_soc - soc)
        max_charge_slot = min(MAX_CHARGE_RATE, remaining_cap / SLOT_HOURS) if remaining_cap > 0.01 else 0

        if excess > 0:
            charge = min(excess, max_charge_slot)
            after_charge = excess - charge
            exp = min(after_charge, DNO_LIMIT)
            curtailed = max(0, after_charge - DNO_LIMIT)
        else:
            avail = max(0, soc)
            msc_max = min(MAX_DISCHARGE_RATE, avail / SLOT_HOURS) if avail > 0.01 else 0
            discharge = min(-excess, msc_max)
            grid_import = max(0, -excess - discharge)

        soc += charge * SLOT_HOURS - discharge * SLOT_HOURS
        soc = max(0, min(max_soc, soc))

        if soc < totals['min_soc']:
            totals['min_soc'] = soc
            totals['min_soc_time'] = time_h

        exp_kwh = exp * SLOT_HOURS
        imp_kwh = grid_import * SLOT_HOURS
        totals['export_kwh'] += exp_kwh
        totals['curtailed_kwh'] += curtailed * SLOT_HOURS
        totals['import_kwh'] += imp_kwh
        totals['pv_generated_kwh'] += (pv - curtailed) * SLOT_HOURS

        e_rate = export_rate(time_h)
        i_rate = import_rate(time_h)
        totals['export_revenue'] += exp_kwh * e_rate
        totals['import_cost'] += imp_kwh * i_rate
        if is_flux_window(time_h):
            totals['flux_export_kwh'] += exp_kwh
        else:
            totals['fit_export_kwh'] += exp_kwh

        if totals['battery_full_time'] is None and soc >= max_soc - 0.05:
            totals['battery_full_time'] = time_h
        totals['end_soc'] = soc

        results.append({
            'time': time_h, 'pv': pv, 'load': load, 'excess': excess,
            'soc': soc, 'soc_pct': soc / BATTERY_KWH * 100,
            'target_soc': max_soc, 'target_pct': max_soc / BATTERY_KWH * 100,
            'export': exp, 'curtailed': curtailed,
            'charge': charge, 'discharge': discharge,
            'grid_import': grid_import, 'flux': is_flux_window(time_h),
        })

    totals['potential_pv_kwh'] = sum(pv * SLOT_HOURS for _, pv, _ in slots)
    totals['self_consumption_kwh'] = totals['pv_generated_kwh'] - totals['export_kwh']
    return results, totals


def print_table(results, title, show_range=(4.5, 21.5)):
    print(f"\n{'=' * 130}")
    print(f"  {title}")
    print(f"{'=' * 130}")
    print(f"{'Time':>5} | {'PV':>5} {'Load':>5} {'Excs':>5} | {'Chrg':>5} {'Disc':>5} {'SOC%':>5} {'Tgt%':>5} {'SOC':>6} | {'Exprt':>5} {'Crtld':>5} | {'Flux':>4}")
    print('-' * 130)
    for r in results:
        if show_range[0] <= r['time'] <= show_range[1]:
            crt = f"{r['curtailed']:>5.1f}" if r['curtailed'] > 0.01 else '    -'
            disc = f"{r['discharge']:>5.1f}" if r['discharge'] > 0.01 else '    -'
            chrg = f"{r['charge']:>5.1f}" if r['charge'] > 0.01 else '    -'
            flux = ' <<' if r.get('flux') else ''
            print(f"{fmt(r['time']):>5} | {r['pv']:>5.2f} {r['load']:>5.2f} {r['excess']:>5.1f} | "
                  f"{chrg} {disc} {r['soc_pct']:>5.1f} {r['target_pct']:>5.0f} {r['soc']:>6.2f} | "
                  f"{r['export']:>5.2f} {crt} | {flux}")


def print_summary(totals, name):
    gen_revenue = totals['pv_generated_kwh'] * FIT_GEN
    exp_revenue = totals.get('export_revenue', totals['export_kwh'] * FIT_EXPORT)
    imp_cost = totals.get('import_cost', totals['import_kwh'] * IMPORT_STANDARD)
    overnight_cost = totals.get('overnight_import_cost', 0)
    net = gen_revenue + exp_revenue - imp_cost - overnight_cost

    print(f"\n  --- {name} ---")
    print(f"  PV generated:     {totals['pv_generated_kwh']:>6.1f} kWh  (curtailed: {totals['curtailed_kwh']:.1f} kWh)")
    print(f"  Export total:     {totals['export_kwh']:>6.1f} kWh  (Flux 4-7pm: {totals.get('flux_export_kwh', 0):.1f} kWh @ 29p, FIT: {totals.get('fit_export_kwh', 0):.1f} kWh @ 12p)")
    print(f"  Import:           {totals['import_kwh']:>6.1f} kWh")
    print(f"  Self-consumption: {totals['self_consumption_kwh']:>6.1f} kWh")
    full = fmt(totals['battery_full_time'])
    print(f"  Battery full at:  {full}")
    print(f"  Min SOC:          {totals['min_soc']/BATTERY_KWH*100:.0f}% ({totals['min_soc']:.1f} kWh) at {fmt(totals['min_soc_time'])}")
    print(f"  End SOC:          {totals['end_soc']/BATTERY_KWH*100:.0f}% ({totals['end_soc']:.1f} kWh)")
    overnight = totals.get('overnight_import_kwh', 0)
    if overnight > 0:
        print(f"  Overnight refill: {overnight:.1f} kWh @ 17p = £{totals.get('overnight_import_cost', 0):.2f}")
    print(f"  Revenue:  FIT gen £{gen_revenue:.2f} + export £{exp_revenue:.2f} - import £{imp_cost:.2f}", end='')
    if overnight_cost > 0:
        print(f" - overnight £{overnight_cost:.2f}", end='')
    print(f" = £{net:.2f}")
    return net


# ============= MAIN =============
days = [
    ("June 19 — 65 kWh peak", "/Users/andrew/code/mum-energy/data/sma/daily_15min/Energy_Balance_2025_06_19.csv", True),
    ("July 12 — 68 kWh peak", "/Users/andrew/code/mum-energy/data/sma/daily_15min/Energy_Balance_2025_07_12.csv", False),
    ("July 9  — 45 kWh cloudy", "/Users/andrew/code/mum-energy/data/sma/daily_15min/Energy_Balance_2025_07_09.csv", False),
    ("July 15 — poor day", "/Users/andrew/code/mum-energy/data/sma/daily_15min/Energy_Balance_2025_07_15.csv", False),
    ("July 1  — 40 kWh day", "/Users/andrew/code/mum-energy/data/sma/daily_15min/Energy_Balance_2025_07_01.csv", False),
    ("May 30  — 47 kWh day", "/Users/andrew/code/mum-energy/data/sma/daily_15min/Energy_Balance_2025_05_30.csv", False),
]

print("\n" + "=" * 130)
print("  CURTAILMENT MODEL v6 — Iterative Target SOC + Octopus Flux")
print("  Flux export: 29p/kWh (4-7pm) | FIT export: 12p/kWh | FIT gen: 19p/kWh")
print("  Import: 17p (2-5am), 28p (standard), 38p (4-7pm)")
print("=" * 130)

all_day_results = []

for day_name, filepath, watts in days:
    print(f"\n{'#' * 130}")
    print(f"#  {day_name}")
    print(f"{'#' * 130}")

    rows = read_csv(filepath, watts=watts)
    slots = aggregate_30min(rows)

    total_pv = sum(pv * SLOT_HOURS for _, pv, _ in slots)
    total_load = sum(load * SLOT_HOURS for _, _, load in slots)

    remaining_overflow = compute_remaining_overflow(slots, DNO_LIMIT)
    total_overflow = remaining_overflow[0]

    initial_target = max(0, BATTERY_KWH - total_overflow)
    print(f"  PV: {total_pv:.0f} kWh | Load: {total_load:.0f} kWh | Excess: {total_pv - total_load:.0f} kWh")
    print(f"  Expected overflow: {total_overflow:.1f} kWh | Initial target SOC: {initial_target:.1f} kWh ({initial_target/BATTERY_KWH*100:.0f}%)")

    # Check what PV looks like during Flux window
    flux_pv = sum(pv * SLOT_HOURS for t, pv, _ in slots if is_flux_window(t))
    flux_load = sum(load * SLOT_HOURS for t, _, load in slots if is_flux_window(t))
    flux_excess = flux_pv - flux_load
    print(f"  Flux window (4-7pm): PV {flux_pv:.1f} kWh, Load {flux_load:.1f} kWh, Excess {flux_excess:.1f} kWh")

    scenarios = []

    # A) MSC baseline
    results_a, totals_a = simulate_msc(slots)
    print_table(results_a, f"{day_name}  —  A) MSC baseline (90% max)")
    net_a = print_summary(totals_a, 'A) MSC baseline')
    scenarios.append(('A) MSC baseline', totals_a, net_a))

    # B) V5 — curtailment only (FIT rates everywhere)
    if total_overflow > 0:
        results_b, totals_b = simulate_v5(slots)
        # Only show table for the Flux comparison, skip V5 detail to keep output manageable
        net_b = print_summary(totals_b, 'B) V5 curtailment (no Flux)')
        scenarios.append(('B) V5 (no Flux)', totals_b, net_b))
    else:
        net_b = net_a
        scenarios.append(('B) V5 OFF (no overflow)', totals_a, net_a))

    # C) V6 — curtailment + Flux forced export
    results_c, totals_c = simulate_v6_flux(slots)
    print_table(results_c, f"{day_name}  —  C) V6 curtailment + Flux export (4-7pm @ 29p)", show_range=(4.5, 21.5))
    net_c = print_summary(totals_c, 'C) V6 curtailment + Flux')
    scenarios.append(('C) V6 + Flux', totals_c, net_c))

    # D) V6 Flux WITHOUT curtailment plugin (just Flux export from MSC)
    # What if we just add Flux to MSC without the plugin?
    # (MSC + forced Flux export would need a separate sim, skip for now)

    # Comparison
    print(f"\n  {'COMPARISON':=^120}")
    print(f"  {'Scenario':<30} {'Crtld':>6} {'Export':>7} {'FluxExp':>8} {'Import':>7} {'Full@':>6} {'EndSOC':>7} {'Overnight':>10} {'Net £':>7}")
    print(f"  {'-'*114}")
    for label, t, net in scenarios:
        full = fmt(t['battery_full_time'])
        end_pct = f"{t['end_soc']/BATTERY_KWH*100:.0f}%"
        flux_exp = f"{t.get('flux_export_kwh', 0):.1f}"
        overnight = f"{t.get('overnight_import_kwh', 0):.1f} kWh" if t.get('overnight_import_kwh', 0) > 0 else '-'
        print(f"  {label:<30} {t['curtailed_kwh']:>5.1f}  {t['export_kwh']:>6.1f}  {flux_exp:>7}  {t['import_kwh']:>6.1f}  {full:>5}  {end_pct:>6}  {overnight:>9}  £{net:>5.2f}")

    # Delta
    if len(scenarios) >= 3:
        v5_net = scenarios[1][2]
        v6_net = scenarios[2][2]
        flux_gain = v6_net - v5_net
        msc_gain = v6_net - scenarios[0][2]
        print(f"\n  Flux value:  V6 vs V5 = {'+' if flux_gain >= 0 else ''}£{flux_gain:.2f}/day")
        print(f"  Total value: V6 vs MSC = {'+' if msc_gain >= 0 else ''}£{msc_gain:.2f}/day")
        all_day_results.append((day_name, scenarios[0][2], v5_net, v6_net, flux_gain))

    print()

# Final summary across all days
print(f"\n{'#' * 130}")
print(f"#  SUMMARY ACROSS ALL DAYS")
print(f"{'#' * 130}")
print(f"\n  {'Day':<30} {'MSC £':>7} {'V5 £':>7} {'V6+Flux £':>9} {'Flux gain':>10}")
print(f"  {'-'*70}")
total_msc = total_v5 = total_v6 = total_flux_gain = 0
for day_name, msc_net, v5_net, v6_net, flux_gain in all_day_results:
    short_name = day_name.split('—')[0].strip()
    print(f"  {short_name:<30} £{msc_net:>5.2f}  £{v5_net:>5.2f}  £{v6_net:>7.2f}  {'+' if flux_gain >= 0 else ''}£{flux_gain:.2f}")
    total_msc += msc_net
    total_v5 += v5_net
    total_v6 += v6_net
    total_flux_gain += flux_gain

print(f"  {'-'*70}")
print(f"  {'TOTAL (6 days)':<30} £{total_msc:>5.2f}  £{total_v5:>5.2f}  £{total_v6:>7.2f}  +£{total_flux_gain:.2f}")
print(f"\n  Average Flux gain per day: +£{total_flux_gain/len(all_day_results):.2f}")
print()
