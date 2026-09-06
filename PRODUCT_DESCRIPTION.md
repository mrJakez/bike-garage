# Bike Garage — Product Description V1

**Status:** Draft for review  
**Version:** 0.3  
**Date:** 2026-08-29

## Product vision

Bike Garage is the digital source of truth for a cyclist's bikes, grouped rides, and mileage.

Strava and Hammerhead supply activity records. Bike Garage groups matching records from connected accounts, determines the bikes ridden by each recorded rider, and maintains the resulting mileage ledger.

Bike Garage determines which physical bike or bikes belong to an activity. Strava gear is one resolver signal and a synchronization target—not the internal source of truth. Connected accounts can record the same group ride independently; Bike Garage bundles those records into one activity while retaining every rider's own bike and mileage.

## Product goals

Bike Garage should answer three questions reliably:

1. Which bike was used for a ride?
2. Why did the system select that bike?
3. How many kilometers has each bike covered since Bike Garage tracking began?

## Product principles

- **Bike Garage owns the assignments.** External gear mappings are evidence and outputs.
- **Provider-neutral evidence.** The resolver works with stored provider facts, not provider APIs.
- **Traceable decisions.** Confidence, resolver version, candidates, and evidence remain inspectable.
- **Ledger-based mileage.** Totals are projections over activity mileage entries, never mutable counters.
- **Reversible automation.** Corrections rebook mileage and rebuild all affected projections.
- **Conservative external writes.** Strava is changed only after a sufficiently reliable decision.

## Primary user

An enthusiast cyclist with multiple bikes who records rides in Strava and Hammerhead and wants dependable bike mileage without manual ride logging.

## V1 scope

- Connect Strava and Hammerhead accounts where supported.
- Import activities from every connected Strava and Hammerhead account using provider push events plus cursor-based catch-up sync.
- Bundle duplicate provider records for the same group ride into one canonical Bike Garage activity.
- Retain the original Hammerhead FIT file on its provider activity.
- Use ordered Bike Slot Rules to determine the expected bike count and slots from group context, with grouped recorded riders as the fallback.
- Use first-match-wins Bike Assignment Rules to resolve each free slot from provider gear, bike availability, and configured Hammerhead FIT attributes; flag incomplete assignments for manual review.
- Review, confirm, ignore, or correct uncertain assignments.
- Book and rebook activity distance through a mileage ledger.
- Resolve one expected bike assignment per recorded rider and optional companion-bike assignments, with independent mileage postings.
- Synchronize confirmed Bike Garage assignments back to Strava gear.

## Out of scope for V1

- Component lifecycle, component mileage, and component maintenance.
- BLE/ANT+ scanning, raw packet storage, battery observations, and garage-presence inference.
- FIT hardware parsing and hardware-identity-based bike resolution.
- Real-time ride tracking or navigation.
- Predictive maintenance based on machine learning.
- Silent automatic replacement of a manually confirmed assignment.

## Main product surfaces

### Dashboard

- Bikes with today's and lifetime mileage.
- Activities requiring review.
- Provider connection or synchronization problems.

### Activity review

- Activity summary and all linked provider records.
- Ranked bike candidates with confidence.
- Human-readable rule trace including source, matched conditions, and action.
- Confirm, choose another bike, or ignore.
- Preview of mileage impact.

### Bike detail

- Prominent bike photo, mileage, and provider-account-specific gear links.
- Ledger-derived lifetime and tracked mileage.
- Linked provider gear mappings and recorded-rider history.

## V1 success criteria

- Duplicate events and independently tracked group rides never create duplicate rows in the activity stream.
- A grouped ride creates one expected assignment per recorded rider.
- A correction moves mileage between bikes without changing other riders' assignments.
- Medium- and low-confidence cases are visible and do not prematurely modify Strava.
- Original provider artifacts can be reparsed with a newer parser version.
- External synchronization failures never invalidate the internal assignment.

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Core Features](docs/CORE_FEATURES.md)
- [Bike Resolver V1](docs/BIKE_RESOLVER.md)
- [User Interface V1](docs/USER_INTERFACE.md)

## Deferred to Phase 2

Component installation history, maintenance, FIT hardware parsing, and the Garage Scanner/BLE design are intentionally deferred. The future scanner design remains documented in [Garage Scanner and BLE Observations — Phase 2](docs/GARAGE_SCANNER.md).
