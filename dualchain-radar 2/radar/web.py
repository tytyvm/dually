"""Authenticated browser UI. Deploy one process behind an HTTPS reverse proxy."""
import collections
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from .dashboard import Dashboard
from .domain import Config

STATIC = Path(__file__).parent / "static"


def create_app(manager, password, origin):
    parsed = urlparse(origin)
    local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
    if len(password) < 16:
        raise ValueError("DASHBOARD_PASSWORD must have at least 16 characters")
    if not parsed.netloc or parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.username:
        raise ValueError("DASHBOARD_ORIGIN must be the app's origin without a path")
    if parsed.scheme != "https" and not (local and parsed.scheme == "http"):
        raise ValueError("Public dashboard requires an HTTPS origin")
    origin = origin.rstrip("/")
    secure = parsed.scheme == "https"
    expected = hashlib.sha256(password.encode()).digest()
    sessions = {}
    attempts = collections.deque(maxlen=1000)
    auth_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(app):
        if manager.meta["collect_requested"]:
            manager.start(resume=False)
        yield
        manager.shutdown()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"})
        if secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    def session(request):
        sid = request.cookies.get("radar_session", "")
        with auth_lock:
            item = sessions.get(sid)
            if not item or item["expires"] < time.time():
                sessions.pop(sid, None)
                raise HTTPException(401, "Sign in to continue")
            return item

    def origin_check(request):
        if request.headers.get("origin") != origin:
            raise HTTPException(403, "Invalid origin")

    def mutation(request):
        origin_check(request)
        item = session(request)
        if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), item["csrf"]):
            raise HTTPException(403, "Invalid session token")

    async def body(request):
        # Bound streamed bytes too: do not trust Content-Length.
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 8192:
                raise HTTPException(413, "Request too large")
        try:
            result = json.loads(data)
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except (ValueError, TypeError):
            raise HTTPException(400, "Expected a JSON object")

    @app.get("/healthz")
    def health():
        return {"ok": True}

    @app.get("/assets/{name}")
    def asset(name):
        if name not in {"app.css", "app.js", "login.js", "favicon.svg"}:
            raise HTTPException(404)
        return FileResponse(STATIC / name)

    @app.get("/login")
    def login_page():
        return FileResponse(STATIC / "login.html")

    @app.get("/")
    def index(request: Request):
        try:
            session(request)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        return FileResponse(STATIC / "index.html")

    @app.post("/api/login")
    async def login(request: Request):
        origin_check(request)
        now = time.time()
        peer = request.client.host if request.client else "unknown"
        with auth_lock:
            while attempts and now - attempts[0][0] > 900:
                attempts.popleft()
            if sum(ip == peer for _, ip in attempts) >= 10 or sum(now-t < 60 for t,_ in attempts) >= 50:
                raise HTTPException(429, "Too many attempts. Try again in 15 minutes.")
            attempts.append((now, peer))
        data = await body(request)
        supplied = str(data.get("password", ""))
        if not hmac.compare_digest(hashlib.sha256(supplied.encode()).digest(), expected):
            raise HTTPException(401, "Incorrect password")
        sid = secrets.token_urlsafe(32)
        with auth_lock:
            for k in list(sessions):
                if sessions[k]["expires"] < now:
                    del sessions[k]
            if len(sessions) >= 100:
                sessions.pop(next(iter(sessions)))
            sessions[sid] = {"expires": now + 43200, "csrf": secrets.token_urlsafe(32)}
            # A successful login clears this peer's failed attempts.
            kept = [(t,ip) for t,ip in attempts if ip != peer]
            attempts.clear()
            attempts.extend(kept)
        response = JSONResponse({"ok": True})
        response.set_cookie("radar_session", sid, max_age=43200, httponly=True, secure=secure, samesite="strict", path="/")
        return response

    @app.post("/api/logout")
    def logout(request: Request):
        mutation(request)
        with auth_lock:
            sessions.pop(request.cookies.get("radar_session"), None)
        response = JSONResponse({"ok": True})
        response.delete_cookie("radar_session", secure=secure, httponly=True, samesite="strict")
        return response

    @app.get("/api/status")
    def status(request: Request, demo: bool = False, experiment: str | None = None):
        item = session(request)
        try:
            result = manager.demo() if demo else manager.status(experiment)
            result["csrf"] = item["csrf"]
            return result
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/control/{action}")
    def control(action: str, request: Request):
        mutation(request)
        actions = {"start": manager.start, "pause": manager.pause, "stop": manager.stop_collection}
        if action not in actions:
            raise HTTPException(404)
        try:
            actions[action]()
            return {"ok": True}
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.post("/api/experiments")
    async def experiment(request: Request):
        mutation(request)
        data = await body(request)
        try:
            key = manager.new_experiment(data)
            return {"id": key}
        except (ValueError, TypeError) as e:
            raise HTTPException(409, str(e))

    @app.get("/api/export/{kind}")
    def export(kind: str, request: Request, experiment: str | None = None):
        session(request)
        if kind not in ("observations", "trades"):
            raise HTTPException(404)
        with manager.lock:
            if experiment and experiment not in manager.meta["experiments"]:
                raise HTTPException(404)
            e = manager.engine(experiment)
            try:
                # Bounded download. Backups handle larger archives.
                if kind == "observations":
                    rows = e.conn.execute("SELECT data FROM (SELECT ts,id,data FROM snapshots ORDER BY ts DESC,id DESC LIMIT 50000) ORDER BY ts,id")
                    output = "".join(r[0] + "\n" for r in rows)
                else:
                    rows = e.conn.execute("SELECT ts,kind,data FROM audit WHERE kind IN ('buy','sell','exit_blocked') ORDER BY id DESC LIMIT 50000")
                    output = "".join(json.dumps({"ts": ts, "kind": k, **json.loads(d)}) + "\n" for ts,k,d in rows)
                return Response(output, media_type="application/x-ndjson", headers={
                    "Content-Disposition": f'attachment; filename="{kind}.jsonl"'})
            finally:
                e.close()

    return app


def main():
    import uvicorn
    password = os.environ.get("DASHBOARD_PASSWORD", "")
    origin = os.environ.get("DASHBOARD_ORIGIN") or os.environ.get("RENDER_EXTERNAL_URL", "http://127.0.0.1:8080")
    manager = Dashboard(os.environ.get("RADAR_DATA_DIR", "data"), Config.load(os.environ.get("RADAR_CONFIG", "config.toml")))
    app = create_app(manager, password, origin)
    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8080")),
                proxy_headers=False, workers=1, timeout_graceful_shutdown=25)


if __name__ == "__main__":
    main()

