from __future__ import annotations

import base64
import binascii
import hmac
import json
import secrets
import traceback
from hashlib import sha256
from http import HTTPStatus
from time import time
from urllib.parse import quote

from cryptography import x509
from cryptography.x509.oid import NameOID
from fido2.attestation import AttestationResult, AttestationVerifier, UntrustedAttestation
from fido2.server import Fido2Server
from fido2.webauthn import (
    AttestationObject,
    AuthenticatorData,
    CollectedClientData,
    PublicKeyCredentialRpEntity,
    PublicKeyCredentialUserEntity,
)

from .config import authn_config, session_secret
from .http import error, read_form, render
from dropzone_ticketing.model.credential import Fido2Credential

AUTHN_CHALLENGE_COOKIE = "authn_challenge"
AUTHN_SESSION_COOKIE = "authn_session"
_CHALLENGE_TTL_SECONDS = 300
_COOKIE_MAX_AGE_SECONDS = 12 * 60 * 60
_YUBIKEY_SERIAL_OID = x509.ObjectIdentifier("1.3.6.1.4.1.41482.3.7")
_YUBICO_FIDO_ROOT_CA = b"""-----BEGIN CERTIFICATE-----
MIIDHjCCAgagAwIBAgIEG0BT9zANBgkqhkiG9w0BAQsFADAuMSwwKgYDVQQDEyNZ
dWJpY28gVTJGIFJvb3QgQ0EgU2VyaWFsIDQ1NzIwMDYzMTAgFw0xNDA4MDEwMDAw
MDBaGA8yMDUwMDkwNDAwMDAwMFowLjEsMCoGA1UEAxMjWXViaWNvIFUyRiBSb290
IENBIFNlcmlhbCA0NTcyMDA2MzEwggEiMA0GCSqGSIb3DQEBAQUAA4IBDwAwggEK
AoIBAQC/jwYuhBVlqaiYWEMsrWFisgJ+PtM91eSrpI4TK7U53mwCIawSDHy8vUmk
5N2KAj9abvT9NP5SMS1hQi3usxoYGonXQgfO6ZXyUA9a+KAkqdFnBnlyugSeCOep
8EdZFfsaRFtMjkwz5Gcz2Py4vIYvCdMHPtwaz0bVuzneueIEz6TnQjE63Rdt2zbw
nebwTG5ZybeWSwbzy+BJ34ZHcUhPAY89yJQXuE0IzMZFcEBbPNRbWECRKgjq//qT
9nmDOFVlSRCt2wiqPSzluwn+v+suQEBsUjTGMEd25tKXXTkNW21wIWbxeSyUoTXw
LvGS6xlwQSgNpk2qXYwf8iXg7VWZAgMBAAGjQjBAMB0GA1UdDgQWBBQgIvz0bNGJ
hjgpToksyKpP9xv9oDAPBgNVHRMECDAGAQH/AgEAMA4GA1UdDwEB/wQEAwIBBjAN
BgkqhkiG9w0BAQsFAAOCAQEAjvjuOMDSa+JXFCLyBKsycXtBVZsJ4Ue3LbaEsPY4
MYN/hIQ5ZM5p7EjfcnMG4CtYkNsfNHc0AhBLdq45rnT87q/6O3vUEtNMafbhU6kt
hX7Y+9XFN9NpmYxr+ekVY5xOxi8h9JDIgoMP4VB1uS0aunL1IGqrNooL9mmFnL2k
LVVee6/VR6C5+KSTCMCWppMuJIZII2v9o4dkoZ8Y7QRjQlLfYzd3qGtKbw7xaF1U
sG/5xUb/Btwb2X2g4InpiB/yt/3CpQXpiWX/K4mBvUKiGn05ZsqeY1gx4g0xLBqc
U9psmyPzK+Vsgw2jeRQ5JlKDyqE0hebfC1tvFu0CCrJFcw==
-----END CERTIFICATE-----
"""


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def _signature(payload: str) -> str:
    return _b64encode(hmac.new(session_secret(), payload.encode("ascii"), sha256).digest())


def _signed(payload: dict[str, object]) -> str:
    encoded = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return f"{encoded}.{_signature(encoded)}"


def _unsign(value: str) -> dict[str, object] | None:
    try:
        encoded, signature = value.split(".", 1)
    except ValueError:
        return None
    if not hmac.compare_digest(signature, _signature(encoded)):
        return None
    try:
        return json.loads(_b64decode(encoded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def _cookies(environ: dict) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for item in environ.get("HTTP_COOKIE", "").split(";"):
        if "=" in item:
            name, value = item.strip().split("=", 1)
            cookies[name] = value
    return cookies


def _cookie(name: str, value: str, *, max_age: int = _COOKIE_MAX_AGE_SECONDS, path: str = "/") -> tuple[str, str]:
    return (
        "Set-Cookie",
        f"{name}={quote(value, safe='')}; Max-Age={max_age}; Path={path}; Secure; HttpOnly; SameSite=Strict",
    )


def _clear_cookie(name: str, *, path: str = "/") -> tuple[str, str]:
    return ("Set-Cookie", f"{name}=; Max-Age=0; Path={path}; Secure; HttpOnly; SameSite=Strict")


def _request_host(environ: dict) -> str:
    return environ.get("HTTP_HOST") or environ.get("SERVER_NAME") or "localhost"


def _rp_id(environ: dict) -> str:
    return _request_host(environ).split(":", 1)[0]


def _origin(environ: dict) -> str:
    scheme = environ.get("wsgi.url_scheme") or "http"
    return f"{scheme}://{_request_host(environ)}"


def _challenge_from_cookie(environ: dict) -> bytes | None:
    payload = _unsign(_cookies(environ).get(AUTHN_CHALLENGE_COOKIE, ""))
    if not payload or time() - float(payload.get("issued", 0)) > _CHALLENGE_TTL_SECONDS:
        return None
    challenge = payload.get("challenge")
    if not isinstance(challenge, str):
        return None
    try:
        return _b64decode(challenge)
    except ValueError:
        return None


def _is_authenticated(environ: dict) -> bool:
    if authn_config().register:
        return True
    payload = _unsign(_cookies(environ).get(AUTHN_SESSION_COOKIE, ""))
    if not payload:
        return False
    serial = payload.get("serial")
    issued = float(payload.get("issued", 0))
    return (
        isinstance(serial, str)
        and _find_credential(serial) is not None
        and time() - issued <= _COOKIE_MAX_AGE_SECONDS
    )


def _find_credential(encoded_id: str) -> Fido2Credential | None:
    try:
        credential_id = _b64decode(encoded_id)
    except (ValueError, binascii.Error):
        return None
    return Fido2Credential.objects(credential_id=credential_id).first()


def _extract_serial(cert: x509.Certificate) -> str | None:
    def _decode_der_integer(encoded: bytes) -> int | None:
        if len(encoded) < 3 or encoded[0] != 0x02:
            return None
        offset = 2
        first_length = encoded[1]
        if first_length & 0x80:
            length_octets = first_length & 0x7F
            if length_octets == 0 or len(encoded) <= 2 + length_octets:
                return None
            length = int.from_bytes(encoded[2 : 2 + length_octets], "big")
            offset = 2 + length_octets
        else:
            length = first_length
        if length <= 0 or offset + length != len(encoded):
            return None
        return int.from_bytes(encoded[offset : offset + length], "big")

    try:
        extension = cert.extensions.get_extension_for_oid(_YUBIKEY_SERIAL_OID).value
        if isinstance(extension, x509.UnrecognizedExtension):
            serial = _decode_der_integer(extension.value)
            if serial is not None:
                return str(serial)
    except x509.ExtensionNotFound:
        pass
    values = cert.subject.get_attributes_for_oid(NameOID.SERIAL_NUMBER)
    if not values:
        return None
    return values[0].value.strip()


def _is_yubico_cert(cert: x509.Certificate) -> bool:
    names = [attribute.value for attribute in cert.subject] + [attribute.value for attribute in cert.issuer]
    return any("Yubico" in value or "YubiKey" in value for value in names)


class _YubiKeyAttestationVerifier(AttestationVerifier):
    def __init__(self, allowed_serials: frozenset[str]):
        super().__init__()
        self.allowed_serials = allowed_serials
        self.serial: str | None = None

    def ca_lookup(self, attestation_result: AttestationResult, auth_data: AuthenticatorData) -> bytes | None:
        if not attestation_result.trust_path:
            raise UntrustedAttestation("YubiKey attestation certificate is required")
        cert = x509.load_der_x509_certificate(attestation_result.trust_path[0])
        serial = _extract_serial(cert)
        if not serial or not _is_yubico_cert(cert):
            raise UntrustedAttestation("Authenticator is not a YubiKey")
        if serial not in self.allowed_serials:
            raise UntrustedAttestation("YubiKey serial number is not allowed")
        self.serial = serial
        return _YUBICO_FIDO_ROOT_CA


def _verify_yubikey_attestation(
    attestation_object: bytes,
    client_data_json: bytes,
    challenge: bytes,
    origin: str,
    allowed_serials: frozenset[str],
) -> str:
    if not allowed_serials:
        raise ValueError("No YubiKey serial numbers are configured.")
    client_data = CollectedClientData(client_data_json)
    if client_data.type != CollectedClientData.TYPE.CREATE:
        raise ValueError("Unexpected WebAuthn response type.")
    if client_data.challenge != challenge:
        raise ValueError("Unexpected WebAuthn challenge.")
    if client_data.origin != origin:
        raise ValueError("Unexpected WebAuthn origin.")

    verifier = _YubiKeyAttestationVerifier(allowed_serials)
    verifier.verify_attestation(AttestationObject(attestation_object), client_data.hash)
    if verifier.serial is None:
        raise ValueError("YubiKey serial number was not present in attestation.")
    return verifier.serial


def begin_authn(environ: dict):
    challenge = secrets.token_bytes(32)
    if authn_config().register:
        mode = "register"
        options = None
        username = ""
    else:
        mode = "authenticate"
        server = _server(environ)
        credentials = list(Fido2Credential.objects())
        options, _state = server.authenticate_begin(
            [_credential_data(credential) for credential in credentials],
            challenge=challenge,
        )
        allow_credentials = [
            _b64encode(credential.credential_id)
            for credential in credentials
        ]
        username = ""
    auth_path = "/register" if mode == "register" and environ.get("PATH_INFO") == "/register" else "/authn"
    status, headers, body = render(
        "authn.html",
        challenge=_b64encode(challenge),
        rp_id=_rp_id(environ),
        mode=mode,
        allow_credentials=allow_credentials if not authn_config().register else [],
        username=username,
        auth_path=auth_path,
    )
    headers.append(_cookie(AUTHN_CHALLENGE_COOKIE, _signed({"challenge": _b64encode(challenge), "issued": time()}), max_age=_CHALLENGE_TTL_SECONDS, path=auth_path))
    return status, headers, body


def complete_authn(environ: dict):
    if authn_config().register:
        return register(environ)
    challenge = _challenge_from_cookie(environ)
    if challenge is None:
        return error(HTTPStatus.FORBIDDEN, "Authentication challenge is missing or expired.")
    form = read_form(environ)
    try:
        response = {
            "id": form.get("id", ""),
            "rawId": form.get("rawId", ""),
            "response": {
                "clientDataJSON": form.get("clientDataJSON", ""),
                "authenticatorData": form.get("authenticatorData", ""),
                "signature": form.get("signature", ""),
                "userHandle": form.get("userHandle", None),
            },
            "type": "public-key",
        }
        credential_id = _b64decode(form.get("rawId", ""))
        credential = _find_credential(form.get("rawId", ""))
        if credential is None:
            raise ValueError("Unknown credential.")
        server = _server(environ)
        stored = _credential_data(credential)
        server.authenticate_complete(
            {"challenge": _b64encode(challenge), "user_verification": None},
            [stored],
            response=response,
        )
        serial = _b64encode(credential_id)
    except (binascii.Error, UntrustedAttestation, ValueError):
        return error(HTTPStatus.FORBIDDEN, "FIDO2 authentication failed.", traceback.format_exc())
    status, headers, body = error(HTTPStatus.SEE_OTHER, "Authenticated.")
    headers = [
        ("Location", "/"),
        _cookie(AUTHN_SESSION_COOKIE, _signed({"serial": serial, "issued": time()})),
        _clear_cookie(AUTHN_CHALLENGE_COOKIE, path="/authn"),
    ]
    return status, headers, body


def _server(environ: dict) -> Fido2Server:
    rp = PublicKeyCredentialRpEntity("dropzone-ticketing", _rp_id(environ))
    return Fido2Server(rp, verify_origin=lambda origin: origin == _origin(environ))


def _credential_data(credential: Fido2Credential):
    from fido2.webauthn import AttestedCredentialData

    return AttestedCredentialData(credential.credential_data)


def register(environ: dict):
    if not authn_config().register:
        return error(HTTPStatus.FORBIDDEN, "Credential registration is disabled.")
    form = read_form(environ)
    username = form.get("username", "").strip()
    if not username:
        return error(HTTPStatus.BAD_REQUEST, "Username is required.")
    challenge = _challenge_from_cookie(environ)
    if challenge is None:
        return error(HTTPStatus.FORBIDDEN, "Registration challenge is missing or expired.")
    try:
        server = _server(environ)
        user = PublicKeyCredentialUserEntity(username=username, id=secrets.token_bytes(16), display_name=username)
        response = {
            "id": form.get("id", ""),
            "rawId": form.get("rawId", ""),
            "response": {"clientDataJSON": form.get("clientDataJSON", ""), "attestationObject": form.get("attestationObject", "")},
            "type": "public-key",
        }
        auth_data = server.register_complete(
            {"challenge": _b64encode(challenge), "user_verification": None},
            response=response,
        )
        credential_data = bytes(auth_data.credential_data)
        Fido2Credential(
            username=username,
            credential_id=auth_data.credential_data.credential_id,
            credential_data=credential_data,
        ).save()
    except (binascii.Error, ValueError):
        return error(HTTPStatus.FORBIDDEN, "FIDO2 registration failed.", traceback.format_exc())
    status, headers, body = error(HTTPStatus.SEE_OTHER, "Credential registered.")
    auth_path = "/register" if environ.get("PATH_INFO") == "/register" else "/authn"
    headers = [("Location", "/authn"), _clear_cookie(AUTHN_CHALLENGE_COOKIE, path=auth_path)]
    return status, headers, body


def logout():
    status, headers, body = error(HTTPStatus.SEE_OTHER, "Signed out.")
    headers = [("Location", "/authn"), _clear_cookie(AUTHN_SESSION_COOKIE)]
    return status, headers, body


def require_auth(environ: dict):
    if authn_config().register:
        return None
    if _is_authenticated(environ):
        return None
    status, headers, body = error(HTTPStatus.SEE_OTHER, "Authentication required.")
    headers = [("Location", "/authn")]
    return status, headers, body
