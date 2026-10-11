#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块二：数据库 DDL/Seed 生成与双模灌库脚本 (scripts/seed_database.py)
严格遵循 HANDOFF.md §1, §2, §3：
  1. 先运行 validate_catalog.py 自洽性闸门，不通过则拒绝灌库；
  2. 校验 data/image_manifest.json (60 张唯一 SHA-256 JPEG 图) 与 data/embeddings_cache.json (60 SKU + 3 规则 1024 维向量)；
  3. 自动生成完全遵循外键顺序 (sku_catalog -> platform_rules -> mock_orders) 与 halfvec(1024) 契约的：
     - deploy/init.sql (建表 DDL + HNSW/GIN/B-Tree 索引 + 3 条核心种子 SKU + 3 条平台规则 + 3 条核心订单)
     - deploy/seed_60skus_full.sql (全量 60 条 SKU 含 halfvec(1024) 向量 + 3 条规则含向量 + 10 条订单幂等 UPSERT)
  4. 构建并校验本地 SQLite 完整镜像库 data/local_vector_store.db (含 IEEE 754 binary16 halfvec(1024) 量化向量 blob)，
     供 CentOS 虚机未开机时离线单测与本地检索引擎使用（检索结果显著标注 storage_backend）；
  5. 若远程 PostgreSQL (192.168.100.128:5432) 在线且可连接，自动执行全量灌库。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import socket
import sqlite3
import struct
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.dont_write_bytecode = True

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
DEPLOY_DIR = ROOT_DIR / "deploy"

CATALOG_PATH = DATA_DIR / "ecommerce_catalog_60skus.json"
RULES_PATH = DATA_DIR / "platform_rules.json"
ORDERS_PATH = DATA_DIR / "mock_orders.json"
MANIFEST_PATH = DATA_DIR / "image_manifest.json"
EMBEDDINGS_PATH = DATA_DIR / "embeddings_cache.json"
SQLITE_DB_PATH = DATA_DIR / "local_vector_store.db"

INIT_SQL_PATH = DEPLOY_DIR / "init.sql"
FULL_SEED_SQL_PATH = DEPLOY_DIR / "seed_60skus_full.sql"
ENV_PATH = ROOT_DIR / ".env"

EXPECTED_DIM = 1024


def load_env() -> Dict[str, str]:
    env: Dict[str, str] = {}
    if ENV_PATH.exists():
        for raw_line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def sql_literal(val: Any) -> str:
    """安全转义 PostgreSQL 字符串/数值/布尔/NULL 字面量。"""
    if val is None:
        return "NULL"
    if isinstance(val, bool):
        return "TRUE" if val else "FALSE"
    if isinstance(val, (int, float)):
        if not math.isfinite(float(val)):
            raise ValueError(f"禁止非有限数值写入 SQL: {val!r}")
        return str(val)
    s = str(val).replace("'", "''")
    return f"'{s}'"


def sql_jsonb(val: Any) -> str:
    """安全转义 PostgreSQL JSONB 字面量。"""
    dumped = json.dumps(val, ensure_ascii=False, sort_keys=True)
    return f"{sql_literal(dumped)}::jsonb"


def quantize_to_fp16_list(vec: List[float]) -> Tuple[List[float], bytes]:
    """将 1024 维 float 向量量化为 IEEE 754 binary16 (halfvec) 并返回 (fp16_floats, raw_bytes)。"""
    if len(vec) != EXPECTED_DIM:
        raise ValueError(f"向量维度必须为 {EXPECTED_DIM}，实测为 {len(vec)}")
    packed = struct.pack(f"<{EXPECTED_DIM}e", *[float(x) for x in vec])
    unpacked = list(struct.unpack(f"<{EXPECTED_DIM}e", packed))
    if not all(math.isfinite(x) for x in unpacked):
        raise ValueError("halfvec(1024) 量化后存在非有限数值 (NaN/Inf)")
    return unpacked, packed


def sql_halfvec(vec: List[float]) -> str:
    """生成 PostgreSQL pgvector halfvec(1024) 字面量: '[v1,v2,...]'::halfvec(1024)。"""
    fp16_vals, _ = quantize_to_fp16_list(vec)
    formatted = ",".join(f"{x:.6g}" for x in fp16_vals)
    return f"'[{formatted}]'::halfvec(1024)"


def build_ddl_header() -> str:
    return """-- ============================================================================
-- 电商导购 Agent (对标淘宝「千问」AI 购物助手) PostgreSQL + pgvector 初始化脚本
-- 权威口径: HANDOFF.md §1, §2, §3
-- 核心约束:
--   1. pgvector 无 int8 类型，全库向量统一使用 halfvec(1024) (IEEE 754 fp16)；
--   2. 60 行规模下查询默认走 Flat 精确扫描，DDL 保留 HNSW 索引供扩展对比实验；
--   3. sku_catalog 强制包含 sub_category，松弛与检索严格锁死在同一 sub_category；
--   4. 插入顺序严格遵守: sku_catalog -> platform_rules -> mock_orders，确保外键零违约。
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS vector;

-- 1. 商品主目录表 (60 SKUs = 3c_digital 25 + apparel 20 + beauty 15)
CREATE TABLE IF NOT EXISTS sku_catalog (
    sku_id VARCHAR(32) PRIMARY KEY,
    category VARCHAR(32) NOT NULL CHECK (category IN ('3c_digital', 'apparel', 'beauty')),
    sub_category VARCHAR(32) NOT NULL,
    title VARCHAR(256) NOT NULL,
    brand VARCHAR(64) NOT NULL,
    original_price NUMERIC(10, 2) NOT NULL CHECK (original_price > 0),
    final_price NUMERIC(10, 2) NOT NULL CHECK (final_price > 0 AND final_price <= original_price),
    inventory INT NOT NULL DEFAULT 100 CHECK (inventory >= 0),
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    specs JSONB NOT NULL DEFAULT '{}'::jsonb,
    highlights JSONB NOT NULL DEFAULT '[]'::jsonb,
    image_path VARCHAR(256) NOT NULL,
    cos_image_url VARCHAR(512) NOT NULL,
    search_query VARCHAR(256) NOT NULL,
    visual_description TEXT NOT NULL,
    visual_description_source VARCHAR(64) NOT NULL DEFAULT 'calibrated_from_retained_image',
    is_synthetic_benchmark BOOLEAN NOT NULL DEFAULT TRUE,
    retrieval_text TEXT NOT NULL,
    embedding halfvec(1024) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 复合 B-Tree 索引：支撑硬约束预过滤与同 sub_category 松弛锁
CREATE INDEX IF NOT EXISTS idx_sku_catalog_cat_sub_price
    ON sku_catalog (category, sub_category, final_price);

CREATE INDEX IF NOT EXISTS idx_sku_catalog_sub_price
    ON sku_catalog (sub_category, final_price);

-- GIN 索引：支撑 JSONB specs 多维度标量/枚举预过滤
CREATE INDEX IF NOT EXISTS idx_sku_catalog_specs_gin
    ON sku_catalog USING GIN (specs);

-- HNSW 向量索引：保留供扩展对比实验（60 行规模下主链路默认走 Flat 扫描）
CREATE INDEX IF NOT EXISTS idx_sku_catalog_embedding_hnsw
    ON sku_catalog USING hnsw (embedding halfvec_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- 2. 平台售后与服务规则表 (简化自淘宝公开规则：7天无理由 / 15天价保 / 退货运费险)
CREATE TABLE IF NOT EXISTS platform_rules (
    rule_id VARCHAR(32) PRIMARY KEY,
    rule_version VARCHAR(32) NOT NULL,
    rule_type VARCHAR(32) NOT NULL UNIQUE,
    title VARCHAR(128) NOT NULL,
    content TEXT NOT NULL,
    max_days INT NOT NULL CHECK (max_days > 0),
    window_hours INT NOT NULL CHECK (window_hours > 0),
    anchor_field VARCHAR(32) NOT NULL,
    fallback_anchor_field VARCHAR(32),
    include_boundary BOOLEAN NOT NULL DEFAULT TRUE,
    allow_unsigned BOOLEAN NOT NULL DEFAULT FALSE,
    require_unopened BOOLEAN NOT NULL DEFAULT FALSE,
    depends_on_rule_id VARCHAR(32),
    source_reference JSONB NOT NULL DEFAULT '{}'::jsonb,
    embedding halfvec(1024) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 3. 模拟订单与物流表 (外键关联 sku_catalog.sku_id，用于售后资格确定性判定闭环)
CREATE TABLE IF NOT EXISTS mock_orders (
    order_id VARCHAR(32) PRIMARY KEY,
    scenario_tag VARCHAR(64) NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    sku_id VARCHAR(32) NOT NULL REFERENCES sku_catalog(sku_id) ON DELETE RESTRICT,
    sku_title VARCHAR(256) NOT NULL,
    pay_amount NUMERIC(10, 2) NOT NULL CHECK (pay_amount > 0),
    order_status VARCHAR(32) NOT NULL CHECK (
        order_status IN ('pending', 'shipped', 'delivered', 'refunded')
    ),
    is_opened BOOLEAN NOT NULL DEFAULT FALSE,
    has_price_guarantee BOOLEAN NOT NULL DEFAULT TRUE,
    has_freight_insurance BOOLEAN NOT NULL DEFAULT TRUE,
    rule_version VARCHAR(32) NOT NULL,
    logistics_company VARCHAR(64),
    tracking_number VARCHAR(64),
    latest_logistics_event TEXT,
    reference_time TIMESTAMPTZ NOT NULL,
    created_offset_hours NUMERIC(10, 2) NOT NULL,
    paid_offset_hours NUMERIC(10, 2) NOT NULL,
    signed_offset_hours NUMERIC(10, 2),
    created_at TIMESTAMPTZ NOT NULL,
    paid_at TIMESTAMPTZ NOT NULL,
    signed_at TIMESTAMPTZ,
    expected_eligibility JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_mock_orders_user_status
    ON mock_orders (user_id, order_status);

CREATE INDEX IF NOT EXISTS idx_mock_orders_sku_id
    ON mock_orders (sku_id);
"""


def format_sku_upsert(sku: Dict[str, Any], vec: List[float]) -> str:
    return (
        "INSERT INTO sku_catalog (\n"
        "    sku_id, category, sub_category, title, brand, original_price, final_price,\n"
        "    inventory, status, specs, highlights,\n"
        "    image_path, cos_image_url, search_query, visual_description,\n"
        "    visual_description_source, is_synthetic_benchmark, retrieval_text, embedding\n"
        ") VALUES (\n"
        f"    {sql_literal(sku['sku_id'])}, {sql_literal(sku['category'])}, {sql_literal(sku['sub_category'])},\n"
        f"    {sql_literal(sku['title'])}, {sql_literal(sku['brand'])}, "
        f"{sql_literal(sku['original_price'])}, {sql_literal(sku['final_price'])},\n"
        f"    {sql_literal(sku.get('inventory', 100))}, {sql_literal(sku.get('status', 'active'))},\n"
        f"    {sql_jsonb(sku['specs'])}, {sql_jsonb(sku['highlights'])},\n"
        f"    {sql_literal(sku['image_path'])}, {sql_literal(sku['cos_image_url'])}, "
        f"{sql_literal(sku['search_query'])},\n"
        f"    {sql_literal(sku['visual_description'])}, {sql_literal(sku['visual_description_source'])}, "
        f"{sql_literal(sku['is_synthetic_benchmark'])},\n"
        f"    {sql_literal(sku['retrieval_text'])},\n"
        f"    {sql_halfvec(vec)}\n"
        ") ON CONFLICT (sku_id) DO UPDATE SET\n"
        "    category = EXCLUDED.category,\n"
        "    sub_category = EXCLUDED.sub_category,\n"
        "    title = EXCLUDED.title,\n"
        "    brand = EXCLUDED.brand,\n"
        "    original_price = EXCLUDED.original_price,\n"
        "    final_price = EXCLUDED.final_price,\n"
        "    inventory = EXCLUDED.inventory,\n"
        "    status = EXCLUDED.status,\n"
        "    specs = EXCLUDED.specs,\n"
        "    highlights = EXCLUDED.highlights,\n"
        "    image_path = EXCLUDED.image_path,\n"
        "    cos_image_url = EXCLUDED.cos_image_url,\n"
        "    search_query = EXCLUDED.search_query,\n"
        "    visual_description = EXCLUDED.visual_description,\n"
        "    visual_description_source = EXCLUDED.visual_description_source,\n"
        "    is_synthetic_benchmark = EXCLUDED.is_synthetic_benchmark,\n"
        "    retrieval_text = EXCLUDED.retrieval_text,\n"
        "    embedding = EXCLUDED.embedding;\n"
    )


def format_rule_upsert(rule: Dict[str, Any], vec: List[float]) -> str:
    return (
        "INSERT INTO platform_rules (\n"
        "    rule_id, rule_version, rule_type, title, content,\n"
        "    max_days, window_hours, anchor_field, fallback_anchor_field,\n"
        "    include_boundary, allow_unsigned, require_unopened,\n"
        "    depends_on_rule_id, source_reference, embedding\n"
        ") VALUES (\n"
        f"    {sql_literal(rule['rule_id'])}, {sql_literal(rule['rule_version'])}, {sql_literal(rule['rule_type'])},\n"
        f"    {sql_literal(rule['title'])}, {sql_literal(rule['content'])},\n"
        f"    {sql_literal(rule['max_days'])}, {sql_literal(rule['window_hours'])},\n"
        f"    {sql_literal(rule['anchor_field'])}, {sql_literal(rule.get('fallback_anchor_field'))},\n"
        f"    {sql_literal(rule['include_boundary'])}, {sql_literal(rule['allow_unsigned'])}, "
        f"{sql_literal(rule['require_unopened'])},\n"
        f"    {sql_literal(rule.get('depends_on_rule_id'))}, {sql_jsonb(rule['source_reference'])},\n"
        f"    {sql_halfvec(vec)}\n"
        ") ON CONFLICT (rule_id) DO UPDATE SET\n"
        "    rule_version = EXCLUDED.rule_version,\n"
        "    rule_type = EXCLUDED.rule_type,\n"
        "    title = EXCLUDED.title,\n"
        "    content = EXCLUDED.content,\n"
        "    max_days = EXCLUDED.max_days,\n"
        "    window_hours = EXCLUDED.window_hours,\n"
        "    anchor_field = EXCLUDED.anchor_field,\n"
        "    fallback_anchor_field = EXCLUDED.fallback_anchor_field,\n"
        "    include_boundary = EXCLUDED.include_boundary,\n"
        "    allow_unsigned = EXCLUDED.allow_unsigned,\n"
        "    require_unopened = EXCLUDED.require_unopened,\n"
        "    depends_on_rule_id = EXCLUDED.depends_on_rule_id,\n"
        "    source_reference = EXCLUDED.source_reference,\n"
        "    embedding = EXCLUDED.embedding,\n"
        "    updated_at = NOW();\n"
    )


def format_order_upsert(order: Dict[str, Any]) -> str:
    return (
        "INSERT INTO mock_orders (\n"
        "    order_id, scenario_tag, user_id, sku_id, sku_title, pay_amount, order_status,\n"
        "    is_opened, has_price_guarantee, has_freight_insurance, rule_version,\n"
        "    logistics_company, tracking_number, latest_logistics_event,\n"
        "    reference_time, created_offset_hours, paid_offset_hours, signed_offset_hours,\n"
        "    created_at, paid_at, signed_at, expected_eligibility\n"
        ") VALUES (\n"
        f"    {sql_literal(order['order_id'])}, {sql_literal(order['scenario_tag'])}, {sql_literal(order['user_id'])},\n"
        f"    {sql_literal(order['sku_id'])}, {sql_literal(order['sku_title'])}, "
        f"{sql_literal(order['pay_amount'])}, {sql_literal(order['order_status'])},\n"
        f"    {sql_literal(order['is_opened'])}, {sql_literal(order['has_price_guarantee'])}, "
        f"{sql_literal(order['has_freight_insurance'])}, {sql_literal(order['rule_version'])},\n"
        f"    {sql_literal(order.get('logistics_company'))}, {sql_literal(order.get('tracking_number'))}, "
        f"{sql_literal(order.get('latest_logistics_event'))},\n"
        f"    {sql_literal(order['reference_time'])}, {sql_literal(order['created_offset_hours'])}, "
        f"{sql_literal(order['paid_offset_hours'])}, {sql_literal(order.get('signed_offset_hours'))},\n"
        f"    {sql_literal(order['created_at'])}, {sql_literal(order['paid_at'])}, "
        f"{sql_literal(order.get('signed_at'))}, {sql_jsonb(order['expected_eligibility'])}\n"
        ") ON CONFLICT (order_id) DO UPDATE SET\n"
        "    scenario_tag = EXCLUDED.scenario_tag,\n"
        "    user_id = EXCLUDED.user_id,\n"
        "    sku_id = EXCLUDED.sku_id,\n"
        "    sku_title = EXCLUDED.sku_title,\n"
        "    pay_amount = EXCLUDED.pay_amount,\n"
        "    order_status = EXCLUDED.order_status,\n"
        "    is_opened = EXCLUDED.is_opened,\n"
        "    has_price_guarantee = EXCLUDED.has_price_guarantee,\n"
        "    has_freight_insurance = EXCLUDED.has_freight_insurance,\n"
        "    rule_version = EXCLUDED.rule_version,\n"
        "    logistics_company = EXCLUDED.logistics_company,\n"
        "    tracking_number = EXCLUDED.tracking_number,\n"
        "    latest_logistics_event = EXCLUDED.latest_logistics_event,\n"
        "    reference_time = EXCLUDED.reference_time,\n"
        "    created_offset_hours = EXCLUDED.created_offset_hours,\n"
        "    paid_offset_hours = EXCLUDED.paid_offset_hours,\n"
        "    signed_offset_hours = EXCLUDED.signed_offset_hours,\n"
        "    created_at = EXCLUDED.created_at,\n"
        "    paid_at = EXCLUDED.paid_at,\n"
        "    signed_at = EXCLUDED.signed_at,\n"
        "    expected_eligibility = EXCLUDED.expected_eligibility;\n"
    )


def generate_sql_files(
    catalog: List[Dict[str, Any]],
    rules: List[Dict[str, Any]],
    orders: List[Dict[str, Any]],
    sku_vecs: Dict[str, List[float]],
    rule_vecs: Dict[str, List[float]],
) -> None:
    DEPLOY_DIR.mkdir(parents=True, exist_ok=True)
    sku_map = {s["sku_id"]: s for s in catalog}

    # 1. 生成 deploy/init.sql：包含 DDL + 3 条核心种子 SKU + 3 条规则 + 3 条核心种子订单
    # 严格保证 INSERT INTO sku_catalog 先于 INSERT INTO mock_orders，杜绝外键违约
    seed_order_ids = ["ORD-20261001-001", "ORD-20261001-002", "ORD-20261001-003"]
    seed_orders = [o for o in orders if o["order_id"] in seed_order_ids]
    seed_sku_ids: List[str] = []
    for o in seed_orders:
        if o["sku_id"] not in seed_sku_ids:
            seed_sku_ids.append(o["sku_id"])

    init_parts: List[str] = [
        build_ddl_header(),
        "\n-- ============================================================================\n"
        "-- Step 1: 插入核心种子商品 (先于 mock_orders 插入，确保外键约束 100% 成立)\n"
        "-- ============================================================================\n",
    ]
    for sid in seed_sku_ids:
        init_parts.append(format_sku_upsert(sku_map[sid], sku_vecs[sid]))

    init_parts.append(
        "\n-- ============================================================================\n"
        "-- Step 2: 插入 3 条平台售后与价保规则\n"
        "-- ============================================================================\n"
    )
    for r in rules:
        init_parts.append(format_rule_upsert(r, rule_vecs[r["rule_id"]]))

    init_parts.append(
        "\n-- ============================================================================\n"
        "-- Step 3: 插入 3 条核心基准测试订单 (外键指向已插入的种子 SKU)\n"
        "-- ============================================================================\n"
    )
    for o in seed_orders:
        init_parts.append(format_order_upsert(o))

    INIT_SQL_PATH.write_text("\n".join(init_parts), encoding="utf-8")

    # 2. 生成 deploy/seed_60skus_full.sql：全量 60 条 SKU + 3 条规则 + 10 条订单幂等灌库脚本
    full_parts: List[str] = [
        "-- ============================================================================\n"
        "-- 全量 60 SKU + 3 平台规则 + 10 模拟订单 幂等灌库脚本 (deploy/seed_60skus_full.sql)\n"
        "-- 由 scripts/seed_database.py 从真源 JSON 与 embeddings_cache.json 自动生成，严禁手工改写\n"
        "-- ============================================================================\n\n"
        "BEGIN;\n\n"
        "-- 1. 全量 60 条 SKU (含 gemini-embedding-2 1024 维 halfvec 向量)\n"
    ]
    for s in catalog:
        full_parts.append(format_sku_upsert(s, sku_vecs[s["sku_id"]]))

    full_parts.append("\n-- 2. 全量 3 条平台规则 (含 1024 维 halfvec 向量)\n")
    for r in rules:
        full_parts.append(format_rule_upsert(r, rule_vecs[r["rule_id"]]))

    full_parts.append("\n-- 3. 全量 10 条基准与边界测试订单 (外键依赖 sku_catalog 已全部就绪)\n")
    for o in orders:
        full_parts.append(format_order_upsert(o))

    full_parts.append("\nCOMMIT;\n")
    FULL_SEED_SQL_PATH.write_text("\n".join(full_parts), encoding="utf-8")


def seed_sqlite_local_store(
    catalog: List[Dict[str, Any]],
    rules: List[Dict[str, Any]],
    orders: List[Dict[str, Any]],
    sku_vecs: Dict[str, List[float]],
    rule_vecs: Dict[str, List[float]],
) -> None:
    """构建本地 SQLite 完整镜像库 data/local_vector_store.db，支持外键约束与 IEEE 754 halfvec(1024) 检索。"""
    conn = sqlite3.connect(str(SQLITE_DB_PATH))
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        cur = conn.cursor()

        # 按依赖反序清理旧表以保证幂等重建
        cur.execute("DROP TABLE IF EXISTS mock_orders;")
        cur.execute("DROP TABLE IF EXISTS platform_rules;")
        cur.execute("DROP TABLE IF EXISTS sku_catalog;")

        cur.execute(
            """
            CREATE TABLE sku_catalog (
                sku_id TEXT PRIMARY KEY,
                category TEXT NOT NULL CHECK (category IN ('3c_digital', 'apparel', 'beauty')),
                sub_category TEXT NOT NULL,
                title TEXT NOT NULL,
                brand TEXT NOT NULL,
                original_price REAL NOT NULL CHECK (original_price > 0),
                final_price REAL NOT NULL CHECK (final_price > 0 AND final_price <= original_price),
                inventory INTEGER NOT NULL DEFAULT 100 CHECK (inventory >= 0),
                status TEXT NOT NULL DEFAULT 'active',
                specs JSON NOT NULL,
                highlights JSON NOT NULL,
                image_path TEXT NOT NULL,
                cos_image_url TEXT NOT NULL,
                search_query TEXT NOT NULL,
                visual_description TEXT NOT NULL,
                visual_description_source TEXT NOT NULL,
                is_synthetic_benchmark INTEGER NOT NULL DEFAULT 1,
                retrieval_text TEXT NOT NULL,
                embedding_halfvec_blob BLOB NOT NULL,
                embedding_halfvec_json TEXT NOT NULL
            );
            """
        )
        cur.execute(
            "CREATE INDEX idx_sqlite_sku_cat_sub_price ON sku_catalog (category, sub_category, final_price);"
        )
        cur.execute(
            "CREATE INDEX idx_sqlite_sku_sub_price ON sku_catalog (sub_category, final_price);"
        )

        cur.execute(
            """
            CREATE TABLE platform_rules (
                rule_id TEXT PRIMARY KEY,
                rule_version TEXT NOT NULL,
                rule_type TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                max_days INTEGER NOT NULL CHECK (max_days > 0),
                window_hours INTEGER NOT NULL CHECK (window_hours > 0),
                anchor_field TEXT NOT NULL,
                fallback_anchor_field TEXT,
                include_boundary INTEGER NOT NULL DEFAULT 1,
                allow_unsigned INTEGER NOT NULL DEFAULT 0,
                require_unopened INTEGER NOT NULL DEFAULT 0,
                depends_on_rule_id TEXT,
                source_reference JSON NOT NULL,
                embedding_halfvec_blob BLOB NOT NULL,
                embedding_halfvec_json TEXT NOT NULL
            );
            """
        )

        cur.execute(
            """
            CREATE TABLE mock_orders (
                order_id TEXT PRIMARY KEY,
                scenario_tag TEXT NOT NULL,
                user_id TEXT NOT NULL,
                sku_id TEXT NOT NULL REFERENCES sku_catalog(sku_id) ON DELETE RESTRICT,
                sku_title TEXT NOT NULL,
                pay_amount REAL NOT NULL CHECK (pay_amount > 0),
                order_status TEXT NOT NULL CHECK (
                    order_status IN ('pending', 'shipped', 'delivered', 'refunded')
                ),
                is_opened INTEGER NOT NULL DEFAULT 0,
                has_price_guarantee INTEGER NOT NULL DEFAULT 1,
                has_freight_insurance INTEGER NOT NULL DEFAULT 1,
                rule_version TEXT NOT NULL,
                logistics_company TEXT,
                tracking_number TEXT,
                latest_logistics_event TEXT,
                reference_time TEXT NOT NULL,
                created_offset_hours REAL NOT NULL,
                paid_offset_hours REAL NOT NULL,
                signed_offset_hours REAL,
                created_at TEXT NOT NULL,
                paid_at TEXT NOT NULL,
                signed_at TEXT,
                expected_eligibility JSON NOT NULL
            );
            """
        )

        # 1. 插入 60 条 SKU (先于 mock_orders 插入)
        for s in catalog:
            fp16_vals, fp16_blob = quantize_to_fp16_list(sku_vecs[s["sku_id"]])
            cur.execute(
                """
                INSERT INTO sku_catalog (
                    sku_id, category, sub_category, title, brand, original_price, final_price,
                    inventory, status, specs, highlights,
                    image_path, cos_image_url, search_query, visual_description,
                    visual_description_source, is_synthetic_benchmark, retrieval_text,
                    embedding_halfvec_blob, embedding_halfvec_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    s["sku_id"],
                    s["category"],
                    s["sub_category"],
                    s["title"],
                    s["brand"],
                    float(s["original_price"]),
                    float(s["final_price"]),
                    int(s.get("inventory", 100)),
                    s.get("status", "active"),
                    json.dumps(s["specs"], ensure_ascii=False, sort_keys=True),
                    json.dumps(s["highlights"], ensure_ascii=False),
                    s["image_path"],
                    s["cos_image_url"],
                    s["search_query"],
                    s["visual_description"],
                    s["visual_description_source"],
                    1 if s.get("is_synthetic_benchmark", True) else 0,
                    s["retrieval_text"],
                    fp16_blob,
                    json.dumps(fp16_vals),
                ),
            )

        # 2. 插入 3 条平台规则
        for r in rules:
            fp16_vals, fp16_blob = quantize_to_fp16_list(rule_vecs[r["rule_id"]])
            cur.execute(
                """
                INSERT INTO platform_rules (
                    rule_id, rule_version, rule_type, title, content,
                    max_days, window_hours, anchor_field, fallback_anchor_field,
                    include_boundary, allow_unsigned, require_unopened,
                    depends_on_rule_id, source_reference,
                    embedding_halfvec_blob, embedding_halfvec_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    r["rule_id"],
                    r["rule_version"],
                    r["rule_type"],
                    r["title"],
                    r["content"],
                    int(r["max_days"]),
                    int(r["window_hours"]),
                    r["anchor_field"],
                    r.get("fallback_anchor_field"),
                    1 if r.get("include_boundary", True) else 0,
                    1 if r.get("allow_unsigned", False) else 0,
                    1 if r.get("require_unopened", False) else 0,
                    r.get("depends_on_rule_id"),
                    json.dumps(r["source_reference"], ensure_ascii=False, sort_keys=True),
                    fp16_blob,
                    json.dumps(fp16_vals),
                ),
            )

        # 3. 插入 10 条模拟订单 (受 PRAGMA foreign_keys = ON 保护)
        for o in orders:
            cur.execute(
                """
                INSERT INTO mock_orders (
                    order_id, scenario_tag, user_id, sku_id, sku_title, pay_amount, order_status,
                    is_opened, has_price_guarantee, has_freight_insurance, rule_version,
                    logistics_company, tracking_number, latest_logistics_event,
                    reference_time, created_offset_hours, paid_offset_hours, signed_offset_hours,
                    created_at, paid_at, signed_at, expected_eligibility
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    o["order_id"],
                    o["scenario_tag"],
                    o["user_id"],
                    o["sku_id"],
                    o["sku_title"],
                    float(o["pay_amount"]),
                    o["order_status"],
                    1 if o.get("is_opened", False) else 0,
                    1 if o.get("has_price_guarantee", True) else 0,
                    1 if o.get("has_freight_insurance", True) else 0,
                    o["rule_version"],
                    o.get("logistics_company"),
                    o.get("tracking_number"),
                    o.get("latest_logistics_event"),
                    o["reference_time"],
                    float(o["created_offset_hours"]),
                    float(o["paid_offset_hours"]),
                    float(o["signed_offset_hours"]) if o.get("signed_offset_hours") is not None else None,
                    o["created_at"],
                    o["paid_at"],
                    o.get("signed_at"),
                    json.dumps(o["expected_eligibility"], ensure_ascii=False, sort_keys=True),
                ),
            )

        conn.commit()
    finally:
        conn.close()


def try_seed_postgres(env: Dict[str, str]) -> Tuple[bool, str]:
    """探测远程 PostgreSQL 是否可达；若可达则尝试执行 init.sql 与 seed_60skus_full.sql。"""
    pg_host = env.get("POSTGRES_HOST", "127.0.0.1").strip()
    pg_port = int(env.get("POSTGRES_PORT", "5432").strip())
    pg_user = env.get("POSTGRES_USER", "postgres").strip()
    pg_pass = env.get("POSTGRES_PASSWORD", "").strip()
    pg_db = env.get("POSTGRES_DB", "ai_sales_assistant").strip()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2.0)
    try:
        sock.connect((pg_host, pg_port))
    except Exception as exc:
        return (
            False,
            f"PostgreSQL 节点 ({pg_host}:{pg_port}) 当前未开机/不可达 ({exc})；"
            f"已生成完整 DDL/Seed SQL 并完成本地 SQLite halfvec(1024) 镜像库灌库。",
        )
    finally:
        sock.close()

    # 若端口可达，优先尝试 psycopg / psycopg2
    for drv_name in ("psycopg", "psycopg2"):
        try:
            drv = __import__(drv_name)
            conn = drv.connect(
                host=pg_host,
                port=pg_port,
                user=pg_user,
                password=pg_pass,
                dbname=pg_db,
                connect_timeout=5,
            )
            try:
                with conn.cursor() as cur:
                    cur.execute(INIT_SQL_PATH.read_text(encoding="utf-8"))
                    cur.execute(FULL_SEED_SQL_PATH.read_text(encoding="utf-8"))
                conn.commit()
                return (
                    True,
                    f"已通过 {drv_name} 成功将 60 SKU + 3 规则 + 10 订单灌入 PostgreSQL ({pg_host}:{pg_port}/{pg_db})",
                )
            finally:
                conn.close()
        except ImportError:
            continue
        except Exception as exc:
            return False, f"PostgreSQL ({pg_host}:{pg_port}) 连接或执行异常: {exc}"

    return (
        False,
        f"PostgreSQL 端口 ({pg_host}:{pg_port}) 可达，但当前 Python 环境未安装 psycopg/psycopg2；"
        f"可在虚机或容器内执行 psql -f deploy/init.sql -f deploy/seed_60skus_full.sql。",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 PostgreSQL pgvector DDL/Seed 并灌入数据库")
    parser.add_argument("--skip-gate", action="store_true", help="跳过 validate_catalog.py 预检")
    args = parser.parse_args()

    t0 = time.perf_counter()
    if not args.skip_gate:
        import validate_catalog

        passed, passed_checks, errors = validate_catalog.validate_all()
        if not passed:
            print("[ERROR] validate_catalog.py 自洽性闸门未通过，中止灌库：", file=sys.stderr)
            for err in errors:
                print(f"  - {err}", file=sys.stderr)
            return 1
        print(f"[OK] Pre-seed Gate: validate_catalog.py {len(passed_checks)} 项自洽性断言全部通过。")

    if not CATALOG_PATH.exists() or not RULES_PATH.exists() or not ORDERS_PATH.exists():
        print("[ERROR] 缺少核心数据 JSON 文件", file=sys.stderr)
        return 1
    if not MANIFEST_PATH.exists():
        print(f"[ERROR] 缺少图片清单 {MANIFEST_PATH}，请先运行 scripts/fetch_and_upload_images.py", file=sys.stderr)
        return 1
    if not EMBEDDINGS_PATH.exists():
        print(f"[ERROR] 缺少向量缓存 {EMBEDDINGS_PATH}，请先运行 scripts/build_embeddings.py", file=sys.stderr)
        return 1

    catalog: List[Dict[str, Any]] = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    rules: List[Dict[str, Any]] = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    orders: List[Dict[str, Any]] = json.loads(ORDERS_PATH.read_text(encoding="utf-8"))
    emb_cache: Dict[str, Any] = json.loads(EMBEDDINGS_PATH.read_text(encoding="utf-8"))

    sku_items = emb_cache.get("sku_embeddings", {})
    rule_items = emb_cache.get("rule_embeddings", {})

    sku_vecs: Dict[str, List[float]] = {}
    for s in catalog:
        sid = s["sku_id"]
        if sid not in sku_items:
            print(f"[ERROR] embeddings_cache.json 缺少 {sid} 的向量", file=sys.stderr)
            return 1
        expected_hash = hashlib.sha256(s["retrieval_text"].encode("utf-8")).hexdigest()
        if sku_items[sid].get("text_sha256") != expected_hash:
            print(
                f"[ERROR] {sid} 的 retrieval_text SHA-256 与 embeddings_cache.json 不一致，请重跑 build_embeddings.py",
                file=sys.stderr,
            )
            return 1
        sku_vecs[sid] = sku_items[sid]["embedding"]

    rule_vecs: Dict[str, List[float]] = {}
    for r in rules:
        rid = r["rule_id"]
        if rid not in rule_items:
            print(f"[ERROR] embeddings_cache.json 缺少规则 {rid} 的向量", file=sys.stderr)
            return 1
        rule_text = f"【规则】{r['title']} ({r['rule_version']})：{r['content']}"
        expected_hash = hashlib.sha256(rule_text.encode("utf-8")).hexdigest()
        if rule_items[rid].get("text_sha256") != expected_hash:
            print(
                f"[ERROR] 规则 {rid} 的文本 SHA-256 与 embeddings_cache.json 不一致，请重跑 build_embeddings.py",
                file=sys.stderr,
            )
            return 1
        rule_vecs[rid] = rule_items[rid]["embedding"]

    # 生成 deploy/init.sql 与 deploy/seed_60skus_full.sql
    generate_sql_files(catalog, rules, orders, sku_vecs, rule_vecs)
    print(f"[OK] 已生成 PostgreSQL DDL 与核心种子脚本 -> {INIT_SQL_PATH}")
    print(f"[OK] 已生成全量 60 SKU + 3 规则 + 10 订单幂等 SQL 脚本 -> {FULL_SEED_SQL_PATH}")

    # 灌入本地 SQLite 完整镜像库
    seed_sqlite_local_store(catalog, rules, orders, sku_vecs, rule_vecs)
    print(f"[OK] 已完成本地 SQLite halfvec(1024) 镜像库灌库 -> {SQLITE_DB_PATH}")

    # 尝试灌入远程 PostgreSQL
    env = load_env()
    pg_ok, pg_msg = try_seed_postgres(env)
    status_tag = "[OK]" if pg_ok else "[INFO]"
    print(f"{status_tag} {pg_msg}")

    elapsed = time.perf_counter() - t0
    print(f"[OK] 数据库构建与灌库流程完成 (耗时={elapsed:.2f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
