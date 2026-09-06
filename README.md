# Bike Garage

Initial Python application for Bike Garage. It provides a local-first Strava Proxy connection screen and creates the initial test user **Jakez** automatically.

## Start locally

```bash
cp .env.example .env
make dev
```

Open <http://localhost:8000>.

## Current scope

- SQLite database mounted at `./data/bike-garage.db`.
- Initial user `Jakez` is created during startup.
- Strava Proxy is the only active first connector. It owns Strava OAuth; Bike Garage does not connect directly to Strava.
- The connection form captures the proxy URL, its optional Bearer API key, and an authorized proxy account identifier such as `dennis` or `conny`.
- The proxy's public `GET /health` endpoint is verified before Bike Garage saves the connection. Every saved connection can also be tested: Bike Garage performs the health check and authenticated `GET /{identifier}/activities?per_page=200`, then displays the fetched activity count.
- The proxy API key is plain text in SQLite only for this prototype; production requires encrypted secret storage.
- A background sync runs every five minutes by default (`SYNC_INTERVAL_SECONDS`) and imports each connected proxy account.
- Imported records are stored as `ProviderActivity` rows and correlated into one canonical `Activity` when sport type, start time (15-minute tolerance), and distance (5% or 1 km tolerance) indicate the same ride.
- The Activity stream is available at `/activities`; `Sync now` triggers the same importer immediately.
- Each Activity Stream row opens `/activities/{id}` with a route map decoded from the imported Strava `summary_polyline`, an activity summary, and its linked provider records.
- Bikes can be created, edited, deleted, and photographed at `/bikes`; uploaded JPEG, PNG, and WebP photos are kept in `data/bike-photos`.
