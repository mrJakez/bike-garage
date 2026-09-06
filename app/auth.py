from __future__ import annotations

import os
import time
from typing import Any

from fastapi import Request
from webauthn import (
    base64url_to_bytes,
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorAttachment,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)


REGISTRATION_ENV = "BIKE_GARAGE_PASSKEY_REGISTRATION_ENABLED"


def registration_enabled() -> bool:
    return os.getenv(REGISTRATION_ENV, "false").strip().lower() in {"1", "true", "yes", "on"}


def relying_party(request: Request) -> tuple[str, str]:
    """Return the stable WebAuthn RP ID and origin for this deployment."""
    host = request.url.hostname or "localhost"
    rp_id = (os.getenv("BIKE_GARAGE_PASSKEY_RP_ID", "").strip() or host).lower()
    origin = os.getenv("BIKE_GARAGE_PASSKEY_ORIGIN", "").strip().rstrip("/") or str(request.base_url).rstrip("/")
    return rp_id, origin


def has_passkey(db: Any) -> bool:
    return db.execute("SELECT 1 FROM passkeys LIMIT 1").fetchone() is not None


def issue_registration_options(request: Request, user: Any) -> dict[str, Any]:
    rp_id, _ = relying_party(request)
    options = generate_registration_options(
        rp_id=rp_id,
        rp_name="Bike Garage",
        user_id=str(user["id"]).encode(),
        user_name="bike-garage",
        user_display_name=str(user["display_name"]),
        authenticator_selection=AuthenticatorSelectionCriteria(
            authenticator_attachment=AuthenticatorAttachment.PLATFORM,
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
    )
    request.session["passkey_registration_challenge"] = bytes_to_base64url(options.challenge)
    request.session["passkey_registration_created_at"] = int(time.time())
    return options_to_json(options)


def complete_registration(request: Request, credential: dict[str, Any], user: Any) -> tuple[str, bytes, int]:
    challenge = request.session.get("passkey_registration_challenge")
    created_at = int(request.session.get("passkey_registration_created_at") or 0)
    if not challenge or time.time() - created_at > 300:
        raise ValueError("The registration request expired. Please try again.")
    rp_id, origin = relying_party(request)
    verified = verify_registration_response(
        credential=credential,
        expected_challenge=base64url_to_bytes(challenge),
        expected_rp_id=rp_id,
        expected_origin=origin,
        require_user_verification=True,
    )
    request.session.pop("passkey_registration_challenge", None)
    request.session.pop("passkey_registration_created_at", None)
    return bytes_to_base64url(verified.credential_id), verified.credential_public_key, verified.sign_count


def issue_authentication_options(request: Request, passkey: Any) -> str:
    rp_id, _ = relying_party(request)
    options = generate_authentication_options(
        rp_id=rp_id,
        allow_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(passkey["credential_id"]))],
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    request.session["passkey_authentication_challenge"] = bytes_to_base64url(options.challenge)
    request.session["passkey_authentication_created_at"] = int(time.time())
    return options_to_json(options)


def complete_authentication(request: Request, credential: dict[str, Any], passkey: Any) -> int:
    challenge = request.session.get("passkey_authentication_challenge")
    created_at = int(request.session.get("passkey_authentication_created_at") or 0)
    if not challenge or time.time() - created_at > 300:
        raise ValueError("The sign-in request expired. Please try again.")
    rp_id, origin = relying_party(request)
    verified = verify_authentication_response(
        credential=credential,
        expected_challenge=base64url_to_bytes(challenge),
        expected_rp_id=rp_id,
        expected_origin=origin,
        credential_public_key=bytes(passkey["credential_public_key"]),
        credential_current_sign_count=int(passkey["sign_count"]),
        require_user_verification=True,
    )
    request.session.pop("passkey_authentication_challenge", None)
    request.session.pop("passkey_authentication_created_at", None)
    return verified.new_sign_count
