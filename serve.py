"""Production entrypoint: waitress WSGI server, listens on localhost only (the Cloudflare tunnel connects to it)."""
import os
from waitress import serve
import app as tb

if __name__ == "__main__":
    host, port = os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8090"))
    print(f"Trackobee on http://{host}:{port}  db={'Postgres/Supabase' if tb.PG else 'SQLite (local file)'}", flush=True)
    serve(tb.app, host=host, port=port, threads=int(os.environ.get("TB_THREADS", "8")))
