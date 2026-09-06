# FIT examples

These binary FIT files are kept as local examples for Hammerhead import and parsing work:

| File | Purpose |
|---|---|
| [`crux.fit`](crux.fit) | Example ride recorded on the Crux. |
| [`creo.fit`](creo.fit) | Example e-bike ride recorded on the Creo. |

## Creo battery data

The actual e-bike charge is a **standard FIT field** on every `record` message (global message `20`): `ebike_battery_level`, field definition `118`, unit `%`. It is present in all 1,583 ride records.

| Timestamp (UTC) | `ebike_battery_level` |
|---|---:|
| 2026-08-23 06:56:21 | 100% |
| 2026-08-23 06:59:27 | 99% |
| 2026-08-23 07:02:00 | 98% |
| … | … |
| 2026-08-23 07:53:47 | 85% |

This exactly matches Hammerhead’s dashboard: the activity begins at 100%, ends at 85%, and therefore uses 15 percentage points of battery charge. The file also contains `ebike_travel_range` (field `117`), `ebike_assist_mode` (field `119`), and `ebike_assist_level_percent` (field `120`).

### Assist time series

The same 1,583 `record` messages contain the data for Hammerhead’s assist graph:

| FIT record field | Samples | Usable values in this file | Interpretation |
|---|---:|---|---|
| `118` `ebike_battery_level` | 1,583 | 100% → 85% | Battery graph. |
| `119` `ebike_assist_mode` | 1,578 | 3, 5 | Provider/sensor-specific assist-mode codes. |
| `120` `ebike_assist_level_percent` | 1,583 | 60%, 100% | Assist-level graph. |

The first five samples have no assist mode. A raw value of `255` occurs briefly in both assist fields; `255` is FIT's invalid `uint8` sentinel, so it must be treated as missing data, not as a real 255% value. Ignoring those gaps, the ride starts at 60% assist and changes to 100% at 07:00:00 UTC.

Separately, the file declares a provider-specific developer field named `charge` on six `device_info` messages. It changes from 76% to 71% and should not be used as the e-bike ride battery curve.
