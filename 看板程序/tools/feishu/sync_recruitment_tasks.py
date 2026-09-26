"""Create and reconcile the owner's Feishu recruitment task list.

The Bitable subtable is the durable task ledger; native Feishu Tasks are the
interaction layer.  Creation is idempotent through stable client tokens and
the ``任务键`` field, so an interrupted run can safely be resumed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable
from zoneinfo import ZoneInfo

from app import feishu, mail_store
from company_aliases import canonical_company_name, company_key


SHANGHAI = ZoneInfo("Asia/Shanghai")
TASK_TABLE_NAME = "求职任务"
TASKLIST_NAME = "秋招任务"
DEFAULT_OWNER_ID = "on_a8a3c0ba2123bbbcce8eaea9c05e6bee"
TASK_NAMESPACE = uuid.UUID("dd4ea38e-5a52-41e0-bbcb-1d43407e5630")
CLOSED_RESULTS = ("挂", "拒绝", "淘汰", "放弃", "撤回", "已结束")
_LAST_RECONCILE_AT = 0.0
RECONCILE_INTERVAL_SECONDS = 300
SINGLE_DATE_EVENTS = {"面试", "面试已预约", "一面", "二面", "三面", "测评", "笔试/机考"}
TASK_RESOLUTIONS = {
    "resend": "待重发邮件",
    "done": "已补做",
    "skip": "已跳过",
}


@dataclass(frozen=True)
class RecruitmentTask:
    key: str
    company: str
    event: str
    title: str
    action: str
    source: str
    source_key: str = ""
    deadline: datetime | None = None
    planned_at: datetime | None = None
    company_record_id: str = ""
    expired: bool = False


def now_shanghai() -> datetime:
    return datetime.now(SHANGHAI)


def _text(value) -> str:
    if isinstance(value, dict):
        return str(value.get("text") or value.get("link") or "")
    return str(value or "")


def _company_key(value: str) -> str:
    # Keep the historical task-key shape so deploying alias normalization does
    # not create a second native task for an already tracked mail action.
    text = company_key(value)
    for suffix in ("nvidia", "集团", "科技", "信息", "光学"):
        text = text.replace(suffix, "")
    return text


def _parse_deadline(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI)


def _timestamp(value) -> datetime | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 10_000_000_000:
        number /= 1000
    try:
        return datetime.fromtimestamp(number, SHANGHAI)
    except (OverflowError, OSError, ValueError):
        return None


def _is_closed(fields: dict) -> bool:
    result = _text(fields.get("结果"))
    return any(word in result for word in CLOSED_RESULTS)


def _interview_times(records: Iterable[dict], now: datetime) -> dict[str, tuple[datetime, str]]:
    result: dict[str, tuple[datetime, str]] = {}
    for record in records:
        fields = record.get("fields") or {}
        company = _text(fields.get("公司名称"))
        if not company or _is_closed(fields):
            continue
        future = []
        for stage in ("一面", "二面", "三面"):
            value = _timestamp(fields.get(stage))
            if value and value > now:
                future.append((value, stage))
        if future:
            result[_company_key(company)] = min(future)
    return result


def _title(company: str, event: str, action: str, *, expired: bool = False) -> str:
    if expired:
        return f"处理过期：{company} · {event}"
    if event == "面试已预约":
        return f"准备{company}面试"
    if event == "面试":
        return f"确认{company}面试时间"
    if event == "测评":
        return f"完成{company}测评"
    if event == "笔试/机考":
        return f"完成{company}笔试/机考"
    if event == "补充材料":
        return f"{company}：{action}"
    return f"{company}：{action or event}"


def _planned_at(event: str, deadline: datetime | None, now: datetime) -> datetime | None:
    if not deadline:
        return None
    # Interviews and assessments already have one authoritative event/deadline.
    # A second "finish early" event makes them appear on two days.
    if event in SINGLE_DATE_EVENTS:
        return None
    if event == "补充材料":
        planned = deadline - timedelta(hours=6)
    elif deadline - now <= timedelta(days=1):
        planned = deadline - timedelta(hours=2)
    else:
        previous = (deadline - timedelta(days=1)).astimezone(SHANGHAI)
        planned = previous.replace(hour=20, minute=0, second=0, microsecond=0)
    return planned if planned > now else None


def collect_candidates(mail_items: Iterable[dict], records: Iterable[dict], now: datetime) -> list[RecruitmentTask]:
    records = list(records)
    interview_times = _interview_times(records, now)
    record_ids = {
        _company_key(_text((record.get("fields") or {}).get("公司名称"))): str(record.get("record_id") or "")
        for record in records
    }
    scheduled = {
        _company_key(item.get("company"))
        for item in mail_items
        if item.get("event") == "面试已预约"
    }
    result: list[RecruitmentTask] = []
    seen: set[tuple[str, str]] = set()
    for item in mail_items:
        company = canonical_company_name(item.get("company") or "")
        event = str(item.get("event") or "").strip()
        action = str(item.get("action") or "").strip()
        company_key = _company_key(company)
        if not company or not event or (event == "面试" and company_key in scheduled):
            continue
        deadline = _parse_deadline(item.get("deadline"))
        if event == "面试已预约" and company_key in interview_times:
            deadline = interview_times[company_key][0]
            event_label = interview_times[company_key][1]
        else:
            event_label = event
        expired = bool(deadline and deadline <= now)
        semantic = (company_key, event_label)
        if semantic in seen:
            continue
        seen.add(semantic)
        source = str(item.get("subject") or item.get("archive_path") or event)
        source_key = str(item.get("mail_key") or item.get("archive_path") or "")
        digest = hashlib.sha256(f"{company_key}|{event_label}|{source}".encode("utf-8")).hexdigest()[:32]
        result.append(RecruitmentTask(
            key=f"mail:{digest}",
            company=company,
            event=event_label,
            title=_title(company, event, action, expired=expired),
            action=action,
            source=source,
            source_key=source_key,
            deadline=deadline,
            planned_at=_planned_at(event, deadline, now),
            company_record_id=record_ids.get(company_key, ""),
            expired=expired,
        ))
    return result


TABLE_FIELDS = [
    {"field_name": "任务", "type": 1},
    {"field_name": "公司", "type": 1},
    {"field_name": "事项类型", "type": 1},
    {"field_name": "截止时间", "type": 5},
    {"field_name": "计划完成时间", "type": 5},
    {"field_name": "状态", "type": 1},
    {"field_name": "完成时间", "type": 5},
    {"field_name": "飞书任务链接", "type": 15},
    {"field_name": "飞书任务GUID", "type": 1},
    {"field_name": "计划日程ID", "type": 1},
    {"field_name": "任务键", "type": 1},
    {"field_name": "来源", "type": 1},
    {"field_name": "来源键", "type": 1},
    {"field_name": "主表记录ID", "type": 1},
    {"field_name": "状态来源", "type": 1},
    {"field_name": "处置状态", "type": 1},
]


def ensure_task_table() -> str:
    for table in feishu.list_tables():
        if table.get("name") == TASK_TABLE_NAME:
            return str(table["table_id"])
    created = feishu.create_table(TASK_TABLE_NAME, TABLE_FIELDS, default_view_name="任务清单")
    table_id = str(created.get("table_id") or "")
    if not table_id:
        raise RuntimeError("飞书未返回求职任务表 ID")
    return table_id


def find_task_table() -> str:
    for table in feishu.list_tables():
        if table.get("name") == TASK_TABLE_NAME:
            return str(table["table_id"])
    return ""


def ensure_status_source_field(table_id: str) -> None:
    """Compatibility wrapper for older callers and existing task tables."""
    fields = feishu.list_fields(table_id)
    if "状态来源" in fields:
        return
    feishu.create_field(table_id, {"field_name": "状态来源", "type": 1})


def ensure_task_tracking_fields(table_id: str) -> None:
    """Add ledger fields introduced after the task table was first created."""
    fields = feishu.list_fields(table_id)
    for field_name in ("状态来源", "来源键", "处置状态"):
        if field_name not in fields:
            feishu.create_field(table_id, {"field_name": field_name, "type": 1})


def ensure_tasklist(owner_id: str) -> dict:
    for tasklist in feishu.list_tasklists():
        if tasklist.get("name") == TASKLIST_NAME:
            return tasklist
    created = feishu.create_tasklist({
        "name": TASKLIST_NAME,
        "owner": {"id": owner_id, "type": "user"},
        "client_token": str(uuid.uuid5(TASK_NAMESPACE, "tasklist:" + TASKLIST_NAME)),
    })
    if not created.get("guid"):
        raise RuntimeError("飞书未返回任务清单 GUID")
    return created


def _task_payload(item: RecruitmentTask, tasklist_guid: str, owner_id: str) -> dict:
    description = [f"公司：{item.company}", f"事项：{item.event}"]
    if item.action:
        description.append(f"下一步：{item.action}")
    if item.deadline:
        description.append(f"最终时间：{item.deadline:%Y-%m-%d %H:%M}")
    if item.planned_at:
        description.append(f"建议提前完成：{item.planned_at:%Y-%m-%d %H:%M}")
    description.append(f"来源：{item.source}")
    payload = {
        "summary": item.title,
        "description": "\n".join(description),
        "members": [{"id": owner_id, "type": "user", "role": "assignee"}],
        "tasklists": [{"tasklist_guid": tasklist_guid}],
        "client_token": str(uuid.uuid5(TASK_NAMESPACE, item.key)),
    }
    if item.deadline:
        payload["due"] = {"timestamp": int(item.deadline.timestamp() * 1000), "is_all_day": False}
        delta = item.deadline - now_shanghai()
        # Native Feishu Tasks allow one reminder. The paired Calendar event
        # remains responsible for the existing 24-hour and 3-hour reminders.
        if delta > timedelta(hours=3):
            payload["reminders"] = [{"relative_fire_minute": 3 * 60}]
    return payload


def _calendar_payload(item: RecruitmentTask) -> dict:
    duration = timedelta(hours=2 if item.event in ("测评", "笔试/机考") else 1)
    start = item.planned_at - duration
    return {
        "summary": f"【提前完成】{item.company} · {item.event}",
        "description": f"任务：{item.title}\n最终时间：{item.deadline:%Y-%m-%d %H:%M}",
        "start_time": {"timestamp": str(int(start.timestamp())), "timezone": "Asia/Shanghai"},
        "end_time": {"timestamp": str(int(item.planned_at.timestamp())), "timezone": "Asia/Shanghai"},
        "reminders": [{"minutes": 30}],
        "free_busy_status": "free",
        "visibility": "default",
        "need_notification": False,
    }


def _record_fields(item: RecruitmentTask, native: dict, calendar_event_id: str) -> dict:
    fields = {
        "任务": item.title,
        "公司": item.company,
        "事项类型": item.event,
        "状态": "待完成",
        "飞书任务GUID": native.get("guid") or "",
        "计划日程ID": calendar_event_id,
        "任务键": item.key,
        "来源": item.source,
        "来源键": item.source_key,
        "主表记录ID": item.company_record_id,
        "处置状态": "待处置" if item.expired else "",
    }
    if item.deadline:
        fields["截止时间"] = int(item.deadline.timestamp() * 1000)
    if item.planned_at:
        fields["计划完成时间"] = int(item.planned_at.timestamp() * 1000)
    if native.get("url"):
        fields["飞书任务链接"] = {"link": native["url"], "text": "打开任务"}
    return fields


def apply_candidates(items: Iterable[RecruitmentTask], owner_id: str) -> dict:
    items = list(items)
    table_id = ensure_task_table()
    ensure_task_tracking_fields(table_id)
    tasklist = ensure_tasklist(owner_id)
    tasklist_guid = str(tasklist["guid"])
    existing = {
        str((record.get("fields") or {}).get("任务键") or ""): record
        for record in feishu.list_records(table_id)
    }
    calendar_id = ""
    created = 0
    repaired = 0
    skipped = 0
    for item in items:
        if item.key in existing:
            record = existing[item.key]
            fields = record.get("fields") or {}
            old_guid = str(fields.get("飞书任务GUID") or "")
            unreadable = not old_guid
            if old_guid:
                try:
                    feishu.get_task(old_guid)
                except Exception as exc:
                    code = getattr(exc, "code", None)
                    if code == 1470404:
                        completed_at = int(datetime.now(SHANGHAI).timestamp() * 1000)
                        feishu.update_record(
                            str(record.get("record_id") or ""),
                            {
                                "状态": "已完成",
                                "完成时间": completed_at,
                                "处置状态": "已跳过",
                                "状态来源": "飞书任务",
                            },
                            table_id=table_id,
                        )
                        skipped += 1
                        continue
                    if code != 1470403:
                        raise
                    unreadable = True
            if not unreadable:
                updates = {}
                if item.source_key and not _text(fields.get("来源键")):
                    updates["来源键"] = item.source_key
                if (
                    item.expired
                    and fields.get("状态") != "已完成"
                    and not _text(fields.get("处置状态"))
                ):
                    updates["处置状态"] = "待处置"
                if updates:
                    feishu.update_record(
                        str(record.get("record_id") or ""),
                        updates,
                        table_id=table_id,
                    )
                skipped += 1
                continue

            # Tasks created by an older Feishu app identity cannot be read or
            # updated by the current app. Recreate only those exact tasks and
            # repoint the durable ledger; the ledger completion state wins.
            native = feishu.create_task(_task_payload(item, tasklist_guid, owner_id))
            if not native.get("guid"):
                raise RuntimeError(f"飞书未返回替代任务 GUID：{item.title}")
            if fields.get("状态") == "已完成":
                completed_at = int(fields.get("完成时间") or datetime.now(SHANGHAI).timestamp() * 1000)
                feishu.update_task(
                    str(native["guid"]),
                    {"completed_at": completed_at},
                    ["completed_at"],
                )
            updates = {
                "任务": item.title,
                "公司": item.company,
                "飞书任务GUID": native.get("guid") or "",
                "来源键": item.source_key,
                "状态来源": "双向同步",
            }
            if native.get("url"):
                updates["飞书任务链接"] = {"link": native["url"], "text": "打开任务"}
            feishu.update_record(
                str(record.get("record_id") or ""),
                updates,
                table_id=table_id,
            )
            repaired += 1
            continue
        native = feishu.create_task(_task_payload(item, tasklist_guid, owner_id))
        if not native.get("guid"):
            raise RuntimeError(f"飞书未返回任务 GUID：{item.title}")
        calendar_event_id = ""
        if item.planned_at:
            if not calendar_id:
                calendar_id = str(feishu.get_primary_calendar().get("calendar_id") or "")
            event = feishu.create_calendar_event(
                calendar_id,
                _calendar_payload(item),
                idempotency_key="task-plan:" + hashlib.sha256(item.key.encode()).hexdigest()[:40],
            )
            calendar_event_id = str(event.get("event_id") or "")
            if calendar_event_id:
                feishu.add_calendar_event_attendees(
                    calendar_id,
                    calendar_event_id,
                    [{"type": "user", "user_id": owner_id, "is_optional": False}],
                    user_id_type="union_id",
                    need_notification=True,
                )
        feishu.create_record(_record_fields(item, native, calendar_event_id), table_id=table_id)
        created += 1
    return {
        "table_id": table_id,
        "tasklist_guid": tasklist_guid,
        "tasklist_url": tasklist.get("url") or "",
        "created": created,
        "repaired": repaired,
        "skipped": skipped,
    }


def reconcile_completion(table_id: str | None = None) -> dict:
    table_id = table_id or find_task_table()
    if not table_id:
        return {"checked": 0, "updated": 0, "failed": 0}
    checked = updated = failed = 0
    for record in feishu.list_records(table_id):
        fields = record.get("fields") or {}
        guid = str(fields.get("飞书任务GUID") or "")
        if not guid:
            continue
        if fields.get("状态来源") == "看板":
            checked += 1
            try:
                desired = int(datetime.now(SHANGHAI).timestamp() * 1000) if fields.get("状态") == "已完成" else 0
                feishu.update_task(guid, {"completed_at": desired}, ["completed_at"])
                feishu.update_record(
                    str(record["record_id"]),
                    {"状态来源": "双向同步"},
                    table_id=table_id,
                )
                updated += 1
            except Exception:
                failed += 1
            continue
        checked += 1
        # A task may have been created by another deployed app identity, or the
        # app's read permission may still be awaiting publication.  One such
        # task must not hide the durable Bitable task ledger from the dashboard.
        try:
            task = feishu.get_task(guid)
        except Exception as exc:
            if getattr(exc, "code", None) == 1470404:
                completed_at = int(datetime.now(SHANGHAI).timestamp() * 1000)
                feishu.update_record(
                    str(record["record_id"]),
                    {
                        "状态": "已完成",
                        "完成时间": completed_at,
                        "处置状态": "已跳过",
                        "状态来源": "飞书任务",
                    },
                    table_id=table_id,
                )
                updated += 1
                continue
            failed += 1
            continue
        completed_at = int(task.get("completed_at") or 0)
        native_completed = bool(completed_at)
        ledger_completed = fields.get("状态") == "已完成"
        if native_completed != ledger_completed:
            updates = {
                "状态": "已完成" if native_completed else "待完成",
                "完成时间": completed_at if native_completed else None,
                "状态来源": "飞书任务",
            }
            resolution = _text(fields.get("处置状态"))
            if native_completed and resolution == "待重发邮件":
                updates["处置状态"] = "已重发"
            elif native_completed and resolution == "待处置":
                updates["处置状态"] = "已补做"
            feishu.update_record(
                str(record["record_id"]),
                updates,
                table_id=table_id,
            )
            updated += 1
    return {"checked": checked, "updated": updated, "failed": failed}


def set_task_status(record_id: str, completed: bool) -> dict:
    """Update one task from the dashboard in both native Tasks and Bitable."""
    table_id = find_task_table()
    if not table_id:
        raise LookupError("求职任务表不存在")

    target = next(
        (record for record in feishu.list_records(table_id)
         if str(record.get("record_id") or "") == record_id),
        None,
    )
    if not target:
        raise LookupError("任务不存在或已被删除")

    fields = target.get("fields") or {}
    task_guid = str(fields.get("飞书任务GUID") or "")
    if not task_guid:
        raise ValueError("该记录没有关联飞书任务")

    completed_at = int(datetime.now(SHANGHAI).timestamp() * 1000) if completed else 0
    ensure_status_source_field(table_id)
    native_synced = True
    try:
        feishu.update_task(task_guid, {"completed_at": completed_at}, ["completed_at"])
    except Exception as exc:
        if getattr(exc, "code", None) not in (1470403, 1470404):
            raise
        native_synced = False
    feishu.update_record(
        record_id,
        {
            "状态": "已完成" if completed else "待完成",
            "完成时间": completed_at if completed else None,
            "状态来源": "双向同步" if native_synced else "看板",
        },
        table_id=table_id,
    )
    return {
        "record_id": record_id,
        "status": "已完成" if completed else "待完成",
        "completed_at": completed_at,
        "native_synced": native_synced,
    }


def resolve_task(record_id: str, resolution: str) -> dict:
    """Record how a mail action will be handled.

    ``resend`` keeps the task open and turns it into a manual resend action.
    ``done`` is reserved for an overdue action that was completed afterwards.
    ``skip`` may close any task without claiming that the recruitment action
    was completed.  This function never creates a draft or sends mail.
    """
    if resolution not in TASK_RESOLUTIONS:
        raise ValueError("不支持的任务处置方式")
    table_id = find_task_table()
    if not table_id:
        raise LookupError("求职任务表不存在")

    target = next(
        (record for record in feishu.list_records(table_id)
         if str(record.get("record_id") or "") == record_id),
        None,
    )
    if not target:
        raise LookupError("任务不存在或已被删除")

    fields = target.get("fields") or {}
    task_guid = str(fields.get("飞书任务GUID") or "")
    if not task_guid:
        raise ValueError("该记录没有关联飞书任务")
    if resolution in ("resend", "done"):
        deadline = _timestamp(fields.get("截止时间"))
        if not deadline or deadline > now_shanghai():
            raise ValueError("只有已过期的任务可以选择重发或补做")

    label = TASK_RESOLUTIONS[resolution]
    if resolution == "resend":
        company = _text(fields.get("公司")) or "未知公司"
        event = _text(fields.get("事项类型")) or "过期事项"
        title = f"重发邮件：{company} · {event}"
        feishu.update_task(
            task_guid,
            {"summary": title, "completed_at": 0},
            ["summary", "completed_at"],
        )
        feishu.update_record(
            record_id,
            {
                "任务": title,
                "状态": "待完成",
                "完成时间": None,
                "处置状态": label,
                "状态来源": "双向同步",
            },
            table_id=table_id,
        )
        return {"record_id": record_id, "resolution": label, "completed": False}

    completed_at = int(datetime.now(SHANGHAI).timestamp() * 1000)
    feishu.update_task(task_guid, {"completed_at": completed_at}, ["completed_at"])
    feishu.update_record(
        record_id,
        {
            "状态": "已完成",
            "完成时间": completed_at,
            "处置状态": label,
            "状态来源": "双向同步",
        },
        table_id=table_id,
    )
    return {"record_id": record_id, "resolution": label, "completed": True}


def resolve_expired_task(record_id: str, resolution: str) -> dict:
    """Backward-compatible name for the formerly overdue-only operation."""
    return resolve_task(record_id, resolution)


def cleanup_duplicate_calendar_events() -> dict:
    """Remove legacy planned events for interviews and assessments."""
    table_id = find_task_table()
    if not table_id:
        return {"checked": 0, "deleted": 0, "updated": 0}
    records = feishu.list_records(table_id)
    targets = [
        record for record in records
        if _text((record.get("fields") or {}).get("事项类型")) in SINGLE_DATE_EVENTS
        and (record.get("fields") or {}).get("计划日程ID")
    ]
    if not targets:
        return {"checked": len(records), "deleted": 0, "updated": 0}
    calendar_id = str(feishu.get_primary_calendar().get("calendar_id") or "")
    deleted = updated = 0
    for record in targets:
        fields = record.get("fields") or {}
        event_id = str(fields.get("计划日程ID") or "")
        feishu.delete_calendar_event(calendar_id, event_id)
        deleted += 1

        task_guid = str(fields.get("飞书任务GUID") or "")
        if task_guid:
            task = feishu.get_task(task_guid)
            description = "\n".join(
                line for line in str(task.get("description") or "").splitlines()
                if not line.startswith("建议提前完成：")
            )
            if description != str(task.get("description") or ""):
                feishu.update_task(task_guid, {"description": description}, ["description"])

        feishu.update_record(
            str(record.get("record_id") or ""),
            {"计划日程ID": "", "计划完成时间": None},
            table_id=table_id,
        )
        updated += 1
    return {"checked": len(records), "deleted": deleted, "updated": updated}


def dashboard_data(*, force_reconcile: bool = False) -> dict:
    """Return task rows and reconcile native completion at a bounded rate."""
    global _LAST_RECONCILE_AT
    table_id = find_task_table()
    if not table_id:
        return {"items": [], "pending_count": 0, "completed_count": 0, "last_reconciled": ""}
    monotonic_now = time.monotonic()
    should_reconcile = force_reconcile or monotonic_now - _LAST_RECONCILE_AT >= RECONCILE_INTERVAL_SECONDS
    if should_reconcile:
        reconcile_completion(table_id)
        _LAST_RECONCILE_AT = monotonic_now
    items = []
    for record in feishu.list_records(table_id):
        fields = record.get("fields") or {}
        if not fields.get("任务键"):
            continue
        link = fields.get("飞书任务链接")
        if isinstance(link, dict):
            link = link.get("link") or ""
        items.append({
            "record_id": str(record.get("record_id") or ""),
            "task": _text(fields.get("任务")),
            "company": _text(fields.get("公司")),
            "event": _text(fields.get("事项类型")),
            "status": _text(fields.get("状态")) or "待完成",
            "source": _text(fields.get("来源")),
            "source_key": _text(fields.get("来源键")),
            "native_sync_pending": fields.get("状态来源") == "看板",
            "resolution": _text(fields.get("处置状态")),
            "deadline": fields.get("截止时间") or 0,
            "planned_at": fields.get("计划完成时间") or 0,
            "completed_at": fields.get("完成时间") or 0,
            "url": str(link or ""),
        })
    items.sort(key=lambda item: (
        item["status"] == "已完成",
        item["planned_at"] or item["deadline"] or 9_999_999_999_999,
        item["task"],
    ))
    return {
        "items": items,
        "pending_count": sum(item["status"] != "已完成" for item in items),
        "completed_count": sum(item["status"] == "已完成" for item in items),
        "last_reconciled": datetime.now(SHANGHAI).isoformat() if should_reconcile else "",
    }


def preview(items: Iterable[RecruitmentTask]) -> list[dict]:
    return [{
        "task": item.title,
        "event": item.event,
        "deadline": item.deadline.isoformat() if item.deadline else None,
        "planned_at": item.planned_at.isoformat() if item.planned_at else None,
    } for item in items]


def main() -> int:
    parser = argparse.ArgumentParser(description="同步秋招待办到飞书任务")
    parser.add_argument("--apply", action="store_true", help="创建任务清单、任务和计划日程")
    parser.add_argument(
        "--mail-key",
        action="append",
        default=[],
        help="仅同步指定邮件键对应的待办；可重复传入",
    )
    parser.add_argument("--reconcile", action="store_true", help="回写已完成的飞书任务")
    parser.add_argument(
        "--cleanup-calendar-duplicates",
        action="store_true",
        help="删除面试和测评的旧「提前完成」重复日程",
    )
    args = parser.parse_args()
    try:
        now = now_shanghai()
        records = feishu.list_records(feishu.MAIN_TABLE_ID)
        mail_items = mail_store.get_dashboard_data(now).get("items") or []
        mail_keys = {str(value).strip() for value in args.mail_key if str(value).strip()}
        if mail_keys:
            mail_items = [item for item in mail_items if str(item.get("mail_key") or "") in mail_keys]
        items = collect_candidates(mail_items, records, now)
        print(json.dumps({"count": len(items), "items": preview(items)}, ensure_ascii=False, indent=2))
        if args.apply:
            owner_id = os.getenv("FEISHU_REMINDER_USER_ID", DEFAULT_OWNER_ID).strip()
            print(json.dumps(apply_candidates(items, owner_id), ensure_ascii=False, indent=2))
        if args.reconcile:
            print(json.dumps(reconcile_completion(), ensure_ascii=False, indent=2))
        if args.cleanup_calendar_duplicates:
            print(json.dumps(cleanup_duplicate_calendar_events(), ensure_ascii=False, indent=2))
    except Exception as exc:
        print(feishu.friendly_error(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
