import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from tools.feishu.sync_recruitment_tasks import (
    _task_payload,
    apply_candidates,
    cleanup_duplicate_calendar_events,
    collect_candidates,
    ensure_status_source_field,
    main as sync_task_main,
    reconcile_completion,
    resolve_expired_task,
    resolve_task,
    set_task_status,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


class RecruitmentTaskTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 8, 1, 0, tzinfo=SHANGHAI)

    def test_expired_items_become_disposition_tasks(self):
        items = collect_candidates([
            {"company": "A", "event": "测评", "action": "完成", "deadline": "2026-09-07T12:00:00+08:00", "subject": "old"},
        ], [], self.now)
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0].expired)
        self.assertEqual(items[0].title, "处理过期：A · 测评")

    def test_confirm_interview_is_replaced_by_scheduled_interview(self):
        mail = [
            {"company": "经纬恒润", "event": "面试", "action": "确认时间", "subject": "invite"},
            {"company": "经纬恒润", "event": "面试已预约", "action": "准备面试", "subject": "confirmed"},
        ]
        records = [{
            "record_id": "rec1",
            "fields": {"公司名称": "经纬恒润", "一面": 1788850800000},
        }]
        items = collect_candidates(mail, records, self.now)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "准备经纬恒润面试")
        self.assertEqual(items[0].event, "一面")
        self.assertIsNone(items[0].planned_at)

    def test_company_aliases_do_not_create_duplicate_tasks(self):
        items = collect_candidates([
            {"company": "小米集团", "event": "测评", "action": "完成", "subject": "same"},
            {"company": "Xiaomi Hire", "event": "测评", "action": "完成", "subject": "same"},
        ], [], self.now)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].company, "小米")

    def test_task_carries_stable_mail_key(self):
        item = collect_candidates([
            {
                "company": "小米集团",
                "event": "测评",
                "action": "完成",
                "subject": "测评邀请",
                "mail_key": "100:42",
            },
        ], [], self.now)[0]

        self.assertEqual(item.source_key, "100:42")

    @patch("tools.feishu.sync_recruitment_tasks.apply_candidates")
    @patch("tools.feishu.sync_recruitment_tasks.feishu.list_records", return_value=[])
    @patch("tools.feishu.sync_recruitment_tasks.mail_store.get_dashboard_data")
    def test_mail_key_option_limits_task_sync_to_current_mail(self, dashboard, _records, apply):
        dashboard.return_value = {"items": [
            {"company": "小米", "event": "测评", "action": "完成", "subject": "new", "mail_key": "new-key"},
            {"company": "华为", "event": "测评", "action": "完成", "subject": "old", "mail_key": "old-key"},
        ]}
        apply.return_value = {"created": 1, "repaired": 0, "skipped": 0}

        with patch("sys.argv", ["sync_recruitment_tasks", "--apply", "--mail-key", "new-key"]):
            self.assertEqual(sync_task_main(), 0)

        items = apply.call_args.args[0]
        self.assertEqual([item.source_key for item in items], ["new-key"])

    @patch("tools.feishu.sync_recruitment_tasks.feishu.update_record")
    @patch("tools.feishu.sync_recruitment_tasks.feishu.update_task")
    @patch("tools.feishu.sync_recruitment_tasks.feishu.create_task")
    @patch("tools.feishu.sync_recruitment_tasks.feishu.get_task")
    @patch("tools.feishu.sync_recruitment_tasks.feishu.list_records")
    @patch("tools.feishu.sync_recruitment_tasks.ensure_tasklist")
    @patch("tools.feishu.sync_recruitment_tasks.ensure_task_tracking_fields")
    @patch("tools.feishu.sync_recruitment_tasks.ensure_task_table", return_value="task-table")
    def test_apply_repairs_task_owned_by_old_app(
        self, _ensure_table, _ensure_fields, ensure_tasklist, list_records,
        get_task, create_task, update_task, update_record,
    ):
        class UnauthorizedTaskError(RuntimeError):
            code = 1470403

        item = collect_candidates([{
            "company": "小米集团",
            "event": "测评",
            "action": "完成",
            "subject": "测评邀请",
            "mail_key": "100:42",
        }], [], self.now)[0]
        ensure_tasklist.return_value = {"guid": "list-guid", "url": "list-url"}
        list_records.return_value = [{
            "record_id": "rec1",
            "fields": {
                "任务键": item.key,
                "飞书任务GUID": "old-guid",
                "状态": "已完成",
                "完成时间": 123456,
            },
        }]
        get_task.side_effect = UnauthorizedTaskError("old owner")
        create_task.return_value = {"guid": "new-guid", "url": "new-url"}

        result = apply_candidates([item], "owner-id")

        self.assertEqual(result["repaired"], 1)
        self.assertEqual(result["created"], 0)
        update_task.assert_called_once_with(
            "new-guid", {"completed_at": 123456}, ["completed_at"],
        )
        update_record.assert_called_once_with(
            "rec1",
            {
                "任务": "完成小米测评",
                "公司": "小米",
                "飞书任务GUID": "new-guid",
                "来源键": "100:42",
                "状态来源": "双向同步",
                "飞书任务链接": {"link": "new-url", "text": "打开任务"},
            },
            table_id="task-table",
        )

    def test_material_is_planned_six_hours_early(self):
        items = collect_candidates([
            {
                "company": "新华三",
                "event": "补充材料",
                "action": "更新简历",
                "deadline": "2026-09-16T00:00:00+08:00",
                "subject": "material",
            },
        ], [], self.now)
        self.assertEqual(items[0].planned_at, datetime(2026, 9, 15, 18, 0, tzinfo=SHANGHAI))

    def test_native_task_uses_only_one_reminder(self):
        item = collect_candidates([{
            "company": "新华三",
            "event": "测评",
            "action": "完成测评",
            "deadline": "2099-09-15T23:59:00+08:00",
            "subject": "assessment",
        }], [], self.now)[0]
        payload = _task_payload(item, "list-guid", "owner-id")
        self.assertEqual(payload["reminders"], [{"relative_fire_minute": 180}])
        self.assertIsNone(item.planned_at)

    @patch("tools.feishu.sync_recruitment_tasks.feishu.update_record")
    @patch("tools.feishu.sync_recruitment_tasks.feishu.get_task", side_effect=RuntimeError("unauthorized"))
    @patch("tools.feishu.sync_recruitment_tasks.feishu.list_records")
    def test_reconcile_skips_unreadable_native_task(self, list_records, _get_task, update_record):
        list_records.return_value = [{
            "record_id": "rec1",
            "fields": {"飞书任务GUID": "task1", "状态": "待完成"},
        }]

        result = reconcile_completion("task-table")

        self.assertEqual(result, {"checked": 1, "updated": 0, "failed": 1})
        update_record.assert_not_called()

    @patch("tools.feishu.sync_recruitment_tasks.feishu.update_record")
    @patch("tools.feishu.sync_recruitment_tasks.feishu.get_task")
    @patch("tools.feishu.sync_recruitment_tasks.feishu.list_records")
    def test_reconcile_treats_deleted_native_task_as_skipped(
        self, list_records, get_task, update_record,
    ):
        class DeletedTaskError(RuntimeError):
            code = 1470404

        list_records.return_value = [{
            "record_id": "rec1",
            "fields": {"飞书任务GUID": "task1", "状态": "待完成"},
        }]
        get_task.side_effect = DeletedTaskError("deleted")

        result = reconcile_completion("task-table")

        self.assertEqual(result, {"checked": 1, "updated": 1, "failed": 0})
        updates = update_record.call_args.args[1]
        self.assertEqual(updates["状态"], "已完成")
        self.assertEqual(updates["处置状态"], "已跳过")

    @patch("tools.feishu.sync_recruitment_tasks.feishu.update_record")
    @patch("tools.feishu.sync_recruitment_tasks.feishu.get_task", return_value={"completed_at": 0})
    @patch("tools.feishu.sync_recruitment_tasks.feishu.list_records")
    def test_reconcile_restores_ledger_when_native_task_is_reopened(
        self, list_records, _get_task, update_record,
    ):
        list_records.return_value = [{
            "record_id": "rec1",
            "fields": {"飞书任务GUID": "task1", "状态": "已完成"},
        }]

        result = reconcile_completion("task-table")

        self.assertEqual(result, {"checked": 1, "updated": 1, "failed": 0})
        update_record.assert_called_once_with(
            "rec1",
            {"状态": "待完成", "完成时间": None, "状态来源": "飞书任务"},
            table_id="task-table",
        )

    @patch("tools.feishu.sync_recruitment_tasks.feishu.update_record")
    @patch("tools.feishu.sync_recruitment_tasks.feishu.get_task", return_value={"completed_at": 123456})
    @patch("tools.feishu.sync_recruitment_tasks.feishu.list_records")
    def test_completing_resend_task_marks_mail_as_resent(
        self, list_records, _get_task, update_record,
    ):
        list_records.return_value = [{
            "record_id": "rec1",
            "fields": {
                "飞书任务GUID": "task1",
                "状态": "待完成",
                "处置状态": "待重发邮件",
            },
        }]

        result = reconcile_completion("task-table")

        self.assertEqual(result, {"checked": 1, "updated": 1, "failed": 0})
        update_record.assert_called_once_with(
            "rec1",
            {
                "状态": "已完成",
                "完成时间": 123456,
                "状态来源": "飞书任务",
                "处置状态": "已重发",
            },
            table_id="task-table",
        )

    @patch("tools.feishu.sync_recruitment_tasks.find_task_table", return_value="task-table")
    @patch("tools.feishu.sync_recruitment_tasks.ensure_status_source_field")
    @patch("tools.feishu.sync_recruitment_tasks.feishu.update_record")
    @patch("tools.feishu.sync_recruitment_tasks.feishu.update_task")
    @patch("tools.feishu.sync_recruitment_tasks.feishu.list_records")
    def test_dashboard_can_complete_native_task(
        self, list_records, update_task, update_record, _ensure_field, _find_task_table,
    ):
        list_records.return_value = [{
            "record_id": "rec1",
            "fields": {"飞书任务GUID": "task1", "状态": "待完成"},
        }]

        result = set_task_status("rec1", True)

        completed_at = result["completed_at"]
        self.assertGreater(completed_at, 0)
        update_task.assert_called_once_with(
            "task1", {"completed_at": completed_at}, ["completed_at"],
        )
        update_record.assert_called_once_with(
            "rec1",
            {"状态": "已完成", "完成时间": completed_at, "状态来源": "双向同步"},
            table_id="task-table",
        )

    @patch("tools.feishu.sync_recruitment_tasks.find_task_table", return_value="task-table")
    @patch("tools.feishu.sync_recruitment_tasks.ensure_status_source_field")
    @patch("tools.feishu.sync_recruitment_tasks.feishu.update_record")
    @patch("tools.feishu.sync_recruitment_tasks.feishu.update_task")
    @patch("tools.feishu.sync_recruitment_tasks.feishu.list_records")
    def test_dashboard_can_restore_native_task(
        self, list_records, update_task, update_record, _ensure_field, _find_task_table,
    ):
        list_records.return_value = [{
            "record_id": "rec1",
            "fields": {"飞书任务GUID": "task1", "状态": "已完成"},
        }]

        result = set_task_status("rec1", False)

        self.assertEqual(result["status"], "待完成")
        update_task.assert_called_once_with("task1", {"completed_at": 0}, ["completed_at"])
        update_record.assert_called_once_with(
            "rec1",
            {"状态": "待完成", "完成时间": None, "状态来源": "双向同步"},
            table_id="task-table",
        )

    @patch("tools.feishu.sync_recruitment_tasks.find_task_table", return_value="task-table")
    @patch("tools.feishu.sync_recruitment_tasks.ensure_status_source_field")
    @patch("tools.feishu.sync_recruitment_tasks.feishu.update_record")
    @patch("tools.feishu.sync_recruitment_tasks.feishu.update_task")
    @patch("tools.feishu.sync_recruitment_tasks.feishu.list_records")
    def test_dashboard_keeps_status_when_native_task_rejects_old_owner(
        self, list_records, update_task, update_record, _ensure_field, _find_task_table,
    ):
        class UnauthorizedTaskError(RuntimeError):
            code = 1470403

        list_records.return_value = [{
            "record_id": "rec1",
            "fields": {"飞书任务GUID": "task1", "状态": "待完成"},
        }]
        update_task.side_effect = UnauthorizedTaskError("old owner")

        result = set_task_status("rec1", True)

        self.assertFalse(result["native_synced"])
        update_record.assert_called_once_with(
            "rec1",
            {
                "状态": "已完成",
                "完成时间": result["completed_at"],
                "状态来源": "看板",
            },
            table_id="task-table",
        )

    @patch("tools.feishu.sync_recruitment_tasks.find_task_table", return_value="task-table")
    @patch("tools.feishu.sync_recruitment_tasks.feishu.update_record")
    @patch("tools.feishu.sync_recruitment_tasks.feishu.update_task")
    @patch("tools.feishu.sync_recruitment_tasks.feishu.list_records")
    def test_expired_task_can_be_changed_to_manual_resend(
        self, list_records, update_task, update_record, _find_task_table,
    ):
        list_records.return_value = [{
            "record_id": "rec1",
            "fields": {
                "公司": "小米",
                "事项类型": "测评",
                "飞书任务GUID": "task1",
                "状态": "待完成",
                "截止时间": 1,
            },
        }]

        result = resolve_expired_task("rec1", "resend")

        self.assertEqual(result, {
            "record_id": "rec1", "resolution": "待重发邮件", "completed": False,
        })
        update_task.assert_called_once_with(
            "task1",
            {"summary": "重发邮件：小米 · 测评", "completed_at": 0},
            ["summary", "completed_at"],
        )
        update_record.assert_called_once_with(
            "rec1",
            {
                "任务": "重发邮件：小米 · 测评",
                "状态": "待完成",
                "完成时间": None,
                "处置状态": "待重发邮件",
                "状态来源": "双向同步",
            },
            table_id="task-table",
        )

    @patch("tools.feishu.sync_recruitment_tasks.find_task_table", return_value="task-table")
    @patch("tools.feishu.sync_recruitment_tasks.feishu.update_record")
    @patch("tools.feishu.sync_recruitment_tasks.feishu.update_task")
    @patch("tools.feishu.sync_recruitment_tasks.feishu.list_records")
    def test_expired_task_can_be_skipped_and_completed(
        self, list_records, update_task, update_record, _find_task_table,
    ):
        list_records.return_value = [{
            "record_id": "rec1",
            "fields": {
                "飞书任务GUID": "task1",
                "状态": "待完成",
                "截止时间": 1,
            },
        }]

        result = resolve_expired_task("rec1", "skip")

        self.assertTrue(result["completed"])
        self.assertEqual(result["resolution"], "已跳过")
        completed_at = update_task.call_args.args[1]["completed_at"]
        self.assertGreater(completed_at, 0)
        update_record.assert_called_once_with(
            "rec1",
            {
                "状态": "已完成",
                "完成时间": completed_at,
                "处置状态": "已跳过",
                "状态来源": "双向同步",
            },
            table_id="task-table",
        )

    @patch("tools.feishu.sync_recruitment_tasks.find_task_table", return_value="task-table")
    @patch("tools.feishu.sync_recruitment_tasks.feishu.update_record")
    @patch("tools.feishu.sync_recruitment_tasks.feishu.update_task")
    @patch("tools.feishu.sync_recruitment_tasks.feishu.list_records")
    def test_active_task_can_be_skipped_without_claiming_completion(
        self, list_records, update_task, update_record, _find_task_table,
    ):
        list_records.return_value = [{
            "record_id": "rec1",
            "fields": {
                "飞书任务GUID": "task1",
                "状态": "待完成",
                "截止时间": 4102444800000,
            },
        }]

        result = resolve_task("rec1", "skip")

        self.assertTrue(result["completed"])
        self.assertEqual(result["resolution"], "已跳过")
        update_task.assert_called_once()
        completed_at = update_task.call_args.args[1]["completed_at"]
        self.assertGreater(completed_at, 0)
        update_record.assert_called_once_with(
            "rec1",
            {
                "状态": "已完成",
                "完成时间": completed_at,
                "处置状态": "已跳过",
                "状态来源": "双向同步",
            },
            table_id="task-table",
        )

    @patch("tools.feishu.sync_recruitment_tasks.feishu.create_field")
    @patch("tools.feishu.sync_recruitment_tasks.feishu.list_fields")
    def test_status_source_field_is_created_only_when_missing(self, list_fields, create_field):
        list_fields.return_value = {"状态": {"field_name": "状态"}}
        ensure_status_source_field("task-table")
        create_field.assert_called_once_with(
            "task-table", {"field_name": "状态来源", "type": 1},
        )

        create_field.reset_mock()
        list_fields.return_value = {"状态来源": {"field_name": "状态来源"}}
        ensure_status_source_field("task-table")
        create_field.assert_not_called()

    @patch("tools.feishu.sync_recruitment_tasks.feishu.update_record")
    @patch("tools.feishu.sync_recruitment_tasks.feishu.update_task")
    @patch("tools.feishu.sync_recruitment_tasks.feishu.list_records")
    def test_reconcile_retries_dashboard_override(
        self, list_records, update_task, update_record,
    ):
        list_records.return_value = [{
            "record_id": "rec1",
            "fields": {
                "飞书任务GUID": "task1",
                "状态": "已完成",
                "状态来源": "看板",
            },
        }]

        result = reconcile_completion("task-table")

        self.assertEqual(result, {"checked": 1, "updated": 1, "failed": 0})
        self.assertGreater(update_task.call_args.args[1]["completed_at"], 0)
        update_record.assert_called_once_with(
            "rec1", {"状态来源": "双向同步"}, table_id="task-table",
        )

    @patch("tools.feishu.sync_recruitment_tasks.find_task_table", return_value="task-table")
    @patch("tools.feishu.sync_recruitment_tasks.feishu.update_record")
    @patch("tools.feishu.sync_recruitment_tasks.feishu.update_task")
    @patch("tools.feishu.sync_recruitment_tasks.feishu.get_task")
    @patch("tools.feishu.sync_recruitment_tasks.feishu.delete_calendar_event")
    @patch("tools.feishu.sync_recruitment_tasks.feishu.get_primary_calendar")
    @patch("tools.feishu.sync_recruitment_tasks.feishu.list_records")
    def test_cleanup_removes_assessment_planned_event(
        self, list_records, get_calendar, delete_event, get_task,
        update_task, update_record, _find_task_table,
    ):
        list_records.return_value = [{
            "record_id": "rec1",
            "fields": {
                "事项类型": "测评",
                "计划日程ID": "evt1",
                "飞书任务GUID": "task1",
            },
        }]
        get_calendar.return_value = {"calendar_id": "cal1"}
        get_task.return_value = {"description": "事项：测评\n建议提前完成：2026-09-14 20:00"}

        result = cleanup_duplicate_calendar_events()

        self.assertEqual(result, {"checked": 1, "deleted": 1, "updated": 1})
        delete_event.assert_called_once_with("cal1", "evt1")
        update_task.assert_called_once_with("task1", {"description": "事项：测评"}, ["description"])
        update_record.assert_called_once_with(
            "rec1", {"计划日程ID": "", "计划完成时间": None}, table_id="task-table",
        )


if __name__ == "__main__":
    unittest.main()
