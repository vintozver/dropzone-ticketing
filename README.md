# dropzone-ticketing
Ticketing software for the drop zone (skydiving)

## Web service

### Configuration

Configuration is loaded from the YAML file named by `CONFIG_FILE`, or
`config.yaml` when that variable is not set:

```yaml
mongodb_uri: mongodb://localhost:27017/dropzone_ticketing
google:
  credential_id: your-client-id
  secret: your-client-secret
  redirect_uri: https://example.test/authn/google/callback
```

### Registration mode

Protected routes require FIDO2 authentication with a credential registered in MongoDB.
To register credentials, set `REGISTRATION_MODE` while starting the service, then open
`/register`, enter a username, and press “Register credential”. Registration mode
disables authentication and should only be enabled during initial setup.

```console
export REGISTRATION_MODE=1
```

For local development, the following command starts the WSGI application on port
8000:

```console
python -c 'from wsgiref.simple_server import make_server; from dropzone_ticketing.service import application; make_server("", 8000, application).serve_forever()'
```
