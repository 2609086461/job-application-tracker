from datetime import datetime, timedelta
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from tools.feishu.sync_deadline_calendar import (
    CalendarItem,
    _stable_key,
    build_plan,
    event_payload,
    fingerprint,
    remove_mail_events,
    preview,
    apply_plan,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


def example(deadline: datetime) -> CalendarItem:
    return CalendarItem(
        key="mail:example",
        company="示例公司",
        job="软件工程师",
        event="在线测评",
        deadline=deadline,
        action="完成测评",
        url="https://example.com/assessment",
    )


class DeadlineCalendarTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 6, 9, tzinfo=SHANGHAI)

    def test_event_has_two_native_reminders(self):
        payload = event_payload(example(self.now + timedelta(days=2)))
        self.assertEqual(payload["reminders"], [{"minutes": 1440}, {"minutes": 180}])
        self.assertEqual(payload["start_time"]["timezone"], "Asia/Shanghai")
        self.assertEqual(payload["free_busy_status"], "free")

    def test_midnight_deadline_stays_on_one_calendar_day(self):
        deadline = datetime(2026, 9, 8, 23, 59, tzinfo=SHANGHAI)
        payload = event_payload(example(deadline))
        start = datetime.fromtimestamp(int(payload["start_time"]["timestamp"]), SHANGHAI)
        end = datetime.fromtimestamp(int(payload["end_time"]["timestamp"]), SHANGHAI)

        self.assertEqual(start, deadline - timedelta(minutes=15))
        self.assertEqual(end, deadline)
        self.assertEqual(start.date(), end.date())

    def test_idempotency_key_meets_feishu_minimum_length(self):
        self.assertGreaterEqual(len(_stable_key("mail", "example")), 32)

    def test_new_item_is_created_once(self):
        item = example(self.now + timedelta(days=2))
        first = build_plan([item], {"items": {}}, self.now)
        known = {"items": {item.key: {"event_id": "evt1", "fingerprint": fingerprint(item)}}}
        second = build_plan([item], known, self.now)
        self.assertEqual(first[0]["action"], "create")
        self.assertEqual(second[0]["action"], "unchanged")

    def test_changed_deadline_updates_existing_event(self):
        original = example(self.now + timedelta(days=2))
        changed = example(self.now + timedelta(days=3))
        state = {"items": {original.key: {"event_id": "evt1", "fingerprint": fingerprint(original)}}}
        plan = build_plan([changed], state, self.now)
        self.assertEqual(plan[0]["action"], "update")
        self.assertEqual(plan[0]["previous"]["event_id"], "evt1")

    def test_expired_deadline_is_not_scheduled(self):
        plan = build_plan([example(self.now - timedelta(minutes=1))], {"items": {}}, self.now)
        self.assertEqual(plan, [])

    def test_existing_expired_event_can_be_repaired(self):
        item = example(self.now - timedelta(minutes=1))
        state = {"items": {item.key: {"event_id": "evt1", "fingerprint": "old-format"}}}

        plan = build_plan([item], state, self.now)

        self.assertEqual(plan[0]["action"], "update")
        self.assertEqual(plan[0]["previous"]["event_id"], "evt1")

    def test_preview_uses_operation_and_omits_private_url(self):
        item = example(self.now + timedelta(days=2))
        result = preview(build_plan([item], {"items": {}}, self.now))
        self.assertEqual(result[0]["operation"], "create")
        self.assertNotIn("url", result[0])

    @patch("tools.feishu.sync_deadline_calendar.write_json")
    @patch("tools.feishu.sync_deadline_calendar.feishu.add_calendar_event_attendees")
    @patch("tools.feishu.sync_deadline_calendar.feishu.create_calendar_event")
    def test_new_event_invites_reminder_user(self, create_event, add_attendees, _write_json):
        create_event.return_value = {"event_id": "evt1"}
        item = example(self.now + timedelta(days=2))
        plan = build_plan([item], {"items": {}}, self.now)

        apply_plan(
            "cal1",
            plan,
            {"items": {}},
            self.now,
            reminder_user_id="on_owner",
            reminder_user_id_type="union_id",
        )

        add_attendees.assert_called_once_with(
            "cal1",
            "evt1",
            [{"type": "user", "user_id": "on_owner", "is_optional": False}],
            user_id_type="union_id",
            need_notification=True,
        )

    @patch("tools.feishu.sync_deadline_calendar.write_json")
    @patch("tools.feishu.sync_deadline_calendar.feishu.delete_calendar_event")
    def test_mail_deadline_events_can_be_removed(self, delete_event, write_json):
        state = {"schema_version": 1, "items": {
            "mail:one": {"event_id": "evt-mail"},
            "bitable:one": {"event_id": "evt-application"},
        }}

        result = remove_mail_events("cal1", state)

        self.assertEqual(result, {"deleted": 1, "remaining": 1})
        delete_event.assert_called_once_with("cal1", "evt-mail")
        written = write_json.call_args.args[1]
        self.assertEqual(set(written["items"]), {"bitable:one"})


if __name__ == "__main__":
    unittest.main()
