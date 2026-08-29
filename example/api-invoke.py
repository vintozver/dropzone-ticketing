#!/usr/bin/env python3

# openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-521 -out ecdsa-private.pem
# openssl pkey -in ecdsa-private.pem -pubout -out ecdsa-public.pem


import requests
import jwt
from urllib.parse import urlunparse

PRIVATE_KEY_FILE = "ecdsa-private.pem"
HOST = "localhost"

with open(PRIVATE_KEY_FILE, "r") as f:
    private_key = f.read()


def do_patch():
    token = jwt.encode(
        payload={
            'internal_id': '000000000000000000000000',
            'external_id': '~XXX~',
        },
        headers={
            'partner': '000000000000000000000000',
            'kid': 'main',
        },
        key=private_key,
        algorithm="ES256"
    )

    response = requests.request(
        method="PATCH",
        url=urlunparse(("https", HOST, "/api/user", "", "", "")),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )

    print("Status:", response.status_code)
    print(response.text)

def do_list():
    token = jwt.encode(
        payload={},
        headers={
            'partner': '000000000000000000000000',
            'kid': 'main',
        },
        key=private_key,
        algorithm="ES256"
    )

    response = requests.request(
        method="GET",
        url=urlunparse(("https", HOST, "/api/user/list", "", "", "")),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )

    print("Status:", response.status_code)
    print(response.text)

do_patch()
do_list()
