"""Poll recruitment mail incrementally and process only newly appended UIDs.

This module is intended for a systemd timer.  It is safe to run repeatedly:
mail retrieval advances by IMAP UID.  Codex and downstream Feishu processing
are skipped completely when the mailbox contains no new messages.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

from tools.mail.mail_pipeline import STATE_FILE, load_json


MODULES = (
    "tools.feishu.match_feishu_mail_preview",
)


def use_shanghai_timezone() -> None:
    os.environ["TZ"] = "Asia/Shanghai"
    if hasattr(time, "tzset"):
        time.tzset()


def run_module(module: str, *args: str) -> None:
    completed = subprocess.run([sys.executable, "-m", module, *args], check=False)
    if completed.returncode:
        raise RuntimeError(f"{module} 执行失败（退出码 {completed.returncode}）")


def main() -> int:
    parser = argparse.ArgumentParser(description="按上海时间执行招聘邮箱与看板同步")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="确认将本次新增、高置信度且唯一匹配的邮件状态写入飞书；默认仅生成预览",
    )
    args = parser.parse_args()

    use_shanghai_timezone()
    try:
        run_module("tools.mail.sync_recruitment_mail", "--codex")
        run = load_json(STATE_FILE, {}).get("last_run") or {}
        scanned = int(run.get("scanned") or 0)
        archived = int(run.get("archived") or 0)
        archived_mail_keys = [
            str(mail_key).strip()
            for mail_key in (run.get("archived_mail_keys") or [])
            if str(mail_key).strip()
        ]
        if scanned == 0:
            print("增量检查完成：没有新邮件，未启动 Codex，也未刷新下游任务。")
            return 0
        if archived == 0:
            print(f"增量检查完成：发现 {scanned} 封新邮件，均非招聘邮件；无需刷新看板。")
            return 0

        run_module("tools.mail.analyze_recruitment_mail")
        for module in MODULES:
            run_module(module)
        if args.apply:
            run_module("tools.feishu.apply_feishu_mail_updates", "--confirm", "--new-only")
            # The main-table update and the native Feishu task are separate
            # projections of the same actionable mail.  Keep them together
            # whenever this scheduled run is permitted to write changes.
            task_args = ["--apply"]
            for mail_key in archived_mail_keys:
                task_args.extend(("--mail-key", mail_key))
            run_module("tools.feishu.sync_recruitment_tasks", *task_args)
    except RuntimeError as exc:
        print(f"定时同步失败：{exc}")
        return 1

    print(f"定时同步完成：{archived} 封新增招聘邮件已批量分析，邮件待办已刷新。")
    if args.apply:
        print("唯一匹配的飞书记录和邮件待办任务已同步；其余项目保留在预览中等待人工确认。")
    else:
        print("本次仅生成预览，未写入飞书。传入 --apply 才会执行写入。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
