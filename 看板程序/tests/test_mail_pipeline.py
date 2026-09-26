from email.message import EmailMessage
import unittest

from tools.mail.mail_pipeline import analyze_message


def make_mail(sender: str, subject: str, body: str, uid: str = "1") -> dict:
    message = EmailMessage()
    message["From"] = sender
    message["Subject"] = subject
    message["Date"] = "Mon, 17 Aug 2026 10:00:00 +0800"
    message.set_content(body)
    raw = message.as_bytes()
    return analyze_message(message, raw, uid, "100")


class MailPipelineTests(unittest.TestCase):
    def test_campaign_is_not_progress(self):
        result = make_mail("滴滴招聘", "【滴滴】诚挚邀请你参加2027届秋季校招！", "欢迎了解校园招聘")
        self.assertEqual(result["event"], "招聘通知")
        self.assertEqual(result["suggested_progress"], "")

    def test_interview_subject_is_actionable(self):
        result = make_mail("CVTE校园招聘", "CVTE校招面试邀约", "请选择面试时间")
        self.assertEqual(result["company"], "CVTE")
        self.assertEqual(result["suggested_progress"], "一面")

    def test_future_interview_wording_is_only_confirmation(self):
        result = make_mail(
            "上海艾为电子技术股份有限公司",
            "感谢您投递本公司职位",
            "我们会第一时间与您联系，安排后续的面试流程。",
        )
        self.assertEqual(result["event"], "投递确认")

    def test_written_exam_instructions_do_not_trigger_rejection(self):
        result = make_mail(
            "百度",
            "【百度】邀请你参加2027届校招在线笔试",
            "考试时间：2026-08-20 19:00。答题后无法返回修改，通过本次笔试后进入下一轮。",
        )
        self.assertEqual(result["event"], "笔试/机考")
        self.assertTrue(result["deadline"].startswith("2026-08-20T19:00"))

    def test_parenthesized_weekday_keeps_exam_time(self):
        result = make_mail(
            "ArcSoft虹软",
            "ArcSoft虹软笔试通知：8月23日（本周日）19：00",
            "请按时参加笔试。",
        )
        self.assertTrue(result["deadline"].startswith("2026-08-23T19:00"))

    def test_explicit_rejection(self):
        result = make_mail(
            "知存科技",
            "感谢投递知存科技的职位",
            "很遗憾，经过仔细评估，我们目前的招聘岗位不太合适您。",
        )
        self.assertEqual(result["event"], "淘汰")
        self.assertEqual(result["suggested_progress"], "已挂")

    def test_deeproute_style_rejection(self):
        result = make_mail(
            "深圳元戎启行科技有限公司",
            "来自deeproute.ai的消息通知",
            "不得不遗憾地通知你：你的简历和该职位有些不匹配，因此无法进入下一个阶段。",
        )
        self.assertEqual(result["company"], "元戎启行")
        self.assertEqual(result["event"], "淘汰")

    def test_job_recommendation_offer_word_is_not_an_offer(self):
        result = make_mail(
            "智联招聘",
            "最新嵌入式岗位推荐给你，投递简历收获心仪offer",
            "以下职位可能适合你。",
        )
        self.assertEqual(result["event"], "招聘通知")

    def test_hour_based_assessment_deadline(self):
        result = make_mail(
            "腾讯",
            "腾讯邀请你参加综合素质测评-请在48小时内完成",
            "请在48小时内完成测评。",
        )
        self.assertEqual(result["company"], "腾讯")
        self.assertTrue(result["deadline"].startswith("2026-08-19T10:00"))

    def test_resume_update_has_priority_over_confirmation(self):
        result = make_mail(
            "汇川技术",
            "邀请您更新您的简历",
            "感谢投递，请于2026年8月20日前更新您的简历。",
        )
        self.assertEqual(result["event"], "补充材料")
        self.assertTrue(result["deadline"].startswith("2026-08-20"))

    def test_midnight_24_hour_deadline_rolls_to_next_day(self):
        result = make_mail(
            "新华三技术有限公司",
            "邀请您更新简历",
            "请在2026-09-15 24:00前完善简历。",
        )
        self.assertEqual(result["company"], "新华三")
        self.assertTrue(result["deadline"].startswith("2026-09-16T00:00"))

    def test_position_exam_subject_is_written_exam(self):
        result = make_mail(
            "星宸科技股份有限公司",
            "星宸科技邀您参加嵌入式软件开发工程师岗位的考试",
            "考试时间：2026年09月08日 19:00 - 21:00。",
        )
        self.assertEqual(result["event"], "笔试/机考")
        self.assertTrue(result["deadline"].startswith("2026-09-08T19:00"))

    def test_sicarrier_commitment_is_actionable_material(self):
        result = make_mail(
            "新凯来校园招聘",
            "新凯来《知识产权和商业秘密保护承诺书》签署通知",
            "感谢您应聘新凯来公司，请您仔细阅读并签署承诺书。",
        )
        self.assertEqual(result["company"], "新凯来")
        self.assertEqual(result["event"], "补充材料")
        self.assertIn("签署", result["action"])

    def test_ai_interview_is_assessment_and_company_is_normalized(self):
        result = make_mail(
            "来自吉利汽车集团",
            "来自吉利汽车集团的AI面试邀请",
            "邀请您参加吉利校园招聘线上测评，测评结果会作为面试关键依据。",
        )
        self.assertEqual(result["company"], "吉利控股")
        self.assertEqual(result["event"], "测评")

    def test_interview_satisfaction_survey_is_not_a_new_interview(self):
        result = make_mail(
            "经纬恒润",
            "来自经纬恒润的面试满意度调查",
            "感谢您参与面试，现邀请您评价本次面试体验。",
        )
        self.assertEqual(result["event"], "招聘通知")

    def test_campus_talk_with_later_interviews_is_not_an_interview_invitation(self):
        result = make_mail(
            "亿道集团",
            "2027届亿道集团校园招聘宣讲会邀请函",
            "邀请你参加宣讲会，宣讲会结束后还有面试环节。",
        )
        self.assertEqual(result["event"], "招聘通知")

    def test_subject_company_wins_over_browser_name_in_body(self):
        result = make_mail(
            "招聘系统",
            "来自DJI 大疆的测评邀请",
            "请使用 Chrome、搜狗或百度浏览器完成作答。",
        )
        self.assertEqual(result["company"], "大疆")

    def test_natural_day_assessment_deadline(self):
        result = make_mail(
            "DJI 大疆",
            "来自DJI 大疆的测评邀请",
            "测评有效期为5个自然日，请在有效期内完成。",
        )
        self.assertEqual(result["company"], "大疆")
        self.assertTrue(result["deadline"].startswith("2026-08-22T10:00"))

    def test_assessment_valid_hours_deadline(self):
        result = make_mail(
            "比亚迪",
            "比亚迪心理测评邀请",
            "测评有效时间为72小时，请尽快完成作答。",
        )
        self.assertEqual(result["company"], "比亚迪")
        self.assertTrue(result["deadline"].startswith("2026-08-20T10:00"))


if __name__ == "__main__":
    unittest.main()
