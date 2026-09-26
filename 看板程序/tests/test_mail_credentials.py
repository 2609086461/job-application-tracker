import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.mail.read_qq_mail import AUTH_CODE_FILE_ENV, load_or_prompt_auth_code


class MailCredentialTests(unittest.TestCase):
    def test_secret_file_takes_precedence_over_keyring(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            secret_path = Path(temp_dir) / "qq-auth-code"
            secret_path.write_text("test-auth-code\n", encoding="utf-8")
            secret_path.chmod(0o600)
            with (
                patch.dict(os.environ, {AUTH_CODE_FILE_ENV: str(secret_path)}),
                patch("tools.mail.read_qq_mail.keyring.get_password") as get_password,
            ):
                auth_code, source = load_or_prompt_auth_code("user@qq.com")

        self.assertEqual(auth_code, "test-auth-code")
        self.assertEqual(source, "file")
        get_password.assert_not_called()

    def test_missing_secret_file_fails_without_prompting(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            missing_path = Path(temp_dir) / "missing"
            with (
                patch.dict(os.environ, {AUTH_CODE_FILE_ENV: str(missing_path)}),
                patch("tools.mail.read_qq_mail.getpass.getpass") as prompt,
            ):
                with self.assertRaisesRegex(RuntimeError, "不存在"):
                    load_or_prompt_auth_code("user@qq.com")

        prompt.assert_not_called()


if __name__ == "__main__":
    unittest.main()
