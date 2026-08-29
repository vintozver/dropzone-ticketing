from __future__ import annotations

from http import HTTPStatus

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from mongoengine.errors import ValidationError

from ..model.partner import Partner, PartnerKeyItem
from .http import read_form, render


def _der(value: str, kind: str) -> bytes:
    try:
        if kind == "certificate":
            return x509.load_pem_x509_certificate(value.encode()).public_bytes(
                serialization.Encoding.DER
            )
        return serialization.load_pem_public_key(value.encode()).public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid PEM {kind}.") from exc


def view(environ: dict, partner_id: str | None = None):
    if partner_id is None:
        return render("admin_partners.html", partners=Partner.objects)
    try:
        partner = Partner.objects(id=partner_id).first()
    except (ValidationError, ValueError):
        partner = None
    if partner is None:
        return render("error.html", HTTPStatus.NOT_FOUND, message="Partner not found.")
    return render("admin_partner.html", partner=partner)


def view_partners(environ: dict):
    return view(environ)


def view_partner(environ: dict, partner_id: str):
    return view(environ, partner_id)


def create(environ: dict):
    form = read_form(environ)
    name = form.get("display_name", "").strip()
    if not name:
        raise ValueError("Display name is required.")
    Partner(display_name=name).save()
    return HTTPStatus.SEE_OTHER, [("Location", "/admin/partners")], b""


def update(environ: dict, partner_id: str):
    try:
        partner = Partner.objects(id=partner_id).first()
    except (ValidationError, ValueError):
        partner = None
    if partner is None:
        return render("error.html", HTTPStatus.NOT_FOUND, message="Partner not found.")
    form = read_form(environ)
    if "display_name" in form:
        display_name = form["display_name"].strip()
        if not display_name:
            raise ValueError("Display name is required.")
        partner.display_name = display_name
    key_id = form.get("key_id", "").strip()
    if key_id:
        if any(item.id == key_id for item in partner.keyset):
            raise ValueError("A key with this id already exists.")
        public_key = form.get("public_key", "").strip()
        certificate = form.get("certificate", "").strip()
        if not public_key and not certificate:
            raise ValueError("A public key or certificate is required.")
        partner.keyset.append(
            PartnerKeyItem(
                id=key_id,
                pub=_der(public_key, "public key") if public_key else None,
                crt=_der(certificate, "certificate") if certificate else None,
            )
        )
    delete_id = form.get("delete_key", "").strip()
    if delete_id:
        partner.keyset = [item for item in partner.keyset if item.id != delete_id]
    partner.save()
    return HTTPStatus.SEE_OTHER, [("Location", f"/admin/partners/{partner.id}")], b""
