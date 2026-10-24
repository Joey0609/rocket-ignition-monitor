import asyncio
import contextlib
import json
import os
import secrets
import subprocess
import time
import uuid
from pathlib import Path

from fastapi import (
    FastAPI,
    File,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from .core import DEFAULTS, Monitor, Store, validate_stream

ROOT = Path(__file__).parent
DATA = Path(os.getenv("DATA_DIR", "./data")).resolve()
DATA.mkdir(parents=True, exist_ok=True)
DATA.chmod(0o700)
UPLOADS = DATA / "uploads"
UPLOADS.mkdir(exist_ok=True)
token_path = DATA / "admin-token"
TOKEN = os.getenv("ADMIN_TOKEN") or (
    token_path.read_text().strip() if token_path.exists() else secrets.token_urlsafe(32)
)
if not os.getenv("ADMIN_TOKEN"):
    token_path.write_text(TOKEN)
    token_path.chmod(0o600)
store = Store(DATA)
monitor = Monitor(store)
sessions = {}
login_attempts = {}


@contextlib.asynccontextmanager
async def lifespan(app):
    print(
        f"Ignition Monitor: admin token is in {token_path} (or ADMIN_TOKEN environment variable)",
        flush=True,
    )
    yield
    await asyncio.to_thread(monitor.stop)
    monitor.commands.close()


app = FastAPI(
    title="Ignition Monitor",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.middleware("http")
async def security(request: Request, call_next):
    if request.url.path.startswith("/api/") or request.url.path == "/preview.mjpg":
        if request.url.path not in ("/api/login", "/api/health"):
            if not authorized(
                request.cookies.get("ignition_session"),
                request.headers.get("authorization"),
            ):
                return JSONResponse({"detail": "请先登录"}, status_code=401)
        if request.method not in ("GET", "HEAD"):
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "跨域请求被拒绝"}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'"
    )
    if request.url.path.startswith("/api/") or request.url.path == "/preview.mjpg":
        response.headers["Cache-Control"] = "no-store"
    return response


def authorized(cookie, bearer=None):
    if bearer and bearer.startswith("Bearer "):
        return secrets.compare_digest(bearer[7:].encode(), TOKEN.encode())
    return cookie in sessions and sessions[cookie] > time.time()


class Login(BaseModel):
    token: str = Field(max_length=256)


@app.post("/api/login")
async def login(body: Login, request: Request):
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    attempts = [t for t in login_attempts.get(ip, []) if now - t < 60]
    if len(attempts) >= 10:
        raise HTTPException(429, "尝试次数过多，请稍后重试")
    login_attempts[ip] = attempts + [now]
    if not secrets.compare_digest(body.token.encode(), TOKEN.encode()):
        raise HTTPException(401, "管理令牌不正确")
    for expired in [key for key, expiry in sessions.items() if expiry <= now]:
        sessions.pop(expired, None)
    session = secrets.token_urlsafe(32)
    sessions[session] = now + 86400
    response = JSONResponse({"ok": True})
    response.set_cookie(
        "ignition_session",
        session,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
        max_age=86400,
    )
    return response


@app.post("/api/logout")
async def logout(request: Request):
    sessions.pop(request.cookies.get("ignition_session"), None)
    response = JSONResponse({"ok": True})
    response.delete_cookie("ignition_session")
    return response


@app.get("/api/health")
async def health():
    return {"ok": True, "version": "1.0.0"}


@app.get("/api/status")
async def status():
    return monitor.snapshot()


@app.get("/api/events")
async def events():
    return store.events()


@app.get("/api/config")
async def config():
    return store.config


class Config(BaseModel):
    roi: list[float] = Field(default=DEFAULTS["roi"], min_length=4, max_length=4)
    candidate_threshold: float = Field(default=0.0005, gt=0, le=1)
    strong_threshold: float = Field(default=0.01, gt=0, le=1)
    min_warm_score: float = Field(default=0.0003, ge=0, le=1)
    commands_enabled: bool = False
    confirm_command: str = Field(default="", max_length=4096)
    retract_command: str = Field(default="", max_length=4096)
    command_timeout: int = Field(default=10, ge=1, le=120)

    @model_validator(mode="after")
    def valid(self):
        x1, y1, x2, y2 = self.roi
        if not (
            0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1 and (x2 - x1) * (y2 - y1) < 0.98
        ):
            raise ValueError("ROI must leave background outside")
        if self.strong_threshold < self.candidate_threshold:
            raise ValueError("strong threshold must be >= candidate threshold")
        return self


@app.put("/api/config")
async def save_config(body: Config):
    if monitor.snapshot()["phase"] in ("running", "connecting", "reconnecting"):
        raise HTTPException(409, "请先停止监测再修改配置")
    store.save(body.model_dump())
    store.event("config_saved")
    return {"ok": True}


class Start(BaseModel):
    kind: str
    url: str = Field(default="", max_length=2048)
    file_id: str = Field(default="", max_length=64)


@app.post("/api/start")
async def start(body: Start):
    if body.kind == "rtmp":
        try:
            source = validate_stream(body.url)
        except ValueError as e:
            raise HTTPException(422, str(e))
        # Never persist embedded credentials or query tokens into events.
        from urllib.parse import urlsplit

        p = urlsplit(source)
        name = f"{p.scheme}://{p.hostname}/…"
    elif body.kind == "file":
        if len(body.file_id) != 32 or any(
            c not in "0123456789abcdef" for c in body.file_id
        ):
            raise HTTPException(422, "无效视频 ID")
        source = str(UPLOADS / (body.file_id + ".media"))
        name = "视频 " + body.file_id[:8]
        if not Path(source).is_file():
            raise HTTPException(404, "视频不存在")
    else:
        raise HTTPException(422, "未知输入类型")
    return await asyncio.to_thread(monitor.start, source, name, body.kind == "rtmp")


@app.post("/api/stop")
async def stop():
    await asyncio.to_thread(monitor.stop)
    return monitor.snapshot()


@app.post("/api/retract")
async def retract():
    try:
        return monitor.retract()
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    file_id = uuid.uuid4().hex
    path = UPLOADS / (file_id + ".media")
    size = 0
    limit = int(os.getenv("MAX_UPLOAD_MB", "4096")) * 1024 * 1024
    try:
        with path.open("wb") as output:
            while data := await file.read(1024 * 1024):
                size += len(data)
                if size > limit:
                    raise HTTPException(413, "视频超过上传大小限制")
                output.write(data)

        def probe():
            return subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=codec_type",
                    "-of",
                    "json",
                    str(path),
                ],
                capture_output=True,
                timeout=15,
            )

        result = await asyncio.to_thread(probe)
        if result.returncode or not json.loads(result.stdout).get("streams"):
            raise HTTPException(422, "未找到可解码的视频轨道")
        return {
            "file_id": file_id,
            "name": Path(file.filename or "video").name,
            "bytes": size,
        }
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    finally:
        await file.close()


@app.get("/preview.mjpg")
async def preview(request: Request):
    async def frames():
        previous = -1
        while not await request.is_disconnected():
            with monitor.lock:
                jpeg, seq = monitor.jpeg, monitor.frame_id
            if jpeg and seq != previous:
                previous = seq
                yield (
                    b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(jpeg)).encode()
                    + b"\r\n\r\n"
                    + jpeg
                    + b"\r\n"
                )
            await asyncio.sleep(0.05)

    return StreamingResponse(
        frames(), media_type="multipart/x-mixed-replace; boundary=frame"
    )


@app.websocket("/ws")
async def websocket(ws: WebSocket):
    origin = ws.headers.get("origin")
    if not authorized(
        ws.cookies.get("ignition_session"), ws.headers.get("authorization")
    ) or (origin and origin.split("://", 1)[-1] != ws.headers.get("host")):
        await ws.close(code=1008)
        return
    await ws.accept()
    last_id = None
    try:
        while True:
            latest = store.events(1)
            event_id = latest[0]["id"] if latest else 0
            payload = {"status": monitor.snapshot()}
            if event_id != last_id:
                payload["events"] = store.events(80)
                last_id = event_id
            await ws.send_json(payload)
            await asyncio.sleep(0.2)
    except (WebSocketDisconnect, RuntimeError):
        pass


@app.get("/")
async def index():
    return FileResponse(ROOT / "static/index.html")


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
