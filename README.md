# dropzone-ticketing
Ticketing software for the drop zone (skydiving)

## Web service

Set `MONGODB_CONNECTION_STRING` to the MongoDB connection string used by the
web service. Protected routes require FIDO2 authentication with a credential registered in
MongoDB. To register credentials, set `AUTH_REGISTER` while starting the
service, then open `/register`, enter a username, and press “Register credential”.
Registration mode disables authentication and should only be enabled during
initial setup.

```console
export MONGODB_CONNECTION_STRING=mongodb://localhost:27017/dropzone_ticketing
# Only during credential registration:
export AUTH_REGISTER=1
```

For local development, the following command starts the WSGI application on port
8000:

```console
python -c 'from wsgiref.simple_server import make_server; from dropzone_ticketing.service import application; make_server("", 8000, application).serve_forever()'
```
