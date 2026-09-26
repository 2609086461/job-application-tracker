"""Preview or apply safe company-name normalization in the Feishu main table."""

from __future__ import annotations

import argparse
import json

from app import feishu
from company_aliases import canonical_company_name, company_key


def _text(value) -> str:
    if isinstance(value, dict):
        return str(value.get("text") or value.get("link") or "")
    return str(value or "")


def build_plan(records: list[dict]) -> dict:
    changes = []
    groups: dict[str, list[dict]] = {}
    for record in records:
        fields = record.get("fields") or {}
        original = _text(fields.get("公司名称")).strip()
        if not original:
            continue
        canonical = canonical_company_name(original)
        key = company_key(canonical)
        groups.setdefault(key, []).append({
            "record_id": str(record.get("record_id") or ""),
            "company": original,
            "job": _text(fields.get("秋招岗位")),
        })
        if canonical != original:
            changes.append({
                "record_id": str(record.get("record_id") or ""),
                "before": original,
                "after": canonical,
            })

    duplicate_groups = [
        {
            "canonical_company": canonical_company_name(items[0]["company"]),
            "records": items,
        }
        for items in groups.values()
        if len(items) > 1
    ]
    return {
        "rename_count": len(changes),
        "changes": changes,
        # Multiple jobs at one company are valid. Report them for review rather
        # than deleting or merging application records automatically.
        "same_company_groups": duplicate_groups,
    }


def apply_plan(plan: dict) -> int:
    updated = 0
    for change in plan.get("changes") or []:
        feishu.update_record(
            change["record_id"],
            {"公司名称": change["after"]},
            table_id=feishu.MAIN_TABLE_ID,
        )
        updated += 1
    return updated


def main() -> int:
    parser = argparse.ArgumentParser(description="统一飞书主表中的公司别名")
    parser.add_argument("--apply", action="store_true", help="写入明确的公司名称重命名")
    args = parser.parse_args()
    try:
        plan = build_plan(feishu.list_records(feishu.MAIN_TABLE_ID))
        result = {**plan, "applied": apply_plan(plan) if args.apply else 0}
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as exc:
        print(feishu.friendly_error(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
