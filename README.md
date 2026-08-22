# dropzone-ticketing
Ticketing software for the drop zone (skydiving)

## Web service

Set `DROPZONE_MONGO_DB` and `DROPZONE_MONGO_HOST` if the MongoDB defaults
(`dropzone_ticketing` and `mongodb://localhost:27017`) are not suitable. For
local development, the following command starts the WSGI application on port
8000:

```console
python -c 'from wsgiref.simple_server import make_server; from dropzone_ticketing.service import application; make_server("", 8000, application).serve_forever()'
```
