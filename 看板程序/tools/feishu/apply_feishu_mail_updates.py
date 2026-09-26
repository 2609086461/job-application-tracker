"""Apply reviewed recruitment-mail suggestions to Feishu with a fresh backup."""

from __future__ import annotations

import argparse
import json
from datetime import datetime

from app import feishu
from tools.mail.mail_pipeline import PENDING_DIR, STATE_FILE, SYNC_DIR, ensure_directories, load_json, write_json
from tools.feishu.record_feishu_application import backup_records


MATCHED_FILE = PENDING_DIR / "飞书匹配结果_最新.json"


def iso_to_milliseconds(value: str) -> int:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return int(parsed.timestamp() * 1000)


def build_updates(
    preview_items: list[dict],
    live_records: list[dict],
    *,
    auto_only: bool = False,
) -> tuple[dict, list[dict]]:
    live_by_id = {record.get("record_id"): record for record in live_records if record.get("record_id")}
    updates: dict[str, dict] = {}
    sources: list[dict] = []
    for item in preview_items:
        if auto_only and not item.get("auto_apply_allowed"):
            continue
        record_id = item.get("record_id")
        changes = item.get("changes") or {}
        live = live_by_id.get(record_id)
        if not live or not changes:
            continue
        current = live.get("fields", {})
        fields = updates.setdefault(record_id, {})

        progress_change = changes.get("进展") or {}
        additions = [
            value for value in progress_change.get("after", [])
            if value not in progress_change.get("before", [])
        ]
        if additions:
            progress = list(fields.get("进展", current.get("进展") or []))
            for value in additions:
                if value not in progress:
                    progress.append(value)
            fields["进展"] = progress

        result_change = changes.get("结果") or {}
        if result_change.get("after") and not current.get("结果"):
            fields["结果"] = result_change["after"]

        exam_change = changes.get("机考时间") or {}
        if exam_change.get("after") and not current.get("机考时间"):
            fields["机考时间"] = iso_to_milliseconds(exam_change["after"])

        sources.append({
            "record_id": record_id,
            "company": item.get("company"),
            "event": item.get("event"),
            "mail_archive": item.get("mail_archive"),
        })
    return {record_id: fields for record_id, fields in updates.items() if fields}, sources


def append_history(payload: dict) -> None:
    path = SYNC_DIR / "feishu_update_history.txt"
    lines = [
        "=" * 72,
        f"写入时间：{payload['applied_at']}",
        f"写入前备份：{payload['backup']}",
        f"成功：{payload['success_count']} 条记录；失败：{payload['failure_count']} 条记录",
    ]
    for item in payload["results"]:
        lines.append(
            f"- {item['company']}：{item['status']}；字段={','.join(item.get('fields') or [])}"
        )
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(existing + "\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="将已确认的邮件状态建议写入飞书")
    parser.add_argument("--confirm", action="store_true", help="确认执行真实写入")
    parser.add_argument("--new-only", action="store_true", help="仅自动写入本次同步新增且高置信度的邮件建议")
    args = parser.parse_args()
    if not args.confirm:
        print("未提供 --confirm，本次未写入飞书。")
        return 1

    ensure_directories()
    preview = load_json(MATCHED_FILE, {})
    items = preview.get("items") or []
    if args.new_only:
        state = load_json(STATE_FILE, {})
        mail_keys = set((state.get("last_run") or {}).get("archived_mail_keys") or [])
        items = [
            item for item in items
            if item.get("mail_key") in mail_keys
            and item.get("auto_apply_allowed")
            and item.get("changes")
        ]
    if not items:
        if args.new_only:
            print("本次同步没有新增且符合自动写入条件的邮件建议。")
            return 0
        print("没有可写入的匹配预览，请先运行“生成飞书更新预览.bat”。")
        return 1

    try:
        live_records = feishu.list_records(feishu.MAIN_TABLE_ID)
        backup_path = backup_records(live_records)
        updates, sources = build_updates(items, live_records, auto_only=args.new_only)
    except Exception as exc:
        print(feishu.friendly_error(exc))
        return 1

    source_by_record: dict[str, list[dict]] = {}
    for source in sources:
        source_by_record.setdefault(source["record_id"], []).append(source)

    results = []
    for record_id, fields in updates.items():
        source_items = source_by_record.get(record_id) or []
        company = source_items[0].get("company") if source_items else record_id
        try:
            feishu.update_record(record_id, fields)
            status = "成功"
            error = ""
        except Exception as exc:
            status = "失败"
            error = feishu.friendly_error(exc)
        results.append({
            "record_id": record_id,
            "company": company,
            "status": status,
            "fields": list(fields),
            "values": fields,
            "sources": source_items,
            "error": error,
        })

    applied_at = datetime.now().astimezone()
    payload = {
        "schema_version": 1,
        "applied_at": applied_at.isoformat(),
        "backup": str(backup_path),
        "success_count": sum(item["status"] == "成功" for item in results),
        "failure_count": sum(item["status"] == "失败" for item in results),
        "results": results,
    }
    timestamped = PENDING_DIR / f"飞书写入结果_{applied_at:%Y%m%d-%H%M%S}.json"
    write_json(timestamped, payload)
    write_json(PENDING_DIR / "飞书写入结果_最新.json", payload)
    append_history(payload)

    print(
        f"飞书写入完成：成功 {payload['success_count']} 条记录，"
        f"失败 {payload['failure_count']} 条记录。"
    )
    print(f"写入前备份：{backup_path}")
    print(f"结果记录：{timestamped}")
    return 0 if payload["failure_count"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
