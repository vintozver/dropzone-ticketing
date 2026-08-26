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
  # Optional certificate authentication: RSA private key in PEM format, optionally
  # followed by the matching certificate. When present it replaces `secret`.
  key: |
    -----BEGIN PRIVATE KEY-----
    ...
    -----END PRIVATE KEY-----
  redirect_uri: https://example.test/authn/microsoft/callback
```

All settings come from this file only; `CONFIG_FILE` merely names it.
If `timezone` is omitted, displayed times default to UTC.

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
