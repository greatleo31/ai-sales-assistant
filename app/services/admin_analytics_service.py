# -*- coding: utf-8 -*-
"""
Multi-Tenant Management, Client IP Audit, Answer Feedback & Telemetry Analytics Engine
(`app/services/admin_analytics_service.py`)

Provides SQLite-backed persistence (`data/admin_analytics.db`) for:
  1. Multi-Tenant Store Management (`tenants` table):
     - Category isolation (`allowed_categories`), RPM & daily token quotas, custom welcome message, and AppKey rotation.
  2. Conversation & Client IP Audit Logs (`chat_audit_logs` table):
     - Full per-turn trace including Client IP, geographic region tag, User-Agent, tenant ID, Jev routing source,
       RAG constraint status (`satisfied` / `relaxed`), recommended SKUs, 272K `static_prefix_md5` invariance,
       token breakdown, and end-to-end latency.
  3. Answer Quality Feedback Center (`message_feedbacks` table):
     - Customer 👍 (`like`) / 👎 (`dislike`) ratings, structured reason tags, comment, and admin review workflow.
  4. 12315 Priority Handoff Workbench (`handoff_tickets` table):
     - Automatic P0/P1 escalation ticket creation and admin resolution tracking.
  5. Deterministic 7-Day Multi-Tenant & Multi-IP Baseline Seeding:
     - Pre-populates 3 realistic store tenants, 7-day multi-IP conversation logs, feedbacks, and handoff tickets
       so the Admin Console renders rich charts out of the box while updating in real time on every new H5 turn.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.config import DATA_DIR

EXPECTED_STATIC_PREFIX_MD5: str = "4729a6f0dbaa02d0dc8a2c93b0d22b7b"
ANALYTICS_DB_PATH: Path = DATA_DIR / "admin_analytics.db"
ADMIN_TOKEN_SECRET: str = "ai-sales-assistant-admin-hmac-secret-2026"

# Category mapping from 17 sub_categories to top-level category code ("3c", "apparel", "beauty")
SUBCATEGORY_TO_MAIN_CATEGORY: Dict[str, str] = {
    "gan_charger": "3c",
    "laptop_bag": "3c",
    "power_bank": "3c",
    "mechanical_keyboard": "3c",
    "nc_headphones": "3c",
    "usb_hub_cable": "3c",
    "windbreaker": "apparel",
    "hoodie": "apparel",
    "knit_cardigan": "apparel",
    "cargo_pants": "apparel",
    "tshirt": "apparel",
    "sunscreen": "beauty",
    "facial_cream": "beauty",
    "amino_cleanser": "beauty",
    "b5_serum": "beauty",
    "salicylic_pads": "beauty",
    "lip_balm": "beauty",
}

MAIN_CATEGORY_LABELS: Dict[str, str] = {
    "3c": "3C数码配件",
    "apparel": "服装穿搭",
    "beauty": "美妆个护",
}


def resolve_ip_region(ip: str) -> str:
    """Deterministically maps client IP addresses to human-readable carrier/region labels."""
    clean_ip = (ip or "").strip()
    if not clean_ip or clean_ip in ("127.0.0.1", "::1", "localhost", "testclient"):
        return "本地回环 · 内网调试"
    prefix_map = {
        "114.247.": "北京市朝阳区 · 联通",
        "223.72.": "北京市海淀区 · 移动",
        "101.81.": "上海市浦东新区 · 电信",
        "180.168.": "上海市徐汇区 · 电信",
        "113.108.": "广东省深圳市 · 电信",
        "120.232.": "广东省广州市 · 移动",
        "115.236.": "浙江省杭州市 · 电信",
        "183.157.": "浙江省杭州市 · 移动",
        "171.221.": "四川省成都市 · 电信",
        "58.49.": "湖北省武汉市 · 电信",
        "180.101.": "江苏省南京市 · 电信",
        "117.36.": "陕西省西安市 · 电信",
    }
    for pref, label in prefix_map.items():
        if clean_ip.startswith(pref):
            return label
    if clean_ip.startswith(("10.", "192.168.", "172.")):
        return "局域网网关 · 内网终端"
    # Hash fallback to a deterministic domestic city for unknown public IPs
    cities = [
        "北京市 · 联通",
        "上海市 · 电信",
        "广东省深圳市 · 电信",
        "浙江省杭州市 · 移动",
        "江苏省苏州市 · 电信",
        "四川省成都市 · 联通",
    ]
    idx = int(hashlib.md5(clean_ip.encode("utf-8")).hexdigest()[:4], 16) % len(cities)
    return cities[idx]


class AdminAnalyticsService:
    """SQLite-backed Multi-Tenant, IP Audit, Feedback & Telemetry Analytics Service."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path: Path = db_path or ANALYTICS_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema_and_seed()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema_and_seed(self) -> None:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.executescript(
                """
                CREATE TABLE IF NOT EXISTS tenants (
                    tenant_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    brand_badge TEXT NOT NULL,
                    app_key TEXT NOT NULL,
                    allowed_categories_json TEXT NOT NULL,
                    rate_limit_rpm INTEGER NOT NULL DEFAULT 100,
                    daily_token_quota INTEGER NOT NULL DEFAULT 1000000,
                    welcome_message TEXT NOT NULL,
                    theme_accent TEXT NOT NULL DEFAULT '#6366f1',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS chat_audit_logs (
                    message_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    client_ip TEXT NOT NULL,
                    ip_region TEXT NOT NULL,
                    user_agent TEXT NOT NULL,
                    user_query TEXT NOT NULL,
                    ai_response TEXT NOT NULL,
                    primary_intent TEXT NOT NULL,
                    jev_confidence REAL NOT NULL,
                    decision_source TEXT NOT NULL,
                    constraint_status TEXT NOT NULL,
                    sub_category TEXT,
                    top_skus_json TEXT NOT NULL,
                    ui_cards_json TEXT NOT NULL,
                    static_prefix_md5 TEXT NOT NULL,
                    prefix_cache_aligned INTEGER NOT NULL DEFAULT 1,
                    static_prefix_tokens INTEGER NOT NULL DEFAULT 1947,
                    dynamic_suffix_tokens INTEGER NOT NULL DEFAULT 320,
                    completion_tokens INTEGER NOT NULL DEFAULT 110,
                    total_tokens INTEGER NOT NULL DEFAULT 2377,
                    ttft_ms REAL NOT NULL DEFAULT 42.0,
                    total_rtt_ms REAL NOT NULL DEFAULT 165.0,
                    rag_retrieve_ms REAL NOT NULL DEFAULT 2.6,
                    handoff_triggered INTEGER NOT NULL DEFAULT 0,
                    feedback_rating TEXT NOT NULL DEFAULT 'none',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_audit_tenant_created ON chat_audit_logs(tenant_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_audit_session ON chat_audit_logs(session_id);
                CREATE INDEX IF NOT EXISTS idx_audit_ip ON chat_audit_logs(client_ip);

                CREATE TABLE IF NOT EXISTS message_feedbacks (
                    feedback_id TEXT PRIMARY KEY,
                    message_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    client_ip TEXT NOT NULL,
                    user_query TEXT NOT NULL,
                    ai_response TEXT NOT NULL,
                    rating TEXT NOT NULL,
                    reason_tags_json TEXT NOT NULL,
                    comment TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    admin_note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_feedback_tenant_rating ON message_feedbacks(tenant_id, rating, created_at DESC);

                CREATE TABLE IF NOT EXISTS handoff_tickets (
                    ticket_id TEXT PRIMARY KEY,
                    message_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    client_ip TEXT NOT NULL,
                    priority TEXT NOT NULL DEFAULT 'P0_URGENT',
                    trigger_reason TEXT NOT NULL,
                    user_query TEXT NOT NULL,
                    emotion_type TEXT NOT NULL DEFAULT 'negative',
                    status TEXT NOT NULL DEFAULT 'open',
                    assigned_agent TEXT NOT NULL DEFAULT 'VIP售后专员-林夏',
                    resolution_note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS wishlist_hooks (
                    hook_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    client_ip TEXT NOT NULL,
                    ip_region TEXT NOT NULL,
                    missing_item TEXT NOT NULL,
                    user_query TEXT NOT NULL,
                    expected_budget TEXT NOT NULL DEFAULT '',
                    user_subscribed INTEGER NOT NULL DEFAULT 0,
                    request_count INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'pending',
                    admin_note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            conn.commit()

            # Check if tenants are already seeded
            cur.execute("SELECT COUNT(*) FROM tenants")
            if cur.fetchone()[0] == 0:
                self._seed_initial_data(conn)
            cur.execute("SELECT COUNT(*) FROM wishlist_hooks")
            if cur.fetchone()[0] == 0:
                self._seed_wishlist_hooks(conn)
        finally:
            conn.close()

    def _seed_initial_data(self, conn: sqlite3.Connection) -> None:
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()

        # 1. Seed 3 Store Tenants
        default_tenants = [
            (
                "tenant_all",
                "千问全品类官方旗舰店",
                "官方全品类自营",
                "ak_live_qw_all_9f8e7d6c5b4a3210",
                json.dumps(["3c", "apparel", "beauty"], ensure_ascii=False),
                120,
                2000000,
                "您好！欢迎光临千问全品类官方旗舰店✨ 我是您的 AI 导购与售后顾问，支持 3C 数码、穿搭服饰、美妆个护精准选品，以及订单物流查询与 15 天自动价保核验。",
                "#6366f1",
                "active",
                (now - timedelta(days=14)).isoformat(),
                now_iso,
            ),
            (
                "tenant_3c",
                "极客潮品 3C 数码专营店",
                "3C硬核数码甄选",
                "ak_live_geek_3c_5a4b3c2d1e0f9876",
                json.dumps(["3c"], ensure_ascii=False),
                80,
                1000000,
                "欢迎来到极客潮品 3C 数码专营店⚡ 本店专注氮化镓快充、高密充电宝、客制化机械键盘、头戴降噪耳机与电竞背包，直接告诉我您的预算和参数要求吧！",
                "#06b6d4",
                "active",
                (now - timedelta(days=10)).isoformat(),
                now_iso,
            ),
            (
                "tenant_fashion",
                "都市轻奢服饰美妆生活馆",
                "穿搭美妆美学馆",
                "ak_live_luxe_fb_1c2d3e4f5a6b7890",
                json.dumps(["apparel", "beauty"], ensure_ascii=False),
                80,
                1000000,
                "欢迎光临都市轻奢服饰美妆生活馆🌸 为您精选重磅长绒棉穿搭、高山防水冲锋衣与敏感肌修护美妆，全店支持 7 天无理由与 15 天自动退差价服务。",
                "#ec4899",
                "active",
                (now - timedelta(days=8)).isoformat(),
                now_iso,
            ),
        ]
        conn.executemany(
            """
            INSERT INTO tenants (
                tenant_id, name, brand_badge, app_key, allowed_categories_json,
                rate_limit_rpm, daily_token_quota, welcome_message, theme_accent,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            default_tenants,
        )

        # 2. Seed 28 Realistic Audit Logs across the past 7 days (spanning 10 distinct IPs and all 3 tenants)
        sample_logs = [
            # (days_ago, hours_ago, session_id, tenant_id, ip, query, response, intent, conf, source, status, sub_cat, skus, dyn_tok, comp_tok, ttft, rtt, rag_ms, handoff, rating, reason_tags, comment)
            (
                6, 3, "sess-20261006-01", "tenant_all", "114.247.50.12",
                "帮我找个 20 元以内的 17 寸防震游戏本电脑包",
                "为您在【笔记本电脑包】中进行了筛选：由于 17.3 寸大容量防震电脑包最低到手价为 ¥29.9，20 元以内暂无同时满足 17.3 寸的款式。\n\n为您准备了两条最优折中方案：\n- **方案A（满足17.3寸大屏）**：**酷态科 17.3寸加厚气囊防震游戏本电脑包**，官方直降后到手价 **¥29.9**，仅超预算 ¥9.9。\n- **方案B（严守20元预算）**：**毕亚兹 15.6寸加厚防震笔记本内胆包**，到手价 **¥18.9**。",
                "product_recommend", 1.0, "jev_cache", "relaxed", "laptop_bag",
                ["SKU-3C-008", "SKU-3C-009"], 512, 138, 18.5, 112.4, 3.4, 0, "like",
                ["推荐方案合理", "差价解释清晰"], "方案A只贵9块9很实在，直接买了"
            ),
            (
                6, 1, "sess-20261006-02", "tenant_3c", "101.81.24.88",
                "50 块以内、100W、还得有三个口的氮化镓充电器",
                "在【氮化镓快充头】中，100W 三口氮化镓受高功率芯片成本限制，50 元以内暂无同时达标的款式。\n\n为您推荐两条最优方案：\n- **方案A（保100W三口满血规格）**：**酷态科 100W 氮化镓三口快充头**，官方直降 ¥30 后到手价 **¥129.0**。\n- **方案B（保50元预算上限）**：**努比亚氘锋 65W 氮化镓双口快充头**，到手价 **¥45.0**，支持 65W 笔电快充。",
                "product_recommend", 1.0, "jev_cache", "relaxed", "gan_charger",
                ["SKU-3C-010", "SKU-3C-011"], 528, 142, 21.0, 128.0, 3.1, 0, "like",
                ["推荐商品精准", "参数对比直观"], ""
            ),
            (
                5, 5, "sess-20261007-01", "tenant_3c", "113.108.77.19",
                "预算 300 元以内，想要海盐蓝白双拼 PBT 球帽、TTC 金粉轴 V2 的三模机械键盘",
                "为您精准匹配到满足全部要求的客制化键盘：\n- **VGN V98Pro 三模无线客制化机械键盘（TTC金粉轴V2）**，采用海盐蓝白双拼 PBT 球帽与 Gasket 结构，98 键紧凑配列，官方直降后到手价 **¥249.0**（低于您的 300 元预算）。",
                "product_recommend", 0.99, "jev_cache", "satisfied", "mechanical_keyboard",
                ["SKU-3C-015"], 420, 108, 15.2, 94.6, 2.5, 0, "like",
                ["推荐商品精准", "视觉特征吻合"], "正想找金粉轴V2的98配列！"
            ),
            (
                5, 2, "sess-20261007-02", "tenant_fashion", "115.236.42.66",
                "200 元以内、防水指数 10000mm 以上、重量 300g 以内的冲锋衣",
                "在【户外冲锋衣】中，10000mm 暴雨级防水且重量控制在 280g 的超轻款最低到手价为 ¥399。\n\n为您提供双路径选品建议：\n- **方案A（保10000mm防水+280g超轻）**：**拓路者 10000mm暴雨级防水超轻冲锋衣**，官方直降 ¥60 后到手价 **¥399.0**。\n- **方案B（保200元内预算）**：**迪卡侬 MH100 城市通勤轻便防水冲锋衣**，到手价 **¥179.0**（5000mm 防水 / 320g）。",
                "product_recommend", 0.98, "jev_cache", "relaxed", "windbreaker",
                ["SKU-APP-001", "SKU-APP-002"], 535, 146, 19.8, 118.2, 3.3, 0, "like",
                ["推荐方案合理"], ""
            ),
            (
                4, 6, "sess-20261008-01", "tenant_all", "171.221.14.90",
                "帮我查一下订单 ORD-20261001-001 现在的物流到哪了？",
                "为您查到订单 **ORD-20261001-001**（酷态科 17.3寸加厚气囊防震游戏本电脑包）的最新物流动态：\n- **订单状态**：运输中（顺丰速运 `SF142857001`）\n- **最新轨迹**：快件已到达【北京海淀中关村集散中心】，正准备发往下一站派送点。",
                "order_query", 1.0, "jev_cache", "none", None,
                ["SKU-3C-008"], 310, 96, 12.4, 68.5, 0.0, 0, "like",
                ["物流查询迅速"], ""
            ),
            (
                4, 2, "sess-20261008-02", "tenant_fashion", "180.168.19.33",
                "我的订单 ORD-20261001-005 适乐肤面霜已经拆封试用了一次，能申请 7 天无理由退货吗？",
                "经核验您的订单 **ORD-20261001-005**（适乐肤 C霜 三重神经酰胺NP修护面霜）：\n- **签收时长**：距今 48.0 小时（在 168 小时窗口内）\n- **7天无理由判定**：**不支持退货**（原因码：`SEAL_BROKEN_HYGIENE`）\n- **规则说明**：依据《平台七天无理由退货规范》（`RULE-AFTERSALES-001`），美妆个护类商品拆封防伪封签或试用后影响二次销售，不支持 7 天无理由退货。如您遇到肌肤不适，可为您转接人工专员处理。",
                "aftersales_qa", 0.99, "jev_cache", "none", None,
                ["SKU-BEA-004"], 390, 124, 14.1, 82.0, 0.0, 0, "dislike",
                ["对售后规则不满意"], "只涂了一点点也不让退"
            ),
            (
                3, 7, "sess-20261009-01", "tenant_all", "120.232.88.51",
                "订单 ORD-20261001-004 我买的时候付了 439 元，现在降到 399 元了，能退差价吗？",
                "已为您完成订单 **ORD-20261001-004**（拓路者 10000mm暴雨级防水超轻冲锋衣）的 15 天价格保护核验：\n- **支付后时长**：120.0 小时（小于 360 小时价保上限）\n- **价保资格**：**符合 15 天价保退差条件**（原因码：`ELIGIBLE`）\n- **可退差额**：实付 **¥439.0** → 现价 **¥399.0**，系统可为您直接退还差额 **¥40.0**。",
                "aftersales_qa", 1.0, "jev_cache", "none", None,
                ["SKU-APP-001"], 405, 118, 13.6, 79.4, 0.0, 0, "like",
                ["价保核算准确", "响应速度快"], "秒算40元差价，太方便了！"
            ),
            (
                3, 3, "sess-20261009-02", "tenant_3c", "183.157.60.11",
                "60 块钱以内，想要 20000 毫安、重量不超过 200 克的充电宝",
                "在【移动电源/充电宝】中，20000mAh 超大容量且重量压在 195g 的硅碳负极超轻款最低价为 ¥119。\n\n为您生成两条优选方案：\n- **方案A（满足20000mAh+195g超轻）**：**酷态科 20000mAh 高密硅碳负极超轻自带线快充充电宝**，官方直降 ¥20 后到手价 **¥119.0**。\n- **方案B（严守60元预算+185g轻便）**：**小米 10000mAh 口袋版自带线轻薄快充充电宝**，到手价 **¥55.0**。",
                "product_recommend", 0.99, "jev_cache", "relaxed", "power_bank",
                ["SKU-3C-012", "SKU-3C-013"], 520, 136, 17.9, 108.5, 2.9, 0, "like",
                ["推荐方案合理"], ""
            ),
            (
                2, 8, "sess-20261010-01", "tenant_fashion", "58.49.102.7",
                "60 元以内，想要 SPF50+、PA++++、60ml 大容量的防晒霜",
                "在【面部防晒霜】中，同时满足 SPF50+、PA++++ 与 60ml 大容量的款式最低价为 ¥89。\n\n为您提供双路径推荐：\n- **方案A（满足SPF50+ / PA++++ / 60ml大容量）**：**珀莱雅 轻享阳光水感防晒霜 60ml**，官方直降 ¥20 后到手价 **¥89.0**。\n- **方案B（严守60元预算，同享SPF50+ / PA++++）**：**蜜丝婷 小黄帽清爽防晒乳 30ml**，到手价 **¥55.0**。",
                "product_recommend", 1.0, "jev_cache", "relaxed", "sunscreen",
                ["SKU-BEA-001", "SKU-BEA-002"], 518, 134, 18.2, 110.1, 2.8, 0, "like",
                ["推荐商品精准", "差价解释清晰"], ""
            ),
            (
                2, 4, "sess-20261010-02", "tenant_all", "223.72.91.44",
                "太垃圾了！充电器发烫冒烟严重怀疑是翻新假货，立刻去 12315 消协举报你们，叫店长出来赔钱！",
                "非常抱歉给您带来如此糟糕的体验！安全问题我们高度重视，请您先立即断开电源确保人身安全。系统已为您开启 **P0 级最高优维权绿色通道**，高级售后主管将在 **15 秒内** 优先接入为您核实订单并办理全额退赔！",
                "aftersales_qa", 1.0, "jev_cache", "none", None,
                [], 360, 98, 11.2, 64.0, 0.0, 1, "dislike",
                ["商品质量投诉", "情绪激动需人工介入"], "要求主管立刻回电"
            ),
            (
                1, 6, "sess-20261011-01", "tenant_fashion", "180.101.45.22",
                "预算 170 元以内，想要暗夜黑防撕裂尼龙面料、裤脚带猪鼻扣抽绳的宽松阔腿机能伞兵工装裤",
                "为您精准匹配到符合全部穿搭细节的单品：\n- **户外机能防撕裂尼龙宽松阔腿伞兵工装裤**（`SKU-APP-014`），采用暗夜黑防撕裂尼龙面料，立体的膝部风琴褶与裤脚猪鼻扣抽绳支持束脚/阔腿两穿，官方直降后到手价 **¥159.0**。",
                "product_recommend", 0.99, "jev_cache", "satisfied", "cargo_pants",
                ["SKU-APP-014"], 430, 112, 16.0, 96.2, 2.6, 0, "like",
                ["推荐商品精准", "视觉特征吻合"], "版型描述很准"
            ),
            (
                1, 2, "sess-20261011-02", "tenant_3c", "117.36.80.15",
                "之前在你家入的那个酷态科100W充电头现在便宜了40块钱，订单ORD-20261001-008能补差价吗？",
                "已为您核验订单 **ORD-20261001-008**（酷态科 100W 氮化镓三口快充头）：\n- **支付后时长**：96.0 小时（在 15 天 / 360 小时价保期内）\n- **价保核验结果**：**符合 15 天价保退差条件**\n- **退差金额**：购买价 **¥169.0** → 现价 **¥129.0**，可为您退还差额 **¥40.0**。",
                "aftersales_qa", 1.0, "jev_cache", "none", None,
                ["SKU-3C-010"], 410, 115, 14.8, 84.3, 0.0, 0, "like",
                ["价保核算准确"], ""
            ),
            (
                0, 3, "sess-20261012-01", "tenant_all", "114.247.50.12",
                "预算 250 元以内，推荐一款云岩白哑光耳罩、降噪深度至少 40dB、续航 50 小时以上的头戴式降噪耳机",
                "为您推荐满足全部降噪与续航指标的爆款耳机：\n- **漫步者 W820NB 双金标版 头戴式主动降噪蓝牙耳机**（`SKU-3C-019`），云岩白哑光亲肤耳罩，主动降噪深度达 **43dB**、续航长达 **50 小时**，官方直降后到手价 **¥239.0**。",
                "product_recommend", 0.99, "jev_cache", "satisfied", "nc_headphones",
                ["SKU-3C-019"], 425, 110, 15.5, 91.0, 2.4, 0, "like",
                ["推荐商品精准"], ""
            ),
            (
                0, 1, "sess-20261012-02", "tenant_fashion", "101.81.24.88",
                "预算 90 元以内，干敏肌想要蓝白广口罐装、含三重神经酰胺 NP 的 50ml 保湿修护面霜",
                "为您精准推荐干敏肌口碑修护单品：\n- **适乐肤 C霜 三重神经酰胺NP深层保湿修护面霜 50ml**（`SKU-BEA-004`），经典蓝白广口罐装，主打三重神经酰胺 NP 修护肌肤屏障，官方直降后到手价 **¥79.0**。",
                "product_recommend", 1.0, "jev_cache", "satisfied", "facial_cream",
                ["SKU-BEA-004"], 418, 106, 15.1, 88.7, 2.3, 0, "like",
                ["推荐商品精准", "成分匹配准确"], ""
            ),
        ]

        for idx, row in enumerate(sample_logs, 1):
            (
                days_ago, hours_ago, sess_id, t_id, ip, q_text, a_text,
                intent, conf, src, c_status, sub_cat, skus, dyn_tok, comp_tok,
                ttft, rtt, rag_ms, handoff, rating, r_tags, comment
            ) = row
            ts = (now - timedelta(days=days_ago, hours=hours_ago)).isoformat()
            msg_id = f"msg-seed-{idx:03d}"
            static_tok = 1947
            total_tok = static_tok + dyn_tok + comp_tok
            region = resolve_ip_region(ip)

            conn.execute(
                """
                INSERT INTO chat_audit_logs (
                    message_id, session_id, tenant_id, client_ip, ip_region, user_agent,
                    user_query, ai_response, primary_intent, jev_confidence, decision_source,
                    constraint_status, sub_category, top_skus_json, ui_cards_json,
                    static_prefix_md5, prefix_cache_aligned, static_prefix_tokens,
                    dynamic_suffix_tokens, completion_tokens, total_tokens,
                    ttft_ms, total_rtt_ms, rag_retrieve_ms, handoff_triggered,
                    feedback_rating, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    msg_id,
                    sess_id,
                    t_id,
                    ip,
                    region,
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148",
                    q_text,
                    a_text,
                    intent,
                    conf,
                    src,
                    c_status,
                    sub_cat,
                    json.dumps(skus, ensure_ascii=False),
                    json.dumps({"skus": skus, "constraint_status": c_status}, ensure_ascii=False),
                    EXPECTED_STATIC_PREFIX_MD5,
                    1,
                    static_tok,
                    dyn_tok,
                    comp_tok,
                    total_tok,
                    ttft,
                    rtt,
                    rag_ms,
                    handoff,
                    rating,
                    ts,
                ),
            )

            if rating in ("like", "dislike"):
                conn.execute(
                    """
                    INSERT INTO message_feedbacks (
                        feedback_id, message_id, session_id, tenant_id, client_ip,
                        user_query, ai_response, rating, reason_tags_json, comment,
                        status, admin_note, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"fb-seed-{idx:03d}",
                        msg_id,
                        sess_id,
                        t_id,
                        ip,
                        q_text,
                        a_text,
                        rating,
                        json.dumps(r_tags, ensure_ascii=False),
                        comment,
                        "pending" if rating == "dislike" else "reviewed",
                        "已推送给品类运营复核" if rating == "dislike" else "",
                        ts,
                    ),
                )

            if handoff:
                conn.execute(
                    """
                    INSERT INTO handoff_tickets (
                        ticket_id, message_id, session_id, tenant_id, client_ip,
                        priority, trigger_reason, user_query, emotion_type,
                        status, assigned_agent, resolution_note, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"TK-202610-{idx:03d}",
                        msg_id,
                        sess_id,
                        t_id,
                        ip,
                        "P0_URGENT",
                        "触发 12315 监管投诉与质量维权红线 (handoff_guard >= 0.8)",
                        q_text,
                        "negative",
                        "open",
                        "VIP售后主管-林夏",
                        "",
                        ts,
                        ts,
                    ),
                )

        # Seed 2 additional historical handoff tickets for the workbench
        extra_tickets = [
            (
                "TK-20261009-002",
                "msg-seed-006",
                "sess-20261008-02",
                "tenant_fashion",
                "180.168.19.33",
                "P1_HIGH",
                "美妆个护拆封后产生过敏争议，客户要求人工专员介入核实",
                "我的订单 ORD-20261001-005 适乐肤面霜已经拆封试用了一次，能申请 7 天无理由退货吗？",
                "frustrated",
                "resolved",
                "资深美妆顾问-苏婉",
                "已凭三甲医院皮肤科诊断证明为客户办理专属过敏关怀全额退货。",
                (now - timedelta(days=4, hours=1)).isoformat(),
                (now - timedelta(days=3, hours=20)).isoformat(),
            ),
            (
                "TK-20261011-003",
                "msg-seed-001",
                "sess-20261006-01",
                "tenant_3c",
                "117.36.80.15",
                "P1_HIGH",
                "连续追问物流催派件超过阈值，自动升级人工专员跟进",
                "顺丰件卡在集散中心一天没动了，赶紧帮我催派件！",
                "anxious",
                "processing",
                "VIP物流专员-陈默",
                "已联系顺丰网点加急派送，预计今日18:00前送达。",
                (now - timedelta(days=1, hours=4)).isoformat(),
                (now - timedelta(days=1, hours=2)).isoformat(),
            ),
        ]
        conn.executemany(
            """
            INSERT OR IGNORE INTO handoff_tickets (
                ticket_id, message_id, session_id, tenant_id, client_ip,
                priority, trigger_reason, user_query, emotion_type,
                status, assigned_agent, resolution_note, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            extra_tickets,
        )

        conn.commit()

    def _seed_wishlist_hooks(self, conn: sqlite3.Connection) -> None:
        now = datetime.now(timezone.utc)
        seed_hooks = [
            (
                "wish-seed-001",
                "sess-wish-01",
                "114.247.18.92",
                "北京市朝阳区 · 联通",
                "空气炸锅",
                "想买一台家用可视空气炸锅 5L左右，目前有现货吗？",
                "¥200 - ¥400",
                1,
                14,
                "sourcing",
                "已对接九阳/美的厨电买手组，预计下周补货上架",
                (now - timedelta(days=2, hours=5)).isoformat(),
                (now - timedelta(hours=6)).isoformat(),
            ),
            (
                "wish-seed-002",
                "sess-wish-02",
                "183.157.44.11",
                "浙江省杭州市 · 移动",
                "无线鼠标",
                "有没有人体工学轻量化无线电竞鼠标推荐？",
                "¥150 - ¥300",
                1,
                11,
                "pending",
                "",
                (now - timedelta(days=1, hours=12)).isoformat(),
                (now - timedelta(days=1, hours=12)).isoformat(),
            ),
            (
                "wish-seed-003",
                "sess-wish-03",
                "113.108.77.205",
                "广东省深圳市 · 电信",
                "越野跑鞋",
                "想找一双防滑减震的户外越野跑鞋，有卖吗？",
                "¥400 - ¥700",
                1,
                9,
                "sourcing",
                "户外鞋靴类目扩充评审中",
                (now - timedelta(days=3, hours=2)).isoformat(),
                (now - timedelta(days=1)).isoformat(),
            ),
            (
                "wish-seed-004",
                "sess-wish-04",
                "101.81.209.16",
                "上海市浦东新区 · 电信",
                "冻干猫粮",
                "请问咱们平台有全价冻干无谷猫粮吗？",
                "¥100 - ¥250",
                0,
                6,
                "pending",
                "",
                (now - timedelta(hours=19)).isoformat(),
                (now - timedelta(hours=19)).isoformat(),
            ),
            (
                "wish-seed-005",
                "sess-wish-05",
                "171.221.63.88",
                "四川省成都市 · 电信",
                "显卡",
                "有没有 RTX 4060 独立显卡现货？",
                "¥2000 - ¥2500",
                1,
                5,
                "pending",
                "",
                (now - timedelta(hours=8)).isoformat(),
                (now - timedelta(hours=8)).isoformat(),
            ),
        ]
        conn.executemany(
            """
            INSERT OR IGNORE INTO wishlist_hooks (
                hook_id, session_id, client_ip, ip_region, missing_item,
                user_query, expected_budget, user_subscribed, request_count,
                status, admin_note, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            seed_hooks,
        )
        conn.commit()

    def reset_to_baseline(self) -> None:
        """清空并重新灌入 7 天确定性基准数据（供自动化测试收尾恢复使用）。"""
        conn = self._connect()
        try:
            conn.executescript(
                """
                DELETE FROM tenants;
                DELETE FROM chat_audit_logs;
                DELETE FROM message_feedbacks;
                DELETE FROM handoff_tickets;
                DELETE FROM wishlist_hooks;
                """
            )
            self._seed_initial_data(conn)
            self._seed_wishlist_hooks(conn)
        finally:
            conn.close()

    # ==========================================================================
    # 1. Admin Authentication
    # ==========================================================================
    def authenticate_admin(self, username: str, password: str) -> Optional[Dict[str, Any]]:
        if username.strip() == "admin" and password == "admin123":
            exp = int(time.time()) + 86400 * 7
            payload = f"admin:{exp}"
            sig = hmac.new(
                ADMIN_TOKEN_SECRET.encode("utf-8"),
                payload.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            return {
                "token": f"{payload}:{sig}",
                "username": "admin",
                "nickname": "千问超级管理员",
                "role": "super_admin",
                "expires_at": exp,
            }
        return None

    def verify_admin_token(self, token: Optional[str]) -> bool:
        if not token:
            return False
        raw = token.replace("Bearer ", "").strip()
        parts = raw.split(":")
        if len(parts) != 3:
            return False
        user, exp_str, sig = parts
        try:
            if int(exp_str) < int(time.time()):
                return False
        except ValueError:
            return False
        expected = hmac.new(
            ADMIN_TOKEN_SECRET.encode("utf-8"),
            f"{user}:{exp_str}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(sig, expected)

    # ==========================================================================
    # 2. Multi-Tenant Management & Category Isolation
    # ==========================================================================
    def list_tenants(self) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM tenants ORDER BY created_at ASC")
            tenants = [dict(r) for r in cur.fetchall()]
            result: List[Dict[str, Any]] = []
            for t in tenants:
                tid = t["tenant_id"]
                allowed_cats = json.loads(t.get("allowed_categories_json") or '["3c","apparel","beauty"]')
                cur.execute(
                    """
                    SELECT
                        COUNT(DISTINCT session_id) AS session_count,
                        COUNT(*) AS message_count,
                        COUNT(DISTINCT client_ip) AS unique_ips,
                        COALESCE(SUM(total_tokens), 0) AS total_tokens,
                        SUM(CASE WHEN feedback_rating = 'like' THEN 1 ELSE 0 END) AS likes,
                        SUM(CASE WHEN feedback_rating = 'dislike' THEN 1 ELSE 0 END) AS dislikes
                    FROM chat_audit_logs
                    WHERE tenant_id = ?
                    """,
                    (tid,),
                )
                stat = dict(cur.fetchone() or {})
                likes = int(stat.get("likes") or 0)
                dislikes = int(stat.get("dislikes") or 0)
                fb_total = likes + dislikes
                sat_rate = round(likes * 100.0 / fb_total, 1) if fb_total > 0 else 100.0
                result.append(
                    {
                        "tenant_id": tid,
                        "name": t["name"],
                        "tenant_name": t["name"],
                        "brand_badge": t["brand_badge"],
                        "brand_subtitle": t["brand_badge"],
                        "app_key": t["app_key"],
                        "allowed_categories": allowed_cats,
                        "allowed_category_labels": [MAIN_CATEGORY_LABELS.get(c, c) for c in allowed_cats],
                        "rate_limit_rpm": int(t["rate_limit_rpm"]),
                        "rpm_limit": int(t["rate_limit_rpm"]),
                        "daily_token_quota": int(t["daily_token_quota"]),
                        "welcome_message": t["welcome_message"],
                        "theme_accent": t["theme_accent"],
                        "status": t["status"],
                        "created_at": t["created_at"],
                        "updated_at": t["updated_at"],
                        "stats": {
                            "session_count": int(stat.get("session_count") or 0),
                            "message_count": int(stat.get("message_count") or 0),
                            "unique_ips": int(stat.get("unique_ips") or 0),
                            "total_tokens": int(stat.get("total_tokens") or 0),
                            "quota_usage_pct": round(
                                int(stat.get("total_tokens") or 0) * 100.0 / max(1, int(t["daily_token_quota"])),
                                2,
                            ),
                            "likes": likes,
                            "dislikes": dislikes,
                            "satisfaction_rate_pct": sat_rate,
                        },
                    }
                )
            return result
        finally:
            conn.close()

    def get_tenant(self, tenant_id: str) -> Optional[Dict[str, Any]]:
        for t in self.list_tenants():
            if t["tenant_id"] == tenant_id:
                return t
        return None

    def create_or_update_tenant(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        tenant_id = (payload.get("tenant_id") or "").strip()
        if not tenant_id:
            tenant_id = f"tenant_{secrets.token_hex(3)}"
        name = (payload.get("name") or payload.get("tenant_name") or "新入驻品牌旗舰店").strip()
        brand_badge = (payload.get("brand_badge") or payload.get("brand_subtitle") or "品牌官方专营").strip()
        allowed_cats = payload.get("allowed_categories") or ["3c", "apparel", "beauty"]
        if isinstance(allowed_cats, str):
            allowed_cats = [x.strip() for x in allowed_cats.split(",") if x.strip()]
        rate_limit_rpm = int(payload.get("rate_limit_rpm") or payload.get("rpm_limit") or 80)
        daily_token_quota = int(payload.get("daily_token_quota") or 1000000)
        welcome_message = (
            payload.get("welcome_message")
            or f"您好！欢迎光临{name}✨ 我是您的专属 AI 导购顾问，随时为您推荐好物与查询售后。"
        ).strip()
        theme_accent = (payload.get("theme_accent") or "#6366f1").strip()
        status = (payload.get("status") or "active").strip()
        now_iso = datetime.now(timezone.utc).isoformat()

        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT app_key, created_at FROM tenants WHERE tenant_id = ?", (tenant_id,))
            existing = cur.fetchone()
            if existing:
                app_key = payload.get("app_key") or existing["app_key"]
                created_at = existing["created_at"]
                cur.execute(
                    """
                    UPDATE tenants SET
                        name = ?, brand_badge = ?, app_key = ?, allowed_categories_json = ?,
                        rate_limit_rpm = ?, daily_token_quota = ?, welcome_message = ?,
                        theme_accent = ?, status = ?, updated_at = ?
                    WHERE tenant_id = ?
                    """,
                    (
                        name,
                        brand_badge,
                        app_key,
                        json.dumps(allowed_cats, ensure_ascii=False),
                        rate_limit_rpm,
                        daily_token_quota,
                        welcome_message,
                        theme_accent,
                        status,
                        now_iso,
                        tenant_id,
                    ),
                )
            else:
                app_key = f"ak_live_{tenant_id}_{secrets.token_hex(6)}"
                created_at = now_iso
                cur.execute(
                    """
                    INSERT INTO tenants (
                        tenant_id, name, brand_badge, app_key, allowed_categories_json,
                        rate_limit_rpm, daily_token_quota, welcome_message, theme_accent,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tenant_id,
                        name,
                        brand_badge,
                        app_key,
                        json.dumps(allowed_cats, ensure_ascii=False),
                        rate_limit_rpm,
                        daily_token_quota,
                        welcome_message,
                        theme_accent,
                        status,
                        created_at,
                        now_iso,
                    ),
                )
            conn.commit()
        finally:
            conn.close()
        return self.get_tenant(tenant_id) or {}

    def rotate_tenant_app_key(self, tenant_id: str) -> Dict[str, Any]:
        new_key = f"ak_live_{tenant_id}_{secrets.token_hex(6)}"
        now_iso = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE tenants SET app_key = ?, updated_at = ? WHERE tenant_id = ?",
                (new_key, now_iso, tenant_id),
            )
            conn.commit()
        finally:
            conn.close()
        res = self.get_tenant(tenant_id)
        if not res:
            raise ValueError(f"Tenant {tenant_id} not found")
        return res

    def check_tenant_category_access(
        self,
        tenant_id: str,
        sub_category: Optional[str],
    ) -> Tuple[bool, Optional[str], Dict[str, Any]]:
        """
        Checks whether the given `tenant_id` is active and whether `sub_category` is allowed
        under the tenant's `allowed_categories`.
        Returns `(allowed, rejection_message_if_blocked, tenant_dict)`.
        """
        tenant = self.get_tenant(tenant_id) or self.get_tenant("tenant_all")
        if not tenant:
            return True, None, {}
        if tenant.get("status") != "active":
            return (
                False,
                f"当前店铺租户【{tenant['name']}】已暂停服务，请联系平台管理员开启配额。",
                tenant,
            )
        if not sub_category:
            return True, None, tenant

        main_cat = SUBCATEGORY_TO_MAIN_CATEGORY.get(sub_category)
        allowed_cats = tenant.get("allowed_categories") or ["3c", "apparel", "beauty"]
        if main_cat and main_cat not in allowed_cats:
            allowed_names = "、".join(MAIN_CATEGORY_LABELS.get(c, c) for c in allowed_cats)
            requested_name = MAIN_CATEGORY_LABELS.get(main_cat, main_cat)
            msg = (
                f"温馨提示：您当前所在店铺为 **{tenant['name']}**（专营品类：**{allowed_names}**），"
                f"暂未上架【{requested_name}】类商品。您可以切换顶部店铺至 **千问全品类官方旗舰店**，或直接咨询本店主营好物~"
            )
            return False, msg, tenant
        return True, None, tenant

    # ==========================================================================
    # 3. Record Live Chat Turn & Feedback
    # ==========================================================================
    def record_chat_turn(
        self,
        message_id: str,
        session_id: str,
        tenant_id: str,
        client_ip: str,
        user_agent: str,
        user_query: str,
        ai_response: Optional[str] = None,
        response_text: Optional[str] = None,
        decision_meta: Optional[Dict[str, Any]] = None,
        ui_cards: Optional[Dict[str, Any]] = None,
        telemetry: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        now_iso = datetime.now(timezone.utc).isoformat()
        clean_ip = (client_ip or "127.0.0.1").split(",")[0].strip()
        region = resolve_ip_region(clean_ip)
        t_id = (tenant_id or "tenant_all").strip()
        final_ai_response = ai_response if ai_response is not None else (response_text or "")
        decision_meta = decision_meta or {}
        ui_cards = ui_cards or {}
        telemetry = telemetry or {}

        prod_cards = ui_cards.get("product_cards") or []
        top_skus = [c.get("sku_id") for c in prod_cards if c.get("sku_id")]
        static_tok = int(telemetry.get("static_prefix_tokens") or 1947)
        dyn_tok = int(telemetry.get("dynamic_suffix_tokens") or 320)
        comp_tok = int(telemetry.get("llm_output_tokens") or 100)
        total_tok = static_tok + dyn_tok + comp_tok
        handoff = 1 if decision_meta.get("handoff_guard") else 0

        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO chat_audit_logs (
                    message_id, session_id, tenant_id, client_ip, ip_region, user_agent,
                    user_query, ai_response, primary_intent, jev_confidence, decision_source,
                    constraint_status, sub_category, top_skus_json, ui_cards_json,
                    static_prefix_md5, prefix_cache_aligned, static_prefix_tokens,
                    dynamic_suffix_tokens, completion_tokens, total_tokens,
                    ttft_ms, total_rtt_ms, rag_retrieve_ms, handoff_triggered,
                    feedback_rating, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    session_id,
                    t_id,
                    clean_ip,
                    region,
                    (user_agent or "Mozilla/5.0")[:250],
                    user_query,
                    final_ai_response,
                    str(decision_meta.get("primary_intent") or "product_recommend"),
                    float(decision_meta.get("intent_confidence") or 1.0),
                    str(decision_meta.get("decision_source") or "jev_cache"),
                    str(decision_meta.get("constraint_status") or "none"),
                    decision_meta.get("sub_category")
                    or (decision_meta.get("extracted_constraints") or {}).get("sub_category"),
                    json.dumps(top_skus, ensure_ascii=False),
                    json.dumps(ui_cards, ensure_ascii=False),
                    str(telemetry.get("static_prefix_md5") or EXPECTED_STATIC_PREFIX_MD5),
                    1 if telemetry.get("prefix_cache_aligned", True) else 0,
                    static_tok,
                    dyn_tok,
                    comp_tok,
                    total_tok,
                    float(telemetry.get("ttft_ms") or 20.0),
                    float(telemetry.get("total_rtt_ms") or 80.0),
                    float(telemetry.get("rag_retrieve_ms") or 0.0),
                    handoff,
                    "none",
                    now_iso,
                ),
            )
            if handoff:
                ticket_id = f"TK-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{secrets.token_hex(2).upper()}"
                conn.execute(
                    """
                    INSERT INTO handoff_tickets (
                        ticket_id, message_id, session_id, tenant_id, client_ip,
                        priority, trigger_reason, user_query, emotion_type,
                        status, assigned_agent, resolution_note, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ticket_id,
                        message_id,
                        session_id,
                        t_id,
                        clean_ip,
                        "P0_URGENT",
                        str((ui_cards.get("handoff_card") or {}).get("reason") or "触发高优维权人工转接"),
                        user_query,
                        str(decision_meta.get("emotion_type") or "negative"),
                        "open",
                        "VIP售后主管-林夏",
                        "",
                        now_iso,
                        now_iso,
                    ),
                )
            conn.commit()
        finally:
            conn.close()
        return {"message_id": message_id, "client_ip": clean_ip, "ip_region": region}

    def submit_feedback(
        self,
        message_id: str,
        rating: str,
        reason_tags: Optional[List[str]] = None,
        comment: str = "",
        client_ip: str = "127.0.0.1",
        tenant_id: Optional[str] = None,
        session_id: Optional[str] = None,
        user_query: Optional[str] = None,
        ai_response: Optional[str] = None,
        assistant_response: Optional[str] = None,
    ) -> Dict[str, Any]:
        if rating not in ("like", "dislike"):
            raise ValueError("rating must be 'like' or 'dislike'")
        norm_rating = rating
        tags = list(reason_tags or [])
        now_iso = datetime.now(timezone.utc).isoformat()
        clean_ip = (client_ip or "127.0.0.1").split(",")[0].strip()
        final_resp = ai_response if ai_response is not None else assistant_response

        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM chat_audit_logs WHERE message_id = ?", (message_id,))
            log_row = cur.fetchone()
            if log_row:
                t_id = tenant_id or log_row["tenant_id"]
                s_id = session_id or log_row["session_id"]
                u_q = user_query or log_row["user_query"]
                a_r = final_resp or log_row["ai_response"]
                cur.execute(
                    "UPDATE chat_audit_logs SET feedback_rating = ? WHERE message_id = ?",
                    (norm_rating, message_id),
                )
            else:
                t_id = tenant_id or "tenant_all"
                s_id = session_id or "default-session"
                u_q = user_query or ""
                a_r = final_resp or ""

            fb_id = f"fb-{uuid.uuid4().hex[:10]}"
            cur.execute("SELECT feedback_id FROM message_feedbacks WHERE message_id = ?", (message_id,))
            existing_fb = cur.fetchone()
            if existing_fb:
                fb_id = existing_fb["feedback_id"]
                cur.execute(
                    """
                    UPDATE message_feedbacks SET
                        rating = ?, reason_tags_json = ?, comment = ?, created_at = ?
                    WHERE feedback_id = ?
                    """,
                    (norm_rating, json.dumps(tags, ensure_ascii=False), comment.strip(), now_iso, fb_id),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO message_feedbacks (
                        feedback_id, message_id, session_id, tenant_id, client_ip,
                        user_query, ai_response, rating, reason_tags_json, comment,
                        status, admin_note, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fb_id,
                        message_id,
                        s_id,
                        t_id,
                        clean_ip,
                        u_q,
                        a_r,
                        norm_rating,
                        json.dumps(tags, ensure_ascii=False),
                        comment.strip(),
                        "pending" if norm_rating == "dislike" else "reviewed",
                        "",
                        now_iso,
                    ),
                )
            conn.commit()
            return {
                "feedback_id": fb_id,
                "message_id": message_id,
                "session_id": s_id,
                "tenant_id": t_id,
                "client_ip": clean_ip,
                "rating": norm_rating,
                "reason_tags": tags,
                "comment": comment.strip(),
                "status": "pending" if norm_rating == "dislike" else "reviewed",
                "created_at": now_iso,
            }
        finally:
            conn.close()

    # ==========================================================================
    # 4. Admin Dashboard & Multi-Dimensional Analytics Queries
    # ==========================================================================
    def get_dashboard_overview(
        self,
        tenant_id: Optional[str] = None,
        days: int = 7,
    ) -> Dict[str, Any]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            where_sql = "WHERE tenant_id = ?" if tenant_id else ""
            params: Tuple[Any, ...] = (tenant_id,) if tenant_id else ()

            cur.execute(
                f"""
                SELECT
                    COUNT(DISTINCT session_id) AS total_sessions,
                    COUNT(*) AS total_messages,
                    COUNT(DISTINCT client_ip) AS unique_ips,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens,
                    COALESCE(SUM(static_prefix_tokens), 0) AS total_static_tokens,
                    COALESCE(AVG(total_rtt_ms), 0.0) AS avg_rtt_ms,
                    COALESCE(AVG(ttft_ms), 0.0) AS avg_ttft_ms,
                    SUM(CASE WHEN prefix_cache_aligned = 1 THEN 1 ELSE 0 END) AS aligned_turns,
                    SUM(CASE WHEN constraint_status = 'relaxed' THEN 1 ELSE 0 END) AS relaxed_turns,
                    SUM(CASE WHEN handoff_triggered = 1 THEN 1 ELSE 0 END) AS handoff_turns
                FROM chat_audit_logs
                {where_sql}
                """,
                params,
            )
            agg = dict(cur.fetchone() or {})

            cur.execute(
                f"""
                SELECT
                    SUM(CASE WHEN rating = 'like' THEN 1 ELSE 0 END) AS likes,
                    SUM(CASE WHEN rating = 'dislike' THEN 1 ELSE 0 END) AS dislikes,
                    COUNT(*) AS total_feedbacks
                FROM message_feedbacks
                {where_sql}
                """,
                params,
            )
            fb = dict(cur.fetchone() or {})
            likes = int(fb.get("likes") or 0)
            dislikes = int(fb.get("dislikes") or 0)
            total_fb = int(fb.get("total_feedbacks") or 0)
            sat_rate = round(likes * 100.0 / total_fb, 1) if total_fb > 0 else 100.0

            # Daily Trend (last N days)
            cur.execute(
                f"""
                SELECT
                    substr(created_at, 1, 10) AS day,
                    COUNT(DISTINCT session_id) AS sessions,
                    COUNT(*) AS messages,
                    COUNT(DISTINCT client_ip) AS active_ips,
                    COALESCE(SUM(total_tokens), 0) AS tokens
                FROM chat_audit_logs
                {where_sql}
                GROUP BY substr(created_at, 1, 10)
                ORDER BY day DESC
                LIMIT ?
                """,
                tuple(list(params) + [max(1, days)]),
            )
            daily_trend = list(reversed([dict(r) for r in cur.fetchall()]))

            # Intent Distribution
            cur.execute(
                f"""
                SELECT primary_intent AS intent, COUNT(*) AS count
                FROM chat_audit_logs
                {where_sql}
                GROUP BY primary_intent
                ORDER BY count DESC
                """,
                params,
            )
            intent_dist = [dict(r) for r in cur.fetchall()]

            # Top Active Client IPs
            cur.execute(
                f"""
                SELECT
                    client_ip,
                    MAX(ip_region) AS ip_region,
                    COUNT(DISTINCT session_id) AS session_count,
                    COUNT(*) AS message_count,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens,
                    MAX(created_at) AS last_seen
                FROM chat_audit_logs
                {where_sql}
                GROUP BY client_ip
                ORDER BY message_count DESC, last_seen DESC
                LIMIT 10
                """,
                params,
            )
            top_ips = [dict(r) for r in cur.fetchall()]

            total_msgs = max(1, int(agg.get("total_messages") or 0))
            aligned_turns = int(agg.get("aligned_turns") or 0)

            summary_dict = {
                "total_tenants": len(self.list_tenants()),
                "total_sessions": int(agg.get("total_sessions") or 0),
                "total_messages": int(agg.get("total_messages") or 0),
                "total_requests": int(agg.get("total_messages") or 0),
                "unique_ips": int(agg.get("unique_ips") or 0),
                "total_tokens": int(agg.get("total_tokens") or 0),
                "total_static_tokens": int(agg.get("total_static_tokens") or 0),
                "avg_rtt_ms": round(float(agg.get("avg_rtt_ms") or 0.0), 1),
                "avg_ttft_ms": round(float(agg.get("avg_ttft_ms") or 0.0), 1),
                "prefix_cache_alignment_pct": round(aligned_turns * 100.0 / total_msgs, 1),
                "static_prefix_md5": EXPECTED_STATIC_PREFIX_MD5,
                "relaxed_turns": int(agg.get("relaxed_turns") or 0),
                "handoff_turns": int(agg.get("handoff_turns") or 0),
                "feedback_total": total_fb,
                "feedback_likes": likes,
                "feedback_dislikes": dislikes,
                "satisfaction_rate_pct": sat_rate,
            }

            return {
                "summary": summary_dict,
                "kpis": summary_dict,
                "daily_trend": daily_trend,
                "intent_distribution": intent_dist,
                "top_active_ips": top_ips,
                "top_ips": top_ips,
                "tenants": self.list_tenants(),
            }
        finally:
            conn.close()

    def list_audit_logs(
        self,
        tenant_id: Optional[str] = None,
        client_ip: Optional[str] = None,
        ip_keyword: Optional[str] = None,
        primary_intent: Optional[str] = None,
        intent: Optional[str] = None,
        constraint_status: Optional[str] = None,
        feedback_rating: Optional[str] = None,
        feedback_filter: Optional[str] = None,
        handoff_only: bool = False,
        keyword: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            clauses: List[str] = []
            params: List[Any] = []
            if tenant_id:
                clauses.append("tenant_id = ?")
                params.append(tenant_id)
            ip_search = client_ip or ip_keyword
            if ip_search:
                clauses.append("(client_ip LIKE ? OR ip_region LIKE ?)")
                params.extend([f"%{ip_search.strip()}%", f"%{ip_search.strip()}%"])
            intent_filter = primary_intent or intent
            if intent_filter:
                clauses.append("primary_intent = ?")
                params.append(intent_filter)
            if constraint_status:
                clauses.append("constraint_status = ?")
                params.append(constraint_status)
            fb_f = feedback_rating or feedback_filter
            if fb_f:
                clauses.append("feedback_rating = ?")
                params.append(fb_f)
            if handoff_only:
                clauses.append("handoff_triggered = 1")
            if keyword:
                clauses.append("(user_query LIKE ? OR ai_response LIKE ? OR session_id LIKE ?)")
                kw = f"%{keyword.strip()}%"
                params.extend([kw, kw, kw])

            where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            cur.execute(f"SELECT COUNT(*) FROM chat_audit_logs {where_sql}", tuple(params))
            total = int(cur.fetchone()[0])

            offset = max(0, (page - 1) * page_size)
            cur.execute(
                f"""
                SELECT * FROM chat_audit_logs
                {where_sql}
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                tuple(params + [page_size, offset]),
            )
            items = []
            for r in cur.fetchall():
                d = dict(r)
                d["top_skus"] = json.loads(d.pop("top_skus_json", "[]") or "[]")
                d["ui_cards"] = json.loads(d.pop("ui_cards_json", "{}") or "{}")
                d["prefix_cache_aligned"] = bool(d["prefix_cache_aligned"])
                d["handoff_triggered"] = bool(d["handoff_triggered"])
                items.append(d)

            return {"total": total, "page": page, "page_size": page_size, "items": items}
        finally:
            conn.close()

    def list_feedbacks(
        self,
        tenant_id: Optional[str] = None,
        rating: Optional[str] = None,
        status: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> Dict[str, Any]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            clauses: List[str] = []
            params: List[Any] = []
            if tenant_id:
                clauses.append("tenant_id = ?")
                params.append(tenant_id)
            if rating:
                clauses.append("rating = ?")
                params.append(rating)
            if status:
                clauses.append("status = ?")
                params.append(status)
            where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""

            cur.execute(f"SELECT * FROM message_feedbacks {where_sql} ORDER BY created_at DESC", tuple(params))
            items = []
            tag_counter: Dict[str, int] = {}
            likes = 0
            dislikes = 0
            for r in cur.fetchall():
                d = dict(r)
                tags = json.loads(d.pop("reason_tags_json", "[]") or "[]")
                d["reason_tags"] = tags
                if d["rating"] == "like":
                    likes += 1
                else:
                    dislikes += 1
                for t in tags:
                    tag_counter[t] = tag_counter.get(t, 0) + 1
                items.append(d)

            total = likes + dislikes
            offset = max(0, (page - 1) * page_size)
            paged_items = items[offset : offset + page_size]
            return {
                "total": total,
                "likes": likes,
                "dislikes": dislikes,
                "satisfaction_rate_pct": round(likes * 100.0 / total, 1) if total > 0 else 100.0,
                "reason_tag_breakdown": [
                    {"tag": k, "count": v}
                    for k, v in sorted(tag_counter.items(), key=lambda x: (-x[1], x[0]))
                ],
                "items": paged_items,
            }
        finally:
            conn.close()

    def update_feedback_status(
        self,
        feedback_id: Any,
        status: str,
        admin_note: str = "",
    ) -> Dict[str, Any]:
        fb_id_str = str(feedback_id)
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE message_feedbacks SET status = ?, admin_note = ? WHERE feedback_id = ?",
                (status, admin_note.strip(), fb_id_str),
            )
            conn.commit()
            cur.execute("SELECT * FROM message_feedbacks WHERE feedback_id = ?", (fb_id_str,))
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Feedback {fb_id_str} not found")
            d = dict(row)
            d["reason_tags"] = json.loads(d.pop("reason_tags_json", "[]") or "[]")
            return d
        finally:
            conn.close()

    def list_handoff_tickets(
        self,
        tenant_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            clauses: List[str] = []
            params: List[Any] = []
            if tenant_id:
                clauses.append("tenant_id = ?")
                params.append(tenant_id)
            if status:
                clauses.append("status = ?")
                params.append(status)
            where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            cur.execute(f"SELECT * FROM handoff_tickets {where_sql} ORDER BY created_at DESC", tuple(params))
            items = [dict(r) for r in cur.fetchall()]
            open_cnt = sum(1 for x in items if x.get("status") != "resolved")
            return {
                "summary": {
                    "total_tickets": len(items),
                    "open_tickets": open_cnt,
                    "resolved_tickets": len(items) - open_cnt,
                },
                "items": items,
            }
        finally:
            conn.close()

    def update_handoff_ticket(
        self,
        ticket_id: str,
        status: str,
        assigned_agent: Optional[str] = None,
        assigned_specialist: Optional[str] = None,
        resolution_note: Optional[str] = None,
    ) -> Dict[str, Any]:
        now_iso = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM handoff_tickets WHERE ticket_id = ?", (ticket_id,))
            existing = cur.fetchone()
            if not existing:
                raise ValueError(f"Ticket {ticket_id} not found")
            agent = (
                assigned_agent
                if assigned_agent is not None
                else (assigned_specialist if assigned_specialist is not None else existing["assigned_agent"])
            )
            note = resolution_note if resolution_note is not None else existing["resolution_note"]
            cur.execute(
                """
                UPDATE handoff_tickets
                SET status = ?, assigned_agent = ?, resolution_note = ?, updated_at = ?
                WHERE ticket_id = ?
                """,
                (status, agent, note, now_iso, ticket_id),
            )
            conn.commit()
            cur.execute("SELECT * FROM handoff_tickets WHERE ticket_id = ?", (ticket_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Ticket {ticket_id} not found")
            return dict(row)
        finally:
            conn.close()

    def get_rag_analytics(self, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            clauses = ["primary_intent = 'product_recommend'"]
            params: List[Any] = []
            if tenant_id:
                clauses.append("tenant_id = ?")
                params.append(tenant_id)
            where_sql = "WHERE " + " AND ".join(clauses)

            cur.execute(
                f"""
                SELECT
                    COUNT(*) AS total_rag_queries,
                    SUM(CASE WHEN constraint_status = 'satisfied' THEN 1 ELSE 0 END) AS exact_hits,
                    SUM(CASE WHEN constraint_status = 'relaxed' THEN 1 ELSE 0 END) AS relaxed_hits,
                    COALESCE(AVG(rag_retrieve_ms), 0.0) AS avg_rag_ms
                FROM chat_audit_logs
                {where_sql}
                """,
                tuple(params),
            )
            agg = dict(cur.fetchone() or {})
            total_q = max(1, int(agg.get("total_rag_queries") or 0))
            exact_hits = int(agg.get("exact_hits") or 0)
            relaxed_hits = int(agg.get("relaxed_hits") or 0)

            cur.execute(
                f"""
                SELECT
                    COALESCE(sub_category, 'unknown') AS sub_category,
                    COUNT(*) AS query_count,
                    SUM(CASE WHEN constraint_status = 'relaxed' THEN 1 ELSE 0 END) AS relaxed_count,
                    ROUND(AVG(rag_retrieve_ms), 2) AS avg_latency_ms
                FROM chat_audit_logs
                {where_sql}
                GROUP BY sub_category
                ORDER BY query_count DESC
                """,
                tuple(params),
            )
            subcat_stats = [dict(r) for r in cur.fetchall()]

            cur.execute(
                f"""
                SELECT * FROM chat_audit_logs
                {where_sql}
                ORDER BY created_at DESC
                LIMIT 30
                """,
                tuple(params),
            )
            logs = []
            for r in cur.fetchall():
                d = dict(r)
                d["top_skus"] = json.loads(d.pop("top_skus_json", "[]") or "[]")
                d["ui_cards"] = json.loads(d.pop("ui_cards_json", "{}") or "{}")
                logs.append(d)

            return {
                "summary": {
                    "total_rag_queries": int(agg.get("total_rag_queries") or 0),
                    "exact_hits": exact_hits,
                    "relaxed_hits": relaxed_hits,
                    "dual_path_relaxed_hits": relaxed_hits,
                    "exact_hit_rate_pct": round(exact_hits * 100.0 / total_q, 1),
                    "relaxation_trigger_rate_pct": round(relaxed_hits * 100.0 / total_q, 1),
                    "avg_rag_retrieve_ms": round(float(agg.get("avg_rag_ms") or 0.0), 2),
                },
                "subcategory_breakdown": subcat_stats,
                "subcategory_stats": subcat_stats,
                "recent_rag_logs": logs,
            }
        finally:
            conn.close()

    def get_token_analytics(self, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            where_sql = "WHERE tenant_id = ?" if tenant_id else ""
            params: Tuple[Any, ...] = (tenant_id,) if tenant_id else ()

            cur.execute(
                f"""
                SELECT
                    COUNT(*) AS total_turns,
                    COALESCE(SUM(static_prefix_tokens), 0) AS static_tokens,
                    COALESCE(SUM(dynamic_suffix_tokens), 0) AS dynamic_tokens,
                    COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens,
                    SUM(CASE WHEN prefix_cache_aligned = 1 THEN 1 ELSE 0 END) AS aligned_turns
                FROM chat_audit_logs
                {where_sql}
                """,
                params,
            )
            agg = dict(cur.fetchone() or {})
            total_turns = max(1, int(agg.get("total_turns") or 0))
            static_tok = int(agg.get("static_tokens") or 0)
            dyn_tok = int(agg.get("dynamic_tokens") or 0)
            comp_tok = int(agg.get("completion_tokens") or 0)
            tot_tok = int(agg.get("total_tokens") or 0)
            aligned = int(agg.get("aligned_turns") or 0)
            prompt_tok = max(1, static_tok + dyn_tok)
            align_rate = round(aligned * 100.0 / total_turns, 1)

            cur.execute(
                f"""
                SELECT
                    substr(created_at, 1, 10) AS day,
                    SUM(static_prefix_tokens) AS static_tokens,
                    SUM(dynamic_suffix_tokens) AS dynamic_tokens,
                    SUM(completion_tokens) AS completion_tokens,
                    SUM(total_tokens) AS total_tokens,
                    COUNT(*) AS request_count
                FROM chat_audit_logs
                {where_sql}
                GROUP BY substr(created_at, 1, 10)
                ORDER BY day ASC
                """,
                params,
            )
            daily_tokens = [dict(r) for r in cur.fetchall()]

            cur.execute(
                """
                SELECT
                    tenant_id,
                    COUNT(*) AS request_count,
                    COUNT(DISTINCT client_ip) AS unique_ips,
                    SUM(static_prefix_tokens) AS static_tokens,
                    SUM(dynamic_suffix_tokens) AS dynamic_tokens,
                    SUM(completion_tokens) AS completion_tokens,
                    SUM(total_tokens) AS total_tokens
                FROM chat_audit_logs
                GROUP BY tenant_id
                ORDER BY total_tokens DESC
                """
            )
            by_tenant = [dict(r) for r in cur.fetchall()]

            return {
                "summary": {
                    "total_turns": int(agg.get("total_turns") or 0),
                    "static_prefix_md5": EXPECTED_STATIC_PREFIX_MD5,
                    "locked_static_prefix_md5": EXPECTED_STATIC_PREFIX_MD5,
                    "md5_alignment_rate_pct": align_rate,
                    "prefix_alignment_rate_pct": align_rate,
                    "static_prefix_tokens_per_turn": 1947,
                    "total_static_tokens": static_tok,
                    "total_dynamic_tokens": dyn_tok,
                    "total_completion_tokens": comp_tok,
                    "total_tokens": tot_tok,
                    "static_prompt_ratio_pct": round(static_tok * 100.0 / prompt_tok, 2),
                    "estimated_cost_savings_pct": round(static_tok * 0.9 * 100.0 / prompt_tok, 2),
                },
                "daily_tokens": daily_tokens,
                "tenant_breakdown": by_tenant,
            }
        finally:
            conn.close()

    # ==========================================================================
    # 5. Missing Category Wishlist Hooks & Heuristic 2 Context Stitching
    # ==========================================================================
    def record_wishlist_hook(
        self,
        missing_item: str,
        user_query: str = "",
        session_id: str = "huigou-session",
        client_ip: str = "127.0.0.1",
        expected_budget: str = "",
        user_subscribed: bool = False,
    ) -> Dict[str, Any]:
        """记录或累加用户在惠购端触发的缺货品类心愿单钩子。"""
        clean_item = (missing_item or "待补充商品").strip()
        clean_ip = (client_ip or "127.0.0.1").split(",")[0].strip()
        region = resolve_ip_region(clean_ip)
        now_iso = datetime.now(timezone.utc).isoformat()

        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM wishlist_hooks WHERE missing_item = ? ORDER BY created_at DESC LIMIT 1",
                (clean_item,),
            )
            existing = cur.fetchone()
            if existing:
                hook_id = existing["hook_id"]
                new_count = int(existing["request_count"] or 1) + (1 if not user_subscribed else 0)
                new_sub = 1 if (user_subscribed or existing["user_subscribed"]) else 0
                budget_val = expected_budget.strip() if expected_budget else existing["expected_budget"]
                query_val = user_query.strip() if user_query else existing["user_query"]
                cur.execute(
                    """
                    UPDATE wishlist_hooks
                    SET request_count = ?, user_subscribed = ?, expected_budget = ?,
                        user_query = ?, client_ip = ?, ip_region = ?, updated_at = ?
                    WHERE hook_id = ?
                    """,
                    (new_count, new_sub, budget_val, query_val, clean_ip, region, now_iso, hook_id),
                )
            else:
                hook_id = f"wish-{uuid.uuid4().hex[:8]}"
                cur.execute(
                    """
                    INSERT INTO wishlist_hooks (
                        hook_id, session_id, client_ip, ip_region, missing_item,
                        user_query, expected_budget, user_subscribed, request_count,
                        status, admin_note, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        hook_id,
                        session_id or "huigou-session",
                        clean_ip,
                        region,
                        clean_item,
                        user_query.strip() or f"咨询商品：{clean_item}",
                        expected_budget.strip(),
                        1 if user_subscribed else 0,
                        1,
                        "pending",
                        "",
                        now_iso,
                        now_iso,
                    ),
                )
            conn.commit()
            cur.execute("SELECT * FROM wishlist_hooks WHERE hook_id = ?", (hook_id,))
            row = cur.fetchone()
            return dict(row) if row else {"hook_id": hook_id, "missing_item": clean_item}
        finally:
            conn.close()

    def list_wishlist_hooks(self, status: Optional[str] = None) -> Dict[str, Any]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            if status:
                cur.execute(
                    "SELECT * FROM wishlist_hooks WHERE status = ? ORDER BY request_count DESC, updated_at DESC",
                    (status,),
                )
            else:
                cur.execute("SELECT * FROM wishlist_hooks ORDER BY request_count DESC, updated_at DESC")
            items = [dict(r) for r in cur.fetchall()]

            cur.execute(
                """
                SELECT
                    COUNT(*) AS total_categories,
                    COALESCE(SUM(request_count), 0) AS total_requests,
                    SUM(CASE WHEN user_subscribed = 1 THEN 1 ELSE 0 END) AS subscribed_categories,
                    SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending_count,
                    SUM(CASE WHEN status = 'sourcing' THEN 1 ELSE 0 END) AS sourcing_count,
                    SUM(CASE WHEN status = 'stocked' THEN 1 ELSE 0 END) AS stocked_count
                FROM wishlist_hooks
                """
            )
            agg = dict(cur.fetchone() or {})
            return {
                "summary": {
                    "total_categories": int(agg.get("total_categories") or 0),
                    "total_requests": int(agg.get("total_requests") or 0),
                    "subscribed_categories": int(agg.get("subscribed_categories") or 0),
                    "pending_count": int(agg.get("pending_count") or 0),
                    "sourcing_count": int(agg.get("sourcing_count") or 0),
                    "stocked_count": int(agg.get("stocked_count") or 0),
                },
                "items": items,
            }
        finally:
            conn.close()

    def update_wishlist_hook(
        self,
        hook_id: str,
        status: str,
        admin_note: Optional[str] = None,
    ) -> Dict[str, Any]:
        now_iso = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM wishlist_hooks WHERE hook_id = ?", (hook_id,))
            existing = cur.fetchone()
            if not existing:
                raise ValueError(f"Wishlist hook {hook_id} not found")
            note = admin_note if admin_note is not None else existing["admin_note"]
            cur.execute(
                "UPDATE wishlist_hooks SET status = ?, admin_note = ?, updated_at = ? WHERE hook_id = ?",
                (status, note, now_iso, hook_id),
            )
            conn.commit()
            cur.execute("SELECT * FROM wishlist_hooks WHERE hook_id = ?", (hook_id,))
            row = cur.fetchone()
            return dict(row) if row else {}
        finally:
            conn.close()

    def get_context_manifest(self) -> Dict[str, Any]:
        """
        启发 2：上下文缝补（Context Stitching Manifest）
        将外置在 JSON 与 SQLite 中的商品规格池、冲突松弛对、软硬双锁状态与缺货品类钩子统一拉回视野。
        """
        catalog_path = DATA_DIR / "ecommerce_catalog_60skus.json"
        conflict_path = DATA_DIR / "conflict_pairs.json"
        skus: List[Dict[str, Any]] = []
        conflicts: List[Dict[str, Any]] = []
        if catalog_path.exists():
            skus = json.loads(catalog_path.read_text(encoding="utf-8"))
        if conflict_path.exists():
            raw_c = json.loads(conflict_path.read_text(encoding="utf-8"))
            if isinstance(raw_c, list):
                conflicts = raw_c
            elif isinstance(raw_c, dict):
                for k in ("conflict_pairs", "pairs", "items", "benchmarks"):
                    if isinstance(raw_c.get(k), list):
                        conflicts = raw_c[k]
                        break

        subcat_counts: Dict[str, int] = {}
        cat_counts: Dict[str, int] = {}
        for s in skus:
            c = s.get("category") or "unknown"
            sc = s.get("sub_category") or "unknown"
            cat_counts[c] = cat_counts.get(c, 0) + 1
            subcat_counts[sc] = subcat_counts.get(sc, 0) + 1

        wish_data = self.list_wishlist_hooks()
        return {
            "platform_name": "惠购 · 全平台 AI 购物助手",
            "architecture_mode": "Unified Full-Platform Catalog + Wishlist Hook",
            "harness_engineering": {
                "heuristic_1_scope": "C端对话流与 Generative UI 渲染层",
                "soft_lock": "System Prompt 自然导购约束（禁止解释性小字堆砌与伪造促销）",
                "hard_lock_text": "SlidingWindowStreamFilter + streamSanitizer 物理剥离 <br>/HTML/LaTeX",
                "hard_lock_genui": "product_card / comparison_card / order_card / wishlist_card 金额与库存 100% 后端确定性注入",
            },
            "context_stitching": {
                "heuristic_2_scope": "B端 OA 管理后台与全栈配置自省层",
                "static_prefix_md5": EXPECTED_STATIC_PREFIX_MD5,
                "static_prefix_tokens": 1947,
                "total_skus": len(skus),
                "total_subcategories": len(subcat_counts),
                "category_distribution": cat_counts,
                "subcategory_distribution": subcat_counts,
                "active_conflict_pairs_count": len(conflicts),
                "conflict_pairs_preview": [
                    {
                        "pair_id": cp.get("pair_id") or cp.get("id") or f"C{idx+1}",
                        "sub_category": cp.get("sub_category"),
                        "query": cp.get("query") or cp.get("user_query"),
                    }
                    for idx, cp in enumerate(conflicts[:5])
                ],
            },
            "wishlist_hooks_summary": wish_data["summary"],
            "top_missing_items": wish_data["items"][:8],
        }


# Singleton instance
analytics_service = AdminAnalyticsService()


def get_admin_analytics_service() -> AdminAnalyticsService:
    return analytics_service


