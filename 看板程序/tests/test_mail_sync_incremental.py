import unittest
from datetime import date
from email.message import EmailMessage
from unittest.mock import Mock, patch

from tools.mail import scheduled_sync, sync_recruitment_mail
from tools.mail.mail_pipeline import analyze_message
from tools.mail.sync_recruitment_mail import limit_uids, search_uids_after, with_rule_fallback


class FakeClient:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.calls = []

    def uid(self, *args):
        self.calls.append(args)
        return "OK", [self.payload]


class FakeMailbox:
    def __init__(self, messages):
        self.messages = messages

    def login(self, *_):
        return "OK", []

    def select(self, *_args, **_kwargs):
        return "OK", []

    def response(self, name):
        return name, [b"100"]

    def uid(self, operation, *_args):
        if operation == "search":
            return "OK", [b"1 2"]
        uid = _args[0]
        return "OK", [(b"BODY[]", self.messages[uid])]

    def logout(self):
        return "BYE", []


class IncrementalMailSyncTests(unittest.TestCase):
    def test_scheduled_apply_syncs_native_tasks_after_mail_updates(self):
        with patch.object(scheduled_sync, "use_shanghai_timezone"), \
             patch.object(scheduled_sync, "run_module") as run_module, \
             patch.object(
                 scheduled_sync,
                 "load_json",
                 return_value={
                     "last_run": {
                         "scanned": 1,
                         "archived": 1,
                         "archived_mail_keys": ["123:456"],
                     },
                 },
             ), \
             patch("sys.argv", ["scheduled_sync", "--apply"]):
            self.assertEqual(scheduled_sync.main(), 0)

        self.assertEqual(
            [call.args for call in run_module.call_args_list],
            [
                ("tools.mail.sync_recruitment_mail", "--codex"),
                ("tools.mail.analyze_recruitment_mail",),
                ("tools.feishu.match_feishu_mail_preview",),
                ("tools.feishu.apply_feishu_mail_updates", "--confirm", "--new-only"),
                ("tools.feishu.sync_recruitment_tasks", "--apply", "--mail-key", "123:456"),
            ],
        )

    def test_uid_search_starts_after_last_successful_uid(self):
        client = FakeClient(b"42 43 44")

        result = search_uids_after(client, 41, 2000)

        self.assertEqual(result, [b"42", b"43", b"44"])
        self.assertEqual(client.calls, [("search", None, "UID", "42:*")])

    def test_limit_keeps_earliest_uids_to_avoid_skipping_a_gap(self):
        self.assertEqual(limit_uids([b"8", b"9", b"10"], 2), [b"8", b"9"])

    def test_uid_search_ignores_wildcard_echo_of_last_uid(self):
        client = FakeClient(b"41")

        self.assertEqual(search_uids_after(client, 41, 2000), [])

    def test_paid_analyzer_failure_disables_it_for_rest_of_run(self):
        calls = []

        def paid(*args):
            calls.append("paid")
            raise RuntimeError("HTTP 429")

        def rules(*args):
            calls.append("rules")
            return {"analysis_provider": "rules"}

        analyzer = with_rule_fallback(paid, rules, lambda _: None)

        self.assertEqual(analyzer(None, b"", "1", "100"), {"analysis_provider": "rules"})
        self.assertEqual(analyzer(None, b"", "2", "100"), {"analysis_provider": "rules"})
        self.assertEqual(calls, ["paid", "rules", "rules"])

    def test_new_messages_are_sent_to_one_batch_after_fetch(self):
        messages = {}
        for uid, subject in ((b"1", "在线测评通知"), (b"2", "周末聚餐")):
            message = EmailMessage()
            message["From"] = "hire@example.com" if uid == b"1" else "friend@example.com"
            message["Subject"] = subject
            message["Date"] = "Thu, 10 Sep 2026 10:00:00 +0800"
            message["Message-ID"] = f"<batch-{uid.decode()}@example.com>"
            message.set_content("请完成测评" if uid == b"1" else "周六见")
            messages[uid] = message.as_bytes()
        mailbox = FakeMailbox(messages)
        batch_calls = []

        def batch(records):
            batch_calls.append(records)
            return {
                record["mail_key"]: analyze_message(
                    record["message"], record["raw"], record["uid"], record["uidvalidity"]
                )
                for record in records
            }

        def archive(meta, _raw):
            return {**meta, "archive_text": "公司投递/测试/mail.txt"}

        inline = Mock(side_effect=AssertionError("inline analyzer must not run"))
        with patch.object(sync_recruitment_mail.imaplib, "IMAP4_SSL", return_value=mailbox), \
             patch.object(sync_recruitment_mail, "ensure_directories"), \
             patch.object(
                 sync_recruitment_mail,
                 "load_json",
                 side_effect=lambda path, default: (
                     {"schema_version": 1, "items": {}}
                     if path == sync_recruitment_mail.PROCESSED_FILE else {}
                 ),
             ), \
             patch.object(sync_recruitment_mail, "write_json"), \
             patch.object(sync_recruitment_mail, "archive_message", side_effect=archive), \
             patch.object(sync_recruitment_mail, "rebuild_company_indexes"), \
             patch.object(sync_recruitment_mail, "append_history"):
            result = sync_recruitment_mail.sync_mailbox(
                "candidate@example.com",
                "secret",
                date(2026, 9, 10),
                10,
                False,
                analyzer=inline,
                batch_analyzer=batch,
            )

        self.assertEqual(len(batch_calls), 1)
        self.assertEqual(len(batch_calls[0]), 2)
        self.assertEqual(result["last_run"]["scanned"], 2)
        self.assertEqual(result["last_run"]["archived"], 1)
        self.assertEqual(result["last_run"]["ignored"], 1)
        inline.assert_not_called()


if __name__ == "__main__":
    unittest.main()
