import unittest

from company_aliases import canonical_company_name, company_key
from tools.feishu.normalize_company_aliases import build_plan


class CompanyAliasTests(unittest.TestCase):
    def test_known_aliases_share_one_identity(self):
        aliases = ["小米", "小米集团", "Xiaomi Hire"]
        self.assertEqual({canonical_company_name(value) for value in aliases}, {"小米"})
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
