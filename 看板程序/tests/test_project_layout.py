import importlib
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from app import mail_store
from project_paths import APP_DIR, BACKUP_DIR, DATA_DIR, EXPORT_DIR, resolve_data_path
from tools import dashboard
from tools.mail import mail_pipeline


class ProjectLayoutTests(unittest.TestCase):
    def test_windows_and_posix_archive_paths_resolve_identically(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(
                resolve_data_path('公司投递\\Example\\mail.txt', folder),
                resolve_data_path('公司投递/Example/mail.txt', folder),
            )

    def test_unsafe_data_paths_are_rejected(self):
        for value in ('../file.txt', '..\\file.txt', '/etc/passwd', 'C:\\file.txt', 'C:file.txt', '\\\\server\\share\\file.txt', ''):
            with self.subTest(path=value), self.assertRaises(ValueError):
                resolve_data_path(value)

    def test_symlink_cannot_escape_data_root(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as outside:
            link = Path(folder) / 'link'
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest('Symlink creation is unavailable')
            with self.assertRaises(ValueError):
                resolve_data_path('link/private.txt', folder)

    def test_reader_and_writer_share_data_root(self):
        self.assertEqual(mail_pipeline.ROOT, DATA_DIR)
        self.assertEqual(Path(mail_store.PROJECT_DIR), DATA_DIR)
        self.assertEqual(Path(mail_store.ARCHIVE_DIR), mail_pipeline.ARCHIVE_DIR)
        self.assertEqual(Path(mail_store.DASHBOARD_FILE), mail_pipeline.DASHBOARD_FILE)
        self.assertFalse(DATA_DIR.is_relative_to(APP_DIR))

    def test_export_and_backup_paths(self):
        from tools.mail import export_qq_recruitment_mail
        from tools.feishu import record_feishu_application

        self.assertEqual(export_qq_recruitment_mail.EXPORT_DIR, EXPORT_DIR)
        self.assertEqual(record_feishu_application.BACKUP_DIR, BACKUP_DIR)

    def test_all_tool_modules_import(self):
        modules = [
            'mail.read_qq_mail', 'mail.export_qq_recruitment_mail',
            'mail.mail_pipeline', 'mail.sync_recruitment_mail',
            'mail.codex_batch_analyzer',
            'mail.analyze_recruitment_mail', 'mail.reclassify_archived_mail',
            'mail.create_qq_draft', 'mail.sync_and_analyze',
            'company_profiles',
            'feishu.record_feishu_application', 'feishu.match_feishu_mail_preview',
            'feishu.apply_feishu_mail_updates',
            'feishu.sync_deadline_calendar',
            'feishu.sync_recruitment_tasks',
            'feishu.normalize_company_aliases',
        ]
        for name in modules:
            with self.subTest(module=name):
                importlib.import_module('tools.' + name)

    def test_archived_mail_is_readable_after_root_change(self):
        message = EmailMessage()
        message['From'] = 'campus@geely.com'
        message['To'] = 'candidate@example.com'
        message['Subject'] = '吉利控股测评邀请'
        message['Date'] = 'Fri, 04 Sep 2026 10:00:00 +0800'
        message['Message-ID'] = '<layout-test@example.com>'
        message.set_content('请在48小时内完成测评。')
        raw = message.as_bytes()
        metadata = mail_pipeline.analyze_message(message, raw, '1', '1')
        self.assertIsNotNone(metadata)
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            archive = data / '公司投递'
            with patch.object(mail_pipeline, 'ROOT', data), patch.object(mail_pipeline, 'ARCHIVE_DIR', archive):
                result = mail_pipeline.archive_message(metadata, raw)
            self.assertFalse(Path(result['archive_text']).is_absolute())
            self.assertTrue((data / result['archive_eml']).is_file())
            with patch.object(mail_store, 'PROJECT_DIR', str(data)), patch.object(mail_store, 'ARCHIVE_DIR', str(archive)):
                detail = mail_store.get_mail_detail(result['archive_text'])
                self.assertIn('48', detail['content'])
                with self.assertRaises(ValueError):
                    mail_store.get_mail_detail('../outside.txt')
                with self.assertRaises(ValueError):
                    mail_store.get_mail_detail(result['archive_eml'])

    def test_repeat_launch_reuses_existing_server(self):
        with patch.object(dashboard, 'is_running', return_value=True), patch.object(dashboard.os, 'chdir'), patch.object(dashboard.webbrowser, 'open') as browser, patch.object(dashboard.subprocess, 'Popen') as spawn:
            dashboard.main()
            browser.assert_called_once_with(dashboard.URL)
            spawn.assert_not_called()

    def test_launcher_handles_unavailable_server(self):
        with patch.object(dashboard, 'urlopen', side_effect=OSError('offline')):
            self.assertFalse(dashboard.is_running())

    def test_calendar_sync_has_an_independent_twice_daily_timer(self):
        deploy = APP_DIR / 'deploy'
        service = (deploy / 'job-tracker-calendar-sync.service').read_text(encoding='utf-8')
        timer = (deploy / 'job-tracker-calendar-sync.timer').read_text(encoding='utf-8')
        mail_runner = (APP_DIR / 'tools' / 'mail' / 'scheduled_sync.py').read_text(encoding='utf-8')

        self.assertIn('-m tools.feishu.sync_deadline_calendar --apply', service)
        self.assertIn('OnCalendar=*-*-* 00:00:00 Asia/Shanghai', timer)
        self.assertIn('OnCalendar=*-*-* 12:00:00 Asia/Shanghai', timer)
        self.assertNotIn('OnCalendar=hourly', timer)
        self.assertNotIn('sync_deadline_calendar', mail_runner)

    def test_company_profile_sync_is_background_and_cached(self):
        deploy = APP_DIR / 'deploy'
        service = (deploy / 'job-tracker-company-profile-sync.service').read_text(encoding='utf-8')
        timer = (deploy / 'job-tracker-company-profile-sync.timer').read_text(encoding='utf-8')
        page = (APP_DIR / 'static' / 'index.html').read_text(encoding='utf-8')

        self.assertIn('-m tools.company_profiles --limit 1', service)
        self.assertIn('COMPANY_PROFILE_MODEL=gpt-5.6-luna', service)
        self.assertIn('OnUnitInactiveSec=15min', timer)
        self.assertIn('function companyProfile(', page)
        self.assertIn('function profileKey(', page)
        self.assertIn('profiles||{})[profileKey(company)]', page)
        self.assertIn('company_profiles', page)

    def test_dashboard_refresh_does_not_force_full_task_reconciliation(self):
        router = (APP_DIR / 'app' / 'routers' / 'dashboard.py').read_text(encoding='utf-8')
        page = (APP_DIR / 'static' / 'index.html').read_text(encoding='utf-8')

        self.assertNotIn('dashboard_data(force_reconcile=True)', router)
        self.assertIn('loadConfigStatus(true);load()', page)

    def test_explicit_feishu_env_is_authoritative(self):
        source = (APP_DIR / 'app' / 'feishu.py').read_text(encoding='utf-8')
        self.assertIn('load_dotenv(ENV_PATH, override=True)', source)

    def test_public_dashboard_data_is_redacted(self):
        from app.routers.dashboard import public_dashboard_data

        result = public_dashboard_data({
            'main': {'recent': [{
                'company': '示例公司',
                'job': '嵌入式软件',
                'url': 'https://career.example.com/private',
                'deadline': '2026-09-30',
            }]},
            'mail': {'items': [{
                'company': '示例公司',
                'subject': '测评链接和账号',
                'archive_path': '公司投递/示例/mail.txt',
                'action_url': 'https://exam.example.com/token',
                'task_record_id': 'rec1',
                'mail_key': '100:42',
            }]},
            'tasks': {'items': [{
                'task': '完成示例公司测评',
                'record_id': 'rec1',
                'url': 'https://open.feishu.cn/task/private',
                'status': '待完成',
            }]},
        })

        self.assertEqual(result['access_mode'], 'public')
        self.assertNotIn('url', result['main']['recent'][0])
        self.assertEqual(result['mail']['items'][0]['subject'], '邮件待办')
        self.assertNotIn('archive_path', result['mail']['items'][0])
        self.assertNotIn('action_url', result['mail']['items'][0])
        self.assertNotIn('task_record_id', result['mail']['items'][0])
        self.assertNotIn('mail_key', result['mail']['items'][0])
        self.assertNotIn('record_id', result['tasks']['items'][0])
        self.assertNotIn('url', result['tasks']['items'][0])

    def test_dashboard_renders_one_combined_work_list(self):
        page = (APP_DIR / 'static' / 'index.html').read_text(encoding='utf-8')

        self.assertIn('id="work-list"', page)
        self.assertIn('id="history-list" hidden', page)
        self.assertIn('history-collapsed', page)
        self.assertIn('id="history-toggle" aria-expanded="false"', page)
        self.assertIn('function toggleHistory()', page)
        self.assertIn('function setHistoryFilter(value)', page)
        self.assertIn('function workActions(', page)
        self.assertIn("resolveTask(this,", page)
        self.assertIn(">跳过</button>", page)
        self.assertIn('openMailDetail(', page)
        self.assertIn('setTaskStatus(', page)
        self.assertNotIn('id="task-tbody"', page)
        self.assertNotIn('id="mail-tbody"', page)


if __name__ == '__main__':
    unittest.main()
