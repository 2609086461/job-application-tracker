import unittest

from company_aliases import canonical_company_name, company_key
from tools.feishu.normalize_company_aliases import build_plan


class CompanyAliasTests(unittest.TestCase):
    def test_known_aliases_share_one_identity(self):
        aliases = ["小米", "小米集团", "Xiaomi Hire"]
        self.assertEqual({canonical_company_name(value) for value in aliases}, {"小米"})
        self.assertEqual(len({company_key(value) for value in aliases}), 1)

    def test_mindray_short_and_full_names_share_one_identity(self):
        aliases = ["迈瑞", "迈瑞医疗", "深圳迈瑞生物医疗电子股份有限公司", "Mindray"]
        self.assertEqual({canonical_company_name(value) for value in aliases}, {"迈瑞医疗"})
        self.assertEqual(len({company_key(value) for value in aliases}), 1)

    def test_willand_names_share_one_identity(self):
        aliases = ["未岚大陆", "未岚大陆（Willand）", "未岚大陆（北京）科技股份有限公司"]
        self.assertEqual({canonical_company_name(value) for value in aliases}, {"未岚大陆"})
        self.assertEqual(len({company_key(value) for value in aliases}), 1)

    def test_new_application_site_aliases_share_their_company_identity(self):
        groups = {
            "美的集团": ["美的", "Midea"],
            "特斯拉中国": ["特斯拉", "Tesla"],
            "西门子": ["西门子中国", "Siemens"],
            "上海纳铁福": ["上海纳铁福传动系统有限公司"],
            "360集团": ["360", "奇虎360"],
            "傅利叶智能": ["傅利叶", "傅里叶", "傅里叶智能", "Fourier Intelligence", "fftai"],
        }
        for canonical, aliases in groups.items():
            with self.subTest(canonical=canonical):
                self.assertEqual(
                    {canonical_company_name(value) for value in aliases},
                    {canonical},
                )
                self.assertEqual(len({company_key(value) for value in aliases}), 1)

    def test_unknown_company_name_is_preserved(self):
        self.assertEqual(canonical_company_name("示例半导体有限公司"), "示例半导体有限公司")
        self.assertEqual(company_key("示例半导体有限公司"), "示例半导体")

    def test_feishu_plan_renames_alias_but_does_not_merge_job_records(self):
        plan = build_plan([
            {"record_id": "rec1", "fields": {"公司名称": "小米集团", "秋招岗位": "岗位A"}},
            {"record_id": "rec2", "fields": {"公司名称": "Xiaomi Hire", "秋招岗位": "岗位B"}},
        ])

        self.assertEqual(plan["rename_count"], 2)
        self.assertEqual({item["after"] for item in plan["changes"]}, {"小米"})
        self.assertEqual(len(plan["same_company_groups"]), 1)
        self.assertEqual(len(plan["same_company_groups"][0]["records"]), 2)


if __name__ == "__main__":
    unittest.main()
