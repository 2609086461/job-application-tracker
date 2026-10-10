from datetime import datetime, timezone
import unittest

from app.mail_store import (
    _current_mail_dashboard,
    completed_mail_references,
    mail_task_records,
)


class MailStoreTests(unittest.TestCase):
    def test_dashboard_uses_request_time_for_deadline_status(self):
        row = {
            "company": "示例公司",
            "event": "测评",
            "deadline": "2026-09-07T09:00:00+00:00",
            "received_at": "2026-09-01T09:00:00+00:00",
            "subject": "测评邀请",
            "archive_text": "公司投递/示例公司/mail.txt",
            # Deliberately stale values from a prior analysis run.
            "urgency": "注意",
            "urgency_rank": 20,
            "urgency_note": "剩余约 7 天",
        }

        before = _current_mail_dashboard(
            [row], datetime(2026, 9, 6, 10, tzinfo=timezone.utc)
        )
        after = _current_mail_dashboard(
            [row], datetime(2026, 9, 7, 10, tzinfo=timezone.utc)
        )

        self.assertEqual(before["items"][0]["urgency"], "紧急")
        self.assertEqual(after["items"][0]["urgency"], "已过期")
        self.assertEqual(after["urgent_count"], 1)

    def test_completed_task_removes_linked_mail_todo(self):
        row = {
            "company": "示例公司",
            "event": "测评",
            "received_at": "2026-09-01T09:00:00+00:00",
            "subject": "测评邀请",
            "archive_text": "公司投递/示例公司/mail.txt",
        }
        handled = completed_mail_references([{
            "company": "示例公司",
            "source": "测评邀请",
            "status": "已完成",
        }])

        result = _current_mail_dashboard(
            [row], datetime(2026, 9, 6, 10, tzinfo=timezone.utc), handled,
        )

        self.assertEqual(result["items"], [])
        self.assertEqual(result["action_count"], 0)

    def test_mail_key_links_task_even_when_company_display_changes(self):
        row = {
            "mail_key": "100:42",
            "company": "小米集团",
            "event": "测评",
            "received_at": "2026-09-01T09:00:00+00:00",
            "subject": "测评邀请",
            "archive_text": "公司投递/小米集团/mail.txt",
        }
        handled = completed_mail_references([{
            "company": "Xiaomi Hire",
            "source": "旧主题",
            "source_key": "100:42",
            "status": "已完成",
        }])

        result = _current_mail_dashboard(
            [row], datetime(2026, 9, 6, 10, tzinfo=timezone.utc), handled,
        )

        self.assertEqual(result["items"], [])

    def test_completed_legacy_task_without_mail_key_uses_company_and_subject_fallback(self):
        row = {
            "mail_key": "new:42",
            "company": "诺瓦星云",
            "event": "补充材料",
            "received_at": "2026-09-13T16:26:56+08:00",
            "subject": "邀请您更新简历",
            "archive_text": "公司投递/诺瓦星云/mail.txt",
        }
        handled = completed_mail_references([{
            "company": "诺瓦星云",
            "source": "邀请您更新简历",
            "source_key": "",
            "status": "已完成",
        }])

        result = _current_mail_dashboard(
            [row], datetime(2026, 9, 19, 10, tzinfo=timezone.utc), handled,
        )

        self.assertEqual(result["items"], [])

    def test_legacy_fallback_does_not_hide_a_different_subject(self):
        row = {
            "mail_key": "new:43",
            "company": "诺瓦星云",
            "event": "补充材料",
            "received_at": "2026-09-13T16:26:56+08:00",
            "subject": "请补充成绩单",
            "archive_text": "公司投递/诺瓦星云/transcript.txt",
        }
        handled = completed_mail_references([{
            "company": "诺瓦星云",
            "source": "邀请您更新简历",
            "source_key": "",
            "status": "已完成",
        }])

        result = _current_mail_dashboard(
            [row], datetime(2026, 9, 19, 10, tzinfo=timezone.utc), handled,
        )

        self.assertEqual(len(result["items"]), 1)

    def test_expired_mail_exposes_linked_task_for_resolution(self):
        row = {
            "mail_key": "100:42",
            "company": "示例公司",
            "event": "测评",
            "deadline": "2026-09-05T09:00:00+00:00",
            "received_at": "2026-09-01T09:00:00+00:00",
            "subject": "测评邀请",
            "archive_text": "公司投递/示例公司/mail.txt",
        }
        tasks = [{
            "record_id": "rec1",
            "company": "示例公司",
            "source": "测评邀请",
            "source_key": "100:42",
            "status": "待完成",
            "resolution": "待处置",
        }]

        result = _current_mail_dashboard(
            [row],
            datetime(2026, 9, 6, 10, tzinfo=timezone.utc),
            task_records=mail_task_records(tasks),
        )

        self.assertEqual(result["items"][0]["task_record_id"], "rec1")
        self.assertEqual(result["items"][0]["resolution"], "待处置")

    def test_interview_mail_uses_one_pending_semantic_task_when_mail_key_changed(self):
        row = {
            "mail_key": "new:2",
            "company": "迈瑞",
            "event": "面试已预约",
            "received_at": "2026-09-16T09:00:00+08:00",
            "subject": "面试安排",
            "archive_text": "公司投递/迈瑞/arranged.txt",
        }
        tasks = [{
            "record_id": "rec-interview",
            "company": "迈瑞医疗",
            "event": "一面",
            "source": "最初的面试邀请",
            "source_key": "old:1",
            "status": "待完成",
        }]

        result = _current_mail_dashboard(
            [row],
            datetime(2026, 9, 17, 9, tzinfo=timezone.utc),
            task_records=mail_task_records(tasks),
        )

        self.assertEqual(result["items"][0]["task_record_id"], "rec-interview")

    def test_ambiguous_pending_interviews_do_not_guess_a_task_record(self):
        row = {
            "mail_key": "new:2",
            "company": "示例公司",
            "event": "面试已预约",
            "received_at": "2026-09-16T09:00:00+08:00",
            "subject": "面试安排",
            "archive_text": "公司投递/示例公司/arranged.txt",
        }
        tasks = [
            {"record_id": "rec1", "company": "示例公司", "event": "一面", "source": "一面", "status": "待完成"},
            {"record_id": "rec2", "company": "示例公司", "event": "二面", "source": "二面", "status": "待完成"},
        ]

        result = _current_mail_dashboard(
            [row],
            datetime(2026, 9, 17, 9, tzinfo=timezone.utc),
            task_records=mail_task_records(tasks),
        )

        self.assertEqual(result["items"][0]["task_record_id"], "")


if __name__ == "__main__":
    unittest.main()
