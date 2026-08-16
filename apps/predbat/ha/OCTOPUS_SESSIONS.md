# Octopus Octoplus sessions — what they are and how to tell them apart

Written 2026-08-16, corrected 2026-08-16 against the integration source and the
upstream issue tracker. Read this before touching anything that keys off an
Octoplus calendar, binary sensor or event entity.

## The short version

Octopus have **consolidated every demand-flexibility scheme into two generic
categories** and hidden the underlying scheme from the user:

| Category      | Means                | Was                                                    |
|---------------|----------------------|--------------------------------------------------------|
| **Power Down**| reduce consumption   | Saving Sessions                                         |
| **Power Up**  | increase consumption | Free Electricity Sessions, Local Power Ups, and others |

Both now arrive on the **`savingSessions` GraphQL query** — the one the
integration still calls "saving sessions". Nothing arrives on the Power Up
entities at all; see "Where power_up actually comes from" below.

**The discriminator is `octopoints_per_kwh`:**

```text
octopoints_per_kwh  > 0   →  Power Down  (paid per kWh saved)    →  export at the cap
octopoints_per_kwh == 0   →  Power Up    (free import)           →  charge from grid
```

A Power Down pays per kWh saved. A Power Up pays nothing per kWh because the
benefit *is* the free import. Zero is not a missing value; it is the signal.

**This is a proxy, and we know it.** It is not the discriminator we would
choose — it is the only field in the payload that varies between the two
categories. See "Why we cannot do better yet" and "Removal trigger".

## Where `power_up` actually comes from

This is the part that was wrong in the first draft of this document, and it
matters because it decides whether the Power Up entities can ever come good on
their own. They cannot.

`custom_components/octopus_energy/api_client/__init__.py`:

```python
async def async_get_free_electricity_sessions(self, account_id: str) -> ...:
    url = f'https://oe-api.davidskendall.co.uk/free_electricity.json'
```

No GraphQL, no authentication, no account number — a **hardcoded third-party
scraped JSON file**. Fetched 2026-08-16: 24 events, codes `1`–`24`, last one
`2025-10-25`. That is exactly what the `power_up` entities show on the box,
because that file *is* what they show.

So `power_up` is not reading a retired Octopus endpoint. It was never reading
an Octopus endpoint. It is an alias of a community scrape that stopped being
updated in October 2025, and it will not start again.

## Why we cannot do better yet

The query the integration *does* use for sessions
(`backend_octoplus_saving_session_query`) selects exactly:

```text
events:       id, code, rewardPerKwhInOctoPoints, startAt, endAt, devEvent, targetRegion
joinedEvents: eventId, startAt, endAt, rewardGivenInOctoPoints
```

No category. No type. No campaign. `rewardPerKwhInOctoPoints` — surfaced as
`octopoints_per_kwh` — is the only field that differs between a Power Up and a
Power Down. Hence the proxy.

**The real API does type them properly.** Octopus expose
`customerFlexibilityCampaignEvents(accountNumber:, campaignSlug:,
supplyPointIdentifier:)`, where `campaignSlug` (e.g. `"free_electricity"`) is a
first-class category. The integration simply does not call it.

We are not alone on the proxy. On upstream issue #1820, three unconnected users
independently landed on the same `octopoints_per_kwh > 0` test, one of them
after nearly running a full power-down in their flat during a free hour.

## Upstream status (checked 2026-08-16)

| Issue | State |
|---|---|
| **#1590** "Change free electricity sensor to use Octopus GraphQL API" | **OPEN** since 2026-03-16, `awaiting-maintainer-response`, assigned, no milestone |
| **#1820** free electricity session appears as a saving session | **OPEN**, 22 comments, no maintainer reply |
| **#1815** Power Up schedule not showing on v19.0.0 | **OPEN** |
| #1822 Power Up entities missing | closed COMPLETED — the answer was that they are `disabled_by: integration` |
| #1802 Power Up attributes don't mirror Power Down | closed **NOT_PLANNED** |

No PR addresses the data source. The only related commit is `ae7ce2f`
(2026-07-10): *"Added new sensors to represent power up and power down events
which will [supersede] saving session and free electricity session sensors
(2 hours dev time)"* — the rename, and nothing else.

## Why the entity names mislead

Two separate changes happened, and only one of them is reflected in the entities.

1. **Octopus** renamed and merged the schemes (above).
2. **The integration** (ADR 0004, v19.0.0) added new sensor *names* alongside the
   old ones — `power_down` beside `saving_sessions`, `power_up` beside
   `free_electricity_session`. Legacy names are removed **January 2027**.

The integration has done the naming half and **not** the categorisation half. So
`power_down` is a straight alias of the saving-sessions query, and a Power Up
event lands on it. No entity currently *carries* Power Up events, even though
entities named for them exist.

Blame splits both ways, and upstream reaches the same split (gcoan on #1820:
*"I think Octopus have screwed up the configuration ... not necessarily
something the integration can cope with"*):

- **Octopus** published a Power Up down the `savingSessions` feed with 0 points
  and no type field. The integration cannot fix that with the current query.
- **The integration** separately still scrapes a file that died in October 2025.
  That one is #1590 and is fixable today.

## Entity map, as observed on v19.0.0 (2026-08-16)

Alive, carrying the unified feed (both categories):

```text
calendar...octoplus_power_down                ==  calendar...octoplus_saving_sessions
event...octoplus_power_down_events            ==  event...octoplus_saving_session_events
```

`joined_events` / `available_events` on the event entities carry `id`, `code`,
`start`, `end`, `duration_in_minutes`, `octopoints_per_kwh` — the last is the
only field that distinguishes the two categories.

Dead — they mirror the scraped file, not Octopus:

```text
calendar...octoplus_power_up                  ==  calendar...octoplus_free_electricity_session
event...octoplus_power_up_events              ==  event...octoplus_free_electricity_session_events
```

**Do not key anything on these** — an automation triggered off them will never
fire again. Confirmed by other users too: the 2026-08-16 Power Up did not appear
on the old free-electricity calendar either.

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
event starts at 14:00.

**A Power Up arrives as several consecutive one-hour slots, joined
individually.** Another account's dump of the same 2026-08-16 event
(upstream #1820) shows four slots — 11:00, 12:00, 13:00, 14:00, all 0 pts —
with only one marked JOINED and the rest `available`. Slots carry codes of the
form `EVENT_43_160826`. Do not assume one session means one window, and do not
assume being offered a slot means being in it.

## Time of day — deliberately NOT used as a gate

Octopus state officially that the network operator gives the green light around
10:00 and that Power Ups run between **11:00 and 16:00**, which matches every
observation above. It is therefore tempting to gate on the hour.

**We do not** (Andrew, 2026-08-16). The gate would be a second, weaker copy of a
test we already have, and the failure it guards against — a genuine 0-point
Power Down — has never been observed. Correlation is recorded here as context,
not as logic. If it ever becomes logic, it belongs in the discriminator sensor
with the rest, not sprinkled through consumers.

## What this means for our code

Everything keys off **one pair of template binary sensors**, defined once in
`ha/octoplus_session_helpers.yaml`:

```text
binary_sensor.octoplus_power_up_active     free import running now  → grid charge
binary_sensor.octoplus_power_down_active   paid session running now → export at cap
```

Consumers read those sensors and never re-implement the test. That is the whole
point: when upstream is fixed there is exactly one body to rewrite.

- **Max Export forcing** (`sig_dispatch_intent_helpers.yaml`) reads
  `power_down_active`. Keying it on the calendar alone makes CM export through a
  Power Up — the opposite of right.
- **Grid charge** (`octoplus_power_up_grid_charge.yaml`) reads `power_up_active`.
- Migrate remaining references from `saving_sessions` to `power_down` naming
  before January 2027. When the legacy calendar is removed,
  `is_state(..., 'on')` becomes permanently false and anything still keyed on it
  **silently stops** — no error, no log.
- Re-source `_get_session_reserve_kwh`, `_get_session_start`, `_get_session_end`
  off the dead binary sensor and onto `joined_events` (PARKED).
- Sessions can start within an hour of announcement. Anything that assumes a
  day's notice will miss them; a short polling sweep will not.

## Removal trigger

Delete the proxy and read the native entities when **either**:

1. upstream #1590 lands and `power_up` is fed from
   `customerFlexibilityCampaignEvents`; or
2. the integration exposes any explicit type/category field on `joined_events`.

Only the body of the two sensors in `ha/octoplus_session_helpers.yaml` changes.
`tests/test_yaml_octoplus_sessions.py` asserts that no consumer has re-inlined
the test, so the swap cannot leave a stale copy behind.

## Open

- **Do Power Ups ever pay octopoints?** Octopus's own Power Up page describes
  **bill credit** for the free electricity, which matches 2026-08-16 (0 pts,
  imported at the normal 12.42p, credit expected at settlement). But secondary
  sources claim Octoplus now rewards points for *both* categories. If a Power Up
  ever arrives with points > 0 the discriminator **inverts** and CM exports into
  a free-import hour. This is the one expensive failure mode; check the next
  Power Up's points value before trusting the run.
- **Who joins, and per slot?** Octopus require opting in before each session, and
  slots are joined individually. Predbat auto-joins via `joinSavingSessionsEvent`,
  which plausibly covers Power Ups since they share the endpoint — unverified.
  One user reports Octopus auto-opting them into a Power Down with no chance to
  decline, so enrolment behaviour is not uniform.
- Whether the 2026-08-16 import shows as a bill credit at settlement.
