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
MANUAL_PRIORITIES = {"", "高", "普通", "低"}
PRIORITY_RANKS = {"高": 0, "普通": 1, "低": 2}


@dataclass(frozen=True)
class RecruitmentTask:
    key: str
    company: str
    event: str
    title: str
    action: str
    source: str
    job: str = ""
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


def _event_family(event: str) -> str:
    value = _text(event).strip()
    # Keep an explicitly named interview round distinct.  Generic invitation
    # and booked-interview notices resolve to the same interview action.
    if value in {"一面", "二面", "三面", "四面", "终面", "主管面", "HR面"}:
        return value
    if "面试" in value:
        return "面试"
    if "笔试" in value or "机考" in value:
        return "笔试/机考"
    return value


def _semantic_task_key(company: str, event: str) -> tuple[str, str]:
    return _company_key(company), _event_family(event)


def _job_key(value: str) -> str:
    return " ".join(_text(value).casefold().split())


def _deadline_day(value) -> str:
    parsed = value if isinstance(value, datetime) else _timestamp(value)
    return parsed.date().isoformat() if parsed else ""


def _is_reminder(item: RecruitmentTask) -> bool:
    text = f"{item.source} {item.action}"
    return any(word in text for word in ("提醒", "催办", "未完成", "再次通知", "温馨提示"))


def _matches_task_session(item: RecruitmentTask, fields: dict) -> bool:
    item_event = _event_family(item.event)
    record_event = _event_family(_text(fields.get("事项类型")))
    interview_rounds = {"一面", "二面", "三面", "四面", "终面", "主管面", "HR面"}
    event_matches = (
        item_event == record_event
        or (item_event == "面试" and record_event in interview_rounds)
        or (record_event == "面试" and item_event in interview_rounds)
    )
    if _company_key(item.company) != _company_key(_text(fields.get("公司"))) or not event_matches:
        return False

    candidate_job = _job_key(item.job)
    record_job = _job_key(_text(fields.get("岗位")))
    if candidate_job and record_job and candidate_job != record_job:
        return False

    candidate_day = _deadline_day(item.deadline)
    record_day = _deadline_day(fields.get("截止时间"))
    if (
        fields.get("状态") == "已完成"
        and candidate_day
        and record_day
        and candidate_day != record_day
        and not _is_reminder(item)
    ):
        # A completed earlier session should not absorb a clearly new
        # invitation with a different scheduled/deadline date.
        return False
    return True


def _semantic_record(item: RecruitmentTask, records: list[dict]) -> dict | None:
    matches = [
        record for record in records
        if _matches_task_session(item, record.get("fields") or {})
    ]
    if not matches:
        return None
    # Reuse an open task first.  If it was already completed, that completion
    # remains the deduplication anchor for follow-up mail about the same round.
    matches.sort(key=lambda record: (
        (record.get("fields") or {}).get("状态") == "已完成",
        bool(_job_key(_text((record.get("fields") or {}).get("岗位"))) != _job_key(item.job)),
        -int((record.get("fields") or {}).get("完成时间") or 0),
    ))
    return matches[0]


def _is_result_notice(event: str, action: str, subject: str) -> bool:
    if event in {"淘汰", "录用"}:
        return True
    text = f"{action} {subject}"
    if event in {"测评", "笔试/机考"}:
        return any(word in text for word in (
            "结果通知", "结果查询", "成绩查询", "成绩通知", "考试结果", "测评结果",
            "成绩公布", "感谢参与", "答题结束",
        ))
    if event in {"面试", "面试已预约", "一面", "二面", "三面", "终面"}:
        return any(word in text for word in (
            "面试反馈", "反馈邀请", "满意度调查", "反馈问卷", "感谢参与面试",
        ))
    return False


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
    seen: set[tuple[str, str, str]] = set()
    for item in mail_items:
        company = canonical_company_name(item.get("company") or "")
        event = str(item.get("event") or "").strip()
        action = str(item.get("action") or "").strip()
        subject = str(item.get("subject") or "")
        if _is_result_notice(event, action, subject):
            continue
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
        job = str(item.get("job") or "").strip()
        semantic = (company_key, _event_family(event_label), _job_key(job))
        if semantic in seen:
            continue
        seen.add(semantic)
        source = str(item.get("subject") or item.get("archive_path") or event)
        source_key = str(item.get("mail_key") or item.get("archive_path") or "")
        # Mail subject and UID identify a message, not the user's action.  The
        # stable key follows the employer, stage/round, job, and session day.
        session = _deadline_day(deadline)
        identity = f"{company_key}|{_event_family(event_label)}|{_job_key(job)}|{session}"
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        result.append(RecruitmentTask(
            key=f"mail:{digest}",
            company=company,
            event=event_label,
            title=_title(company, event, action, expired=expired),
            action=action,
            source=source,
            job=job,
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
    {"field_name": "岗位", "type": 1},
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
    {"field_name": "手动优先级", "type": 1},
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
    for field_name in ("状态来源", "来源键", "处置状态", "手动优先级", "岗位"):
        if field_name not in fields:
            feishu.create_field(table_id, {"field_name": field_name, "type": 1})


def task_priority(event: str, manual: str = "") -> dict:
    """Return the effective priority; an explicit user choice wins."""
    manual = _text(manual).strip()
    if manual in PRIORITY_RANKS:
        label = manual
        source = "manual"
    else:
        event = _text(event)
        if "面试" in event or event in {"一面", "二面", "三面"}:
            label = "高"
        elif "笔试" in event or "机考" in event:
            label = "普通"
        else:
            label = "低"
        source = "automatic"
    return {"priority": label, "priority_rank": PRIORITY_RANKS[label], "priority_source": source}


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
        "岗位": item.job,
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
    existing_records = feishu.list_records(table_id)
    existing = {
        str((record.get("fields") or {}).get("任务键") or ""): record
        for record in existing_records
    }
    calendar_id = ""
    created = 0
    repaired = 0
    skipped = 0
    calendar_failed = 0
    for item in items:
        record = existing.get(item.key)
        if record is None:
            record = _semantic_record(item, existing_records)
        if record is not None:
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
                if item.job and not _text(fields.get("岗位")):
                    updates["岗位"] = item.job
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
            if item.job:
                updates["岗位"] = item.job
            if native.get("url"):
                updates["飞书任务链接"] = {"link": native["url"], "text": "打开任务"}
            feishu.update_record(
                str(record.get("record_id") or ""),
                updates,
                table_id=table_id,
            )
            repaired += 1
            continue
        # Reserve the durable ledger row before creating external objects.  If
        # Tasks succeeds but Calendar is unavailable, the next 15-minute sync
        # must see this row instead of creating another native task.
        reserved = feishu.create_record(_record_fields(item, {}, ""), table_id=table_id)
        record_id = str(reserved.get("record_id") or reserved.get("id") or "")
        if not record_id:
            raise RuntimeError(f"飞书未返回求职任务记录 ID：{item.title}")
        created += 1

        native = feishu.create_task(_task_payload(item, tasklist_guid, owner_id))
        if not native.get("guid"):
            raise RuntimeError(f"飞书未返回任务 GUID：{item.title}")
        task_updates = {
            "飞书任务GUID": native.get("guid") or "",
            "状态来源": "双向同步",
        }
        if native.get("url"):
            task_updates["飞书任务链接"] = {"link": native["url"], "text": "打开任务"}
        feishu.update_record(record_id, task_updates, table_id=table_id)

        calendar_event_id = ""
        if item.planned_at:
            try:
                if not calendar_id:
                    calendar_id = str(feishu.get_primary_calendar().get("calendar_id") or "")
                event = feishu.create_calendar_event(
                    calendar_id,
                    _calendar_payload(item),
                    idempotency_key="task-plan:" + hashlib.sha256(item.key.encode()).hexdigest()[:40],
                )
                calendar_event_id = str(event.get("event_id") or "")
                if calendar_event_id:
                    # Persist the event ID before inviting attendees.  Even if
                    # the invitation call fails, the event remains traceable.
                    feishu.update_record(
                        record_id,
                        {"计划日程ID": calendar_event_id},
                        table_id=table_id,
                    )
                    feishu.add_calendar_event_attendees(
                        calendar_id,
                        calendar_event_id,
                        [{"type": "user", "user_id": owner_id, "is_optional": False}],
                        user_id_type="union_id",
                        need_notification=True,
                    )
            except Exception:
                # Calendar is an optional early-finish aid.  A missing app bot
                # or a transient calendar error must not roll back the task
                # ledger and cause duplicate native tasks on the next sync.
                calendar_failed += 1
    return {
        "table_id": table_id,
        "tasklist_guid": tasklist_guid,
        "tasklist_url": tasklist.get("url") or "",
        "created": created,
        "repaired": repaired,
        "skipped": skipped,
        "calendar_failed": calendar_failed,
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


def set_task_priority(record_id: str, priority: str) -> dict:
    """Persist a dashboard priority override without touching native task state."""
    priority = _text(priority).strip()
    if priority not in MANUAL_PRIORITIES:
        raise ValueError("优先级只能是自动、高、普通或低")
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
    ensure_task_tracking_fields(table_id)
    feishu.update_record(record_id, {"手动优先级": priority}, table_id=table_id)
    event = _text((target.get("fields") or {}).get("事项类型"))
    return {"record_id": record_id, "manual_priority": priority, **task_priority(event, priority)}


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


def dashboard_data(*, force_reconcile: bool = False, allow_reconcile: bool = True) -> dict:
    """Return task rows and reconcile native completion at a bounded rate."""
    global _LAST_RECONCILE_AT
    table_id = find_task_table()
    if not table_id:
        return {"items": [], "pending_count": 0, "completed_count": 0, "last_reconciled": ""}
    monotonic_now = time.monotonic()
    should_reconcile = allow_reconcile and (
        force_reconcile or monotonic_now - _LAST_RECONCILE_AT >= RECONCILE_INTERVAL_SECONDS
    )
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
        manual_priority = _text(fields.get("手动优先级")).strip()
        priority = task_priority(_text(fields.get("事项类型")), manual_priority)
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
            "manual_priority": manual_priority if manual_priority in MANUAL_PRIORITIES else "",
            **priority,
            "deadline": fields.get("截止时间") or 0,
            "planned_at": fields.get("计划完成时间") or 0,
            "completed_at": fields.get("完成时间") or 0,
            "url": str(link or ""),
        })
    items.sort(key=lambda item: (
        item["status"] == "已完成",
        item["priority_rank"],
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


def collect_missing_candidates(
    now: datetime | None = None,
    mail_keys: set[str] | None = None,
) -> list[RecruitmentTask]:
    """Return active mail actions that still have no task-ledger association."""
    current = now or now_shanghai()
    task_state = dashboard_data(allow_reconcile=False)
    mail = mail_store.get_dashboard_data(
        current,
        handled_references=mail_store.completed_mail_references(task_state.get("items") or []),
        task_records=mail_store.mail_task_records(task_state.get("items") or []),
    )
    rows = [item for item in (mail.get("items") or []) if not item.get("task_record_id")]
    keys = {str(value).strip() for value in (mail_keys or set()) if str(value).strip()}
    if keys:
        rows = [item for item in rows if str(item.get("mail_key") or "") in keys]
    records = feishu.list_records(feishu.MAIN_TABLE_ID)
    return collect_candidates(rows, records, current)


def repair_missing_tasks(mail_key: str = "") -> dict:
    """Idempotently create missing task records for active mail actions."""
    key = str(mail_key or "").strip()
    items = collect_missing_candidates(mail_keys={key} if key else set())
    if key and not items:
        raise LookupError("该邮件已经关联任务，或已不在待办列表中")
    if not items:
        return {"candidates": 0, "created": 0, "repaired": 0, "skipped": 0}
    owner_id = os.getenv("FEISHU_REMINDER_USER_ID", DEFAULT_OWNER_ID).strip()
    return {"candidates": len(items), **apply_candidates(items, owner_id)}


def main() -> int:
    parser = argparse.ArgumentParser(description="同步秋招待办到飞书任务")
    parser.add_argument("--apply", action="store_true", help="创建任务清单、任务和计划日程")
    parser.add_argument(
        "--mail-key",
        action="append",
        default=[],
        help="仅同步指定邮件键对应的待办；可重复传入",
    )
    parser.add_argument(
        "--missing-only",
        action="store_true",
        help="仅补建当前看板中尚未关联求职任务的待办",
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
        mail_keys = {str(value).strip() for value in args.mail_key if str(value).strip()}
        if args.missing_only:
            items = collect_missing_candidates(now, mail_keys)
        else:
            records = feishu.list_records(feishu.MAIN_TABLE_ID)
            mail_items = mail_store.get_dashboard_data(now).get("items") or []
            if mail_keys:
                mail_items = [item for item in mail_items if str(item.get("mail_key") or "") in mail_keys]
            items = collect_candidates(mail_items, records, now)
        print(json.dumps({"count": len(items), "items": preview(items)}, ensure_ascii=False, indent=2))
        if args.apply:
            if items:
                owner_id = os.getenv("FEISHU_REMINDER_USER_ID", DEFAULT_OWNER_ID).strip()
                result = {"candidates": len(items), **apply_candidates(items, owner_id)}
            else:
                result = {"candidates": 0, "created": 0, "repaired": 0, "skipped": 0}
            print(json.dumps(result, ensure_ascii=False, indent=2))
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
