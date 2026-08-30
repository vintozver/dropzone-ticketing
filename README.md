# dropzone-ticketing
Ticketing software for the drop zone (skydiving)

## Web service

### Configuration

Configuration is loaded from the YAML file named by `CONFIG_FILE`, or
`config.yaml` when that variable is not set:

```yaml
mongodb_uri: mongodb://localhost:27017/dropzone_ticketing
registration_mode: false
# List of available time zone names may be obtained by running:
# $ echo "import zoneinfo; print(zoneinfo.available_timezones())" | python3
timezone: UTC
business_name: The Dropzone
google:
  client_id: your-client-id
  secret: your-client-secret
  redirect_uri: https://example.test/authn/google/callback
microsoft:
  client_id: your-client-id
  secret: your-client-secret
  # Optional certificate authentication: the certificate and its private key,
  # both in PEM format, concatenated in any order. When present it replaces `secret`.
  certificate: |
    -----BEGIN CERTIFICATE-----
    ...
    -----END CERTIFICATE-----
    -----BEGIN PRIVATE KEY-----
    ...
    -----END PRIVATE KEY-----
  redirect_uri: https://example.test/authn/microsoft/callback
email:
  smtp: smtp.example.test
  from:
    address: no-reply@example.test
    name: The Dropzone
```

The configured SMTP server is expected to be on a trusted local network; delivery
uses STARTTLS.

### Partner API

Partners call these endpoints with an Authorization header carrying a bearer JWT:

* `GET /api/user/list` returns every user as an array of objects containing
  `external_id` (or `null` when the partner has no mapping), `internal_id`,
  `display_name`, and `email`.
* `PATCH /api/user` updates one mapping. Send a JSON object containing
  `internal_id` and `external_id`; the same values must be present in the
  signed JWT claims.
* `POST /api/ticket/redeem` redeems one ticket by code. Send a JSON object
  containing required `code` and `external_id`, plus optional `display_name`
  and `reason`; required fields must also be present in signed JWT claims.
  Returns `201` with redeemed ticket details (`code`, `internal_id`, and
  `redeemed` with `at`/`by`/`reason`), `404` when the ticket code is unknown,
  and `409` when the ticket is already redeemed.
* `GET /api/report/ticket-redeem/today` returns tickets redeemed today.
* `GET /api/report/ticket-redeem/yesterday` returns tickets redeemed yesterday.

Both endpoints return arrays grouped by user. Each item includes
`internal_id`, `external_id`, `display_name`, and `tickets`.
Each entry in `tickets` includes `internal_id`, `payment`, `purpose`,
and `redeemed` (`at`, `by`, `reason`) details.

The JWT must use a `partner` header containing the partner ObjectId and a `kid`
header selecting a configured key. Every request must include a `dt` claim
formatted as UTC `YYYYMMDDTHHMMSSZ`; it must be within one minute of the
server's current time.
Supported signatures are RS256, PS256, ES256, and EdDSA. Keys are managed by
authenticated administrators at `/admin/partner/list`; PEM public keys and
certificates are converted to DER before storage.

All settings come from this file only; `CONFIG_FILE` merely names it.
If `timezone` is omitted, displayed times default to UTC.

#### Microsoft certificate authentication

A Microsoft certificate can be generated using the following commands:

```console
openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:secp521r1 -out client-key.pem
openssl req -x509 -key client-key.pem -sha256 -days 7300 -subj "/CN=credential" -issuer "/CN=credential" -nodes -out client-crt.pem
```

Combine `client-crt.pem` and `client-key.pem` and add the resulting PEM content
to the `microsoft.certificate` YAML configuration value.

The `client-crt.pem` file must also be uploaded to the Microsoft Azure portal
under **App registration → Certificates & secrets → Certificates**.

### Registration mode

Protected routes require authentication with a registered FIDO2 or Google credential.
To register credentials, set `registration_mode` in the configuration file, restart the
service, then open `/register`, enter a username, and press “Register credential”.
FIDO2 credential registration requests enterprise attestation from the authenticator.
Registration mode disables every other route and should only be enabled during initial
setup: while it is on, only `/register` is served and all other paths answer `403`.

```yaml
# Only during credential registration:
registration_mode: true
```

For local development, the following command starts the WSGI application on port
8000:

```console
python -c 'from wsgiref.simple_server import make_server; from dropzone_ticketing.service import application; make_server("", 8000, application).serve_forever()'
```
