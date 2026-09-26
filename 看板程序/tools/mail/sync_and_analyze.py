"""One-click entry: sync QQ Mail, then refresh analysis and dashboard data."""

from __future__ import annotations

from tools.mail.analyze_recruitment_mail import run_analysis
from tools.mail.sync_recruitment_mail import main as sync_main


def main() -> int:
    result = sync_main()
    if result != 0:
        return result
    analysis = run_analysis()
    print(
        f"\n看板邮件待办已刷新：待确认 {analysis['candidates']} 项，"
        f"紧急/过期 {analysis['urgent']} 项。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
