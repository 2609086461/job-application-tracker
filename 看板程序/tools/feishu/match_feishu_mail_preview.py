"""Match mail-derived status suggestions to Feishu records without writing changes."""

from __future__ import annotations

import json
import re
from datetime import datetime

from app import feishu
from company_aliases import company_key
from tools.mail.mail_pipeline import PENDING_DIR, ensure_directories, load_json, write_json


PREVIEW_FILE = PENDING_DIR / "飞书更新预览_最新.json"
MATCHED_FILE = PENDING_DIR / "飞书匹配结果_最新.json"
MATCHED_TEXT = PENDING_DIR / "飞书匹配结果_最新.txt"

def canonical_company(value: str) -> str:
    return company_key(value)


def text_value(value) -> str:
    if isinstance(value, dict):
        return str(value.get("text") or value.get("link") or "")
    return str(value or "")


def job_tokens(value: str) -> set[str]:
    value = re.sub(r"20\d{2}届|校招|校园招聘|提前批|秋招|正式|热招", "", value or "")
    parts = re.findall(r"[a-zA-Z+#]+|[\u4e00-\u9fff]{2,}", value.lower())
    return {part for part in parts if part not in {"工程师", "软件工程师", "开发工程师"}}


def job_score(mail_job: str, record_job: str) -> int:
    left, right = job_tokens(mail_job), job_tokens(record_job)
    if not left or not right:
        return 0
    return len(left & right) * 10 - abs(len(left) - len(right))


def pick_record(candidate: dict, records: list[dict]) -> tuple[dict | None, str]:
    target = canonical_company(candidate.get("company") or "")
    matches = [
        record for record in records
        if canonical_company(text_value(record.get("fields", {}).get("公司名称"))) == target
    ]
    if not matches:
        return None, "未找到同名公司"
    if len(matches) == 1:
        return matches[0], "公司名称唯一匹配"
    mail_job = candidate.get("job") or ""
    ranked = sorted(
        ((job_score(mail_job, text_value(record.get("fields", {}).get("秋招岗位"))), record) for record in matches),
        key=lambda item: item[0],
        reverse=True,
    )
    if ranked[0][0] > 0 and (len(ranked) == 1 or ranked[0][0] > ranked[1][0]):
        return ranked[0][1], "公司和岗位共同匹配"
    return None, "同一公司存在多个岗位，需人工选择"


def proposed_changes(candidate: dict, fields: dict) -> dict:
    output: dict = {}
    suggestion = candidate.get("suggested_fields") or {}
    additions = (suggestion.get("进展") or {}).get("add") or []
    progress = list(fields.get("进展") or [])
    merged = progress + [item for item in additions if item not in progress]
    if merged != progress:
        output["进展"] = {"before": progress, "after": merged}
    if suggestion.get("结果") and suggestion["结果"] != fields.get("结果"):
        output["结果"] = {"before": fields.get("结果") or "", "after": suggestion["结果"]}
    if suggestion.get("机考时间") and not fields.get("机考时间"):
        output["机考时间"] = {"before": "", "after": suggestion["机考时间"]}
    return output


def auto_apply_allowed(candidate: dict, record: dict | None) -> bool:
    return bool(
        record
        and candidate.get("analysis_provider") in ("openai", "codex_cli")
        and candidate.get("analysis_confidence") == "high"
    )


def render_text(payload: dict) -> str:
    lines = [
        "飞书邮件状态匹配结果",
        f"生成时间：{payload['generated_at']}",
        f"建议项：{len(payload['items'])}",
        "说明：仅匹配并预览，未修改飞书。",
        "",
    ]
    for index, item in enumerate(payload["items"], 1):
        lines.extend([
            "=" * 76,
            f"[{index:03d}] {item['company']} - {item['event']}",
            f"邮件岗位：{item.get('mail_job') or '未识别'}",
            f"飞书岗位：{item.get('feishu_job') or '未匹配'}",
            f"匹配结果：{item['match_status']}（{item['match_reason']}）",
            f"建议修改：{json.dumps(item.get('changes') or {}, ensure_ascii=False)}",
            f"截止时间：{item.get('deadline') or '未识别'}",
            f"下一步：{item.get('action')}",
            f"邮件原文：{item.get('mail_archive')}",
            f"邮件操作链接：{item.get('action_url') or '未识别'}",
            "",
        ])
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    ensure_directories()
    preview = load_json(PREVIEW_FILE, {})
    candidates = preview.get("candidates") or []
    if not candidates:
        print("没有可匹配的邮件建议。请先运行“同步邮箱并分析.bat”。")
        return 1
    try:
        records = feishu.list_records(feishu.MAIN_TABLE_ID)
    except Exception as exc:
        print(feishu.friendly_error(exc))
        return 1

    items = []
    for candidate in candidates:
        record, reason = pick_record(candidate, records)
        fields = record.get("fields", {}) if record else {}
        changes = proposed_changes(candidate, fields) if record else {}
        items.append({
            "mail_key": candidate.get("mail_key"),
            "company": candidate.get("company"),
            "event": candidate.get("event"),
            "mail_job": candidate.get("job"),
            "feishu_job": text_value(fields.get("秋招岗位")),
            "record_id": record.get("record_id") if record else "",
            "match_status": "已匹配" if record else "待人工匹配",
            "match_reason": reason,
            "changes": changes,
            "deadline": candidate.get("deadline"),
            "action": candidate.get("action"),
            "mail_archive": candidate.get("mail_archive"),
            "action_url": candidate.get("action_url"),
            "review_status": "待确认",
            "analysis_provider": candidate.get("analysis_provider", "rules"),
            "analysis_model": candidate.get("analysis_model", ""),
            "analysis_confidence": candidate.get("analysis_confidence", ""),
            "auto_apply_allowed": auto_apply_allowed(candidate, record),
        })

    payload = {
        "schema_version": 1,
        "generated_at": datetime.now().astimezone().isoformat(),
        "mode": "preview_only",
        "feishu_modified": False,
        "items": items,
    }
    write_json(MATCHED_FILE, payload)
    MATCHED_TEXT.write_text(render_text(payload), encoding="utf-8")
    matched = sum(item["match_status"] == "已匹配" for item in items)
    changed = sum(bool(item["changes"]) for item in items)
    print(f"匹配完成：{matched}/{len(items)} 项找到飞书记录，其中 {changed} 项存在建议修改。")
    print(f"查看：{MATCHED_TEXT}")
    print("本次未修改飞书。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
