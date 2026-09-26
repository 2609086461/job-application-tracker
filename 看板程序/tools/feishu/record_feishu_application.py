"""Quickly create or update job applications in the configured Feishu Bitable."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import feishu
from company_aliases import canonical_company_name, company_key


from project_paths import BACKUP_DIR


def parse_record(text: str) -> dict[str, str]:
    parts = [part.strip() for part in re.split(r"\s*[|｜]\s*", text.strip())]
    if len(parts) != 3 or not all(parts):
        raise ValueError(f"格式错误：{text}\n请使用：公司｜岗位｜链接")
    company, job, link = parts
    if not re.match(r"^https?://", link, flags=re.I):
        raise ValueError(f"链接格式错误：{link}")
    return {"company": canonical_company_name(company), "job": job, "link": link}


def normalize_company(value: str) -> str:
    return company_key(value)


def date_to_milliseconds(value: str) -> int:
    parsed = datetime.strptime(value, "%Y-%m-%d")
    localized = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
    return int(localized.timestamp() * 1000)


def backup_records(records: list[dict]) -> Path:
    BACKUP_DIR.mkdir(exist_ok=True)
    output = BACKUP_DIR / f"feishu_records_backup_{datetime.now():%Y%m%d-%H%M%S}.json"
    payload = {
        "table_id": feishu.MAIN_TABLE_ID,
        "backup_time": datetime.now().isoformat(timespec="seconds"),
        "records": records,
    }
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return output


def build_fields(record: dict[str, str], application_date: str, existing=None) -> dict:
    existing = existing or {}
    progress = list(existing.get("进展") or [])
    if "已投递" not in progress:
        progress.append("已投递")

    fields = {
        "公司名称": record["company"],
        "秋招岗位": record["job"],
        "投递链接": {"link": record["link"], "text": record["link"]},
        "投递时间": date_to_milliseconds(application_date),
        "进展": progress,
    }
    return fields


def upsert_records(lines: list[str], application_date: str, dry_run=False) -> list[dict]:
    parsed = [parse_record(line) for line in lines]
    live_records = feishu.list_records(feishu.MAIN_TABLE_ID)
    backup_path = backup_records(live_records) if not dry_run else None
    by_company = {
        normalize_company(item.get("fields", {}).get("公司名称", "")): item
        for item in live_records
        if item.get("fields", {}).get("公司名称")
    }

    results = []
    for record in parsed:
        key = normalize_company(record["company"])
        existing_record = by_company.get(key)
        existing_fields = existing_record.get("fields", {}) if existing_record else {}
        fields = build_fields(record, application_date, existing_fields)

        if existing_record:
            action = "updated"
            if not dry_run:
                feishu.update_record(existing_record["record_id"], fields)
        else:
            action = "created"
            if not dry_run:
                created = feishu.create_record(fields)
                record_id = created.get("record_id") or created.get("id")
                by_company[key] = {"record_id": record_id, "fields": fields}

        results.append({
            "action": action,
            "company": record["company"],
            "job": record["job"],
            "backup": str(backup_path) if backup_path else None,
        })
    return results


def read_interactive() -> list[str]:
    print("请输入：公司｜岗位｜链接")
    print("可连续输入多条，空行后开始写入飞书。")
    lines = []
    while True:
        line = input("投递记录：").strip()
        if not line:
            break
        lines.append(line)
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description="记录投递到飞书多维表格")
    parser.add_argument("records", nargs="*", help="公司｜岗位｜链接")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    lines = args.records or read_interactive()
    if not lines:
        print("未输入记录，飞书表格没有修改。")
        return 0

    try:
        results = upsert_records(lines, args.date, args.dry_run)
    except Exception as exc:
        print(feishu.friendly_error(exc) if not isinstance(exc, ValueError) else str(exc))
        return 1

    labels = {"created": "已新增", "updated": "已更新"}
    for result in results:
        print(f"{labels[result['action']]}：{result['company']}｜{result['job']}")
    if results and results[0]["backup"]:
        print(f"写入前备份：{results[0]['backup']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
