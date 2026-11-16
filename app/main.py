"""Web server: starts debates, streams them live, handles file upload/download."""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth
from . import settings as app_settings
from . import store
from .auth import admin_user, current_user
from .config import DEFAULTS, EFFORTS, MAX_BUDGET_LIMIT, MAX_TURNS_LIMIT, MOCK_LLM, MODELS
from .debate import Debate, Registry
from .llm import ClaudeRunner, MockRunner

STATIC = Path(__file__).parent / "static"
MAX_UPLOAD_BYTES = 100 * 1024 * 1024

@asynccontextmanager
async def lifespan(_app):
    auth.ensure_admin()
    yield


app = FastAPI(title="Multi-AI Fact Check", lifespan=lifespan)
registry = Registry(linger=float(os.environ.get("LIVE_LINGER_SECONDS", "120")))
_runner = None
_runner_key = None


def runner():
    """The API client, rebuilt whenever the saved key changes."""
    global _runner, _runner_key
    if MOCK_LLM:
        if _runner is None:
            _runner = MockRunner()
        return _runner
    key, _ = app_settings.get_api_key()
    if not key:
        raise HTTPException(400, "No Claude API key yet. An admin needs to add one under Settings.")
    if _runner is None or key != _runner_key:
        _runner, _runner_key = ClaudeRunner(key), key
    return _runner


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ---- accounts ----------------------------------------------------------------

class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


@app.get("/api/me")
async def me(request: Request):
    if auth.needs_setup():
        return JSONResponse({"needs_setup": True}, status_code=401)
    return auth.public_user(current_user(request))


@app.post("/api/login")
async def login(body: LoginIn, request: Request):
    username = body.username.strip()
    auth.check_rate(request, username)
    user = store.get_user_by_name(username)
    ok = auth.verify_password(body.password, user["password_hash"] if user else auth._DUMMY_HASH)
    if not user or not ok:
        auth.record_failure(request, username)
        raise HTTPException(401, "Wrong username or password.")
    resp = JSONResponse(auth.public_user(user))
    auth.start_session(resp, request, user["id"])
    return resp


@app.post("/api/setup")
async def setup(body: LoginIn, request: Request):
    """Create the first (admin) account. Only works while there are no accounts."""
    if not auth.needs_setup():
        raise HTTPException(409, "Already set up.")
    auth.validate_new_password(body.password)
    uid = store.create_user(body.username.strip(), auth.hash_password(body.password), True)
    store.claim_unowned_debates(uid)
    resp = JSONResponse(auth.public_user(store.get_user(uid)))
    auth.start_session(resp, request, uid)
    return resp


@app.post("/api/logout")
async def logout(request: Request):
    resp = JSONResponse({"ok": True})
    auth.end_session(resp, request)
    return resp


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


@app.post("/api/me/password")
async def change_password(body: PasswordChange, request: Request,
                          user: dict = Depends(current_user)):
    if not auth.verify_password(body.current_password, user["password_hash"]):
        raise HTTPException(400, "Your current password is wrong.")
    auth.validate_new_password(body.new_password)
    store.set_password(user["id"], auth.hash_password(body.new_password))
    store.delete_user_sessions(user["id"], except_token=request.cookies.get(auth.COOKIE))
    return {"ok": True}


class NewUser(BaseModel):
    username: str = Field(min_length=2, max_length=64, pattern=r"^[A-Za-z0-9._@-]+$")
    password: str = Field(min_length=1, max_length=256)
    is_admin: bool = False


@app.get("/api/users")
async def list_users(_: dict = Depends(admin_user)):
    return store.list_users()


@app.post("/api/users")
async def add_user(body: NewUser, _: dict = Depends(admin_user)):
    auth.validate_new_password(body.password)
    if store.get_user_by_name(body.username):
        raise HTTPException(409, "That username is taken.")
    uid = store.create_user(body.username, auth.hash_password(body.password), body.is_admin)
    return auth.public_user(store.get_user(uid))


class ResetPassword(BaseModel):
    password: str = Field(min_length=1, max_length=256)


@app.post("/api/users/{user_id}/password")
async def reset_password(user_id: int, body: ResetPassword, _: dict = Depends(admin_user)):
    if not store.get_user(user_id):
        raise HTTPException(404, "No such user.")
    auth.validate_new_password(body.password)
    store.set_password(user_id, auth.hash_password(body.password))
    store.delete_user_sessions(user_id)
    return {"ok": True}


@app.delete("/api/users/{user_id}")
async def remove_user(user_id: int, admin: dict = Depends(admin_user)):
    if user_id == admin["id"]:
        raise HTTPException(400, "You can't delete your own account.")
    if not store.get_user(user_id):
        raise HTTPException(404, "No such user.")
    store.delete_user(user_id)
    return {"ok": True}


# ---- app settings -------------------------------------------------------------

@app.get("/api/config")
async def config(_: dict = Depends(current_user)):
    return {"models": {k: v["label"] for k, v in MODELS.items()}, "defaults": DEFAULTS,
            "efforts": EFFORTS, "max_turns_limit": MAX_TURNS_LIMIT,
            "max_budget_limit": MAX_BUDGET_LIMIT, "mock": MOCK_LLM}


@app.get("/api/settings")
async def get_settings(_: dict = Depends(current_user)):
    key, source = app_settings.get_api_key()
    return {"has_key": bool(key) or MOCK_LLM, "key_hint": app_settings.hint(key),
            "source": source, "mock": MOCK_LLM}


class KeyIn(BaseModel):
    anthropic_api_key: str = Field(min_length=10, max_length=500)


@app.post("/api/settings/key")
async def save_key(body: KeyIn, _: dict = Depends(admin_user)):
    import anthropic

    key = body.anthropic_api_key.strip()
    try:
        await ClaudeRunner(key).check_key()
    except anthropic.AuthenticationError:
        raise HTTPException(400, "Anthropic rejected this key. Check that you copied all of it.")
    except anthropic.PermissionDeniedError:
        raise HTTPException(400, "This key doesn't have permission to use the models.")
    except anthropic.APIError as e:
        raise HTTPException(502, f"Couldn't check the key with Anthropic: {e}")
    app_settings.save_api_key(key)
    return {"ok": True, "key_hint": app_settings.hint(key)}


@app.delete("/api/settings/key")
async def remove_key(_: dict = Depends(admin_user)):
    app_settings.delete_api_key()
    return {"ok": True}


class FileRef(BaseModel):
    file_id: str
    filename: str
    mime: str = ""


class NewDebate(BaseModel):
    question: str = Field(min_length=1, max_length=50_000)
    model_a: str = DEFAULTS["model_a"]
    model_b: str = DEFAULTS["model_b"]
    first: str = DEFAULTS["first"]
    max_turns: int = DEFAULTS["max_turns"]
    effort: str = DEFAULTS["effort"]
    budget_usd: float = DEFAULTS["budget_usd"]
    files: list[FileRef] = []


def owned_debate(debate_id: str, user: dict) -> dict:
    d = store.get_debate(debate_id)
    if not d or d["user_id"] != user["id"]:
        raise HTTPException(404, "Not found")
    return d


def owned_live(debate_id: str, user: dict):
    owned_debate(debate_id, user)
    return registry.get(debate_id)


# ---- debates ------------------------------------------------------------------

@app.post("/api/debates")
async def create_debate(body: NewDebate, user: dict = Depends(current_user)):
    if body.model_a not in MODELS or body.model_b not in MODELS:
        raise HTTPException(400, "Unknown model")
    if body.first not in ("A", "B"):
        raise HTTPException(400, "first must be A or B")
    if body.effort not in EFFORTS:
        raise HTTPException(400, "Unknown effort level")
    settings = {
        "model_a": body.model_a,
        "model_b": body.model_b,
        "first": body.first,
        "max_turns": max(2, min(body.max_turns, MAX_TURNS_LIMIT)),
        "effort": body.effort,
        "budget_usd": max(0.05, min(body.budget_usd, MAX_BUDGET_LIMIT)),
    }
    debate = Debate(runner(), body.question.strip(), settings, [f.model_dump() for f in body.files],
                    user_id=user["id"])
    registry.start(debate)
    return {"id": debate.id}


@app.get("/api/debates")
async def list_debates(user: dict = Depends(current_user)):
    return store.list_debates(user["id"])


@app.get("/api/debates/{debate_id}")
async def get_debate(debate_id: str, user: dict = Depends(current_user)):
    return owned_debate(debate_id, user)


@app.delete("/api/debates/{debate_id}")
async def delete_debate(debate_id: str, user: dict = Depends(current_user)):
    live = owned_live(debate_id, user)
    if live and live.status == "running":
        raise HTTPException(409, "Stop the debate before deleting it")
    store.delete_debate(debate_id)
    return {"ok": True}


@app.post("/api/debates/{debate_id}/stop")
async def stop_debate(debate_id: str, user: dict = Depends(current_user)):
    live = owned_live(debate_id, user)
    if not live:
        raise HTTPException(404, "Debate is not running")
    live.stop()
    return {"ok": True}


class Note(BaseModel):
    text: str = Field(min_length=1, max_length=5000)


@app.post("/api/debates/{debate_id}/note")
async def add_note(debate_id: str, note: Note, user: dict = Depends(current_user)):
    live = owned_live(debate_id, user)
    if not live or live.status != "running":
        raise HTTPException(409, "Debate is not running")
    await live.add_note(note.text.strip())
    return {"ok": True}


@app.get("/api/debates/{debate_id}/events")
async def stream_events(debate_id: str, request: Request, user: dict = Depends(current_user)):
    """Server-sent events: everything so far, then live updates."""
    owned_debate(debate_id, user)

    async def gen():
        sent = -1
        while True:
            live = registry.get(debate_id)
            if live is None:
                # Finished (or from before a restart): replay what was saved.
                for ev in store.get_events(debate_id, after=sent):
                    yield f"data: {json.dumps(ev)}\n\n"
                yield 'data: {"type": "replay_done"}\n\n'
                return
            pending = [e for e in live.events if e["seq"] > sent]
            for ev in pending:
                yield f"data: {json.dumps(ev)}\n\n"
                sent = ev["seq"]
                if ev["type"] == "end":
                    return
            if await request.is_disconnected():
                return
            async with live.cond:
                try:
                    await asyncio.wait_for(live.cond.wait(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/upload")
async def upload(file: UploadFile = File(...), _: dict = Depends(current_user)):
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File is larger than 100 MB")
    name = Path(file.filename or "upload").name
    mime = file.content_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
    try:
        file_id = await runner().upload(name, data, mime)
    except Exception as e:
        raise HTTPException(502, f"Upload to Anthropic failed: {e}")
    return {"file_id": file_id, "filename": name, "mime": mime}


@app.get("/api/files/{file_id}")
async def download(file_id: str, user: dict = Depends(current_user)):
    # Only serve files that one of this user's debates produced.
    if not file_id.replace("_", "").isalnum() or store.file_owner(file_id) != user["id"]:
        raise HTTPException(404, "Unknown file")
    try:
        name, data = await runner().download(file_id)
    except Exception as e:
        raise HTTPException(502, f"Download failed: {e}")
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    safe = "".join(c for c in name if c.isalnum() or c in "._- ") or "download"
    return Response(data, media_type=mime,
                    headers={"Content-Disposition": f'attachment; filename="{safe}"'})


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    return JSONResponse({"detail": f"Server error: {exc!r}"}, status_code=500)
