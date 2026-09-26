"""Analyze archived recruitment emails and build a review-only Feishu preview."""

from __future__ import annotations

import argparse
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path

from company_aliases import canonical_company_name, company_key
from tools.mail.mail_pipeline import (
    DASHBOARD_FILE,
    PENDING_DIR,
    STATE_FILE,
    SYNC_DIR,
    ensure_directories,
    iter_archived_metadata,
    load_json,
    refresh_urgency,
    write_json,
)


ACTION_EVENTS = {"测评", "笔试/机考", "面试", "面试已预约", "二面", "终面", "补充材料", "淘汰", "录用"}
MAIL_CONFIRMED_PROGRESS_EVENTS = {"面试已预约", "淘汰", "录用"}
BROWSER_REVIEW_FILE = SYNC_DIR / "browser_status_review_latest.json"


def apply_browser_review(rows: list[dict]) -> list[dict]:
    """Mark mail actions already completed according to reviewed browser evidence."""
    review = load_json(BROWSER_REVIEW_FILE, {})
    reviewed = {
        (company_key(item.get("company")), item.get("event")): item.get("status")
        for item in review.get("items", [])
    }
    output = []
    for row in rows:
        copy = dict(row)
        copy["company"] = canonical_company_name(copy.get("company"))
        status = reviewed.get((company_key(copy.get("company")), copy.get("event")))
        if status == "completed":
            copy.update(
                completion_status="已完成",
                urgency="已结束",
                urgency_rank=90,
                urgency_note="已通过浏览器记录确认完成",
            )
        elif status == "closed":
            copy.update(
                completion_status="入口已关闭",
                urgency="已结束",
                urgency_rank=90,
                urgency_note="已确认链接过期或流程关闭",
            )
        output.append(copy)
    return output


def deduplicate_updates(rows: list[dict]) -> list[dict]:
    """Keep every original mail, but collapse duplicate notices in the update preview."""
    booked_at: dict[str, str] = {}
    for row in rows:
        if row.get("event") == "面试已预约":
            company = company_key(row.get("company"))
            booked_at[company] = max(booked_at.get(company, ""), row.get("received_at") or "")

    selected: dict[tuple[str, str], dict] = {}
    for row in rows:
        row = dict(row)
        row["company"] = canonical_company_name(row.get("company"))
        company = company_key(row.get("company"))
        event = row.get("event", "")
        if event == "面试" and (row.get("received_at") or "") <= booked_at.get(company, ""):
            continue
        key = (company, event)
        current = selected.get(key)
        row_quality = (bool(row.get("deadline")), row.get("received_at") or "")
        current_quality = (bool(current and current.get("deadline")), (current or {}).get("received_at") or "")
        if not current or row_quality > current_quality:
            selected[key] = row
    return sorted(selected.values(), key=lambda row: (row.get("urgency_rank", 99), row.get("received_at", "")))


def render_analysis(rows: list[dict], generated_at: datetime, last_sync: str) -> str:
    counts = Counter(row.get("event") or "未知" for row in rows)
    actionable = [row for row in rows if row.get("event") in ACTION_EVENTS]
    lines = [
        "招聘邮件分析",
        f"生成时间：{generated_at.isoformat()}",
        f"邮箱最近成功同步：{last_sync or '尚未同步'}",
        f"已归档招聘邮件：{len(rows)} 封",
        "事件统计：" + "；".join(f"{name} {count}" for name, count in counts.items()),
        f"需要复核或处理：{len(actionable)} 项",
        "",
        "说明：本文件只提供分析和飞书更新建议，未修改飞书。",
        "",
    ]
    for index, row in enumerate(rows, 1):
        lines.extend([
            "=" * 76,
            f"[{index:03d}] {row.get('company') or '待识别'} - {row.get('event') or '未知'}",
            f"岗位：{row.get('job') or '待与飞书记录匹配'}",
            f"邮件时间：{row.get('received_at') or '未知'}",
            f"主题：{row.get('subject') or '(无主题)'}",
            f"判断：{row.get('category')} -> 建议进展 {row.get('suggested_progress') or '不变'}",
            f"紧急程度：{row.get('urgency')}（{row.get('urgency_note')}）",
            f"截止时间：{row.get('deadline') or '未识别'}",
            f"下一步：{row.get('action')}",
            f"邮件原文：{row.get('archive_text')}",
            f"操作链接：{row.get('action_url') or '未识别'}",
            "",
        ])
    return "\n".join(lines).rstrip() + "\n"


def build_preview(rows: list[dict], generated_at: datetime) -> dict:
    candidates = []
    for row in deduplicate_updates(rows):
        if row.get("event") not in ACTION_EVENTS:
            continue
        if row.get("completion_status") in ("已完成", "入口已关闭"):
            continue
        suggested_fields: dict = {}
        # An invitation means an action is pending, not that the candidate has
        # completed that stage. Browser review supplies completion evidence.
        progress = row.get("suggested_progress") if row.get("event") in MAIL_CONFIRMED_PROGRESS_EVENTS else ""
        if progress:
            suggested_fields["进展"] = {"add": [progress]}
        if row.get("event") == "淘汰":
            suggested_fields["结果"] = "挂"
        if row.get("event") == "笔试/机考" and row.get("deadline"):
            suggested_fields["机考时间"] = row["deadline"]
        candidates.append({
            "mail_key": row.get("mail_key"),
            "company": row.get("company"),
            "job": row.get("job"),
            "event": row.get("event"),
            "mail_time": row.get("received_at"),
            "deadline": row.get("deadline"),
            "urgency": row.get("urgency"),
            "action": row.get("action"),
            "mail_subject": row.get("subject"),
            "mail_archive": row.get("archive_text"),
            "mail_archive_url": row.get("archive_url"),
            "action_url": row.get("action_url"),
            "suggested_fields": suggested_fields,
            "review_status": "待确认",
            "analysis_provider": row.get("analysis_provider", "rules"),
            "analysis_model": row.get("analysis_model", ""),
            "analysis_confidence": row.get("analysis_confidence", ""),
        })
    return {
        "schema_version": 1,
        "generated_at": generated_at.isoformat(),
        "mode": "preview_only",
        "feishu_modified": False,
        "candidates": candidates,
    }


def build_dashboard(rows: list[dict], generated_at: datetime, last_sync: str) -> dict:
    actionable = [row for row in deduplicate_updates(rows) if row.get("event") in ACTION_EVENTS]
    active = [row for row in actionable if row.get("urgency") not in ("已结束",)]
    active.sort(key=lambda row: (row.get("urgency_rank", 99), row.get("deadline") or "9999"))
    return {
        "generated_at": generated_at.isoformat(),
        "last_sync_at": last_sync,
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
            }
            for row in active[:30]
        ],
    }


def run_analysis() -> dict:
    ensure_directories()
    generated_at = datetime.now().astimezone()
    state = load_json(STATE_FILE, {})
    last_sync = state.get("last_successful_sync_at", "")
    rows = [refresh_urgency(row, generated_at) for row in iter_archived_metadata()]
    rows = apply_browser_review(rows)

    text = render_analysis(rows, generated_at, last_sync)
    latest_text = PENDING_DIR / "招聘邮件分析_最新.txt"
    latest_text.write_text(text, encoding="utf-8")
    history_text = SYNC_DIR / "分析历史" / f"招聘邮件分析_{generated_at:%Y%m%d-%H%M%S}.txt"
    shutil.copyfile(latest_text, history_text)

    preview = build_preview(rows, generated_at)
    write_json(PENDING_DIR / "飞书更新预览_最新.json", preview)
    dashboard = build_dashboard(rows, generated_at, last_sync)
    write_json(DASHBOARD_FILE, dashboard)
    return {
        "rows": len(rows),
        "candidates": len(preview["candidates"]),
        "urgent": dashboard["urgent_count"],
        "analysis": str(latest_text),
        "preview": str(PENDING_DIR / "飞书更新预览_最新.json"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="分析本地招聘邮件归档并生成飞书更新预览")
    parser.parse_args()
    result = run_analysis()
    print(
        f"分析完成：归档 {result['rows']} 封，待确认更新 {result['candidates']} 项，"
        f"紧急/过期 {result['urgent']} 项。"
    )
    print(f"分析文本：{result['analysis']}")
    print(f"飞书预览：{result['preview']}")
    print("本次未修改飞书。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
