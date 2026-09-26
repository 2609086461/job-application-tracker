import tempfile
import unittest
from base64 import b64encode
from pathlib import Path
from unittest.mock import patch

from tools import company_profiles


class CompanyProfileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.profile_file = Path(temporary.name) / "company_profiles.json"

    def test_processes_one_active_company_then_uses_cache(self):
        tasks = {"items": [{
            "company": "小米集团",
            "event": "测评",
            "status": "待完成",
        }]}
        source = {"url": "https://www.mi.com/", "title": "小米", "description": "", "text": "小米公开资料"}
        summary = {
            "industry": "消费电子", "business": "智能手机和智能硬件",
            "company_type": "上市公司", "summary": "小米是一家消费电子公司。",
        }
        with patch.object(company_profiles, "PROFILE_FILE", self.profile_file), \
             patch.object(company_profiles.sync_recruitment_tasks, "dashboard_data", return_value=tasks), \
             patch.object(company_profiles.mail_store, "get_dashboard_data", return_value={"items": []}), \
             patch.object(company_profiles, "fetch_public_source", return_value=source) as fetch, \
             patch.object(company_profiles, "summarize_public_source", return_value=summary) as summarize:
            first = company_profiles.process_pending()
            second = company_profiles.process_pending()

        self.assertEqual(first, {"active": 1, "attempted": 1, "ready": 1, "failed": 0})
        self.assertEqual(second, {"active": 1, "attempted": 0, "ready": 0, "failed": 0})
        fetch.assert_called_once_with("小米")
        summarize.assert_called_once()

    def test_pending_profiles_include_mail_only_company(self):
        source = {"url": "https://example.com", "title": "比亚迪", "text": "公开资料"}
        summary = {"industry": "汽车", "business": "新能源汽车", "company_type": "上市公司", "summary": "比亚迪主营新能源汽车。"}
        with patch.object(company_profiles, "PROFILE_FILE", self.profile_file), \
             patch.object(company_profiles.sync_recruitment_tasks, "dashboard_data", return_value={"items": []}), \
             patch.object(company_profiles.mail_store, "get_dashboard_data", return_value={"items": [{"company": "比亚迪"}]}), \
             patch.object(company_profiles, "fetch_public_source", return_value=source) as fetch, \
             patch.object(company_profiles, "summarize_public_source", return_value=summary):
            result = company_profiles.process_pending()

        self.assertEqual(result, {"active": 0, "attempted": 1, "ready": 1, "failed": 0})
        fetch.assert_called_once_with("比亚迪")

    def test_dashboard_returns_only_ready_active_profiles(self):
        self.profile_file.write_text(
            '{"schema_version": 1, "profiles": {'
            '"小米": {"status": "ready", "company": "小米", "summary": "简介", "source_url": "https://www.mi.com/"},'
            '"华为": {"status": "error", "company": "华为"}'
            '}}',
            encoding="utf-8",
        )
        with patch.object(company_profiles, "PROFILE_FILE", self.profile_file):
            profiles = company_profiles.profiles_for_tasks([
                {"company": "小米集团", "status": "待完成"},
                {"company": "华为", "status": "待完成"},
                {"company": "腾讯", "status": "已完成"},
            ])

        self.assertEqual(set(profiles), {"小米", "华为"})
        self.assertEqual(profiles["小米"]["summary"], "简介")
        self.assertEqual(profiles["华为"]["status"], "unavailable")

    def test_profiles_for_companies_includes_mail_only_company(self):
        self.profile_file.write_text(
            '{"schema_version": 2, "profiles": {'
            '"上海贝岭": {"status": "ready", "company": "上海贝岭", "summary": "芯片设计公司。"}'
            '}}',
            encoding="utf-8",
        )
        with patch.object(company_profiles, "PROFILE_FILE", self.profile_file):
            profiles = company_profiles.profiles_for_companies(["上海贝岭"])

        self.assertEqual(profiles["上海贝岭"]["summary"], "芯片设计公司。")

    def test_manual_company_retry_can_replace_an_error_profile(self):
        self.profile_file.write_text(
            '{"schema_version": 2, "profiles": {'
            '"华为": {"status": "error", "company": "华为", "error": "旧失败"}'
            '}}',
            encoding="utf-8",
        )
        source = {"url": "https://example.com", "title": "华为", "text": "公开资料"}
        summary = {"industry": "通信", "business": "信息与通信技术", "company_type": "企业", "summary": "华为提供信息与通信技术产品和服务。"}
        with patch.object(company_profiles, "PROFILE_FILE", self.profile_file), \
             patch.object(company_profiles, "fetch_public_source", return_value=source), \
             patch.object(company_profiles, "summarize_public_source", return_value=summary):
            result = company_profiles.process_companies(["华为"], limit=1, retry_errors=True)
            store = company_profiles._load_store()

        self.assertEqual(result, {"companies": 1, "attempted": 1, "ready": 1, "failed": 0})
        self.assertEqual(store["profiles"]["华为"]["status"], "ready")

    def test_clean_existing_profiles_removes_event_context(self):
        self.profile_file.write_text(
            '{"schema_version": 1, "profiles": {'
            '"示例": {"status": "ready", "company": "示例公司", "job_fit": "测评关联未确认", '
            '"summary": "示例公司提供软件服务。此次招聘为测评，岗位匹配度未确认。"}'
            '}}',
            encoding="utf-8",
        )
        with patch.object(company_profiles, "PROFILE_FILE", self.profile_file):
            result = company_profiles.clean_existing_profiles()
            store = company_profiles._load_store()

        self.assertEqual(result, {"profiles": 1, "updated": 1})
        self.assertEqual(store["schema_version"], 2)
        self.assertNotIn("job_fit", store["profiles"]["示例"])
        self.assertEqual(store["profiles"]["示例"]["summary"], "示例公司提供软件服务。")

    def test_search_uses_bing_when_duckduckgo_returns_no_results(self):
        target = "https://example.com/about"
        encoded = b64encode(target.encode("utf-8")).decode("ascii")
        duckduckgo_challenge = "<html><body>challenge</body></html>"
        bing_results = f'<h2><a href="https://www.bing.com/ck/a?u=a1{encoded}">About</a></h2>'
        with patch.object(company_profiles, "_download", side_effect=[
            ("https://duckduckgo.com/", duckduckgo_challenge),
            ("https://www.bing.com/", bing_results),
        ]):
            urls = company_profiles._search_urls("示例公司")

        self.assertEqual(urls, [target])


if __name__ == "__main__":
    unittest.main()
