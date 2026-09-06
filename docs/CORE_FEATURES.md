# Bike Garage — Core Features V1

## 1. Provider connections

Each user can connect Strava and Hammerhead accounts. Every connection exposes its health, last successful synchronization, granted capabilities, and required user action.

Every connection retains its own activity synchronization cursor and last-successful-sync timestamp, so Jakez's Strava, Conny's Strava, and Jakez's Hammerhead account can be imported independently and safely retried.

The system must not assume that all accounts or data types are available. A missing Hammerhead record does not prevent manual assignment.

## 2. Activity ingestion and correlation

- A Strava webhook queues a cursor-based activity pull for the account that recorded it.
- A Hammerhead activity-sync push event queues a cursor-based pull for that Hammerhead connection.
- A periodic catch-up sync pulls each connected Strava and Hammerhead account, so missed or delayed push events do not leave a gap.
- Provider activities are unique per connection and external activity ID; duplicate push deliveries and repeated pulls are idempotent.
- Activity correlation bundles matching provider activities from Jakez, Conny, or other connected accounts into one canonical group activity.
- The activity stream shows the canonical group activity once and displays its source records and riders.
- A matching Hammerhead ProviderActivity joins the canonical activity and owns the original FIT artifact.
- Configured Hammerhead FIT attributes, initially bike type, are normalized onto the ProviderActivity for resolver rules.
- Ambiguous provider matches do not produce strong resolver evidence.
- Original FIT files and relevant source payloads are retained as versioned artifacts.
- Activity updates and deletions update or reverse their downstream effects safely.

## 3. Bike management

A bike contains its name, type, required primary photo, active state, creation source, and availability intervals. It may be created manually or from an imported Strava gear record.

Strava gear mappings are stored as `BikeProviderLink`s, not on `Bike`: each link binds a bike to one `external_gear_id` inside one specific `ProviderConnection`. Thus the same physical bike can be linked to Jakez's, Conny's, or Dennis's Strava accounts with different gear IDs. A manually created bike starts without any link.

Each bike has an individual mileage-tracking cutoff. Its previous mileage is stored as an `OPENING_BALANCE` ledger entry effective at that cutoff. Activities before the cutoff are ignored unless the user explicitly requests a historical import.

### Activity stream

The activity stream displays one canonical group activity per ride, not one row per imported Strava record. A row shows its linked sources, such as “Jakez on Strava” and “Conny on Strava”, the recorded riders, and the unresolved or confirmed bike assignments. Opening the row reveals the individual provider distances, timestamps, gear values, and resolver evidence.

## 4. Activity bike assignments

An activity may have several bike assignments. Each assignment represents one expected or confirmed bike participation and can optionally identify the rider and their source activity:

```text
ActivityBikeAssignment
  activity_id
  bike_id = optional until resolved
  rider_user_id = optional
  source_provider_activity_id = optional
  role = RECORDED_RIDER | COMPANION
  distance_m
  coverage = FULL_ACTIVITY | MANUAL_DISTANCE | SEGMENTS
  confidence
  status
  resolver_version
  evidence_snapshot_id
  decided_at
```

Every linked `RIDER_RECORD` creates one expected `RECORDED_RIDER` assignment for the User who owns that connection. In V1, Strava activities are rider records; Hammerhead activities are `DEVICE_RECORD`s that retain their FIT artifact without creating another assignment. `COMPANION` assignments cover untracked riders. Each recorded-rider assignment may synchronize only its own Strava source activity and only if the selected bike has a `BikeProviderLink` for that connection; companion assignments never change Strava gear.

Bike Slot Rules determine `Activity.expected_bike_count` and the required slots first; the number of linked rider records is the fallback. Bike Assignment Rules then select a fixed bike for each free slot or deliberately require review when declared provider and group conditions match. An incomplete count creates a manual-review alert.

Example:

```text
Activity 123456
  RECORDED_RIDER  Jakez -> Tarmac  73.42 km · Strava Jakez #123
  RECORDED_RIDER  Conny -> Crux    73.18 km · Strava Conny #456
  COMPANION       Lea   -> Gravel  73.42 km
```

| Status | Meaning | Mileage | Strava |
|---|---|---:|---:|
| `AUTO_CONFIRMED` | First matching rule assigned the bike | Final | Synchronize |
| `NEEDS_CONFIRMATION` | Reserved for a future confirm-before-book rule action | Provisional | No change |
| `NEEDS_REVIEW` | Rule required review or an expected bike slot is unfilled | None | No change |
| `MANUALLY_CONFIRMED` | User confirmed or selected the bike | Final | Synchronize |
| `CORRECTED` | Earlier decision was superseded | Rebook | Synchronize |
| `IGNORED` | Activity should not accrue bike mileage | None | Explicit policy |

## 5. Mileage ledger

Mileage is posted per activity:

```text
MileageEntry
  assignment_id
  activity_id
  bike_id
  entry_type = OPENING_BALANCE | ACTIVITY | MANUAL_ADJUSTMENT | REVERSAL
  distance_m
  booking_status = FINAL | PROVISIONAL | REVERSED
```

Bike totals are sums over active ledger entries. `OPENING_BALANCE` and `MANUAL_ADJUSTMENT` entries have no assignment or activity. There is at most one active activity entry per assignment, not per activity. When an assignment changes, Bike Garage reverses or deactivates its old posting and creates the correct posting. Other bikes assigned to the activity remain unaffected.

## 6. Strava feedback loop

For a high-confidence or manually confirmed recorded-rider assignment, Bike Garage synchronizes the gear from the selected bike's `BikeProviderLink` for that assignment's own Strava connection if it differs from the source activity. Companion assignments never change Strava gear.

- No Strava mutation for `NEEDS_CONFIRMATION` or `NEEDS_REVIEW`.
- Transient failures are retried.
- Permanent failures and missing provider-account links are visible to the user.
- A Strava failure never rolls back the internal assignment or mileage booking.
- Provider callbacks caused by Bike Garage must not create an update loop.

## Acceptance scenarios

1. Duplicate Strava or Hammerhead push events produce one provider activity after the cursor-based pull.
2. Matching Jakez and Conny Strava activities are bundled into one canonical activity-stream row.
3. A Hammerhead activity-sync event or periodic catch-up imports a Hammerhead ProviderActivity and its FIT file, which link to the correct canonical activity.
4. A missing provider connection still permits review and manual assignment.
5. One canonical activity can book distance to every recorded rider's bike and one or more companion bikes.
6. Correcting one assignment recalculates its bike mileage without removing other assignments.
7. Late provider correlation creates a new resolver result without overwriting a manual decision.
8. A failed Strava synchronization remains retryable and visible.

## Deferred to Phase 2

Component installations, maintenance rules, FIT hardware parsing, and Garage Scanner/BLE observations are deliberately outside V1.
