import json
import unittest
from email.message import EmailMessage

from tools.mail.llm_analyzer import API_URL, analyze_message_with_llm


class FakeResponse:
    status_code = 200

    def __init__(self, decision):
        self.decision = decision

    def json(self):
        return {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": json.dumps(self.decision, ensure_ascii=False)}
                    ],
                }
            ]
        }


class FakeSession:
    def __init__(self, decision):
        self.decision = decision
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self.decision)


def make_message():
    message = EmailMessage()
    message["From"] = "腾讯招聘 <hire@example.com>"
    message["Subject"] = "在线测评通知"
    message["Date"] = "Sun, 06 Sep 2026 10:00:00 +0800"
    message["Message-ID"] = "<mail-1@example.com>"
    message.set_content("请在48小时内完成测评：https://hire.example.com/exam")
    return message


class LlmAnalyzerTests(unittest.TestCase):
    def test_structured_response_becomes_archived_metadata(self):
        decision = {
            "is_recruitment": True,
            "company": "腾讯",
            "job": "嵌入式软件工程师",
            "event": "测评",
            "suggested_progress": "",
            "action": "在截止时间前完成测评",
            "deadline": "2026-09-08T10:00:00+08:00",
            "action_url": "https://hire.example.com/exam",
            "confidence": "high",
            "evidence": ["请在48小时内完成测评"],
        }
        session = FakeSession(decision)
        message = make_message()

        result = analyze_message_with_llm(
            message,
            message.as_bytes(),
            "10",
            "100",
            api_key="test-key",
            session=session,
        )

        self.assertTrue(result["is_recruitment"])
        self.assertEqual(result["company"], "腾讯")
        self.assertEqual(result["deadline"], "2026-09-08T10:00+08:00")
        self.assertEqual(result["analysis_confidence"], "high")
        url, kwargs = session.calls[0]
        self.assertEqual(url, API_URL)
        self.assertFalse(kwargs["json"]["store"])
        self.assertEqual(kwargs["json"]["reasoning"], {"effort": "low"})
        self.assertEqual(kwargs["json"]["text"]["format"]["type"], "json_schema")

    def test_untrusted_url_and_invalid_deadline_block_auto_confidence(self):
        decision = {
            "is_recruitment": True,
            "company": "腾讯",
            "job": "",
            "event": "测评",
            "suggested_progress": "",
            "action": "完成测评",
            "deadline": "明天晚上",
            "action_url": "https://evil.example.com/",
            "confidence": "high",
            "evidence": [],
        }
        session = FakeSession(decision)
        message = make_message()

        result = analyze_message_with_llm(
            message,
            message.as_bytes(),
            "10",
            "100",
            api_key="test-key",
            session=session,
        )

        self.assertEqual(result["analysis_confidence"], "low")
        self.assertEqual(result["deadline"], "")
        self.assertEqual(result["action_url"], "")
        self.assertEqual(len(result["analysis_validation_issues"]), 2)

    def test_unrelated_personal_mail_stays_local(self):
        message = EmailMessage()
        message["From"] = "friend@example.com"
        message["Subject"] = "周末吃饭"
        message.set_content("周六下午老地方见。")
        session = FakeSession({})

        result = analyze_message_with_llm(
            message,
            message.as_bytes(),
            "11",
            "100",
            api_key="test-key",
            session=session,
        )

        self.assertFalse(result["is_recruitment"])
        self.assertEqual(result["analysis_provider"], "local_prefilter")
        self.assertEqual(session.calls, [])


if __name__ == "__main__":
    unittest.main()
