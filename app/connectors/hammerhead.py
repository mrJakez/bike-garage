from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


DEFAULT_API_BASE_URL = "https://api.hammerhead.io/v1/api"
DEFAULT_AUTH_BASE_URL = "https://api.hammerhead.io/v1/auth"
SPORT_TYPE_MAP = {
    "RIDE": "Ride", "GRAVEL": "GravelRide", "MOUNTAIN_BIKE": "MountainBikeRide",
    "EBIKE": "EBikeRide", "EMOUNTAIN_BIKE": "EMountainBikeRide", "VELOMOBILE": "Velomobile",
}


def authorization_url(client_id: str, redirect_uri: str, state: str) -> str:
    return f"{DEFAULT_AUTH_BASE_URL}/oauth/authorize?" + urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
        "scope": "activity:read", "state": state,
    })


def exchange_code(client_id: str, client_secret: str, code: str, redirect_uri: str) -> dict:
    payload = urlencode({
        "client_id": client_id, "client_secret": client_secret, "grant_type": "authorization_code",
        "code": code, "redirect_uri": redirect_uri,
    }).encode()
    request = Request(f"{DEFAULT_AUTH_BASE_URL}/oauth/token", data=payload, headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    with urlopen(request, timeout=15) as response:
        token = json.loads(response.read().decode("utf-8"))
    if not isinstance(token, dict) or not token.get("access_token"):
        raise ValueError("Hammerhead returned an invalid token response.")
    return token


@dataclass(frozen=True)
class HammerheadConnectionTest:
    is_healthy: bool
    message: str
    activity_count: int | None = None
    activities: tuple[dict[str, str], ...] = ()


def normalise_base_url(endpoint_url: str | None) -> str:
    """Return the documented API base and transparently repair the old V1 URL."""
    base_url = (endpoint_url or DEFAULT_API_BASE_URL).strip().rstrip("/")
    # The first implementation stored the version root.  Keep already-created
    # connections working while moving requests to the documented /v1/api feed.
    if base_url == "https://api.hammerhead.io/v1":
        return DEFAULT_API_BASE_URL
    return base_url


def _activities_url(endpoint_url: str, *, page: int = 1, per_page: int = 100) -> str:
    query = urlencode({"page": max(1, page), "perPage": min(max(per_page, 1), 100)})
    return f"{normalise_base_url(endpoint_url)}/activities?{query}"


def _activity_url(endpoint_url: str, activity_id: str) -> str:
    return f"{normalise_base_url(endpoint_url)}/activities/{quote(activity_id, safe='')}"


def _request_activities(endpoint_url: str, access_token: str, *, page: int = 1, per_page: int = 100) -> tuple[list[dict], int | None]:
    request = Request(
        _activities_url(endpoint_url, page=page, per_page=per_page),
        headers={"Accept": "application/json", "Authorization": f"Bearer {access_token}"},
    )
    with urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)], len(payload)
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("The Hammerhead activity endpoint returned an unexpected response.")
    total = payload.get("totalItems")
    return [item for item in payload["data"] if isinstance(item, dict)], int(total) if isinstance(total, int) else None


def _request_activity_detail(endpoint_url: str, access_token: str, activity_id: str) -> dict:
    """Load the detail object: Hammerhead exposes ``polyline`` there, not in a list summary."""
    request = Request(
        _activity_url(endpoint_url, activity_id),
        headers={"Accept": "application/json", "Authorization": f"Bearer {access_token}"},
    )
    with urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("The Hammerhead activity detail endpoint returned an unexpected response.")
    return payload


def test_connection(endpoint_url: str | None, access_token: str | None, account_identifier: str = "") -> HammerheadConnectionTest:
    if not access_token:
        return HammerheadConnectionTest(False, "A Hammerhead OAuth access token is required.")
    try:
        activities, total = _request_activities(endpoint_url or DEFAULT_API_BASE_URL, access_token, per_page=5)
        preview = tuple({
            "name": str(item.get("name") or "Unnamed activity"),
            "date": str(item.get("createdAt") or item.get("startTime") or item.get("start_date") or "")[:10],
            "distance": f"{float(item.get('distance') or 0) / 1000:.1f} km",
        } for item in activities[:5])
        count = total if total is not None else len(activities)
        return HammerheadConnectionTest(True, f"Connected. Fetched a Hammerhead activity preview ({count} total activities reported).", count, preview)
    except HTTPError as error:
        if error.code == 401:
            return HammerheadConnectionTest(False, "Hammerhead rejected the OAuth access token (HTTP 401).")
        if error.code == 403:
            return HammerheadConnectionTest(False, "The OAuth token does not have access to this Hammerhead account (HTTP 403).")
        if error.code == 404:
            return HammerheadConnectionTest(False, "The Hammerhead account identifier was not found (HTTP 404).")
        return HammerheadConnectionTest(False, f"Hammerhead returned HTTP {error.code} while loading activities.")
    except (URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return HammerheadConnectionTest(False, "Bike Garage could not load activities from Hammerhead.")


def _normalise_activity(item: dict) -> dict:
    # `startedAt` is the authoritative timestamp in Hammerhead's current
    # activity payload. `createdAt` is the upload/completion timestamp and
    # must only be a last-resort fallback.
    started_at = item.get("startedAt") or item.get("startTime") or item.get("start_date")
    # The current ActivitySummary exposes createdAt and duration. createdAt is
    # the completion/import timestamp, not necessarily the ride start. Derive
    # a usable start timestamp when no explicit startTime is supplied.
    if not started_at and item.get("createdAt") and item.get("duration") is not None:
        try:
            ended_at = datetime.fromisoformat(str(item["createdAt"]).replace("Z", "+00:00"))
            duration_ms = float(item["duration"])
            started_at = (ended_at - timedelta(milliseconds=duration_ms)).isoformat().replace("+00:00", "Z")
        except (TypeError, ValueError):
            started_at = item.get("createdAt")
    started_at = started_at or item.get("createdAt")
    raw_type = str(item.get("activityType") or item.get("type") or "RIDE")
    return {
        "id": item.get("id"),
        "name": item.get("name") or "Unnamed Hammerhead activity",
        "start_date": started_at,
        "start_date_local": started_at,
        "distance": item.get("distance") or 0,
        "sport_type": SPORT_TYPE_MAP.get(raw_type.upper(), raw_type),
        "type": SPORT_TYPE_MAP.get(raw_type.upper(), raw_type),
        "map": {"summary_polyline": item.get("polyline")} if item.get("polyline") else {},
        "raw_hammerhead": item,
    }


def fetch_activity_detail(endpoint_url: str | None, access_token: str | None, activity_id: str) -> dict:
    """Fetch one detailed Activity record, including its encoded polyline."""
    if not access_token:
        raise ValueError("A Hammerhead OAuth access token is required.")
    return _normalise_activity(_request_activity_detail(endpoint_url or DEFAULT_API_BASE_URL, access_token, activity_id))


def fetch_activity_fit(endpoint_url: str | None, access_token: str | None, activity_id: str) -> bytes:
    """Download an activity's original FIT file for local hardware parsing.

    Hammerhead deployments have exposed the download under both ``fit`` and
    ``file``; try the documented API shape first and accept only FIT bytes.
    """
    if not access_token:
        raise ValueError("A Hammerhead OAuth access token is required.")
    base = normalise_base_url(endpoint_url or DEFAULT_API_BASE_URL)
    last_error: Exception | None = None
    for suffix in ("fit", "file"):
        request = Request(
            f"{base}/activities/{quote(activity_id, safe='')}/{suffix}",
            headers={"Accept": "application/octet-stream", "Authorization": f"Bearer {access_token}"},
        )
        try:
            with urlopen(request, timeout=30) as response:
                payload = response.read()
            if len(payload) >= 14 and payload[8:12] == b".FIT":
                return payload
            raise ValueError("Hammerhead did not return a FIT file.")
        except (HTTPError, URLError, TimeoutError, ValueError) as error:
            last_error = error
    detail = str(last_error) if last_error else "unknown response"
    raise ValueError(f"Hammerhead FIT download failed: {detail}") from last_error


def fetch_activities(endpoint_url: str | None, access_token: str | None, account_identifier: str = "", *, after: int | None = None) -> list[dict]:
    """Fetch ActivitySummary records quickly; details are loaded only for new records."""
    if not access_token:
        raise ValueError("A Hammerhead OAuth access token is required.")
    records, _ = _request_activities(endpoint_url or DEFAULT_API_BASE_URL, access_token, per_page=100)
    return [_normalise_activity(summary) for summary in records]
