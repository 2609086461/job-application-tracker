import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from app.routers import feishu_oauth


class FeishuOauthTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.env = patch.dict(
            os.environ,
            {
                "FEISHU_APP_ID": "cli_test",
                "FEISHU_APP_SECRET": "secret",
                "JOB_TRACKER_PRIVATE_STATE_DIR": self.tempdir.name,
            },
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tempdir.cleanup()

    def test_authorization_url_and_one_time_state(self):
        url = feishu_oauth.create_authorization_url(now=1000)
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query["app_id"], ["cli_test"])
        self.assertIn("task:task:write", query["scope"][0])
        state = query["state"][0]
        feishu_oauth._consume_state(state, now=1001)
        with self.assertRaisesRegex(ValueError, "不存在"):
            feishu_oauth._consume_state(state, now=1001)

    def test_exchange_code_stores_tokens_privately(self):
        app_response = Mock()
        app_response.json.return_value = {
            "code": 0,
            "app_access_token": "a-token",
        }
        user_response = Mock()
        user_response.json.return_value = {
            "code": 0,
            "data": {
                "access_token": "u-token",
                "refresh_token": "r-token",
                "expires_in": 7200,
                "refresh_expires_in": 3600 * 24 * 30,
                "open_id": "ou_test",
                "union_id": "on_test",
            },
        }
        with patch.object(
            feishu_oauth.requests,
            "post",
            side_effect=[app_response, user_response],
        ):
            result = feishu_oauth.exchange_code("code", now=1000)
        self.assertEqual(result["open_id"], "ou_test")
        stored = json.loads(Path(feishu_oauth.token_file()).read_text(encoding="utf-8"))
        self.assertEqual(stored["access_token"], "u-token")
        self.assertEqual(stored["expires_at"], 8200)
        self.assertEqual(oct(feishu_oauth.token_file().stat().st_mode & 0o777), "0o600")


if __name__ == "__main__":
    unittest.main()
