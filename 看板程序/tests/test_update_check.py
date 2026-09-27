import unittest

from tools import check_update


class UpdateCheckTests(unittest.TestCase):
    def test_version_parts_accepts_release_tags(self):
        self.assertEqual(check_update.version_parts("v1.2.3"), (1, 2, 3))

    def test_newer_version_compares_higher(self):
        self.assertGreater(
            check_update.version_parts("0.3.0"),
            check_update.version_parts("0.2.9"),
        )

    def test_latest_public_version_uses_newer_tag_than_release(self):
        original = check_update.github_json
        try:
            check_update.github_json = lambda url, timeout=10: (
                {"tag_name": "v0.1.0", "name": "old", "html_url": "release"}
                if url.endswith("/releases/latest")
                else [{"name": "v0.2.0"}, {"name": "v0.1.0"}]
            )
            latest = check_update.latest_public_version("owner/repo")
        finally:
            check_update.github_json = original
        self.assertEqual(latest["tag_name"], "v0.2.0")
        self.assertEqual(latest["html_url"], "https://github.com/owner/repo/tree/v0.2.0")


if __name__ == "__main__":
    unittest.main()
