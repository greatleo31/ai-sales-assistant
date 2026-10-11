#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块一自洽性闸门、单一真源生成、词表消歧与售后时间边界自动化回归测试套件 (Standard Library unittest)
覆盖 Claude Sonnet 5.5 审查报告 (01_catalog_and_validator_review.md) 中的：
  - R1: 检索语料事实隔离（4 个反例回归 + 注入拦截）
  - R2: 物理量纲一致性（100g/100ml 错配拦截、面料克重 g/㎡ 拦截）
  - R3: conflict_pairs.json 单一真源隔离重生成验证与非法阈值拒绝
  - R4: 全部 18 组隔离变异测试（缺文件、空规则、空订单、重复ID、NaN、非整数接口、specs=null 等）
  - R5: zh_aliases 最长匹配、PA+ 边界、否定词守卫、模糊词软处理与跨子类目意图识别
  - R6: 售后 168h / 价保 360h 精确到 ±1 秒边界、未签收、未来签收与“次日零时”矛盾拦截
  - R7: compute_numeric_closeness_score 枚举参数拒绝与 C3/C4 对抗竞争候选测试
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict

sys.dont_write_bytecode = True

ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
DATA_DIR = ROOT_DIR / "data"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import generate_catalog  # noqa: E402
import validate_catalog  # noqa: E402

SHANGHAI_TZ = timezone(timedelta(hours=8))
FILE_NAMES = (
    "spec_registry.json",
    "conflict_pairs.json",
    "ecommerce_catalog_60skus.json",
    "platform_rules.json",
    "mock_orders.json",
)


def load_base_data() -> Dict[str, Any]:
    data = {
        name: json.loads((DATA_DIR / name).read_text(encoding="utf-8"))
        for name in FILE_NAMES
    }
    manifest_file = DATA_DIR / "image_manifest.json"
    if manifest_file.exists():
        data["image_manifest.json"] = json.loads(manifest_file.read_text(encoding="utf-8"))
    return data


class TestCatalogAndGateSuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.base_data = load_base_data()

    def run_mutated_gate(self, mutator: Callable[[Dict[str, Any]], None]) -> tuple[bool, list[str], list[str]]:
        """在隔离临时目录写入变异后的 JSON 并运行 validate_all()，绝不触碰真实 data/ 目录。"""
        data_copy = copy.deepcopy(self.base_data)
        mutator(data_copy)
        with tempfile.TemporaryDirectory(prefix="gate_mut_") as tmp_str:
            tmp_dir = Path(tmp_str)
            for fname, content in data_copy.items():
                if content is not None:
                    (tmp_dir / fname).write_text(
                        json.dumps(content, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
            return validate_catalog.validate_all(data_dir=tmp_dir)

    # ==================== 0. 基准正例校验 ====================
    def test_00_baseline_data_passes_100_percent(self) -> None:
        ok, passed, errors = validate_catalog.validate_all(data_dir=DATA_DIR)
        self.assertTrue(ok, f"Baseline should pass 100%, but got errors: {errors}")
        self.assertEqual(errors, [])
        self.assertGreaterEqual(len(passed), 26)

    # ==================== R1. 检索语料事实隔离回归 ====================
    def test_r1_four_confirmed_counterexamples_absent(self) -> None:
        sku_map = {s["sku_id"]: s for s in self.base_data["ecommerce_catalog_60skus.json"]}

        # 1. SKU-3C-015 (TTC金粉轴V2, linear) 不得出现 "红轴" 或 "快银轴"
        rt_015 = sku_map["SKU-3C-015"]["retrieval_text"]
        self.assertIn("TTC金粉轴V2", rt_015)
        self.assertIn("linear(线性轴)", rt_015)
        self.assertNotIn("红轴", rt_015)
        self.assertNotIn("快银轴", rt_015)

        # 2. SKU-APP-003 (圆领无帽卫衣) 不得出现 "连帽卫衣"
        rt_app003 = sku_map["SKU-APP-003"]["retrieval_text"]
        self.assertIn("圆领卫衣", rt_app003)
        self.assertNotIn("连帽卫衣", rt_app003)

        # 3. SKU-APP-004 (连帽卫衣) 不得出现 "圆领卫衣"
        rt_app004 = sku_map["SKU-APP-004"]["retrieval_text"]
        self.assertIn("连帽卫衣", rt_app004)
        self.assertNotIn("圆领卫衣", rt_app004)

        # 4. SKU-3C-014 (100W无自带线充电宝) 不得出现 "自带线充电宝" 或 "自带线"
        rt_3c014 = sku_map["SKU-3C-014"]["retrieval_text"]
        self.assertNotIn("自带线充电宝", rt_3c014)
        self.assertNotIn("自带线", rt_3c014)

    def test_r1_gate_rejects_alias_pollution_in_retrieval_text(self) -> None:
        def mutate(d: Dict[str, Any]) -> None:
            for s in d["ecommerce_catalog_60skus.json"]:
                if s["sku_id"] == "SKU-3C-015":
                    s["retrieval_text"] += " 红轴"

        ok, _, errors = self.run_mutated_gate(mutate)
        self.assertFalse(ok)
        self.assertTrue(any("SKU-3C-015" in e for e in errors))

    # ==================== R2. 物理量纲一致性回归 ====================
    def test_r2_sku_bea_008_uses_consistent_ml_unit(self) -> None:
        sku_map = {s["sku_id"]: s for s in self.base_data["ecommerce_catalog_60skus.json"]}
        bea_008 = sku_map["SKU-BEA-008"]
        self.assertEqual(bea_008["specs"]["volume_ml"], 100)
        self.assertIn("100ml", bea_008["title"])
        self.assertIn("100ml", bea_008["search_query"])
        self.assertNotIn("100g", bea_008["title"])
        self.assertNotIn("100g", bea_008["search_query"])

    def test_r2_gate_rejects_g_vs_ml_and_bare_fabric_weight(self) -> None:
        # 100g 冒充 100ml
        def mutate_g_ml(d: Dict[str, Any]) -> None:
            for s in d["ecommerce_catalog_60skus.json"]:
                if s["sku_id"] == "SKU-BEA-008":
                    s["title"] = "芙丽芳丝 净润洗面霜 100g 日本进口氨基酸洁面"

        ok1, _, errs1 = self.run_mutated_gate(mutate_g_ml)
        self.assertFalse(ok1)
        self.assertTrue(any("量纲错配(R2" in e for e in errs1))

        # 面料克重裸写 300g 不带 /㎡
        def mutate_fabric_g(d: Dict[str, Any]) -> None:
            for s in d["ecommerce_catalog_60skus.json"]:
                if s["sku_id"] == "SKU-APP-003":
                    s["title"] = "日系重磅 300g 新疆长绒棉圆领卫衣"

        ok2, _, errs2 = self.run_mutated_gate(mutate_fabric_g)
        self.assertFalse(ok2)
        self.assertTrue(any("量纲歧义(R2" in e for e in errs2))

    # ==================== R3. conflict_pairs.json 唯一真源验证 ====================
    def test_r3_generator_consumes_conflict_pairs_single_source_of_truth(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gen_ssot_") as tmp_str:
            tmp_dir = Path(tmp_str)
            reg_copy = copy.deepcopy(self.base_data["spec_registry.json"])
            cp_copy = copy.deepcopy(self.base_data["conflict_pairs.json"])

            # 将 C1 path_a (SKU-3C-008) 的期望价格从 29.9 改为 30.9
            cp_copy["pairs"][0]["path_a"]["expect"]["final_price"] = 30.9
            (tmp_dir / "spec_registry.json").write_text(json.dumps(reg_copy, ensure_ascii=False), encoding="utf-8")
            (tmp_dir / "conflict_pairs.json").write_text(json.dumps(cp_copy, ensure_ascii=False), encoding="utf-8")

            orig_data_dir = generate_catalog.DATA_DIR
            try:
                generate_catalog.DATA_DIR = tmp_dir
                generate_catalog.main()
            finally:
                generate_catalog.DATA_DIR = orig_data_dir

            gen_catalog = json.loads((tmp_dir / "ecommerce_catalog_60skus.json").read_text(encoding="utf-8"))
            sku_008 = next(s for s in gen_catalog if s["sku_id"] == "SKU-3C-008")
            self.assertEqual(sku_008["final_price"], 30.9)
            self.assertIn("到手价¥30.9", sku_008["retrieval_text"])

            # 隔离生成的三份产物必须直接通过闸门
            ok, _, errs = validate_catalog.validate_all(data_dir=tmp_dir)
            self.assertTrue(ok, f"Expected isolated regeneration to pass gate, got: {errs}")

    def test_r3_generator_rejects_contradictory_conflict_pair_price(self) -> None:
        raw_skus = generate_catalog.get_raw_skus()
        cp_bad = copy.deepcopy(self.base_data["conflict_pairs.json"])
        # 将 SKU-3C-008 的 final_price 设为超过其 original_price(39.9) 的 99.0
        cp_bad["pairs"][0]["path_a"]["expect"]["final_price"] = 99.0
        with self.assertRaises(ValueError):
            generate_catalog.apply_conflict_pairs_truth(
                raw_skus, cp_bad, self.base_data["spec_registry.json"]
            )

    # ==================== R4. 18 组隔离变异负例测试 ====================
    def test_r4_01_missing_orders_and_rules_files_rejected(self) -> None:
        def mutate(d: Dict[str, Any]) -> None:
            d["mock_orders.json"] = None
            d["platform_rules.json"] = None

        ok, _, errors = self.run_mutated_gate(mutate)
        self.assertFalse(ok)
        self.assertTrue(any("platform_rules.json" in e for e in errors))
        self.assertTrue(any("mock_orders.json" in e for e in errors))

    def test_r4_02_empty_dict_rules_rejected(self) -> None:
        ok, _, errors = self.run_mutated_gate(lambda d: d.update({"platform_rules.json": [{}, {}]}))
        self.assertFalse(ok)
        self.assertTrue(any("platform_rules" in e for e in errors))

    def test_r4_03_empty_orders_list_rejected(self) -> None:
        ok, _, errors = self.run_mutated_gate(lambda d: d.update({"mock_orders.json": []}))
        self.assertFalse(ok)
        self.assertTrue(any("mock_orders.json" in e for e in errors))

    def test_r4_04_duplicate_order_id_rejected(self) -> None:
        def mutate(d: Dict[str, Any]) -> None:
            d["mock_orders.json"].append(copy.deepcopy(d["mock_orders.json"][0]))

        ok, _, errors = self.run_mutated_gate(mutate)
        self.assertFalse(ok)
        self.assertTrue(any("重复的 order_id" in e for e in errors))

    def test_r4_05_invalid_order_bool_future_signed_and_missing_rule_version_rejected(self) -> None:
        def mutate(d: Dict[str, Any]) -> None:
            od = d["mock_orders.json"][1]  # delivered order
            od["is_opened"] = "false"
            od["signed_offset_hours"] = 24.0
            od["rule_version"] = "missing"

        ok, _, errors = self.run_mutated_gate(mutate)
        self.assertFalse(ok)
        self.assertGreaterEqual(len(errors), 3)

    def test_r4_06_nan_pay_amount_rejected(self) -> None:
        def mutate(d: Dict[str, Any]) -> None:
            d["mock_orders.json"][0]["pay_amount"] = float("nan")

        ok, _, errors = self.run_mutated_gate(mutate)
        self.assertFalse(ok)
        self.assertTrue(any("pay_amount" in e for e in errors))

    def test_r4_07_five_copies_of_c1_rejected(self) -> None:
        def mutate(d: Dict[str, Any]) -> None:
            c1 = copy.deepcopy(d["conflict_pairs.json"]["pairs"][0])
            d["conflict_pairs.json"]["pairs"] = [copy.deepcopy(c1) for _ in range(5)]

        ok, _, errors = self.run_mutated_gate(mutate)
        self.assertFalse(ok)
        self.assertTrue(any("冲突对 ID 列表必须严格为" in e for e in errors))

    def test_r4_08_deleting_unused_neutral_enum_aliases_rejected(self) -> None:
        def mutate(d: Dict[str, Any]) -> None:
            del d["spec_registry.json"]["sub_categories"]["facial_cream"]["specs"]["skin_type"]["zh_aliases"]["neutral"]

        ok, _, errors = self.run_mutated_gate(mutate)
        self.assertFalse(ok)
        self.assertTrue(any("遗漏了 vocabulary 中的枚举值" in e for e in errors))

    def test_r4_09_unknown_alias_target_outside_vocabulary_rejected(self) -> None:
        def mutate(d: Dict[str, Any]) -> None:
            d["spec_registry.json"]["sub_categories"]["facial_cream"]["specs"]["skin_type"]["zh_aliases"]["unknown_skin"] = ["未知肤质"]

        ok, _, errors = self.run_mutated_gate(mutate)
        self.assertFalse(ok)
        self.assertTrue(any("不在 vocabulary 中的未知目标" in e for e in errors))

    def test_r4_10_registry_default_op_drift_rejected(self) -> None:
        def mutate(d: Dict[str, Any]) -> None:
            d["spec_registry.json"]["sub_categories"]["gan_charger"]["specs"]["power_w"]["op"] = "<="

        ok, _, errors = self.run_mutated_gate(mutate)
        self.assertFalse(ok)
        self.assertTrue(any("默认比较方向漂移" in e for e in errors))

    def test_r4_11_invalid_sku_field_types_rejected(self) -> None:
        def mutate(d: Dict[str, Any]) -> None:
            sku = d["ecommerce_catalog_60skus.json"][0]
            sku["title"] = 123
            sku["retrieval_text"] = []
            sku["visual_description"] = {}
            sku["highlights"] = []

        ok, _, errors = self.run_mutated_gate(mutate)
        self.assertFalse(ok)
        self.assertGreaterEqual(len(errors), 4)

    def test_r4_12_fractional_port_count_rejected(self) -> None:
        def mutate(d: Dict[str, Any]) -> None:
            for s in d["ecommerce_catalog_60skus.json"]:
                if s["sku_id"] == "SKU-3C-023":
                    s["specs"]["port_count"] = 3.5

        ok, _, errors = self.run_mutated_gate(mutate)
        self.assertFalse(ok)
        self.assertTrue(any("离散整数规格" in e for e in errors))

    def test_r4_13_bool_as_number_rejected(self) -> None:
        def mutate(d: Dict[str, Any]) -> None:
            d["ecommerce_catalog_60skus.json"][0]["specs"]["power_w"] = True

        ok, _, errors = self.run_mutated_gate(mutate)
        self.assertFalse(ok)
        self.assertTrue(any("不是纯数字" in e for e in errors))

    def test_r4_14_enum_outside_vocabulary_rejected(self) -> None:
        def mutate(d: Dict[str, Any]) -> None:
            for s in d["ecommerce_catalog_60skus.json"]:
                if s["sku_id"] == "SKU-3C-015":
                    s["specs"]["layout"] = "108_keys"

        ok, _, errors = self.run_mutated_gate(mutate)
        self.assertFalse(ok)
        self.assertTrue(any("不在封闭词表" in e for e in errors))

    def test_r4_15_c1_path_a_price_tie_rejected(self) -> None:
        def mutate(d: Dict[str, Any]) -> None:
            for s in d["ecommerce_catalog_60skus.json"]:
                if s["sku_id"] == "SKU-3C-007":  # 17.3寸 ROG bag
                    s["final_price"] = 29.9

        ok, _, errors = self.run_mutated_gate(mutate)
        self.assertFalse(ok)
        self.assertTrue(any("Path A 最低价存在并列歧义" in e for e in errors))

    def test_r4_16_c1_path_b_closeness_tie_rejected(self) -> None:
        def mutate(d: Dict[str, Any]) -> None:
            for s in d["ecommerce_catalog_60skus.json"]:
                if s["sku_id"] == "SKU-3C-004":
                    s["final_price"] = 19.5
                    s["specs"]["screen_size"] = 15.6

        ok, _, errors = self.run_mutated_gate(mutate)
        self.assertFalse(ok)
        self.assertTrue(any("Path B 接近度得分存在并列歧义" in e for e in errors))

    def test_r4_17_c1_zero_intersection_violation_rejected(self) -> None:
        def mutate(d: Dict[str, Any]) -> None:
            for s in d["ecommerce_catalog_60skus.json"]:
                if s["sku_id"] == "SKU-3C-007":
                    s["final_price"] = 19.9  # <=20 and screen_size=17.3 >= 17

        ok, _, errors = self.run_mutated_gate(mutate)
        self.assertFalse(ok)
        self.assertTrue(any("违反断言1（零交集冲突）" in e for e in errors))

    def test_r4_18_null_specs_gracefully_rejected_without_crash(self) -> None:
        def mutate(d: Dict[str, Any]) -> None:
            d["ecommerce_catalog_60skus.json"][0]["specs"] = None

        ok, _, errors = self.run_mutated_gate(mutate)
        self.assertFalse(ok)
        self.assertTrue(any("specs 必须为 JSON object" in e for e in errors))

    # ==================== R5. 中文触发词消歧与边界策略回归 ====================
    def test_r5_alias_disambiguation_and_longest_match_policy(self) -> None:
        reg = self.base_data["spec_registry.json"]

        # 1. PA++++ 只命中 pa_plus_plus_plus_plus，不触发 PA+/PA++/PA+++
        r_pa = validate_catalog.match_aliases_with_policy("想要一款 SPF50+ PA++++ 的防晒霜", reg)
        self.assertEqual(r_pa["resolved_sub_category"], "sunscreen")
        self.assertEqual(r_pa["hard_enum_matches"].get("pa"), ["pa_plus_plus_plus_plus"])

        # 2. 有声段落轴 只命中 clicky，不触发 tactile(段落轴)
        r_sw = validate_catalog.match_aliases_with_policy("推荐一把有声段落轴的机械键盘", reg)
        self.assertEqual(r_sw["resolved_sub_category"], "mechanical_keyboard")
        self.assertEqual(r_sw["hard_enum_matches"].get("switch_type"), ["clicky"])

        # 3. 混油皮 只命中 combination，不触发 oily_acne(油皮)
        r_skin = validate_catalog.match_aliases_with_policy("适合混油皮的保湿面霜", reg)
        self.assertEqual(r_skin["resolved_sub_category"], "facial_cream")
        self.assertEqual(r_skin["hard_enum_matches"].get("skin_type"), ["combination"])

        # 4. 108键、全肤质、敏感肌、USB3.2 走 ambiguous_or_soft_hits，不强填硬过滤枚举
        r_108 = validate_catalog.match_aliases_with_policy("有没有 108键 机械键盘", reg)
        self.assertNotIn("layout", r_108["hard_enum_matches"])
        self.assertIn("108键", r_108["ambiguous_or_soft_hits"])

        r_all_skin = validate_catalog.match_aliases_with_policy("全肤质可用的面霜", reg)
        self.assertNotIn("skin_type", r_all_skin["hard_enum_matches"])
        self.assertIn("全肤质", r_all_skin["ambiguous_or_soft_hits"])

        r_sens = validate_catalog.match_aliases_with_policy("敏感肌修护面霜", reg)
        self.assertNotIn("skin_type", r_sens["hard_enum_matches"])
        self.assertIn("敏感肌", r_sens["ambiguous_or_soft_hits"])

        r_dry_sens = validate_catalog.match_aliases_with_policy("干敏肌修护面霜", reg)
        self.assertEqual(r_dry_sens["hard_enum_matches"].get("skin_type"), ["dry_sensitive"])

        r_usb32 = validate_catalog.match_aliases_with_policy("买个 USB3.2 拓展坞", reg)
        self.assertNotIn("protocol", r_usb32["hard_enum_matches"])
        self.assertIn("USB3.2", r_usb32["ambiguous_or_soft_hits"])

        r_usb32g2 = validate_catalog.match_aliases_with_policy("买个 USB3.2Gen2 拓展坞", reg)
        self.assertEqual(r_usb32g2["hard_enum_matches"].get("protocol"), ["usb3.2gen2"])

        # 5. 连帽卫衣开衫 归入 hoodie；多品类 Query 标记 multi_sub_category
        r_comp = validate_catalog.match_aliases_with_policy("想买件连帽卫衣开衫", reg)
        self.assertEqual(r_comp["resolved_sub_category"], "hoodie")

        r_multi = validate_catalog.match_aliases_with_policy("想买面霜和防晒霜", reg)
        self.assertEqual(r_multi["sub_category_ambiguity"], "multi_sub_category")
        self.assertEqual(set(r_multi["matched_sub_categories"]), {"facial_cream", "sunscreen"})

        # 6. 否定前缀守卫
        r_neg = validate_catalog.match_aliases_with_policy("不要青轴，想要红轴机械键盘", reg)
        self.assertEqual(r_neg["hard_enum_matches"].get("switch_type"), ["linear"])

    # ==================== R6. 售后与价保时间契约 ±1 秒边界测试 ====================
    def test_r6_aftersales_and_price_guarantee_second_level_boundaries(self) -> None:
        rules_by_type = {r["rule_type"]: r for r in self.base_data["platform_rules.json"]}
        sku_map = {s["sku_id"]: s for s in self.base_data["ecommerce_catalog_60skus.json"]}
        ref_dt = datetime.fromisoformat("2026-10-10T12:00:00+08:00")
        sku_charger = sku_map["SKU-3C-010"]  # original=169.0, final=129.0

        def make_order(signed_delta: timedelta | None, is_opened: bool = False, pay_amount: float = 129.0) -> Dict[str, Any]:
            paid_dt = ref_dt - timedelta(days=16)
            return {
                "order_id": "ORD-TEST",
                "order_status": "delivered" if signed_delta is not None else "shipped",
                "is_opened": is_opened,
                "has_price_guarantee": True,
                "has_freight_insurance": True,
                "pay_amount": pay_amount,
                "paid_at": paid_dt.isoformat(timespec="seconds"),
                "signed_at": (ref_dt - signed_delta).isoformat(timespec="seconds") if signed_delta is not None else None,
            }

        # 168h - 1s -> 允许退货
        res_minus_1s = validate_catalog.evaluate_order_eligibility(
            make_order(timedelta(hours=168) - timedelta(seconds=1)), sku_charger, rules_by_type, ref_dt
        )
        self.assertTrue(res_minus_1s["return_7d_eligible"])

        # 168h 00m 00s -> 含端点允许退货
        res_exact_168h = validate_catalog.evaluate_order_eligibility(
            make_order(timedelta(hours=168)), sku_charger, rules_by_type, ref_dt
        )
        self.assertTrue(res_exact_168h["return_7d_eligible"])

        # 168h + 1s -> 超时拒绝退货
        res_plus_1s = validate_catalog.evaluate_order_eligibility(
            make_order(timedelta(hours=168) + timedelta(seconds=1)), sku_charger, rules_by_type, ref_dt
        )
        self.assertFalse(res_plus_1s["return_7d_eligible"])
        self.assertEqual(res_plus_1s["return_7d_reason_code"], "WINDOW_EXPIRED_7D")

        # 未来签收时间 (-1h elapsed) -> 拒绝并标记 INVALID_FUTURE_SIGNED_AT
        res_future = validate_catalog.evaluate_order_eligibility(
            make_order(timedelta(hours=-1)), sku_charger, rules_by_type, ref_dt
        )
        self.assertFalse(res_future["return_7d_eligible"])
        self.assertEqual(res_future["return_7d_reason_code"], "INVALID_FUTURE_SIGNED_AT")

        # 360h 整点价保 + 有正差价 (169 - 129 = 40) -> 允许价保
        res_pg_exact = validate_catalog.evaluate_order_eligibility(
            make_order(timedelta(hours=360), is_opened=True, pay_amount=169.0),
            sku_charger,
            rules_by_type,
            ref_dt,
        )
        self.assertTrue(res_pg_exact["price_guarantee_15d_eligible"])
        self.assertEqual(res_pg_exact["price_diff_amount"], 40.0)

        # 360h + 1s 价保 -> 超出15天拒绝
        res_pg_plus_1s = validate_catalog.evaluate_order_eligibility(
            make_order(timedelta(hours=360) + timedelta(seconds=1), is_opened=True, pay_amount=169.0),
            sku_charger,
            rules_by_type,
            ref_dt,
        )
        self.assertFalse(res_pg_plus_1s["price_guarantee_15d_eligible"])
        self.assertEqual(res_pg_plus_1s["price_guarantee_15d_reason_code"], "WINDOW_EXPIRED_15D")

    def test_r6_gate_rejects_contradictory_next_day_midnight_rule_text(self) -> None:
        def mutate(d: Dict[str, Any]) -> None:
            d["platform_rules.json"][0]["content"] += "（自签收次日零时起算）"

        ok, _, errors = self.run_mutated_gate(mutate)
        self.assertFalse(ok)
        self.assertTrue(any("次日零时" in e for e in errors))

    # ==================== R7. 数值接近度函数边界与对抗样本竞争回归 ====================
    def test_r7_closeness_rejects_enum_constraint_explicitly(self) -> None:
        sunscreen_meta = self.base_data["spec_registry.json"]["sub_categories"]["sunscreen"]["specs"]
        sku_bea_002 = next(
            s for s in self.base_data["ecommerce_catalog_60skus.json"] if s["sku_id"] == "SKU-BEA-002"
        )
        with self.assertRaises(ValueError) as ctx:
            validate_catalog.compute_numeric_closeness_score(
                sku_bea_002,
                [{"key": "pa", "op": "==", "value": "pa_plus_plus_plus_plus"}],
                sunscreen_meta,
            )
        self.assertIn("仅支持数值型(number)规格", str(ctx.exception))

    def test_r7_adversarial_competition_c3_and_c4(self) -> None:
        reg = self.base_data["spec_registry.json"]["sub_categories"]
        sku_map = {s["sku_id"]: s for s in self.base_data["ecommerce_catalog_60skus.json"]}

        # C3 对抗候选：同在 <=60 元预算内，容量更大(15000mAh)但严重超重(248g > 200g)
        c3_specs = [
            {"key": "capacity_mah", "op": ">=", "value": 20000},
            {"key": "weight_g", "op": "<=", "value": 200},
        ]
        pb_meta = reg["power_bank"]["specs"]
        adv_pb = {"sku_id": "SKU-ADV-PB", "specs": {"capacity_mah": 15000, "output_w": 22.5, "weight_g": 248}}
        score_013 = validate_catalog.compute_numeric_closeness_score(sku_map["SKU-3C-013"], c3_specs, pb_meta)
        score_adv = validate_catalog.compute_numeric_closeness_score(adv_pb, c3_specs, pb_meta)
        # SKU-3C-013 (10000mAh, 185g -> Score 0.667) > adv_pb (15000mAh, 248g -> Score 1 - (0.333+0.48)/2 = 0.593)
        self.assertGreater(score_013, score_adv)

        # C4 对抗候选：同在 <=200 元预算内，防水更高(7000mm)但严重超重(395g > 300g)
        c4_specs = [
            {"key": "waterproof_mm", "op": ">=", "value": 10000},
            {"key": "weight_g", "op": "<=", "value": 300},
        ]
        wb_meta = reg["windbreaker"]["specs"]
        adv_wb = {"sku_id": "SKU-ADV-WB", "specs": {"waterproof_mm": 7000, "weight_g": 395}}
        score_app002 = validate_catalog.compute_numeric_closeness_score(sku_map["SKU-APP-002"], c4_specs, wb_meta)
        score_adv_wb = validate_catalog.compute_numeric_closeness_score(adv_wb, c4_specs, wb_meta)
        # SKU-APP-002 (5000mm, 320g -> Score 0.725) > adv_wb (7000mm, 395g -> Score 1 - (0.25+0.633)/2 = 0.558)
        self.assertGreater(score_app002, score_adv_wb)

    # ==================== S1~S11 复审加固回归测试 ====================
    def test_s1_cargo_pants_fit_no_cross_enum_contradiction(self) -> None:
        sku_map = {s["sku_id"]: s for s in self.base_data["ecommerce_catalog_60skus.json"]}
        for sid in ("SKU-APP-014", "SKU-APP-016"):
            sku = sku_map[sid]
            self.assertEqual(sku["specs"]["fit"], "loose")
            for fn in ("title", "visual_description", "search_query", "retrieval_text"):
                self.assertNotIn("直筒", sku[fn], f"{sid}.{fn} should not contain regular fit alias '直筒'")
            self.assertNotIn("直筒", " ".join(sku["highlights"]))

        # 负例：若在 fit='loose' 的 SKU-APP-016 title 或 SKU-APP-014 highlights 中写回 '直筒'，闸门必须拒绝
        def mutate_s1(d: Dict[str, Any]) -> None:
            reg = d["spec_registry.json"]
            for s in d["ecommerce_catalog_60skus.json"]:
                if s["sku_id"] == "SKU-APP-016":
                    s["title"] = "美式复古高密涤纶抗皱宽松直筒多袋工装长裤"
                    s["retrieval_text"] = validate_catalog.build_expected_retrieval_text(
                        s, reg["sub_categories"][s["sub_category"]]
                    )

        ok, _, errs = self.run_mutated_gate(mutate_s1)
        self.assertFalse(ok)
        self.assertTrue(any("违反 S1 跨枚举属性一致性" in e and "SKU-APP-016" in e for e in errs))

    def test_s2_non_self_referential_anti_pollution_rejects_p20_p21_p22_p23(self) -> None:
        # P20: hoodie.display_label -> '连帽卫衣' 并重算 retrieval_text
        def mutate_p20(d: Dict[str, Any]) -> None:
            reg = d["spec_registry.json"]
            reg["sub_categories"]["hoodie"]["display_label"] = "连帽卫衣"
            for s in d["ecommerce_catalog_60skus.json"]:
                if s["sub_category"] == "hoodie":
                    s["retrieval_text"] = validate_catalog.build_expected_retrieval_text(
                        s, reg["sub_categories"]["hoodie"]
                    )

        ok20, _, errs20 = self.run_mutated_gate(mutate_p20)
        self.assertFalse(ok20)
        self.assertTrue(any("display_label 偏离中性基准(S2/A2)" in e for e in errs20))
        self.assertTrue(any("SKU-APP-003" in e and "连帽卫衣" in e for e in errs20))

        # P21: power_bank.display_label -> '自带线充电宝' 并重算 retrieval_text
        def mutate_p21(d: Dict[str, Any]) -> None:
            reg = d["spec_registry.json"]
            reg["sub_categories"]["power_bank"]["display_label"] = "自带线充电宝"
            for s in d["ecommerce_catalog_60skus.json"]:
                if s["sub_category"] == "power_bank":
                    s["retrieval_text"] = validate_catalog.build_expected_retrieval_text(
                        s, reg["sub_categories"]["power_bank"]
                    )

        ok21, _, errs21 = self.run_mutated_gate(mutate_p21)
        self.assertFalse(ok21)
        self.assertTrue(any("display_label 偏离中性基准(S2/A2)" in e for e in errs21))
        self.assertTrue(any("SKU-3C-014" in e and "自带线充电宝" in e for e in errs21))

        # P22: switch_type.display_labels['linear'] -> '红轴' 并重算 retrieval_text
        def mutate_p22(d: Dict[str, Any]) -> None:
            reg = d["spec_registry.json"]
            reg["sub_categories"]["mechanical_keyboard"]["specs"]["switch_type"]["display_labels"]["linear"] = "红轴"
            for s in d["ecommerce_catalog_60skus.json"]:
                if s["sub_category"] == "mechanical_keyboard":
                    s["retrieval_text"] = validate_catalog.build_expected_retrieval_text(
                        s, reg["sub_categories"]["mechanical_keyboard"]
                    )

        ok22, _, errs22 = self.run_mutated_gate(mutate_p22)
        self.assertFalse(ok22)
        self.assertTrue(any("display_label 偏离中性基准(S2/A2)" in e for e in errs22))
        self.assertTrue(any("SKU-3C-015" in e and "红轴" in e for e in errs22))

    def test_s3_chinese_dimension_regex_rejects_m9_to_m14(self) -> None:
        # M9: 中文紧邻数字无空格 '洁面乳液100g'
        def mutate_m9(d: Dict[str, Any]) -> None:
            reg = d["spec_registry.json"]
            for s in d["ecommerce_catalog_60skus.json"]:
                if s["sku_id"] == "SKU-BEA-008":
                    s["title"] = "芙丽芳丝 净润氨基酸温和洁面乳液100g 日本进口"
                    s["retrieval_text"] = validate_catalog.build_expected_retrieval_text(
                        s, reg["sub_categories"]["amino_cleanser"]
                    )

        ok9, _, errs9 = self.run_mutated_gate(mutate_m9)
        self.assertFalse(ok9)
        self.assertTrue(any("SKU-BEA-008" in e and "量纲错配(R2/S3/F3)" in e for e in errs9))

        # M10: 中文单位 '100克'
        def mutate_m10(d: Dict[str, Any]) -> None:
            reg = d["spec_registry.json"]
            for s in d["ecommerce_catalog_60skus.json"]:
                if s["sku_id"] == "SKU-BEA-008":
                    s["title"] = "芙丽芳丝 净润氨基酸温和洁面乳液 净含量100克"
                    s["retrieval_text"] = validate_catalog.build_expected_retrieval_text(
                        s, reg["sub_categories"]["amino_cleanser"]
                    )

        ok10, _, errs10 = self.run_mutated_gate(mutate_m10)
        self.assertFalse(ok10)
        self.assertTrue(any("SKU-BEA-008" in e and "100克" in e for e in errs10))

        # M11 & M12: 把 '100g' 塞进 highlights 或 visual_description
        def mutate_m11_m12(d: Dict[str, Any]) -> None:
            reg = d["spec_registry.json"]
            for s in d["ecommerce_catalog_60skus.json"]:
                if s["sku_id"] == "SKU-BEA-008":
                    s["highlights"][0] = "100g大容量家庭装"
                    s["visual_description"] += " 瓶身印有100G字样"
                    s["retrieval_text"] = validate_catalog.build_expected_retrieval_text(
                        s, reg["sub_categories"]["amino_cleanser"]
                    )

        ok11, _, errs11 = self.run_mutated_gate(mutate_m11_m12)
        self.assertFalse(ok11)
        self.assertGreaterEqual(len([e for e in errs11 if "SKU-BEA-008" in e and "量纲错配(R2/S3/F3)" in e]), 2)

        # M13: 面料克重裸写进 highlights ('300g重磅纯棉')
        def mutate_m13(d: Dict[str, Any]) -> None:
            reg = d["spec_registry.json"]
            for s in d["ecommerce_catalog_60skus.json"]:
                if s["sku_id"] == "SKU-APP-003":
                    s["highlights"][0] = "300g重磅新疆长绒棉"
                    s["retrieval_text"] = validate_catalog.build_expected_retrieval_text(
                        s, reg["sub_categories"]["hoodie"]
                    )

        ok13, _, errs13 = self.run_mutated_gate(mutate_m13)
        self.assertFalse(ok13)
        self.assertTrue(any("SKU-APP-003" in e and "量纲歧义(R2/S3/F3)" in e for e in errs13))

        # M14: title 标 15ml 而 specs.volume_ml=30
        def mutate_m14(d: Dict[str, Any]) -> None:
            reg = d["spec_registry.json"]
            for s in d["ecommerce_catalog_60skus.json"]:
                if s["sku_id"] == "SKU-BEA-002":
                    s["title"] = s["title"].replace("30ml", "15ml")
                    s["retrieval_text"] = validate_catalog.build_expected_retrieval_text(
                        s, reg["sub_categories"]["sunscreen"]
                    )

        ok14, _, errs14 = self.run_mutated_gate(mutate_m14)
        self.assertFalse(ok14)
        self.assertTrue(any("SKU-BEA-002" in e and "容量数值不一致(R2/S3/F1)" in e for e in errs14))

    def test_s4_structured_rule_fields_consumed_rejects_m2_m3_m4_m5(self) -> None:
        # M2: aftersales.include_boundary -> False (翻转 168.0h 整点边界订单 ORD-20261001-007)
        def mutate_m2(d: Dict[str, Any]) -> None:
            for r in d["platform_rules.json"]:
                if r["rule_type"] == "aftersales":
                    r["include_boundary"] = False

        ok2, _, errs2 = self.run_mutated_gate(mutate_m2)
        self.assertFalse(ok2)
        self.assertTrue(any("ORD-20261001-007" in e for e in errs2))

        # M3: aftersales.require_unopened -> False (翻转已拆封订单 ORD-20261001-002 / ORD-20261001-005)
        def mutate_m3(d: Dict[str, Any]) -> None:
            for r in d["platform_rules.json"]:
                if r["rule_type"] == "aftersales":
                    r["require_unopened"] = False

        ok3, _, errs3 = self.run_mutated_gate(mutate_m3)
        self.assertFalse(ok3)
        self.assertTrue(any("ORD-20261001-005" in e for e in errs3))

        # M4: aftersales.allow_unsigned -> True (翻转在途未签收订单 ORD-20261001-001 / ORD-20261001-003)
        def mutate_m4(d: Dict[str, Any]) -> None:
            for r in d["platform_rules.json"]:
                if r["rule_type"] == "aftersales":
                    r["allow_unsigned"] = True

        ok4, _, errs4 = self.run_mutated_gate(mutate_m4)
        self.assertFalse(ok4)
        self.assertTrue(any("ORD-20261001-001" in e for e in errs4))

        # M5: price_guarantee.fallback_anchor_field -> 'created_at_WRONG'
        def mutate_m5(d: Dict[str, Any]) -> None:
            for r in d["platform_rules.json"]:
                if r["rule_type"] == "price_guarantee":
                    r["fallback_anchor_field"] = "created_at_WRONG"

        ok5, _, errs5 = self.run_mutated_gate(mutate_m5)
        self.assertFalse(ok5)
        self.assertTrue(any("fallback_anchor_field" in e for e in errs5))
        self.assertTrue(any("ORD-20261001-001" in e for e in errs5))

    def test_s5_negation_guard_does_not_false_positive_on_feichang_or_feiyao(self) -> None:
        reg = self.base_data["spec_registry.json"]
        for q in ("非常想要红轴机械键盘", "键盘非常推荐红轴", "我不是非要红轴的键盘"):
            res = validate_catalog.match_aliases_with_policy(q, reg)
            self.assertEqual(
                res["hard_enum_matches"].get("switch_type"),
                ["linear"],
                f"Query '{q}' should match linear without false negation",
            )

    def test_s6_s7_s8_s9_s10_s11_gate_hardening(self) -> None:
        # S6 / A1: 重复 rule_type 插入在列表最前面必须报错
        def mutate_s6(d: Dict[str, Any]) -> None:
            rules = d["platform_rules.json"]
            ghost = copy.deepcopy(next(r for r in rules if r["rule_type"] == "aftersales"))
            ghost["rule_id"] = "RULE-AFTER-GHOST"
            d["platform_rules.json"] = [ghost] + rules

        ok6, _, errs6 = self.run_mutated_gate(mutate_s6)
        self.assertFalse(ok6)
        self.assertTrue(any("重复的 rule_type='aftersales' (S6/A1)" in e for e in errs6))

        # S7 (M24): shipping 规则改为 max_days=30 / window_hours=720 必须报错
        def mutate_s7(d: Dict[str, Any]) -> None:
            for r in d["platform_rules.json"]:
                if r["rule_type"] == "shipping":
                    r["max_days"] = 30
                    r["window_hours"] = 720

        ok7, _, errs7 = self.run_mutated_gate(mutate_s7)
        self.assertFalse(ok7)
        self.assertTrue(any("RULE-SHIP-001" in e and "(S7)" in e for e in errs7))

        # S8 (M25): refunded 订单的物化 signed_at 与 signed_offset_hours 不一致必须报错
        def mutate_s8(d: Dict[str, Any]) -> None:
            ref_od = copy.deepcopy(d["mock_orders.json"][0])
            ref_od["order_id"] = "ORD-20261001-999"
            ref_od["order_status"] = "refunded"
            ref_od["signed_offset_hours"] = -72.0
            ref_od["signed_at"] = "1999-01-01T00:00:00+08:00"
            d["mock_orders.json"].append(ref_od)

        ok8, _, errs8 = self.run_mutated_gate(mutate_s8)
        self.assertFalse(ok8)
        self.assertTrue(any("ORD-20261001-999" in e and "(S8)" in e for e in errs8))

        # S9 (P26/P27/P28): 伪造 calibrated_from_retained_image（无 image_manifest）、删除溯源字段或注入非法 cos_image_url 必须报错
        def mutate_s9(d: Dict[str, Any]) -> None:
            d["image_manifest.json"] = None
            d["ecommerce_catalog_60skus.json"][0]["visual_description_source"] = "calibrated_from_retained_image"
            d["ecommerce_catalog_60skus.json"][1]["is_synthetic_benchmark"] = False
            d["ecommerce_catalog_60skus.json"][2]["cos_image_url"] = "https://evil.example.com/fake.jpg"

        ok9, _, errs9 = self.run_mutated_gate(mutate_s9)
        self.assertFalse(ok9)
        self.assertGreaterEqual(len([e for e in errs9 if "(S9)" in e]), 3)

        # S10 (P29/P30): 清空 matching_policy 或 ambiguous_terms 必须报错
        def mutate_s10(d: Dict[str, Any]) -> None:
            d["spec_registry.json"]["matching_policy"] = {}
            d["spec_registry.json"]["ambiguous_terms"] = {}

        ok10, _, errs10 = self.run_mutated_gate(mutate_s10)
        self.assertFalse(ok10)
        self.assertTrue(any("S10" in e for e in errs10))

        # S11: 验证 ORD-008 与 ORD-009 的 price_diff_amount 独立真值为 40.0 与 130.0
        ord_map = {o["order_id"]: o for o in self.base_data["mock_orders.json"]}
        self.assertEqual(ord_map["ORD-20261001-008"]["expected_eligibility"]["price_diff_amount"], 40.0)
        self.assertEqual(ord_map["ORD-20261001-009"]["expected_eligibility"]["price_diff_amount"], 130.0)

    # ==================== F1~F4 & A1~A2 第二轮复审专项回归测试 ====================
    def test_f1_conflict_truth_spec_sync_and_expect_keys_closed_loop(self) -> None:
        # 1) 修改 C1 path_a.expect.screen_size 为 17.1，生成器自动同步更新 specs 与全部文案，且通过闸门
        with tempfile.TemporaryDirectory(prefix="f1_ssot_") as tmp_str:
            tdir = Path(tmp_str)
            reg_copy = copy.deepcopy(self.base_data["spec_registry.json"])
            cp_copy = copy.deepcopy(self.base_data["conflict_pairs.json"])
            cp_copy["pairs"][0]["path_a"]["expect"]["screen_size"] = 17.1
            (tdir / "spec_registry.json").write_text(
                json.dumps(reg_copy, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (tdir / "conflict_pairs.json").write_text(
                json.dumps(cp_copy, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            orig_data_dir = generate_catalog.DATA_DIR
            try:
                generate_catalog.DATA_DIR = tdir
                generate_catalog.main()
            finally:
                generate_catalog.DATA_DIR = orig_data_dir

            new_cat = json.loads((tdir / "ecommerce_catalog_60skus.json").read_text(encoding="utf-8"))
            sku_008 = next(s for s in new_cat if s["sku_id"] == "SKU-3C-008")
            self.assertEqual(sku_008["specs"]["screen_size"], 17.1)
            self.assertIn("17.1寸", sku_008["title"])
            self.assertNotIn("17.3寸", sku_008["title"])
            self.assertNotIn("17.3", sku_008["retrieval_text"])

            ok, _, errs = validate_catalog.validate_all(tdir)
            self.assertTrue(ok, f"Expected synced 17.1 catalog to pass gate, got: {errs}")

        # 2) 若只改 specs 与 expect 为 17.1 而保留 title 中的 '17.3寸'，闸门必须报 F1 文本与规格数值漂移
        def mutate_f1_drift(d: Dict[str, Any]) -> None:
            reg = d["spec_registry.json"]
            d["conflict_pairs.json"]["pairs"][0]["path_a"]["expect"]["screen_size"] = 17.1
            for s in d["ecommerce_catalog_60skus.json"]:
                if s["sku_id"] == "SKU-3C-008":
                    s["specs"]["screen_size"] = 17.1
                    # 故意不改 s["title"] 中的 17.3寸
                    s["retrieval_text"] = validate_catalog.build_expected_retrieval_text(
                        s, reg["sub_categories"]["laptop_bag"]
                    )

        ok_drift, _, errs_drift = self.run_mutated_gate(mutate_f1_drift)
        self.assertFalse(ok_drift)
        self.assertTrue(any("文本与结构化规格数值漂移(F1)" in e and "SKU-3C-008" in e for e in errs_drift))

        # 3) 删除 path_a.expect.screen_size 或注入未知字段 foo=null，生成器与闸门双双拒绝
        def mutate_f1_missing_expect_key(d: Dict[str, Any]) -> None:
            del d["conflict_pairs.json"]["pairs"][0]["path_a"]["expect"]["screen_size"]

        ok_del, _, errs_del = self.run_mutated_gate(mutate_f1_missing_expect_key)
        self.assertFalse(ok_del)
        self.assertTrue(any("path_a.expect 键集合必须严格等于" in e and "(F1)" in e for e in errs_del))

        def mutate_f1_extra_expect_key(d: Dict[str, Any]) -> None:
            d["conflict_pairs.json"]["pairs"][0]["path_a"]["expect"]["foo"] = None

        ok_ext, _, errs_ext = self.run_mutated_gate(mutate_f1_extra_expect_key)
        self.assertFalse(ok_ext)
        self.assertTrue(any("path_a.expect 键集合必须严格等于" in e and "(F1)" in e for e in errs_ext))

        with self.assertRaises(ValueError):
            bad_cp = copy.deepcopy(self.base_data["conflict_pairs.json"])
            bad_cp["pairs"][0]["path_a"]["expect"]["foo"] = None
            generate_catalog.apply_conflict_pairs_truth(
                generate_catalog.get_raw_skus(), bad_cp, self.base_data["spec_registry.json"]
            )

    def test_f2_corrupted_inputs_return_structured_errors_without_traceback(self) -> None:
        # 1) 删除 laptop_bag.screen_size.range -> 结构化报错，不抛 KeyError
        def mutate_del_range(d: Dict[str, Any]) -> None:
            del d["spec_registry.json"]["sub_categories"]["laptop_bag"]["specs"]["screen_size"]["range"]

        ok_r, _, errs_r = self.run_mutated_gate(mutate_del_range)
        self.assertFalse(ok_r)
        self.assertTrue(any("[laptop_bag.screen_size.range]" in e and "(F2)" in e for e in errs_r))

        # 2) 删除 sunscreen.pa.vocabulary -> 结构化报错，不抛 KeyError
        def mutate_del_vocab(d: Dict[str, Any]) -> None:
            del d["spec_registry.json"]["sub_categories"]["sunscreen"]["specs"]["pa"]["vocabulary"]

        ok_v, _, errs_v = self.run_mutated_gate(mutate_del_vocab)
        self.assertFalse(ok_v)
        self.assertTrue(any("[sunscreen.pa.vocabulary]" in e and "(F2)" in e for e in errs_v))

        # 3) 无时区偏移的订单时间 -> 结构化报错，不抛 TypeError
        def mutate_naive_time(d: Dict[str, Any]) -> None:
            d["mock_orders.json"][0]["reference_time"] = "2026-10-10T12:00:00"

        ok_t, _, errs_t = self.run_mutated_gate(mutate_naive_time)
        self.assertFalse(ok_t)
        self.assertTrue(any("缺少 UTC 时区偏移(F2)" in e for e in errs_t))

    def test_f3_unit_check_covers_search_query_and_chinese_kg_gram_units(self) -> None:
        # 1) 面料克重商品 SKU-APP-003 search_query 出现裸 '300g'
        def mutate_sq_300g(d: Dict[str, Any]) -> None:
            for s in d["ecommerce_catalog_60skus.json"]:
                if s["sku_id"] == "SKU-APP-003":
                    s["search_query"] += " 300g"

        ok1, _, errs1 = self.run_mutated_gate(mutate_sq_300g)
        self.assertFalse(ok1)
        self.assertTrue(any("量纲歧义(R2/S3/F3)" in e and "search_query" in e for e in errs1))

        # 2) volume_ml 商品 highlights 出现 '0.5公斤'
        def mutate_hl_kg(d: Dict[str, Any]) -> None:
            reg = d["spec_registry.json"]
            for s in d["ecommerce_catalog_60skus.json"]:
                if s["sku_id"] == "SKU-BEA-002":
                    s["highlights"][0] = "重0.5公斤大瓶装"
                    s["retrieval_text"] = validate_catalog.build_expected_retrieval_text(
                        s, reg["sub_categories"]["sunscreen"]
                    )

        ok2, _, errs2 = self.run_mutated_gate(mutate_hl_kg)
        self.assertFalse(ok2)
        self.assertTrue(any("量纲错配(R2/S3/F3)" in e and "0.5公斤" in e for e in errs2))

    def test_f4_ambiguous_terms_deep_schema_and_a2_display_labels(self) -> None:
        # F4: 破坏 ambiguous_terms 内部 action 与 candidates
        def mutate_f4(d: Dict[str, Any]) -> None:
            d["spec_registry.json"]["ambiguous_terms"]["108键"]["action"] = "bad_action"
            d["spec_registry.json"]["ambiguous_terms"]["108键"]["candidates"] = ["non_existent_layout"]

        ok_f4, _, errs_f4 = self.run_mutated_gate(mutate_f4)
        self.assertFalse(ok_f4)
        self.assertGreaterEqual(len([e for e in errs_f4 if "(F4)" in e]), 2)

        # A2: 将 pa_plus 的 display_label 改为 'PA++'
        def mutate_a2(d: Dict[str, Any]) -> None:
            d["spec_registry.json"]["sub_categories"]["sunscreen"]["specs"]["pa"]["display_labels"]["pa_plus"] = "PA++"

        ok_a2, _, errs_a2 = self.run_mutated_gate(mutate_a2)
        self.assertFalse(ok_a2)
        self.assertTrue(any("[sunscreen.pa.pa_plus]" in e and "(S2/A2)" in e for e in errs_a2))

    # ==================== D1~D8 第三轮复审专项回归测试 ====================
    def test_d1_ports_composition_and_visual_sync(self) -> None:
        # 1) 在隔离目录将 C2 path_a.expect.ports 改为 4 重生成：
        #    验证 highlights 同步为 '3C1A四口智能分流'，visual_description 同步为 '3个Type-C口与1个橙色舌片USB-A口'，且全绿通过
        with tempfile.TemporaryDirectory(prefix="d1_ports_") as tmp_str:
            tdir = Path(tmp_str)
            reg_copy = copy.deepcopy(self.base_data["spec_registry.json"])
            cp_copy = copy.deepcopy(self.base_data["conflict_pairs.json"])
            cp_copy["pairs"][1]["path_a"]["expect"]["ports"] = 4
            (tdir / "spec_registry.json").write_text(
                json.dumps(reg_copy, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (tdir / "conflict_pairs.json").write_text(
                json.dumps(cp_copy, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            orig_data_dir = generate_catalog.DATA_DIR
            try:
                generate_catalog.DATA_DIR = tdir
                generate_catalog.main()
            finally:
                generate_catalog.DATA_DIR = orig_data_dir

            new_cat = json.loads((tdir / "ecommerce_catalog_60skus.json").read_text(encoding="utf-8"))
            sku_010 = next(s for s in new_cat if s["sku_id"] == "SKU-3C-010")
            self.assertEqual(sku_010["specs"]["ports"], 4)
            self.assertIn("3C1A四口", sku_010["highlights"][1])
            self.assertNotIn("2C1A", sku_010["highlights"][1])
            self.assertIn("3个Type-C口与1个橙色舌片USB-A口", sku_010["visual_description"])
            self.assertNotIn("2个Type-C口", sku_010["visual_description"])

            ok, _, errs = validate_catalog.validate_all(tdir)
            self.assertTrue(ok, f"Expected synced ports=4 catalog to pass gate, got: {errs}")

        # 2) 负例：若 specs.ports=4，但 highlights 仍写 '2C1A四口' 或 visual_description 仍写 '2个Type-C口与1个橙色舌片USB-A口'，闸门必须拒绝
        def mutate_d1_contradiction(d: Dict[str, Any]) -> None:
            reg = d["spec_registry.json"]
            d["conflict_pairs.json"]["pairs"][1]["path_a"]["expect"]["ports"] = 4
            for s in d["ecommerce_catalog_60skus.json"]:
                if s["sku_id"] == "SKU-3C-010":
                    s["specs"]["ports"] = 4
                    s["title"] = s["title"].replace("三口", "四口")
                    s["search_query"] = s["search_query"].replace("三口", "四口")
                    s["highlights"][1] = "2C1A四口智能分流"
                    # visual_description 保留旧的 "2个Type-C口与1个橙色舌片USB-A口"
                    s["retrieval_text"] = validate_catalog.build_expected_retrieval_text(
                        s, reg["sub_categories"]["gan_charger"]
                    )

        ok_bad, _, errs_bad = self.run_mutated_gate(mutate_d1_contradiction)
        self.assertFalse(ok_bad)
        self.assertTrue(any("2C1A" in e and "(D1/F1)" in e for e in errs_bad))
        self.assertTrue(any("Type-C 与 USB-A" in e and "(D1/F1)" in e for e in errs_bad))

    def test_d2_chinese_haoan_capacity_sync_and_gate(self) -> None:
        # 1) 负例：20000mAh 的 SKU-3C-012 追加 '19000毫安超大容量'，闸门必须拦截
        def mutate_d2_bad_haoan(d: Dict[str, Any]) -> None:
            reg = d["spec_registry.json"]
            for s in d["ecommerce_catalog_60skus.json"]:
                if s["sku_id"] == "SKU-3C-012":
                    s["highlights"].append("19000毫安超大容量")
                    s["retrieval_text"] = validate_catalog.build_expected_retrieval_text(
                        s, reg["sub_categories"]["power_bank"]
                    )

        ok_bad, _, errs_bad = self.run_mutated_gate(mutate_d2_bad_haoan)
        self.assertFalse(ok_bad)
        self.assertTrue(any("19000" in e and "(F1)" in e for e in errs_bad))

        # 2) 正例：生成器同步中文 '毫安'
        dummy_sku = {
            "title": "小米 10000毫安 充电宝",
            "highlights": ["10000毫安时大容量", "1万毫安便携"],
            "visual_description": "小巧机身",
            "search_query": "小米 10000毫安 充电宝",
        }
        generate_catalog._sync_sku_text_with_spec(dummy_sku, "capacity_mah", 11000)
        self.assertIn("11000毫安", dummy_sku["title"])
        self.assertIn("11000毫安时", dummy_sku["highlights"][0])
        self.assertIn("11000毫安", dummy_sku["highlights"][1])
        self.assertIn("11000毫安", dummy_sku["search_query"])

    def test_d3_kg_weight_sync_and_conversion_gate(self) -> None:
        # 1) 生成器同步 kg / 公斤 换算
        dummy_sku = {
            "title": "超轻充电宝",
            "highlights": ["195g轻便"],
            "visual_description": "整机重量0.195kg（约0.195公斤）",
            "search_query": "195g 充电宝",
        }
        generate_catalog._sync_sku_text_with_spec(dummy_sku, "weight_g", 190)
        self.assertIn("190g", dummy_sku["highlights"][0])
        self.assertIn("0.19kg", dummy_sku["visual_description"])
        self.assertIn("0.19公斤", dummy_sku["visual_description"])

        # 2) 负例：weight_g=195 的 SKU-3C-012 若在 visual_description 写 '整机重量0.190kg'，闸门必须拦截
        def mutate_d3_bad_kg(d: Dict[str, Any]) -> None:
            reg = d["spec_registry.json"]
            for s in d["ecommerce_catalog_60skus.json"]:
                if s["sku_id"] == "SKU-3C-012":
                    s["visual_description"] += " 整机重量0.190kg。"
                    s["retrieval_text"] = validate_catalog.build_expected_retrieval_text(
                        s, reg["sub_categories"]["power_bank"]
                    )

        ok_bad, _, errs_bad = self.run_mutated_gate(mutate_d3_bad_kg)
        self.assertFalse(ok_bad)
        self.assertTrue(any("(D3/F1)" in e and "0.190" in e for e in errs_bad))

    def test_d4_mixed_case_kg_unit_gate(self) -> None:
        # 在 100ml 乳液 SKU-BEA-008 中注入混合大小写 '0.5Kg' 与 '0.5kG'，闸门必须拦截
        for unit_variant in ("乳液0.5Kg家庭装", "乳液0.5kG家庭装"):
            def mutate_d4(d: Dict[str, Any], uv: str = unit_variant) -> None:
                reg = d["spec_registry.json"]
                for s in d["ecommerce_catalog_60skus.json"]:
                    if s["sku_id"] == "SKU-BEA-008":
                        s["highlights"].append(uv)
                        s["retrieval_text"] = validate_catalog.build_expected_retrieval_text(
                            s, reg["sub_categories"][s["sub_category"]]
                        )

            ok_v, _, errs_v = self.run_mutated_gate(mutate_d4)
            self.assertFalse(ok_v, f"Variant {unit_variant} should be rejected by gate")
            self.assertTrue(any("量纲错配(R2/S3/F3)" in e for e in errs_v))

    def test_d5_global_longest_match_cross_enum_gate(self) -> None:
        # 在 switch_type='tactile' (段落轴) 的 SKU-3C-016 中追加更长的其他枚举别名 '有声段落轴' (clicky)，闸门必须拒绝
        def mutate_d5_tactile_with_clicky(d: Dict[str, Any]) -> None:
            reg = d["spec_registry.json"]
            for s in d["ecommerce_catalog_60skus.json"]:
                if s["sku_id"] == "SKU-3C-016":
                    s["highlights"].append("清脆有声段落轴手感")
                    s["retrieval_text"] = validate_catalog.build_expected_retrieval_text(
                        s, reg["sub_categories"]["mechanical_keyboard"]
                    )

        ok1, _, errs1 = self.run_mutated_gate(mutate_d5_tactile_with_clicky)
        self.assertFalse(ok1)
        self.assertTrue(any("违反 S1 跨枚举属性一致性(D5)" in e and "有声段落轴" in e for e in errs1))

        # 在 pa='pa_plus_plus_plus' (PA+++) 的 SKU-BEA-003 中追加 'PA++++'，闸门必须拒绝
        def mutate_d5_pa3_with_pa4(d: Dict[str, Any]) -> None:
            reg = d["spec_registry.json"]
            for s in d["ecommerce_catalog_60skus.json"]:
                if s["sku_id"] == "SKU-BEA-003":
                    s["highlights"].append("升级PA++++防护")
                    s["retrieval_text"] = validate_catalog.build_expected_retrieval_text(
                        s, reg["sub_categories"]["sunscreen"]
                    )

        ok2, _, errs2 = self.run_mutated_gate(mutate_d5_pa3_with_pa4)
        self.assertFalse(ok2)
        self.assertTrue(any("违反 S1 跨枚举属性一致性(D5)" in e and "pa++++" in e for e in errs2))

    def test_d6_negation_particles_and_whitespace_guard(self) -> None:
        reg = self.base_data["spec_registry.json"]
        for neg_query in (
            "不要红轴机械键盘",
            "不要用红轴机械键盘",
            "不要 红轴机械键盘",
            "不需要带青轴机械键盘",
        ):
            res = validate_catalog.match_aliases_with_policy(neg_query, reg)
            self.assertEqual(
                res["hard_enum_matches"],
                {},
                f"Negated query {neg_query!r} should produce empty hard_enum_matches, got {res['hard_enum_matches']}",
            )

        # 同时确保正向“非常想要红轴”、“不是非要红轴”仍正常识别为 linear
        for pos_query in ("非常想要红轴机械键盘", "我不是非要红轴的键盘"):
            res_pos = validate_catalog.match_aliases_with_policy(pos_query, reg)
            self.assertEqual(res_pos["hard_enum_matches"].get("switch_type"), ["linear"])

    def test_d7_ambiguous_terms_case_insensitive_uniqueness(self) -> None:
        def mutate_d7_dup_ambig(d: Dict[str, Any]) -> None:
            d["spec_registry.json"]["ambiguous_terms"]["usb3.2"] = {
                "action": "clarify_or_multi_match",
                "scope": "mechanical_keyboard.layout",
                "candidates": ["87_keys"],
                "reason": "duplicate lowercase key conflict",
            }

        ok, _, errs = self.run_mutated_gate(mutate_d7_dup_ambig)
        self.assertFalse(ok)
        self.assertTrue(any("(D7/F4)" in e and "usb3.2" in e.lower() for e in errs))

    def test_d8_corrupted_range_schema_no_unhandled_exception(self) -> None:
        for bad_range in (None, {}, []):
            def mutate_d8(d: Dict[str, Any], br: Any = bad_range) -> None:
                d["spec_registry.json"]["sub_categories"]["laptop_bag"]["specs"]["screen_size"]["range"] = br

            ok, _, errs = self.run_mutated_gate(mutate_d8)
            self.assertFalse(ok)
            self.assertTrue(any("[laptop_bag.screen_size.range]" in e for e in errs))


if __name__ == "__main__":
    unittest.main(verbosity=2)
