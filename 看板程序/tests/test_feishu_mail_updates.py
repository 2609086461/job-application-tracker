import unittest

from tools.feishu.match_feishu_mail_preview import auto_apply_allowed

from tools.feishu.apply_feishu_mail_updates import build_updates


class FeishuMailUpdateTests(unittest.TestCase):
    def test_high_confidence_codex_result_can_auto_apply(self):
        candidate = {"analysis_provider": "codex_cli", "analysis_confidence": "high"}

        self.assertTrue(auto_apply_allowed(candidate, {"record_id": "rec1"}))
        self.assertFalse(
            auto_apply_allowed(
                {**candidate, "analysis_confidence": "low"},
                {"record_id": "rec1"},
            )
        )

    def test_automatic_mode_only_uses_explicitly_allowed_items(self):
        records = [{"record_id": "rec1", "fields": {"进展": ["已投递"]}}]
        blocked = {
            "record_id": "rec1",
            "company": "示例公司",
            "event": "淘汰",
            "changes": {"结果": {"before": "", "after": "挂"}},
            "auto_apply_allowed": False,
        }
        allowed = {
            **blocked,
            "auto_apply_allowed": True,
        }

        updates, _ = build_updates([blocked], records, auto_only=True)
        allowed_updates, _ = build_updates([allowed], records, auto_only=True)

        self.assertEqual(updates, {})
        self.assertEqual(allowed_updates, {"rec1": {"结果": "挂"}})


if __name__ == "__main__":
    unittest.main()
