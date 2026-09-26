import json
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from tools.mail import codex_batch_analyzer, scheduled_sync


def make_record(
    subject="在线测评通知",
    body="请在48小时内完成测评：https://hire.example.com/exam",
    sender="腾讯招聘 <hire@example.com>",
):
    message = EmailMessage()
    message["From"] = sender
    message["Subject"] = subject
    message["Date"] = "Sun, 06 Sep 2026 10:00:00 +0800"
    message["Message-ID"] = "<codex-batch@example.com>"
    message.set_content(body)
    return {
        "mail_key": "100:10",
        "uid": "10",
        "uidvalidity": "100",
        "message_id": "<codex-batch@example.com>",
        "message": message,
        "raw": message.as_bytes(),
    }


def decision(*, confidence="high", deep=False, company="腾讯"):
    return {
        "mail_key": "100:10",
        "is_recruitment": True,
        "company": company,
        "job": "嵌入式软件工程师",
        "event": "测评",
        "suggested_progress": "",
        "action": "在截止时间前完成测评",
        "deadline": "2026-09-08T10:00:00+08:00",
        "action_url": "https://hire.example.com/exam",
        "confidence": confidence,
        "evidence": ["请在48小时内完成测评"],
        "needs_deep_review": deep,
    }


class CodexBatchAnalyzerTests(unittest.TestCase):
    def test_unrelated_mail_skips_codex_entirely(self):
        record = make_record("周末吃饭", "周六下午老地方见。", "friend@example.com")
        with patch.object(codex_batch_analyzer, "_codex_binary", side_effect=AssertionError):
            result = codex_batch_analyzer.analyze_records_with_codex([record])

        self.assertFalse(result["100:10"]["is_recruitment"])
        self.assertEqual(result["100:10"]["analysis_provider"], "local_prefilter")

    def test_ambiguous_fast_decision_is_the_only_part_escalated(self):
        calls = []

        def fake_batches(items, **kwargs):
            calls.append((items, kwargs["model"], kwargs["effort"]))
            if kwargs["effort"] == "low":
                return {"100:10": decision(confidence="medium", deep=True, company="")}
            return {"100:10": decision(company="腾讯")}

        with patch.object(codex_batch_analyzer, "_codex_binary", return_value="/usr/bin/codex"), \
             patch.object(codex_batch_analyzer, "_assert_chatgpt_login"), \
             patch.object(codex_batch_analyzer, "_run_codex_batches", side_effect=fake_batches):
            result = codex_batch_analyzer.analyze_records_with_codex([make_record()])

        self.assertEqual(
            [(model, effort) for _, model, effort in calls],
            [("gpt-5.6-luna", "low"), ("gpt-5.6-terra", "medium")],
        )
        self.assertEqual(result["100:10"]["company"], "腾讯")
        self.assertEqual(result["100:10"]["analysis_model"], "gpt-5.6-terra")

    def test_gpt6_is_rejected_even_when_configured(self):
        with patch.dict("os.environ", {"MAIL_CODEX_FAST_MODEL": "gpt-6-astra"}, clear=False):
            with self.assertRaises(codex_batch_analyzer.CodexAnalysisError):
                codex_batch_analyzer.resolve_model(
                    "MAIL_CODEX_FAST_MODEL", codex_batch_analyzer.DEFAULT_FAST_MODEL
                )

    def test_codex_outage_defers_batch_by_default(self):
        messages = []
        analyzer = codex_batch_analyzer.build_codex_batch_analyzer(messages.append)
        with patch.object(
            codex_batch_analyzer,
            "analyze_records_with_codex",
            side_effect=codex_batch_analyzer.CodexAnalysisError("offline"),
        ):
            with self.assertRaises(codex_batch_analyzer.CodexAnalysisError):
                analyzer([make_record()])

        self.assertEqual(messages, [])

    def test_explicit_fallback_marks_rule_result_low_confidence(self):
        messages = []
        analyzer = codex_batch_analyzer.build_codex_batch_analyzer(
            messages.append,
            fallback_to_rules=True,
        )
        with patch.object(
            codex_batch_analyzer,
            "analyze_records_with_codex",
            side_effect=codex_batch_analyzer.CodexAnalysisError("offline"),
        ):
            result = analyzer([make_record()])

        self.assertEqual(result["100:10"]["analysis_provider"], "rules_fallback")
        self.assertEqual(result["100:10"]["analysis_confidence"], "low")
        self.assertEqual(len(messages), 1)


class ScheduledSyncTests(unittest.TestCase):
    def _run_with_state(self, last_run):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        state = Path(temporary.name) / "state.json"
        state.write_text(json.dumps({"last_run": last_run}), encoding="utf-8")
        calls = []
        patches = (
            patch.object(scheduled_sync, "STATE_FILE", state),
            patch.object(scheduled_sync, "run_module", side_effect=lambda module, *args: calls.append((module, args))),
            patch("sys.argv", ["scheduled_sync"]),
        )
        with patches[0], patches[1], patches[2]:
            result = scheduled_sync.main()
        return result, calls

    def test_no_new_uid_does_not_start_downstream_work(self):
        result, calls = self._run_with_state({"scanned": 0, "archived": 0})

        self.assertEqual(result, 0)
        self.assertEqual(calls, [("tools.mail.sync_recruitment_mail", ("--codex",))])

    def test_new_recruitment_mail_refreshes_and_matches(self):
        result, calls = self._run_with_state({"scanned": 2, "archived": 1})

        self.assertEqual(result, 0)
        self.assertEqual(calls[0], ("tools.mail.sync_recruitment_mail", ("--codex",)))
        self.assertIn(("tools.mail.analyze_recruitment_mail", ()), calls)
        self.assertIn(("tools.feishu.match_feishu_mail_preview", ()), calls)


if __name__ == "__main__":
    unittest.main()
