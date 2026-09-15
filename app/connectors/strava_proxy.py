from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class ProxyHealth:
    is_healthy: bool
    message: str


@dataclass(frozen=True)
class ProxyConnectionTest:
    is_healthy: bool
    message: str
    activity_count: int | None = None
    activities: tuple[dict[str, str], ...] = ()


def normalise_base_url(endpoint_url: str) -> str:
    return endpoint_url.strip().rstrip("/")


def health_check(endpoint_url: str, timeout_seconds: int = 5) -> ProxyHealth:
    """Check the proxy without sending its API key; /health is deliberately public."""
    base_url = normalise_base_url(endpoint_url)
    if not base_url.startswith(("http://", "https://")):
        return ProxyHealth(False, "The proxy URL must start with http:// or https://.")
    try:
        with urlopen(Request(urljoin(base_url + "/", "health")), timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if response.status == 200 and payload.get("status") == "ok":
            return ProxyHealth(True, "Strava Proxy is reachable.")
        return ProxyHealth(False, "The proxy did not return the expected health response.")
    except HTTPError as error:
        return ProxyHealth(False, f"The proxy returned HTTP {error.code} on /health.")
    except (URLError, TimeoutError, ValueError):
        return ProxyHealth(False, "Bike Garage could not reach the proxy health endpoint.")


def test_connection(endpoint_url: str, api_key: str | None, account_identifier: str) -> ProxyConnectionTest:
    """Verify both the public service status and authenticated activity access."""
    health = health_check(endpoint_url)
    if not health.is_healthy:
        return ProxyConnectionTest(False, health.message)

    base_url = normalise_base_url(endpoint_url)
    url = urljoin(base_url + "/", f"{quote(account_identifier, safe='')}/activities?{urlencode({'per_page': 200})}")
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        with urlopen(Request(url, headers=headers), timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, list):
            return ProxyConnectionTest(False, "The proxy activity endpoint returned an unexpected response.")
        suffix = "activity" if len(payload) == 1 else "activities"
        preview = tuple(
            {
                "name": str(activity.get("name") or "Unnamed activity"),
                "date": str(activity.get("start_date_local") or "")[:10],
                "distance": f"{float(activity.get('distance') or 0) / 1000:.1f} km",
            }
            for activity in payload[:5]
            if isinstance(activity, dict)
        )
        return ProxyConnectionTest(
            True,
            f"Connected. Fetched {len(payload)} {suffix} from Strava Proxy.",
            len(payload),
            preview,
        )
    except HTTPError as error:
        if error.code == 401:
            return ProxyConnectionTest(False, "Strava Proxy rejected the Proxy API Key (HTTP 401).")
        if error.code == 404:
            return ProxyConnectionTest(False, "The proxy account identifier is not authorized (HTTP 404).")
        return ProxyConnectionTest(False, f"The proxy activity endpoint returned HTTP {error.code}.")
    except (URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return ProxyConnectionTest(False, "Bike Garage could not load activities from Strava Proxy.")


def fetch_activities(endpoint_url: str, api_key: str | None, account_identifier: str,
                     *, after: int | None = None, per_page: int = 200) -> list[dict]:
    """Fetch one proxy page for the importer."""
    base_url = normalise_base_url(endpoint_url)
    query = {"per_page": min(max(per_page, 1), 200)}
    if after:
        query["after"] = after
    url = urljoin(base_url + "/", f"{quote(account_identifier, safe='')}/activities?{urlencode(query)}")
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    with urlopen(Request(url, headers=headers), timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, list):
        raise ValueError("The proxy activity endpoint returned an unexpected response.")
    return [item for item in payload if isinstance(item, dict)]


def fetch_activity(endpoint_url: str, api_key: str | None, account_identifier: str, activity_id: str) -> dict:
    """Fetch the current full detail record for one Strava activity through the proxy."""
    payload = _authenticated_request(
        endpoint_url,
        api_key,
        f"{quote(account_identifier, safe='')}/activities/{quote(str(activity_id), safe='')}",
    )
    if not isinstance(payload, dict):
        raise ValueError("The proxy activity detail endpoint returned an unexpected response.")
    return payload


def _authenticated_request(endpoint_url: str, api_key: str | None, path: str, *, method: str = "GET",
                           payload: dict | None = None, timeout_seconds: int = 15) -> dict | list:
    base_url = normalise_base_url(endpoint_url)
    headers = {"Accept": "application/json"}
    data = None
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(urljoin(base_url + "/", path.lstrip("/")), headers=headers, data=data, method=method)
    with urlopen(request, timeout=timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_bikes(endpoint_url: str, api_key: str | None, account_identifier: str, *, timeout_seconds: int = 4) -> list[dict]:
    """Fetch the active Strava bike catalogue from one proxy account."""
    payload = _authenticated_request(
        endpoint_url, api_key, f"{quote(account_identifier, safe='')}/bikes", timeout_seconds=timeout_seconds
    )
    if not isinstance(payload, list):
        raise ValueError("The proxy bike endpoint returned an unexpected response.")
    return [item for item in payload if isinstance(item, dict)]


def update_activity(endpoint_url: str, api_key: str | None, account_identifier: str, activity_id: str, *,
                    gear_id: str | None = None, sport_type: str | None = None,
                    description: str | None = None) -> dict:
    """Update the configured Bike Garage fields of a Strava activity."""
    update_payload: dict[str, str] = {}
    if gear_id is not None:
        update_payload["gear_id"] = gear_id
    if sport_type is not None:
        update_payload["sport_type"] = sport_type
    if description is not None:
        update_payload["description"] = description
    if not update_payload:
        raise ValueError("At least one Strava activity field must be provided.")
    payload = _authenticated_request(
        endpoint_url,
        api_key,
        f"{quote(account_identifier, safe='')}/activities/{quote(str(activity_id), safe='')}",
        method="PUT",
        payload=update_payload,
    )
    if not isinstance(payload, dict):
        raise ValueError("The proxy activity update returned an unexpected response.")
    return payload


def update_activity_gear(endpoint_url: str, api_key: str | None, account_identifier: str,
                         activity_id: str, gear_id: str, *, description: str | None = None) -> dict:
    """Backward-compatible helper for callers that update only the gear."""
    return update_activity(endpoint_url, api_key, account_identifier, activity_id, gear_id=gear_id,
                           description=description)
