"""Reclassify local .eml archives without connecting to the mailbox."""

from __future__ import annotations

import email

from tools.mail.analyze_recruitment_mail import run_analysis
from tools.mail.mail_pipeline import ROOT, analyze_message, archive_message, iter_archived_metadata, rebuild_company_indexes
from project_paths import resolve_data_path


def main() -> int:
    rows = iter_archived_metadata()
    updated = 0
    missing = 0
    for row in rows:
        eml_path = resolve_data_path(row.get("archive_eml", ""), ROOT)
        if not eml_path.is_file():
            missing += 1
            continue
        raw = eml_path.read_bytes()
        message = email.message_from_bytes(raw)
        meta = analyze_message(
            message,
            raw,
            str(row.get("uid") or "unknown"),
            str(row.get("uidvalidity") or "unknown"),
        )
        if not meta:
            continue
        archive_message(meta, raw)
        updated += 1

    rebuild_company_indexes()
    analysis = run_analysis()
    print(f"本地重分类完成：处理 {updated} 封，缺失原始邮件 {missing} 封。")
    print(f"当前有效归档 {analysis['rows']} 封，待确认 {analysis['candidates']} 项。")
    print("未连接QQ邮箱，未修改飞书。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
