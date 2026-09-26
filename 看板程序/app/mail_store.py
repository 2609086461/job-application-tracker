"""Read local mail analysis and expose safe, text-only mail details."""

import json
import os
from datetime import datetime

from company_aliases import company_key
from project_paths import DATA_DIR, resolve_data_path
from tools.mail.analyze_recruitment_mail import (
    ACTION_EVENTS,
    apply_browser_review,
    deduplicate_updates,
)
from tools.mail.mail_pipeline import iter_archived_metadata, refresh_urgency

PROJECT_DIR = str(DATA_DIR)
DASHBOARD_FILE = os.path.join(PROJECT_DIR, "同步记录", "mail_dashboard.json")
ARCHIVE_DIR = os.path.join(PROJECT_DIR, "公司投递")


def _load_json(path: str, default=None):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


def _resolve_text_path(archive_path: str) -> str:
    if not isinstance(archive_path, str) or not archive_path.strip():
        raise ValueError("缺少邮件归档路径")

    candidate = str(resolve_data_path(archive_path, PROJECT_DIR))
    archive_root = os.path.realpath(ARCHIVE_DIR)
    try:
        inside_archive = os.path.commonpath([archive_root, candidate]) == archive_root
    except ValueError:
        inside_archive = False
    if not inside_archive or os.path.splitext(candidate)[1].lower() != ".txt":
        raise ValueError("邮件路径不在允许的归档目录内")
    if not os.path.isfile(candidate):
        raise FileNotFoundError("邮件原文不存在，可能需要重新同步邮箱")
    return candidate


def _metadata_for(text_path: str) -> dict:
    value = _load_json(os.path.splitext(text_path)[0] + ".json", {})
    return value if isinstance(value, dict) else {}


def _mail_body(text: str) -> str:
    marker = "邮件正文\n"
    if marker not in text:
        return text.strip()
    body = text.split(marker, 1)[1]
    body = body.lstrip("=\r\n ")
    if "\n邮件内链接\n" in body:
        body = body.split("\n邮件内链接\n", 1)[0]
    return body.strip()


def _mail_reference(company, source, source_key="") -> tuple[str, str]:
    if source_key:
        return "mail_key", str(source_key).strip()
    return company_key(company), str(source or "").strip()


def completed_mail_references(task_items: list[dict]) -> set[tuple[str, str]]:
    """Return mail references whose linked recruitment task is complete."""
    return {
        _mail_reference(item.get("company"), item.get("source"), item.get("source_key"))
        for item in task_items
        if item.get("status") == "已完成" and item.get("source")
    }


def mail_task_records(task_items: list[dict]) -> dict[tuple[str, str], dict]:
    """Index linked task state by stable mail reference for dashboard actions."""
    return {
        _mail_reference(item.get("company"), item.get("source"), item.get("source_key")): item
        for item in task_items
        if item.get("source")
    }


def _current_mail_dashboard(
    rows: list[dict],
    now: datetime,
    handled_references: set[tuple[str, str]] | None = None,
    task_records: dict[tuple[str, str], dict] | None = None,
) -> dict:
    """Build live mail to-dos without reading the mailbox or writing data."""
    handled_references = handled_references or set()
    task_records = task_records or {}
    refreshed = [refresh_urgency(row, now) for row in rows]
    reviewed = apply_browser_review(refreshed)
    actionable = [row for row in deduplicate_updates(reviewed) if row.get("event") in ACTION_EVENTS]
    active = [
        row for row in actionable
        if row.get("urgency") != "已结束"
        and _mail_reference(
            row.get("company"),
            row.get("subject") or row.get("archive_text") or row.get("event"),
            row.get("mail_key"),
        ) not in handled_references
    ]
    active.sort(key=lambda row: (row.get("urgency_rank", 99), row.get("deadline") or "9999"))
    return {
        "generated_at": now.isoformat(),
        "archived_count": len(rows),
        "action_count": len(active),
        "urgent_count": sum(row.get("urgency") in ("紧急", "优先", "已过期") for row in active),
        "items": [
            {
                "company": row.get("company"),
                "job": row.get("job"),
                "event": row.get("event"),
                "urgency": row.get("urgency"),
                "urgency_note": row.get("urgency_note"),
                "mail_time": row.get("received_at"),
                "deadline": row.get("deadline"),
                "action": row.get("action"),
                "subject": row.get("subject"),
                "archive_path": row.get("archive_text"),
                "action_url": row.get("action_url"),
                "mail_key": row.get("mail_key"),
                "task_record_id": str((task_records.get(_mail_reference(
                    row.get("company"),
                    row.get("subject") or row.get("archive_text") or row.get("event"),
                    row.get("mail_key"),
                )) or {}).get("record_id") or ""),
                "resolution": str((task_records.get(_mail_reference(
                    row.get("company"),
                    row.get("subject") or row.get("archive_text") or row.get("event"),
                    row.get("mail_key"),
                )) or {}).get("resolution") or ""),
            }
            for row in active[:30]
        ],
    }


def get_dashboard_data(
    now: datetime | None = None,
    handled_references: set[tuple[str, str]] | None = None,
    task_records: dict[tuple[str, str], dict] | None = None,
) -> dict:
    default = {
        "generated_at": "",
        "last_sync_at": "",
        "archived_count": 0,
        "action_count": 0,
        "urgent_count": 0,
        "items": [],
    }
    try:
        value = _load_json(DASHBOARD_FILE, default)
        result = {**default, **value} if isinstance(value, dict) else default
        # The on-disk dashboard is a sync snapshot. Rebuild the time-sensitive
        # portion on every API request so deadlines never use the last sync time.
        current = _current_mail_dashboard(
            iter_archived_metadata(),
            now or datetime.now().astimezone(),
            handled_references,
            task_records,
        )
        result.update(current)
        enriched_items = []
        for item in result.get("items", []):
            if not isinstance(item, dict):
                continue
            enriched = dict(item)
            try:
                metadata = _metadata_for(_resolve_text_path(item.get("archive_path", "")))
            except (ValueError, FileNotFoundError):
                metadata = {}
            enriched["action_url"] = metadata.get("action_url", "")
            enriched_items.append(enriched)
        result["items"] = enriched_items
        return result
    except OSError:
        return default


def get_mail_detail(archive_path: str) -> dict:
    text_path = _resolve_text_path(archive_path)
    metadata = _metadata_for(text_path)
    with open(text_path, "r", encoding="utf-8", errors="replace") as handle:
        text = handle.read(256 * 1024)
    return {
        "company": metadata.get("company", ""),
        "job": metadata.get("job", ""),
        "event": metadata.get("event", ""),
        "subject": metadata.get("subject", ""),
        "sender": metadata.get("sender", ""),
        "mail_time": metadata.get("received_at") or metadata.get("mail_time", ""),
        "deadline": metadata.get("deadline", ""),
        "action": metadata.get("action", ""),
        "action_url": metadata.get("action_url", ""),
        "content": _mail_body(text),
    }
