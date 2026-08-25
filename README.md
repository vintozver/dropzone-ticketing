# dropzone-ticketing
Ticketing software for the drop zone (skydiving)

## Web service

### Configuration

Configuration is loaded from the YAML file named by `CONFIG_FILE`, or
`config.yaml` when that variable is not set:

```yaml
mongodb_uri: mongodb://localhost:27017/dropzone_ticketing
authn_session_secret: your-session-secret
registration_mode: false
google:
  client_id: your-client-id
  secret: your-client-secret
  redirect_uri: https://example.test/authn/google/callback
```

Every top level setting can also be given as an environment variable named after the
upper case form of its YAML key (`MONGODB_URI`, `AUTHN_SESSION_SECRET`,
`REGISTRATION_MODE`); the environment takes precedence over the file.

### Registration mode

Protected routes require authentication with a registered FIDO2 or Google credential.
To register credentials, set `REGISTRATION_MODE` while starting the service, then open
`/register`, enter a username, and press “Register credential”. Registration mode
disables authentication and should only be enabled during initial setup.

```console
# Only during credential registration:
export REGISTRATION_MODE=1
```

For local development, the following command starts the WSGI application on port
8000:

```console
python -c 'from wsgiref.simple_server import make_server; from dropzone_ticketing.service import application; make_server("", 8000, application).serve_forever()'
```
