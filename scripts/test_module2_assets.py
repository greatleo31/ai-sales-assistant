#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块二：多模态图片双落盘、gemini-embedding-2 1024维向量缓存、DDL/Seed 与本地向量库全量验收测试套件
覆盖正向契约与负向边界测试：
  1. 60 张商品图本地落盘存在性、JPEG 魔数 (\xff\xd8\xff)、Pillow 解码、尺寸 >= 200x150、SHA-256 100% 唯一性；
  2. image_manifest.json 与 ecommerce_catalog_60skus.json 的 cos_image_url / visual_description_source 联动一致性；
  3. 腾讯云 COS 线上 URL 真实 HTTP 200 + Content-Type: image/jpeg 抽样与二进制魔数核验；
  4. embeddings_cache.json (60 SKU + 3 规则) 1024 维向量完整性、text_sha256 一致性、IEEE 754 halfvec(1024) 量化无损性及异常向量拦截；
  5. deploy/docker-compose.yml、deploy/init.sql、deploy/seed_60skus_full.sql 外键顺序与 pgvector halfvec(1024) 契约；
  6. data/local_vector_store.db (SQLite) 表行数、PRAGMA foreign_key_check 零违约、halfvec 二进制解包与余弦相似度检出测试。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import struct
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List

import requests
from PIL import Image

sys.dont_write_bytecode = True

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import build_embeddings  # noqa: E402
import fetch_and_upload_images  # noqa: E402
import seed_database  # noqa: E402
import validate_catalog  # noqa: E402

DATA_DIR = ROOT_DIR / "data"
DEPLOY_DIR = ROOT_DIR / "deploy"
IMAGES_DIR = DATA_DIR / "images"
MANIFEST_PATH = DATA_DIR / "image_manifest.json"
CATALOG_PATH = DATA_DIR / "ecommerce_catalog_60skus.json"
RULES_PATH = DATA_DIR / "platform_rules.json"
ORDERS_PATH = DATA_DIR / "mock_orders.json"
EMBEDDINGS_PATH = DATA_DIR / "embeddings_cache.json"
SQLITE_DB_PATH = DATA_DIR / "local_vector_store.db"
DOCKER_COMPOSE_PATH = DEPLOY_DIR / "docker-compose.yml"
INIT_SQL_PATH = DEPLOY_DIR / "init.sql"
FULL_SEED_SQL_PATH = DEPLOY_DIR / "seed_60skus_full.sql"


class TestModule2Assets(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog: List[Dict[str, Any]] = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        cls.rules: List[Dict[str, Any]] = json.loads(RULES_PATH.read_text(encoding="utf-8"))
        cls.orders: List[Dict[str, Any]] = json.loads(ORDERS_PATH.read_text(encoding="utf-8"))
        cls.manifest: Dict[str, Dict[str, Any]] = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        cls.emb_cache: Dict[str, Any] = json.loads(EMBEDDINGS_PATH.read_text(encoding="utf-8"))

    def test_01_catalog_gate_remains_100_percent_green(self) -> None:
        """模块一 + 模块二联动后，validate_catalog.py 27 项自洽性断言必须保持 100% 通过。"""
        ok, passed, errors = validate_catalog.validate_all()
        self.assertTrue(ok, f"validate_catalog 失败: {errors}")
        self.assertEqual(len(errors), 0)
        self.assertGreaterEqual(len(passed), 27)

    def test_02_all_60_images_exist_valid_jpeg_magic_and_unique_sha256(self) -> None:
        """校验 60 张本地商品图存在、JPEG 魔数正确、分辨率 >= 200x150，且 60 个 SHA-256 100% 唯一。"""
        self.assertEqual(len(self.manifest), 60)
        seen_sha256 = set()
        seen_source_urls = set()

        for sku in self.catalog:
            sid = sku["sku_id"]
            self.assertIn(sid, self.manifest, f"image_manifest.json 缺少 {sid}")
            entry = self.manifest[sid]

            img_file = IMAGES_DIR / f"{sid}.jpg"
            self.assertTrue(img_file.exists(), f"本地图片不存在: {img_file}")
            raw_bytes = img_file.read_bytes()
            self.assertGreaterEqual(len(raw_bytes), 2048, f"{sid} 图片体积过小: {len(raw_bytes)}B")

            # 校验 JPEG 二进制魔数 \xff\xd8\xff
            self.assertEqual(
                fetch_and_upload_images.detect_magic_format(raw_bytes),
                "JPEG",
                f"{sid} 本地文件不是合法 JPEG 魔数",
            )
            self.assertEqual(entry.get("magic_format"), "JPEG")

            # 校验 SHA-256 与 manifest 完全一致且全局唯一
            actual_sha = hashlib.sha256(raw_bytes).hexdigest()
            self.assertEqual(actual_sha, entry.get("sha256"), f"{sid} 本地文件 SHA-256 与 manifest 不一致")
            self.assertNotIn(actual_sha, seen_sha256, f"{sid} 出现了重复图片 SHA-256: {actual_sha}")
            seen_sha256.add(actual_sha)

            src_url = entry.get("source_url")
            self.assertTrue(isinstance(src_url, str) and src_url.startswith("http"))
            self.assertNotIn(src_url, seen_source_urls, f"{sid} 出现了重复图片源 URL: {src_url}")
            seen_source_urls.add(src_url)

            # Pillow 解码与最小分辨率校验
            with Image.open(img_file) as im:
                self.assertEqual(im.format, "JPEG")
                w, h = im.size
                self.assertEqual(w, entry.get("width"))
                self.assertEqual(h, entry.get("height"))
                self.assertGreaterEqual(w, 200, f"{sid} 宽度不足 200px: {w}")
                self.assertGreaterEqual(h, 150, f"{sid} 高度不足 150px: {h}")

            # 校验 catalog 中溯源标记与 COS URL 已联动更新
            self.assertEqual(sku.get("visual_description_source"), "calibrated_from_retained_image")
            self.assertTrue(sku.get("is_synthetic_benchmark") is True)
            self.assertEqual(sku.get("cos_image_url"), entry.get("cos_image_url"))
            self.assertEqual(entry.get("calibrated_visual_description"), sku.get("visual_description"))

        self.assertEqual(len(seen_sha256), 60, "60 张 SKU 商品图必须拥有 60 个完全不同的 SHA-256")

    def test_03_cos_remote_urls_reachable_and_match_local_sha256(self) -> None:
        """抽样验证三大类目及冲突对核心 SKU 在腾讯云 COS 上的公开 URL 可直接 GET 200 且字节流 SHA-256 与本地一致。"""
        sample_skus = [
            "SKU-3C-008",   # C1 Path A
            "SKU-3C-009",   # C1 Path B
            "SKU-3C-010",   # C2 Path A
            "SKU-APP-001",  # C4 Path A
            "SKU-BEA-001",  # C5 Path A
        ]
        session = requests.Session()
        try:
            for sid in sample_skus:
                entry = self.manifest[sid]
                cos_url = entry["cos_image_url"]
                # Bucket 配置了防盗链白名单 (localhost:5173 / localhost:8123)，携带白名单 Referer 即可免签公开读取
                resp = session.get(
                    cos_url,
                    headers={"Referer": "http://localhost:5173/"},
                    timeout=10,
                )
                self.assertEqual(resp.status_code, 200, f"{sid} COS URL 访问失败: {cos_url}")
                self.assertIn("image/jpeg", resp.headers.get("Content-Type", "").lower())
                remote_sha = hashlib.sha256(resp.content).hexdigest()
                self.assertEqual(remote_sha, entry["sha256"], f"{sid} COS 远端图片与本地 SHA-256 不一致")
        finally:
            session.close()

    def test_04_embeddings_cache_completeness_and_halfvec_quantization(self) -> None:
        """校验 embeddings_cache.json 含 60 SKU + 3 规则的 1024 维向量，且全量通过 IEEE 754 halfvec(1024) 量化校验。"""
        meta = self.emb_cache.get("metadata", {})
        self.assertEqual(meta.get("dimension"), 1024)
        self.assertEqual(meta.get("quantization_target"), "halfvec(1024)")
        self.assertEqual(meta.get("sku_count"), 60)
        self.assertEqual(meta.get("rule_count"), 3)

        sku_embs = self.emb_cache.get("sku_embeddings", {})
        rule_embs = self.emb_cache.get("rule_embeddings", {})
        self.assertEqual(len(sku_embs), 60)
        self.assertEqual(len(rule_embs), 3)

        for sku in self.catalog:
            sid = sku["sku_id"]
            self.assertIn(sid, sku_embs)
            rec = sku_embs[sid]
            vec = rec["embedding"]
            ok, msg = build_embeddings.validate_embedding_vector(vec, expected_dim=1024)
            self.assertTrue(ok, f"{sid} 向量校验失败: {msg}")

            expected_sha = hashlib.sha256(sku["retrieval_text"].encode("utf-8")).hexdigest()
            self.assertEqual(rec["text_sha256"], expected_sha, f"{sid} retrieval_text SHA-256 失配")

            # 验证 fp16 量化最大误差 < 1e-3
            fp16_vals, fp16_blob = seed_database.quantize_to_fp16_list(vec)
            self.assertEqual(len(fp16_blob), 2048)
            max_diff = max(abs(a - b) for a, b in zip(vec, fp16_vals))
            self.assertLess(max_diff, 1e-3, f"{sid} halfvec(1024) 量化误差过大: {max_diff}")

        for rule in self.rules:
            rid = rule["rule_id"]
            self.assertIn(rid, rule_embs)
            rec = rule_embs[rid]
            vec = rec["embedding"]
            ok, msg = build_embeddings.validate_embedding_vector(vec, expected_dim=1024)
            self.assertTrue(ok, f"{rid} 规则向量校验失败: {msg}")
            rule_text = f"【规则】{rule['title']} ({rule['rule_version']})：{rule['content']}"
            expected_sha = hashlib.sha256(rule_text.encode("utf-8")).hexdigest()
            self.assertEqual(rec["text_sha256"], expected_sha, f"{rid} 规则文本 SHA-256 失配")

    def test_05_embedding_validator_negative_cases(self) -> None:
        """负向测试：验证向量校验器能拦截维度错误、NaN/Inf、零向量等损坏输入。"""
        ok_dim, _ = build_embeddings.validate_embedding_vector([0.1] * 512, expected_dim=1024)
        self.assertFalse(ok_dim)

        ok_nan, _ = build_embeddings.validate_embedding_vector([float("nan")] + [0.1] * 1023, expected_dim=1024)
        self.assertFalse(ok_nan)

        ok_inf, _ = build_embeddings.validate_embedding_vector([float("inf")] + [0.1] * 1023, expected_dim=1024)
        self.assertFalse(ok_inf)

        ok_zero, _ = build_embeddings.validate_embedding_vector([0.0] * 1024, expected_dim=1024)
        self.assertFalse(ok_zero)

    def test_06_docker_compose_and_sql_scripts_contract(self) -> None:
        """校验 deploy/docker-compose.yml、deploy/init.sql、deploy/seed_60skus_full.sql 符合 HANDOFF.md §2 & §3。"""
        self.assertTrue(DOCKER_COMPOSE_PATH.exists())
        compose_text = DOCKER_COMPOSE_PATH.read_text(encoding="utf-8")
        self.assertIn("pgvector/pgvector:pg16", compose_text)
        self.assertIn("POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?", compose_text)
        self.assertNotIn("shared_preload_libraries=vector", compose_text)

        self.assertTrue(INIT_SQL_PATH.exists())
        init_sql = INIT_SQL_PATH.read_text(encoding="utf-8")
        self.assertIn("CREATE EXTENSION IF NOT EXISTS vector;", init_sql)
        self.assertIn("sub_category VARCHAR(32) NOT NULL", init_sql)
        self.assertIn("embedding halfvec(1024) NOT NULL", init_sql)
        self.assertIn("halfvec_cosine_ops", init_sql)
        self.assertIn("REFERENCES sku_catalog(sku_id)", init_sql)

        # 验证 init.sql 中 INSERT INTO sku_catalog 严格先于 INSERT INTO mock_orders
        first_sku_insert = init_sql.find("INSERT INTO sku_catalog")
        first_order_insert = init_sql.find("INSERT INTO mock_orders")
        self.assertGreater(first_sku_insert, 0)
        self.assertGreater(first_order_insert, first_sku_insert, "init.sql 必须先插入 sku_catalog 再插入 mock_orders")

        # 验证 init.sql 中 mock_orders 引用的所有 sku_id 都已在 init.sql 前文插入了 sku_catalog
        init_inserted_skus = set(
            re.findall(r"INSERT INTO sku_catalog\s*\([^)]+\)\s*VALUES\s*\(\s*'([^']+)'", init_sql)
        )
        init_order_skus = set(
            re.findall(
                r"INSERT INTO mock_orders\s*\([^)]+\)\s*VALUES\s*\(\s*'[^']+',\s*'[^']+',\s*'[^']+',\s*'([^']+)'",
                init_sql,
            )
        )
        self.assertTrue(init_order_skus.issubset(init_inserted_skus), "init.sql 存在未先插入的订单外键 SKU")

        # 验证 seed_60skus_full.sql 包含完整 60 条 SKU、3 条规则、10 条订单且外键顺序正确
        self.assertTrue(FULL_SEED_SQL_PATH.exists())
        full_sql = FULL_SEED_SQL_PATH.read_text(encoding="utf-8")
        full_sku_inserts = re.findall(r"INSERT INTO sku_catalog", full_sql)
        full_rule_inserts = re.findall(r"INSERT INTO platform_rules", full_sql)
        full_order_inserts = re.findall(r"INSERT INTO mock_orders", full_sql)
        self.assertEqual(len(full_sku_inserts), 60)
        self.assertEqual(len(full_rule_inserts), 3)
        self.assertEqual(len(full_order_inserts), 10)
        self.assertLess(full_sql.rfind("INSERT INTO sku_catalog"), full_sql.find("INSERT INTO mock_orders"))

    def test_07_sqlite_local_vector_store_integrity_and_halfvec_similarity(self) -> None:
        """校验本地 SQLite 镜像库 data/local_vector_store.db 行数、外键完整性与 halfvec(1024) 向量检索能力。"""
        self.assertTrue(SQLITE_DB_PATH.exists())
        conn = sqlite3.connect(str(SQLITE_DB_PATH))
        try:
            conn.execute("PRAGMA foreign_keys = ON;")
            cur = conn.cursor()

            # 外键零违约检查
            fk_errors = cur.execute("PRAGMA foreign_key_check;").fetchall()
            self.assertEqual(len(fk_errors), 0, f"SQLite 外键检查发现违约: {fk_errors}")

            sku_count = cur.execute("SELECT COUNT(*) FROM sku_catalog;").fetchone()[0]
            rule_count = cur.execute("SELECT COUNT(*) FROM platform_rules;").fetchone()[0]
            order_count = cur.execute("SELECT COUNT(*) FROM mock_orders;").fetchone()[0]
            self.assertEqual(sku_count, 60)
            self.assertEqual(rule_count, 3)
            self.assertEqual(order_count, 10)

            # 验证外键拦截生效：插入指向不存在 SKU 的订单必须触发 IntegrityError
            with self.assertRaises(sqlite3.IntegrityError):
                cur.execute(
                    """
                    INSERT INTO mock_orders (
                        order_id, scenario_tag, user_id, sku_id, sku_title, pay_amount, order_status,
                        is_opened, has_price_guarantee, has_freight_insurance, rule_version,
                        reference_time, created_offset_hours, paid_offset_hours, created_at, paid_at,
                        expected_eligibility
                    ) VALUES (
                        'ORD-BAD-FK', 'bad', 'U1', 'SKU-NON-EXISTENT', 'Bad', 10.0, 'paid',
                        0, 1, 1, 'v2026.10', '2026-10-10T12:00:00+08:00', -1.0, -1.0,
                        '2026-10-10T11:00:00+08:00', '2026-10-10T11:00:00+08:00', '{}'
                    );
                    """
                )

            # 验证全量 60 条 SKU 的 halfvec blob 可以解包为 1024 维 fp16 向量并计算余弦相似度
            rows = cur.execute(
                "SELECT sku_id, sub_category, embedding_halfvec_blob FROM sku_catalog;"
            ).fetchall()
            self.assertEqual(len(rows), 60)

            vecs_by_id: Dict[str, List[float]] = {}
            sub_by_id: Dict[str, str] = {}
            for sid, sub_cat, blob in rows:
                self.assertEqual(len(blob), 2048)
                vals = list(struct.unpack("<1024e", blob))
                self.assertTrue(all(math.isfinite(v) for v in vals))
                vecs_by_id[sid] = vals
                sub_by_id[sid] = sub_cat

            # 以旗舰场景 C1 Path A (SKU-3C-008: 17.3寸防震电脑包) 为锚点，验证同子类目 laptop_bag 在向量空间的聚集性
            anchor_vec = vecs_by_id["SKU-3C-008"]

            def cosine_sim(a: List[float], b: List[float]) -> float:
                dot = sum(x * y for x, y in zip(a, b))
                na = math.sqrt(sum(x * x for x in a))
                nb = math.sqrt(sum(y * y for y in b))
                return dot / (na * nb) if na > 0 and nb > 0 else 0.0

            scored = [
                (cosine_sim(anchor_vec, v), sid, sub_by_id[sid])
                for sid, v in vecs_by_id.items()
                if sid != "SKU-3C-008"
            ]
            scored.sort(reverse=True)
            top3_subs = [item[2] for item in scored[:3]]
            self.assertTrue(
                all(sc == "laptop_bag" for sc in top3_subs),
                f"SKU-3C-008 (电脑包) 的 Top-3 最相似商品应全部属于 laptop_bag，实际为: {scored[:3]}",
            )
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
