# dropzone-ticketing
Ticketing software for the drop zone (skydiving)

## Web service

Set `MONGODB_CONNECTION_STRING` to the MongoDB connection string used by the
web service. Protected routes require FIDO2 authentication with a YubiKey whose
attestation certificate serial number is listed in `AUTHN_YUBIKEY_IDS` as a
comma-separated list, for example:

```console
export MONGODB_CONNECTION_STRING=mongodb://localhost:27017/dropzone_ticketing
export AUTHN_YUBIKEY_IDS=1234567,7654321
```

For local development, the following command starts the WSGI application on port
8000:

```console
python -c 'from wsgiref.simple_server import make_server; from dropzone_ticketing.service import application; make_server("", 8000, application).serve_forever()'
```
