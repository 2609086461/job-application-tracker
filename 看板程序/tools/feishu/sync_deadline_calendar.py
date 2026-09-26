"""Synchronize explicit application deadlines to Feishu Calendar.

Each calendar event carries two native Feishu reminders: 24 hours and 3 hours
before the deadline.  The module is preview-only unless ``--apply`` is passed.
The source-to-event mapping lives in persistent business data, so releases do
not create duplicate calendar events.

Mail actions such as interviews and assessments already exist as native Feishu
Tasks with their real due time.  They are intentionally not duplicated here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable
from zoneinfo import ZoneInfo

from app import feishu
from company_aliases import canonical_company_name
from project_paths import DATA_DIR
from tools.mail.mail_pipeline import load_json, write_json


SHANGHAI = ZoneInfo("Asia/Shanghai")
STATE_FILE = DATA_DIR / "同步记录" / "deadline_calendar_state.json"
REMINDERS = ({"minutes": 24 * 60}, {"minutes": 3 * 60})
CLOSED_RESULTS = ("挂", "拒绝", "淘汰", "放弃", "撤回", "已结束")
# This is the owner's stable union ID.  The environment variable keeps the
# personal tracker portable without relying on app-specific open IDs.
DEFAULT_REMINDER_USER_ID = "on_a8a3c0ba2123bbbcce8eaea9c05e6bee"


@dataclass(frozen=True)
class CalendarItem:
    key: str
    company: str
    job: str
    event: str
    deadline: datetime
    action: str = ""
    url: str = ""


def now_shanghai() -> datetime:
    return datetime.now(SHANGHAI)


def _text(value) -> str:
    if isinstance(value, dict):
        return str(value.get("text") or value.get("link") or "")
    return str(value or "")


def _timestamp_deadline(value) -> datetime | None:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    if timestamp <= 0:
        return None
    if timestamp > 10_000_000_000:
        timestamp /= 1000
    try:
        return datetime.fromtimestamp(timestamp, tz=SHANGHAI)
    except (OverflowError, OSError, ValueError):
        return None


def _iso_deadline(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI)


def _stable_key(prefix: str, value: str) -> str:
    # Feishu Calendar requires idempotency_key to contain at least 32 chars.
    digest = hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:40]
    return f"{prefix}:{digest}"


def _is_closed(fields: dict) -> bool:
    result = _text(fields.get("结果"))
    return any(status in result for status in CLOSED_RESULTS)


def bitable_items(records: Iterable[dict]) -> list[CalendarItem]:
    items: list[CalendarItem] = []
    for record in records:
        fields = record.get("fields") or {}
        deadline = _timestamp_deadline(fields.get("投递截止时间"))
        company = canonical_company_name(_text(fields.get("公司名称")))
        record_id = str(record.get("record_id") or "")
        if not deadline or not company or not record_id or _is_closed(fields):
            continue
        items.append(CalendarItem(
            key=_stable_key("bitable", record_id),
            company=company,
            job=_text(fields.get("秋招岗位")),
            event="投递截止",
            deadline=deadline,
            action="完成投递",
            url=_text(fields.get("投递链接")),
        ))
    return items


def mail_items(items: Iterable[dict]) -> list[CalendarItem]:
    result: list[CalendarItem] = []
    for item in items:
        deadline = _iso_deadline(item.get("deadline"))
        company = canonical_company_name(item.get("company") or "")
        source = str(item.get("archive_path") or item.get("subject") or "")
        if not deadline or not company or not source:
            continue
        result.append(CalendarItem(
            key=_stable_key("mail", source),
            company=company,
            job=str(item.get("job") or ""),
            event=str(item.get("event") or "招聘事项"),
            deadline=deadline,
            action=str(item.get("action") or ""),
            url=str(item.get("action_url") or ""),
        ))
    return result


def collect_items(now: datetime) -> list[CalendarItem]:
    records = feishu.list_records(feishu.MAIN_TABLE_ID)
    return bitable_items(records)


def event_payload(item: CalendarItem) -> dict:
    # A 23:59 deadline followed by a 15-minute duration crosses midnight and
    # Feishu renders it as "day 1 / day 2".  Make the deadline the event end
    # instead, so every deadline remains a single-day calendar item.
    start = item.deadline - timedelta(minutes=15)
    description = [
        f"公司：{item.company}",
        f"事项：{item.event}",
    ]
    if item.job:
        description.append(f"岗位：{item.job}")
    if item.action:
        description.append(f"下一步：{item.action}")
    if item.url:
        description.append(f"链接：{item.url}")
    return {
        "summary": f"【求职截止】{item.company} · {item.event}",
        "description": "\n".join(description),
        "start_time": {
            "timestamp": str(int(start.timestamp())),
            "timezone": "Asia/Shanghai",
        },
        "end_time": {
            "timestamp": str(int(item.deadline.timestamp())),
            "timezone": "Asia/Shanghai",
        },
        "reminders": list(REMINDERS),
        "free_busy_status": "free",
        "visibility": "default",
        "need_notification": False,
    }


def fingerprint(item: CalendarItem) -> str:
    payload = json.dumps(event_payload(item), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_plan(items: Iterable[CalendarItem], state: dict, now: datetime) -> list[dict]:
    known = state.get("items") or {}
    plan: list[dict] = []
    seen: set[str] = set()
    for item in sorted(items, key=lambda value: value.deadline):
        if item.key in seen:
            continue
        seen.add(item.key)
        previous = known.get(item.key) or {}
        # Never create a new event after its deadline, but allow a one-time
        # repair of an existing event when the payload format changes.
        if item.deadline <= now and not previous.get("event_id"):
            continue
        current_fingerprint = fingerprint(item)
        if not previous.get("event_id"):
            action = "create"
        elif previous.get("fingerprint") != current_fingerprint:
            action = "update"
        else:
            action = "unchanged"
        plan.append({"action": action, "item": item, "previous": previous, "fingerprint": current_fingerprint})
    return plan


def apply_plan(
    calendar_id: str,
    plan: Iterable[dict],
    state: dict,
    now: datetime,
    *,
    reminder_user_id: str,
    reminder_user_id_type: str,
) -> dict:
    saved = dict(state.get("items") or {})
    for step in plan:
        action = step["action"]
        item = step["item"]
        if action == "unchanged":
            continue
        if action == "create":
            created = feishu.create_calendar_event(
                calendar_id,
                event_payload(item),
                idempotency_key=item.key,
            )
            event_id = created.get("event_id")
            if not event_id:
                raise RuntimeError(f"飞书未返回日程 ID：{item.company}")
            feishu.add_calendar_event_attendees(
                calendar_id,
                event_id,
                [{"type": "user", "user_id": reminder_user_id, "is_optional": False}],
                user_id_type=reminder_user_id_type,
                need_notification=True,
            )
        else:
            event_id = step["previous"]["event_id"]
            feishu.update_calendar_event(calendar_id, event_id, event_payload(item))
        saved[item.key] = {
            "event_id": event_id,
            "fingerprint": step["fingerprint"],
            "deadline": item.deadline.isoformat(),
            "company": item.company,
            "event": item.event,
            "synced_at": now.isoformat(),
        }
        write_json(STATE_FILE, {"schema_version": 1, "items": saved})
    return {"schema_version": 1, "items": saved}


def remove_mail_events(calendar_id: str, state: dict) -> dict:
    """Delete legacy calendar events that duplicate native recruitment tasks."""
    saved = dict(state.get("items") or {})
    targets = [
        (key, value) for key, value in saved.items()
        if key.startswith("mail:") and value.get("event_id")
    ]
    deleted = 0
    for key, value in targets:
        feishu.delete_calendar_event(calendar_id, str(value["event_id"]))
        saved.pop(key, None)
        deleted += 1
        write_json(STATE_FILE, {"schema_version": 1, "items": saved})
    return {"deleted": deleted, "remaining": len(saved)}


def preview(plan: Iterable[dict]) -> list[dict]:
    return [
        {
            "operation": step["action"],
            "company": step["item"].company,
            "job": step["item"].job,
            "event": step["item"].event,
            "deadline": step["item"].deadline.isoformat(),
        }
        for step in plan
        if step["action"] != "unchanged"
    ]


def resolve_calendar_id() -> str:
    configured = os.getenv("FEISHU_CALENDAR_ID", "").strip()
    if configured:
        return configured
    calendar = feishu.get_primary_calendar()
    calendar_id = str(calendar.get("calendar_id") or "").strip()
    if not calendar_id:
        raise RuntimeError("飞书应用主日历缺少 calendar_id")
    return calendar_id


def main() -> int:
    parser = argparse.ArgumentParser(description="同步求职截止时间到飞书日历")
    parser.add_argument("--apply", action="store_true", help="确认创建或更新飞书日程；默认仅预览")
    parser.add_argument(
        "--remove-mail-events",
        action="store_true",
        help="删除与飞书任务重复的历史邮件截止日程",
    )
    args = parser.parse_args()

    now = now_shanghai()
    try:
        items = collect_items(now)
        state = load_json(STATE_FILE, {"schema_version": 1, "items": {}})
        plan = build_plan(items, state, now)
    except Exception as exc:
        print(feishu.friendly_error(exc))
        return 1

    changes = preview(plan)
    creates = sum(item["operation"] == "create" for item in changes)
    updates = sum(item["operation"] == "update" for item in changes)
    print(json.dumps({"create": creates, "update": updates, "items": changes}, ensure_ascii=False, indent=2))
    if not args.apply and not args.remove_mail_events:
        print("以上为预览；传入 --apply 才会修改飞书日历。")
        return 0

    try:
        calendar_id = resolve_calendar_id()
        reminder_user_id = os.getenv("FEISHU_REMINDER_USER_ID", DEFAULT_REMINDER_USER_ID).strip()
        reminder_user_id_type = os.getenv("FEISHU_REMINDER_USER_ID_TYPE", "union_id").strip()
        if not reminder_user_id:
            raise RuntimeError("未配置接收日历提醒的飞书用户 ID")
        if args.remove_mail_events:
            result = remove_mail_events(calendar_id, state)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.apply:
            apply_plan(
                calendar_id,
                plan,
                state,
                now,
                reminder_user_id=reminder_user_id,
                reminder_user_id_type=reminder_user_id_type,
            )
    except Exception as exc:
        print(feishu.friendly_error(exc))
        return 1
    if args.apply:
        print(f"飞书日历同步完成：新建 {creates} 条，更新 {updates} 条。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
