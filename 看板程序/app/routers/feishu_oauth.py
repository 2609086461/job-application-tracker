"""One-click Feishu user authorization for user-owned task maintenance."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

import requests
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse


router = APIRouter(prefix="/api/feishu/oauth", tags=["feishu-oauth"])

AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
API_ROOT = "https://open.feishu.cn/open-apis"
DEFAULT_REDIRECT_URI = "http://localhost:8765/api/feishu/oauth/callback"
USER_SCOPES = (
    "task:task:read",
    "task:task:write",
    "task:tasklist:read",
    "task:tasklist:write",
    "offline_access",
)


def _private_dir() -> Path:
    configured = os.environ.get("JOB_TRACKER_PRIVATE_STATE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
        return (base / "AutumnRecruitment").resolve()
    return Path("/var/lib/job-tracker").resolve()


def _state_file() -> Path:
    return _private_dir() / "feishu-oauth-state.json"


def token_file() -> Path:
    return _private_dir() / "feishu-user-token.json"


def _write_private_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    os.chmod(path, 0o600)


def _app_credentials() -> tuple[str, str]:
    app_id = os.environ.get("FEISHU_APP_ID", "").strip()
    app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        raise RuntimeError("飞书应用凭证未配置")
    return app_id, app_secret


def redirect_uri() -> str:
    return os.environ.get("FEISHU_OAUTH_REDIRECT_URI", DEFAULT_REDIRECT_URI).strip()


def create_authorization_url(now: float | None = None) -> str:
    app_id, _ = _app_credentials()
    state = secrets.token_urlsafe(32)
    _write_private_json(
        _state_file(),
        {
            "state_hash": hashlib.sha256(state.encode()).hexdigest(),
            "expires_at": int((now or time.time()) + 600),
        },
    )
    query = urlencode(
        {
            "app_id": app_id,
            "redirect_uri": redirect_uri(),
            "scope": " ".join(USER_SCOPES),
            "state": state,
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def _consume_state(state: str, now: float | None = None) -> None:
    path = _state_file()
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise ValueError("授权状态不存在或已经使用") from exc
    path.unlink(missing_ok=True)
    expected = str(saved.get("state_hash") or "")
    actual = hashlib.sha256(state.encode()).hexdigest()
    if not state or not hmac.compare_digest(expected, actual):
        raise ValueError("授权状态校验失败")
    if int(saved.get("expires_at") or 0) < int(now or time.time()):
        raise ValueError("授权链接已过期，请重新发起")


def exchange_code(code: str, now: float | None = None) -> dict:
    app_id, app_secret = _app_credentials()
    app_response = requests.post(
        f"{API_ROOT}/auth/v3/app_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=15,
    )
    app_response.raise_for_status()
    app_body = app_response.json()
    if app_body.get("code"):
        raise RuntimeError(app_body.get("msg") or "获取应用令牌失败")
    app_token = str(app_body.get("app_access_token") or "")
    if not app_token:
        raise RuntimeError("飞书未返回应用令牌")

    user_response = requests.post(
        f"{API_ROOT}/authen/v1/access_token",
        headers={"Authorization": f"Bearer {app_token}"},
        json={"grant_type": "authorization_code", "code": code},
        timeout=15,
    )
    user_response.raise_for_status()
    user_body = user_response.json()
    if user_body.get("code"):
        raise RuntimeError(user_body.get("msg") or "获取用户令牌失败")
    data = user_body.get("data") or {}
    if not data.get("access_token"):
        raise RuntimeError("飞书未返回用户令牌")

    current = int(now or time.time())
    stored = {
        "access_token": data.get("access_token"),
        "refresh_token": data.get("refresh_token"),
        "expires_at": current + int(data.get("expires_in") or 0),
        "refresh_expires_at": current + int(data.get("refresh_expires_in") or 0),
        "open_id": data.get("open_id"),
        "union_id": data.get("union_id"),
        "authorized_at": current,
    }
    _write_private_json(token_file(), stored)
    return {"open_id": stored["open_id"], "expires_at": stored["expires_at"]}


@router.get("/start")
def start_authorization():
    try:
        return RedirectResponse(create_authorization_url(), status_code=302)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/callback", response_class=HTMLResponse)
def authorization_callback(
    code: str = Query(""),
    state: str = Query(""),
    error: str = Query(""),
):
    if error:
        raise HTTPException(status_code=400, detail=f"飞书授权未完成：{error}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="缺少飞书授权参数")
    try:
        _consume_state(state)
        exchange_code(code)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"飞书授权失败：{exc}") from exc
    return HTMLResponse(
        "<!doctype html><meta charset='utf-8'><title>授权成功</title>"
        "<body style='font-family:sans-serif;padding:2rem'>"
        "<h2>飞书任务授权成功</h2><p>可以关闭此页面并返回聊天。</p></body>"
    )


@router.get("/status")
def authorization_status():
    try:
        data = json.loads(token_file().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"authorized": False}
    return {
        "authorized": bool(data.get("refresh_token")),
        "expires_at": int(data.get("expires_at") or 0),
        "open_id": str(data.get("open_id") or "")[:8],
    }
