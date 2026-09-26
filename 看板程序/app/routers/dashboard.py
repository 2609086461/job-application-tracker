"""看板数据接口：GET /api/dashboard 返回主表统计。
"""
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app import feishu, mail_store, state
from tools import company_profiles
from tools.feishu import sync_recruitment_tasks

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])
public_router = APIRouter(prefix="/api/public", tags=["public-dashboard"])


class TaskStatusRequest(BaseModel):
    completed: bool


class TaskResolutionRequest(BaseModel):
    resolution: str


def _mail_data(tasks: dict) -> dict:
    items = tasks.get("items") or []
    return mail_store.get_dashboard_data(
        handled_references=mail_store.completed_mail_references(items),
        task_records=mail_store.mail_task_records(items),
    )


def _company_profiles(tasks: dict, mail: dict) -> dict:
    companies = [str(item.get("company") or "") for item in (tasks.get("items") or []) if isinstance(item, dict)]
    companies.extend(str(item.get("company") or "") for item in (mail.get("items") or []) if isinstance(item, dict))
    return company_profiles.profiles_for_companies(companies)


def _empty(error: str) -> dict:
    """飞书不可达时的降级空数据，结构与正常返回一致，附带 error 供前端提示。"""
    return {
        "main": {
            "total_companies": 0,
            "exam_count": 0, "interview_count": 0, "offer_count": 0,
            "directions": [], "recent": [],
        },
        "mail": mail_store.get_dashboard_data(),
        "tasks": {"items": [], "pending_count": 0, "completed_count": 0},
        "company_profiles": {},
        "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "error": error,
    }


def _public_main(main: dict) -> dict:
    return {
        **main,
        "recent": [
            {key: row.get(key, "") for key in (
                "company", "dir", "progress", "job", "deadline", "apply_date",
                "exam_date", "interview1", "interview2", "interview3", "warm", "result",
            )}
            for row in (main.get("recent") or [])
        ],
    }


def _public_mail(mail: dict) -> dict:
    return {
        **mail,
        "items": [
            {
                "company": row.get("company", ""),
                "event": row.get("event", ""),
                "deadline": row.get("deadline", ""),
                "mail_time": row.get("mail_time", ""),
                "urgency": row.get("urgency", ""),
                "urgency_note": row.get("urgency_note", ""),
                "resolution": row.get("resolution", ""),
                "subject": "邮件待办",
            }
            for row in (mail.get("items") or [])
        ],
    }


def _public_tasks(tasks: dict) -> dict:
    return {
        **tasks,
        "items": [
            {key: row.get(key, "") for key in (
                "task", "company", "event", "status", "planned_at", "deadline", "resolution",
            )}
            for row in (tasks.get("items") or [])
        ],
    }


def public_dashboard_data(data: dict) -> dict:
    return {
        **data,
        "access_mode": "public",
        "main": _public_main(data.get("main") or {}),
        "mail": _public_mail(data.get("mail") or {}),
        "tasks": _public_tasks(data.get("tasks") or {}),
        "company_profiles": data.get("company_profiles") or {},
    }


@router.get("")
def get_dashboard():
    # 30 秒缓存，减少飞书 API 压力
    cached = state.get_cache(max_age=30.0)
    if cached:
        # 主表可以短暂缓存；任务表仍会读取，但原生飞书任务核对在模块内限频至每 5 分钟。
        tasks = sync_recruitment_tasks.dashboard_data()
        mail = _mail_data(tasks)
        return {
            **cached,
            "tasks": tasks,
            "mail": mail,
            "company_profiles": _company_profiles(tasks, mail),
            "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    try:
        data = feishu.get_dashboard_data()
        data["tasks"] = sync_recruitment_tasks.dashboard_data()
        data["mail"] = _mail_data(data["tasks"])
        data["company_profiles"] = _company_profiles(data["tasks"], data["mail"])
        state.set_cache(data)
        return data
    except Exception as e:
        # 有旧缓存就返回旧缓存 + 提示；否则返回空结构 + 提示。
        stale = state.get_cache(max_age=1e9)
        if stale:
            return {**stale, "error": feishu.friendly_error(e), "stale": True}
        return _empty(feishu.friendly_error(e))


@public_router.get("/dashboard")
def get_public_dashboard():
    return public_dashboard_data(get_dashboard())


@router.get("/mail")
def get_mail(archive_path: str = Query(..., min_length=1)):
    try:
        return mail_store.get_mail_detail(archive_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/refresh")
def refresh_dashboard():
    try:
        data = feishu.get_dashboard_data()
        data["tasks"] = sync_recruitment_tasks.dashboard_data()
        data["mail"] = _mail_data(data["tasks"])
        data["company_profiles"] = _company_profiles(data["tasks"], data["mail"])
        state.set_cache(data)
        return data
    except Exception as e:
        stale = state.get_cache(max_age=1e9)
        if stale:
            return {**stale, "error": feishu.friendly_error(e), "stale": True}
        return _empty(feishu.friendly_error(e))


@router.post("/tasks/{record_id}/status")
def update_task_status(record_id: str, request: TaskStatusRequest):
    try:
        change = sync_recruitment_tasks.set_task_status(record_id, request.completed)
        state.update(cache=None, cache_at=0)
        data = refresh_dashboard()
        data["task_update"] = change
        return data
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=feishu.friendly_error(exc)) from exc


@router.post("/tasks/{record_id}/resolution")
def resolve_task(record_id: str, request: TaskResolutionRequest):
    try:
        change = sync_recruitment_tasks.resolve_task(record_id, request.resolution)
        state.update(cache=None, cache_at=0)
        data = refresh_dashboard()
        data["task_resolution"] = change
        return data
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=feishu.friendly_error(exc)) from exc
