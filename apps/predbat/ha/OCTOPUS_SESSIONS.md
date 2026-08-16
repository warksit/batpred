# Octopus Octoplus sessions — what they are and how to tell them apart

Written 2026-08-16 after spending an afternoon getting this wrong repeatedly. Read
this before touching anything that keys off an Octoplus calendar, binary sensor or
event entity.

## The short version

Octopus have **consolidated every demand-flexibility scheme into two generic
categories** and hidden the underlying scheme from the user:

| Category      | Means                | Was                                                    |
|---------------|----------------------|--------------------------------------------------------|
| **Power Down**| reduce consumption   | Saving Sessions                                         |
| **Power Up**  | increase consumption | Free Electricity Sessions, Local Power Ups, and others |

Both now arrive through the **same API endpoint** — the one the integration still
calls "saving sessions". The old free-electricity endpoint is **retired**, not
merely quiet.

**The discriminator is `octopoints_per_kwh`:**

```text
octopoints_per_kwh  > 0   →  Power Down  (paid per kWh saved)    →  export at the cap
octopoints_per_kwh == 0   →  Power Up    (free import)           →  charge from grid
```

A Power Down pays per kWh saved. A Power Up pays nothing per kWh because the
benefit *is* the free import. Zero is not a missing value; it is the signal.

## Why the entity names mislead

Two separate changes happened, and only one of them is reflected in the entities.

1. **Octopus** renamed and merged the schemes (above).
2. **The integration** (ADR 0004, v19.0.0) added new sensor *names* alongside the
   old ones — `power_down` beside `saving_sessions`, `power_up` beside
   `free_electricity_session`. Legacy names are removed **January 2027**.

The integration has done the naming half and **not** the categorisation half. So
`power_down` is a straight alias of the saving-sessions endpoint, and a Power Up
event lands on it. There is no entity that currently means "Power Up".

Source: <https://github.com/BottlecapDave/HomeAssistant-OctopusEnergy/issues/1759>
and ADR 0004.

## Entity map, as observed on v19.0.0 (2026-08-16)

Alive, carrying the unified feed (both categories):

```text
calendar...octoplus_power_down                ==  calendar...octoplus_saving_sessions
event...octoplus_power_down_events            ==  event...octoplus_saving_session_events
```

`joined_events` / `available_events` on the event entities carry `id`, `start`,
`end`, `duration_in_minutes`, `octopoints_per_kwh` — the last is the only field
that distinguishes the two categories.

Effectively dead — the endpoint they read is retired:

```text
calendar...octoplus_power_up                  ==  calendar...octoplus_free_electricity_session
event...octoplus_power_up_events              ==  event...octoplus_free_electricity_session_events
```

Both return the same 24 events, codes `1`–`24`, all between 2024-08-15 and
**2025-10-25**, none since. **Do not key anything on these** — an automation
triggered off them will never fire again.

Already removed by the integration:

```text
binary_sensor...octoplus_saving_sessions      unavailable / restored: true
```

CM still reads it in three places (see PARKED.md). That is why
`session_need_kwh` is null: no source, not no session.

**Most Octoplus entities ship `disabled_by: integration`** — the power_up pair,
the free_electricity trio, both baselines and the `*_data_last_retrieved`
sensors. Enabled here on 2026-08-16. A search that finds nothing may be finding
a disabled entity, not an absent one.

## Evidence behind the discriminator

This account, 18 joined events:

```text
2026-03-23 18:00  102 pts     2026-07-24 18:00   96 pts
2026-05-21 18:00   96 pts     2026-08-03 19:00  109 pts
2026-06-24 19:30  415 pts     2026-08-05 20:00   93 pts
...                           2026-08-12 18:00   16 pts   ← Power Down, exported at cap
2026-08-16 14:00    0 pts   ← Power Up, imported 4.35 kWh
```

Every Power Down starts 18:00–20:30 and pays 16–415 points. The one 0-point
event starts at 14:00. Time of day correlates but **must not be used as the
test** — Octopus now announce around 10:00 and sessions can start within the
hour, so an evening Power Up is possible.

## What this means for our code

- Key the **Max Export forcing** on the unified feed filtered to
  `octopoints_per_kwh > 0`. Keying it on the calendar alone makes CM export
  through a Power Up — the opposite of right.
- Key the **grid charge** on the same feed filtered to `octopoints_per_kwh == 0`.
- Migrate references from `saving_sessions` to `power_down` naming before
  January 2027. When the legacy calendar is removed, `is_state(..., 'on')`
  becomes permanently false and the forcing **silently stops** — no error, no log.
- Re-source `_get_session_reserve_kwh`, `_get_session_start`, `_get_session_end`
  off the dead binary sensor and onto `joined_events`.
- Sessions can start within an hour of announcement. Anything that assumes a
  day's notice will miss them; a short polling sweep will not.

## Open

- Whether Octopus credits a Power Up as a bill credit rather than a 0p rate. The
  2026-08-16 import was charged at the normal 12.42p; if a credit appears in
  settlement that confirms the credit model, and confirms the classification.
- Whether the integration will eventually type these properly. If it does, the
  `octopoints_per_kwh` test should be replaced by whatever field it exposes.
