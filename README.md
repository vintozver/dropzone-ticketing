# dropzone-ticketing
Ticketing software for the drop zone (skydiving)

## Web service

Set `MONGODB_URI` to the MongoDB connection string used by the
web service. Protected routes require FIDO2 authentication with a credential registered in
MongoDB. To register credentials, set `REGISTRATION_MODE` while starting the
service, then open `/register`, enter a username, and press “Register credential”.
Registration mode disables authentication and should only be enabled during
initial setup.

Configuration is loaded from the YAML file named by `CONFIG_FILE`, or
`config.yaml` when that variable is not set. Environment variables override
values from the file. The file uses `mongodb_uri` and a nested `google` object:

```yaml
mongodb_uri: mongodb://localhost:27017/dropzone_ticketing
google:
  credential_id: your-client-id
  secret: your-client-secret
```

```console
export MONGODB_URI=mongodb://localhost:27017/dropzone_ticketing
# For Google authentication, configure an OAuth web client:
export GOOGLE_CLIENT_ID=...
export GOOGLE_CLIENT_SECRET=...
export GOOGLE_REDIRECT_URI=https://example.test/authn/google/callback
# Only during credential registration:
export REGISTRATION_MODE=1
```

For local development, the following command starts the WSGI application on port
8000:

```console
python -c 'from wsgiref.simple_server import make_server; from dropzone_ticketing.service import application; make_server("", 8000, application).serve_forever()'
```
