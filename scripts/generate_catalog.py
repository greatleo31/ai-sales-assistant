#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
60 SKU 电商导购受控基准商品库、平台规则与测试订单生成器
权威真源：
  - data/spec_registry.json (17 个子类目、规格单位、默认比较方向、取值范围、离散整数标记、封闭枚举词表、中性事实标签)
  - data/conflict_pairs.json (C1~C5 五组硬约束冲突对唯一真源，生成器动态读取并注入路径 SKU 的价格与规格)
生成产物：
  - data/ecommerce_catalog_60skus.json
  - data/platform_rules.json
  - data/mock_orders.json
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"

# 固定基准参考时间 (Asia/Shanghai, UTC+8)，用于确定性物化测试订单时间戳
SHANGHAI_TZ = timezone(timedelta(hours=8))
REFERENCE_TIME_ISO = "2026-10-10T12:00:00+08:00"
REFERENCE_DT = datetime.fromisoformat(REFERENCE_TIME_ISO)


def format_offset_iso(ref_dt: datetime, offset_hours: Optional[float]) -> Optional[str]:
    """根据固定 reference_time 与相对小时偏移生成确定性 ISO-8601 时间戳。"""
    if offset_hours is None:
        return None
    dt = ref_dt + timedelta(hours=float(offset_hours))
    return dt.astimezone(SHANGHAI_TZ).isoformat(timespec="seconds")


def build_retrieval_text(
    sku: Dict[str, Any],
    sub_cat_meta: Dict[str, Any],
) -> str:
    """
    将结构化商品字段与离线中文视觉描述拼接为统一的多模态检索语料 retrieval_text。
    【R1 修复】：严禁读取查询侧 zh_aliases 拼入商品事实语料，仅使用中性 display_label / display_labels 与商品自身真实属性。
    """
    spec_parts: List[str] = []
    specs_def = sub_cat_meta.get("specs", {})
    for k, v in sku["specs"].items():
        s_meta = specs_def.get(k, {})
        label = s_meta.get("label", k)
        unit = s_meta.get("unit", "")
        if s_meta.get("type") == "enum":
            display_labels = s_meta.get("display_labels", {})
            neutral_label = display_labels.get(str(v), str(v))
            spec_parts.append(f"{label}:{v}({neutral_label})")
        else:
            spec_parts.append(f"{label}:{v}{unit}")

    sub_display_label = sub_cat_meta.get("display_label", sku["sub_category"])
    highlights_str = "；".join(sku["highlights"])
    discount = round(float(sku["original_price"]) - float(sku["final_price"]), 2)

    return (
        f"【商品】{sku['title']} "
        f"【品牌】{sku['brand']} "
        f"【类目】{sku['category']}/{sku['sub_category']} ({sub_display_label}) "
        f"【价格】到手价¥{sku['final_price']} (原价¥{sku['original_price']}，直降¥{discount}) "
        f"【核心规格】{' | '.join(spec_parts)} "
        f"【核心卖点】{highlights_str} "
        f"【视觉外观】{sku['visual_description']}"
    )


def get_raw_skus() -> List[Dict[str, Any]]:
    """
    返回 60 条高区分度受控演示 SKU 基础定义（25 3C数码 + 20 换季服饰 + 15 美妆个护）。
    【R3 修复】：C1~C5 共 10 条冲突路径 SKU（SKU-3C-008/009/010/011/012/013、SKU-APP-001/002、SKU-BEA-001/002）
    的受控价格(final_price)与受控规格数值不在本函数中硬编码，统一设为 None / 空缺，由 apply_conflict_pairs_truth()
    从 data/conflict_pairs.json 唯一真源动态读取并注入。
    """
    return [
        # ==================== 1. 3C 数码周边 (25 SKUs) ====================
        # --- 1.1 gan_charger (5 SKUs: SKU-3C-001, 002, 003, 010, 011) ---
        {
            "sku_id": "SKU-3C-001",
            "category": "3c_digital",
            "sub_category": "gan_charger",
            "title": "安克 Anker 30W 氮化镓超能充 Nano3 折叠插脚单口快充头",
            "brand": "Anker",
            "original_price": 59.0,
            "final_price": 35.0,
            "specs": {"power_w": 30, "ports": 1},
            "highlights": ["氮化镓温控芯片", "折叠插脚", "体积小巧如耳塞盒", "1C单口PD快充"],
            "visual_description": "冰晶紫方糖造型微型充电头，表面磨砂搭配亮面金属装饰圈，底部配备可折叠国标双扁插脚，单Type-C接口居中设计。",
            "search_query": "Anker 30W 氮化镓 折叠插脚 小巧充电器 商品图",
        },
        {
            "sku_id": "SKU-3C-002",
            "category": "3c_digital",
            "sub_category": "gan_charger",
            "title": "倍思 65W 氮化镓 GaN5 Pro 三口快充充电器套装",
            "brand": "Baseus",
            "original_price": 129.0,
            "final_price": 89.0,
            "specs": {"power_w": 65, "ports": 3},
            "highlights": ["第五代氮化镓芯片", "2C1A三口盲插", "折叠插脚轻巧便携", "附赠Type-C编织快充线"],
            "visual_description": "黑色修长柱状机身，消光细腻磨砂外壳，冰蓝电源呼吸指示灯，顶部排列2个Type-C与1个USB-A接口（2C1A），配折叠插脚。",
            "search_query": "倍思 65W 氮化镓 三口快充充电器 黑色 商品图",
        },
        {
            "sku_id": "SKU-3C-003",
            "category": "3c_digital",
            "sub_category": "gan_charger",
            "title": "绿联 140W 氮化镓 PD3.1 四口桌面闪充充电器",
            "brand": "UGREEN",
            "original_price": 249.0,
            "final_price": 199.0,
            "specs": {"power_w": 140, "ports": 4},
            "highlights": ["PD3.1满血140W输出", "3C1A四口同充", "氮化镓双芯片散热", "折叠插脚"],
            "visual_description": "深空灰金属质感方正外壳，斜切防滑纹理侧面，正面印有140W金色标识，输出端为3个Type-C口加1个USB-A口。",
            "search_query": "绿联 140W 氮化镓 四口快充充电器 商品图",
        },
        # C2 Path A: final_price, power_w, ports 由 conflict_pairs.json[C2].path_a.expect 注入
        {
            "sku_id": "SKU-3C-010",
            "category": "3c_digital",
            "sub_category": "gan_charger",
            "title": "酷态科 100W 氮化镓三口快充头 GaN-100W-Pro 笔记本手机同充",
            "brand": "CukTech",
            "original_price": 169.0,
            "final_price": None,
            "specs": {},
            "highlights": ["100W满血笔记本快充", "2C1A三口智能分流", "体积小巧", "折叠插脚便携"],
            "visual_description": "钛灰拼黑撞色外壳，机甲风条纹散热槽，体积紧凑小巧，配备2个Type-C口与1个橙色舌片USB-A口。",
            "search_query": "酷态科 100W 氮化镓 三口快充头 商品图",
        },
        # C2 Path B: final_price, power_w, ports 由 conflict_pairs.json[C2].path_b.expect 注入
        {
            "sku_id": "SKU-3C-011",
            "category": "3c_digital",
            "sub_category": "gan_charger",
            "title": "努比亚氘锋 65W 氮化镓双口快充头 GaN-65W-Mini",
            "brand": "Nubia",
            "original_price": 69.0,
            "final_price": None,
            "specs": {},
            "highlights": ["65W轻薄本快充", "1C1A双口输出", "体积小巧", "折叠插脚"],
            "visual_description": "银灰机械风涂装小方块充电器，侧边印有红色氘字Logo，折叠插脚收纳设计，顶部提供1C1A双快充口。",
            "search_query": "努比亚 氘锋 65W 氮化镓 双口充电器 商品图",
        },

        # --- 1.2 laptop_bag (6 SKUs: SKU-3C-004, 005, 006, 007, 008, 009) ---
        {
            "sku_id": "SKU-3C-004",
            "category": "3c_digital",
            "sub_category": "laptop_bag",
            "title": "绿联 13.3寸轻薄笔记本防刮内胆包 适配MacBook Air",
            "brand": "UGREEN",
            "original_price": 25.9,
            "final_price": 15.9,
            "specs": {"screen_size": 13.3},
            "highlights": ["轻便纤薄", "细腻水晶绒内里", "防泼水PU皮面"],
            "visual_description": "浅灰色极简信封式轻薄内胆包，荔枝纹PU皮表面，隐藏式磁吸封口，贴合13.3寸超极本。",
            "search_query": "绿联 13.3寸 笔记本内胆包 灰色 商品图",
        },
        {
            "sku_id": "SKU-3C-005",
            "category": "3c_digital",
            "sub_category": "laptop_bag",
            "title": "倍思 14寸商务手提笔记本电脑包 防泼水气囊内胆",
            "brand": "Baseus",
            "original_price": 59.0,
            "final_price": 39.0,
            "specs": {"screen_size": 14.0},
            "highlights": ["隐藏式手提带", "防震海绵层", "防泼水面料", "前袋收纳电源鼠标"],
            "visual_description": "深空灰商务风手提电脑包，正面设斜拉链配件仓，高密度防泼水涤纶面料，顶部配可伸缩软皮提手。",
            "search_query": "倍思 14寸 手提笔记本电脑包 深空灰 商品图",
        },
        {
            "sku_id": "SKU-3C-006",
            "category": "3c_digital",
            "sub_category": "laptop_bag",
            "title": "小米 16寸多功能都市电脑双肩包 防震气囊独立电脑仓",
            "brand": "Xiaomi",
            "original_price": 79.0,
            "final_price": 49.0,
            "specs": {"screen_size": 16.0},
            "highlights": ["16寸独立防震仓", "S型减压背带", "4级防泼水面料", "轻便通勤"],
            "visual_description": "炭黑色立体剪裁双肩电脑背包，正面几何折线造型，背部透气网孔海绵垫，内置加厚绒毛16寸电脑隔层。",
            "search_query": "小米 16寸 电脑双肩包 黑色 商品图",
        },
        {
            "sku_id": "SKU-3C-007",
            "category": "3c_digital",
            "sub_category": "laptop_bag",
            "title": "ROG 玩家国度 17.3寸硬壳装甲防震游戏本双肩包",
            "brand": "ROG",
            "original_price": 129.0,
            "final_price": 79.0,
            "specs": {"screen_size": 17.3},
            "highlights": ["EVA硬壳防撞外甲", "四角气囊防震", "17.3寸巨无霸游戏本专属", "防泼水涂层"],
            "visual_description": "纯黑机甲硬壳双肩包，正面压印败家之眼反光徽标与赛博斜纹，大容量加厚防震主仓可容纳17.3寸厚重游戏本。",
            "search_query": "ROG 17.3寸 游戏本双肩电脑包 黑色 商品图",
        },
        # C1 Path A: final_price, screen_size 由 conflict_pairs.json[C1].path_a.expect 注入
        {
            "sku_id": "SKU-3C-008",
            "category": "3c_digital",
            "sub_category": "laptop_bag",
            "title": "酷态科 17.3寸加厚气囊防震游戏本电脑包",
            "brand": "CukTech",
            "original_price": 39.9,
            "final_price": None,
            "specs": {},
            "highlights": ["四角专利气囊防震", "防泼水牛津布", "适配拯救者/ROG 17.3寸", "加厚毛绒护机内胆"],
            "visual_description": "星空黑大号手提内胆包，四角内置立体蜂窝气囊缓冲垫，外层耐磨防泼水牛津布，顺滑双金属拉链开合。",
            "search_query": "17.3寸 气囊防震 游戏本手提电脑包 黑色 商品图",
        },
        # C1 Path B: final_price, screen_size 由 conflict_pairs.json[C1].path_b.expect 注入
        {
            "sku_id": "SKU-3C-009",
            "category": "3c_digital",
            "sub_category": "laptop_bag",
            "title": "毕亚兹 15.6寸加厚防震笔记本内胆包 防泼水轻便手提包",
            "brand": "BIAZE",
            "original_price": 29.9,
            "final_price": None,
            "specs": {},
            "highlights": ["珍珠棉防震夹层", "防泼水面料", "轻便手提", "15.6寸主流游戏本/全能本通用"],
            "visual_description": "深灰简约手提内胆包，正面带拉链电源收纳副袋，内层加厚防震海绵与柔软绒布，适合15.6寸笔记本日常携带。",
            "search_query": "15.6寸 防震笔记本电脑内胆包 手提包 深灰 商品图",
        },

        # --- 1.3 power_bank (3 SKUs: SKU-3C-012, 013, 014) ---
        # C3 Path A: final_price, capacity_mah, weight_g 由 conflict_pairs.json[C3].path_a.expect 注入
        {
            "sku_id": "SKU-3C-012",
            "category": "3c_digital",
            "sub_category": "power_bank",
            "title": "酷态科 20000mAh 高密硅碳负极超轻自带线快充充电宝 45W",
            "brand": "CukTech",
            "original_price": 149.0,
            "final_price": None,
            "specs": {"output_w": 45},
            "highlights": ["硅碳负极电芯195g超轻机身", "20000mAh大容量", "自带线双向快充", "登机可带"],
            "visual_description": "钛灰磨砂超薄卡片式机身，侧边嵌入式编织Type-C自带线，正面LED数显百分比屏幕，掌心大小轻巧便携。",
            "search_query": "酷态科 20000毫安 自带线 轻薄充电宝 数显 商品图",
        },
        # C3 Path B: final_price, capacity_mah, weight_g 由 conflict_pairs.json[C3].path_b.expect 注入
        {
            "sku_id": "SKU-3C-013",
            "category": "3c_digital",
            "sub_category": "power_bank",
            "title": "小米 10000mAh 口袋版自带线轻薄快充充电宝 22.5W",
            "brand": "Xiaomi",
            "original_price": 79.0,
            "final_price": None,
            "specs": {"output_w": 22.5},
            "highlights": ["185g轻巧塞口袋", "自带线免带线材", "22.5W双向快充", "登机可带"],
            "visual_description": "莫兰迪灰圆润口袋移动电源，一体式收纳Type-C自带快充线可兼作挂绳，磨砂亲肤外壳不沾指纹。",
            "search_query": "小米 10000mAh 口袋版 自带线充电宝 商品图",
        },
        {
            "sku_id": "SKU-3C-014",
            "category": "3c_digital",
            "sub_category": "power_bank",
            "title": "绿联 20000mAh 100W 笔记本闪充移动电源 数显大功率充电宝",
            "brand": "UGREEN",
            "original_price": 139.0,
            "final_price": 89.0,
            "specs": {"capacity_mah": 20000, "output_w": 100, "weight_g": 245},
            "highlights": ["100W大功率充笔记本", "20000mAh长续航", "智能数显屏", "登机可带"],
            "visual_description": "深空灰金属喷砂机身，正面大尺寸高清数字显示屏，顶部配2C1A三接口，机身厚实稳重。",
            "search_query": "绿联 20000mAh 100W 笔记本充电宝 数显 商品图",
        },

        # --- 1.4 mechanical_keyboard (4 SKUs: SKU-3C-015, 016, 017, 018) ---
        {
            "sku_id": "SKU-3C-015",
            "category": "3c_digital",
            "sub_category": "mechanical_keyboard",
            "title": "VGN V98Pro 三模无线客制化机械键盘 TTC金粉轴V2 Gasket结构",
            "brand": "VGN",
            "original_price": 329.0,
            "final_price": 249.0,
            "specs": {"switch_type": "linear", "layout": "98_keys", "connectivity": "triple_mode"},
            "highlights": ["TTC金粉轴V2轻柔线性手感", "Gasket消音结构", "全键热插拔", "RGB背光98配列带小键盘"],
            "visual_description": "海盐蓝白双拼PBT球帽，紧凑98键配列保留右侧数字区，窄边框磨砂外壳透出柔和RGB背光。",
            "search_query": "VGN V98Pro 三模机械键盘 蓝白 98配列 商品图",
        },
        {
            "sku_id": "SKU-3C-016",
            "category": "3c_digital",
            "sub_category": "mechanical_keyboard",
            "title": "RK R87 有线单模游戏机械键盘 茶轴微段落 87键紧凑配列",
            "brand": "RK",
            "original_price": 149.0,
            "final_price": 99.0,
            "specs": {"switch_type": "tactile", "layout": "87_keys", "connectivity": "wired"},
            "highlights": ["经典茶轴确认感段落手感", "87键无数字区省桌面空间", "全键热插拔", "键线分离"],
            "visual_description": "复古灰白配色87键无小键盘紧凑机身，双色注塑透光键帽，侧边带Type-C键线分离接口。",
            "search_query": "RK R87 87键 灰白 机械键盘 商品图",
        },
        {
            "sku_id": "SKU-3C-017",
            "category": "3c_digital",
            "sub_category": "mechanical_keyboard",
            "title": "雷柏 V500PRO 104键全尺寸蓝牙双模机械键盘 清脆青轴",
            "brand": "Rapoo",
            "original_price": 179.0,
            "final_price": 129.0,
            "specs": {"switch_type": "clicky", "layout": "104_keys", "connectivity": "bluetooth"},
            "highlights": ["清脆咔哒有声青轴", "104键全尺寸办公游戏兼顾", "铝合金上盖悬浮键帽"],
            "visual_description": "黑色阳极氧化铝合金拉丝面板，104键全尺寸悬浮式键帽设计，金属质感硬朗耐脏。",
            "search_query": "雷柏 V500PRO 104键 黑色 机械键盘 商品图",
        },
        {
            "sku_id": "SKU-3C-018",
            "category": "3c_digital",
            "sub_category": "mechanical_keyboard",
            "title": "迈从 K87 2.4G无线电竞机械键盘 快银线性轴 Gasket热插拔",
            "brand": "MCHOSE",
            "original_price": 239.0,
            "final_price": 179.0,
            "specs": {"switch_type": "linear", "layout": "87_keys", "connectivity": "2.4g"},
            "highlights": ["快银轴极速触发直上直下", "1ms低延迟2.4G无线", "Gasket五层填充消音", "RGB灯效热插拔"],
            "visual_description": "黑莓粉紫渐变侧刻键帽，87键TKL配列，背部配磁吸金属铭牌与2.4G接收器隐藏收纳仓。",
            "search_query": "迈从 K87 87键 渐变 机械键盘 商品图",
        },

        # --- 1.5 nc_headphones (4 SKUs: SKU-3C-019, 020, 021, 022) ---
        {
            "sku_id": "SKU-3C-019",
            "category": "3c_digital",
            "sub_category": "nc_headphones",
            "title": "漫步者 W820NB 双金标版 头戴式主动降噪蓝牙耳机",
            "brand": "Edifier",
            "original_price": 329.0,
            "final_price": 239.0,
            "specs": {"anc_db": 43, "battery_h": 50, "weight_g": 220},
            "highlights": ["-43dB深度主动降噪", "50小时超长续航", "220g轻量化久戴不夹头", "Hi-Res双金标音质"],
            "visual_description": "云岩白哑光耳罩配柔软蛋白皮耳垫，可伸缩头梁内侧加厚海绵，流线型极简头戴式外观。",
            "search_query": "漫步者 W820NB 云岩白 头戴式降噪耳机 商品图",
        },
        {
            "sku_id": "SKU-3C-020",
            "category": "3c_digital",
            "sub_category": "nc_headphones",
            "title": "倍思 H1i 头戴式无线蓝牙主动降噪耳机 空间音频长续航",
            "brand": "Baseus",
            "original_price": 199.0,
            "final_price": 129.0,
            "specs": {"anc_db": 38, "battery_h": 65, "weight_g": 225},
            "highlights": ["65小时跨周超长续航", "-38dB主动降噪", "BISA空间音频", "久戴不夹头"],
            "visual_description": "星耀黑圆润耳壳设计，金属转轴支持折叠收纳，耳罩外侧带磨砂同心圆纹理。",
            "search_query": "倍思 H1i 黑色 头戴式蓝牙耳机 商品图",
        },
        {
            "sku_id": "SKU-3C-021",
            "category": "3c_digital",
            "sub_category": "nc_headphones",
            "title": "声阔 Soundcore Space One 太空舱头戴式深度降噪耳机",
            "brand": "Soundcore",
            "original_price": 599.0,
            "final_price": 449.0,
            "specs": {"anc_db": 48, "battery_h": 55, "weight_g": 258},
            "highlights": ["-48dB自适应深度降噪", "LDAC高清传输", "8度浮动耳罩久戴不夹头", "空间音频"],
            "visual_description": "冰川蓝金属光泽耳罩，亮面音符Logo点缀，8度倾斜人体工学转轴搭配记忆海绵全包耳垫。",
            "search_query": "声阔 Space One 头戴式降噪耳机 蓝色 商品图",
        },
        {
            "sku_id": "SKU-3C-022",
            "category": "3c_digital",
            "sub_category": "nc_headphones",
            "title": "QCY H3 轻羽版 头戴式主动降噪蓝牙耳机 学生党图书馆自习",
            "brand": "QCY",
            "original_price": 149.0,
            "final_price": 89.0,
            "specs": {"anc_db": 30, "battery_h": 35, "weight_g": 195},
            "highlights": ["195g超轻机身久戴不夹头", "自习室人声降噪", "可折叠便携", "有线无线双模"],
            "visual_description": "奶昔白小巧可折叠头戴耳机，轻盈塑料头梁包裹软皮，耳垫亲肤透气适合眼镜党。",
            "search_query": "QCY H3 白色 头戴式降噪耳机 商品图",
        },

        # --- 1.6 usb_hub_cable (3 SKUs: SKU-3C-023, 024, 025) ---
        {
            "sku_id": "SKU-3C-023",
            "category": "3c_digital",
            "sub_category": "usb_hub_cable",
            "title": "绿联 Type-C拓展坞 5合1扩展坞 4K高清HDMI千兆网口分线器",
            "brand": "UGREEN",
            "original_price": 99.0,
            "final_price": 69.0,
            "specs": {"port_count": 5, "protocol": "usb3.0"},
            "highlights": ["5Gbps高速传输", "铝合金外壳高效散热", "即插即用免驱", "支持PD100W供电"],
            "visual_description": "深空灰阳极氧化铝合金条形机身，自带编织Type-C短线，侧面排列3个蓝色USB3.0口、1个HDMI口与1个PD口。",
            "search_query": "绿联 Type-C 五合一拓展坞 铝合金 商品图",
        },
        {
            "sku_id": "SKU-3C-024",
            "category": "3c_digital",
            "sub_category": "usb_hub_cable",
            "title": "倍思 8合1 Type-C高速扩展坞 10Gbps双屏异显分线器",
            "brand": "Baseus",
            "original_price": 199.0,
            "final_price": 149.0,
            "specs": {"port_count": 8, "protocol": "usb3.2gen2"},
            "highlights": ["USB3.2 Gen2 10Gbps秒传大文件", "8口全功能满配", "铝合金外壳", "免驱"],
            "visual_description": "枪灰色一体成型铝合金机身，橙色点缀高速USB3.2接口，带收纳槽可将Type-C连接头隐藏防损。",
            "search_query": "倍思 八合一 Type-C拓展坞 商品图",
        },
        {
            "sku_id": "SKU-3C-025",
            "category": "3c_digital",
            "sub_category": "usb_hub_cable",
            "title": "阿卡西斯 雷电4拓展坞 40Gbps高速四口Type-C专业工作站分线器",
            "brand": "ACASIS",
            "original_price": 499.0,
            "final_price": 369.0,
            "specs": {"port_count": 4, "protocol": "thunderbolt4"},
            "highlights": ["雷电4满血40Gbps带宽", "支持8K/双4K60Hz显示器", "CNC铝合金外壳散热鳍片", "免驱"],
            "visual_description": "银色CNC精雕铝合金方盒机身，表面带凹凸散热鳍片，正面与背面分布4个带闪电标志的雷电4接口。",
            "search_query": "阿卡西斯 雷电4 拓展坞 银色铝合金 商品图",
        },

        # ==================== 2. 换季服饰 (20 SKUs) ====================
        # --- 2.1 windbreaker (4 SKUs: SKU-APP-001, 002, 005, 006) ---
        # C4 Path A: final_price, waterproof_mm, weight_g 由 conflict_pairs.json[C4].path_a.expect 注入
        {
            "sku_id": "SKU-APP-001",
            "category": "apparel",
            "sub_category": "windbreaker",
            "title": "拓路者 10000mm暴雨级防水超轻冲锋衣 280g便携户外防风外套",
            "brand": "PioneerCamp",
            "original_price": 529.0,
            "final_price": None,
            "specs": {},
            "highlights": ["10000mm全压胶暴雨级防水", "280g超轻量化可收纳进袋", "三合一内胆挂载系统", "腋下透气拉链"],
            "visual_description": "山系苍岭绿拼碳黑连帽冲锋衣，全衣防水压胶拉链，立体防风护脖高领，微宽松户外机能剪裁。",
            "search_query": "户外冲锋衣 绿色 防水防风外套 商品图",
        },
        # C4 Path B: final_price, waterproof_mm, weight_g 由 conflict_pairs.json[C4].path_b.expect 注入
        {
            "sku_id": "SKU-APP-002",
            "category": "apparel",
            "sub_category": "windbreaker",
            "title": "迪卡侬 MH100 城市通勤轻便防水冲锋衣外套 5000mm防风夹克",
            "brand": "Decathlon",
            "original_price": 249.0,
            "final_price": None,
            "specs": {},
            "highlights": ["5000mm日常防中雨", "320g轻便易穿", "可收纳进袋", "三合一兼容拉链"],
            "visual_description": "岩石灰简约连帽防水夹克，门襟魔术贴加拉链双层防风，袖口松紧收口，可整件折叠塞入侧袋。",
            "search_query": "迪卡侬 灰色 连帽防水冲锋衣夹克 商品图",
        },
        {
            "sku_id": "SKU-APP-005",
            "category": "apparel",
            "sub_category": "windbreaker",
            "title": "森马 都市防泼水轻薄防风外套 户外休闲连帽夹克",
            "brand": "Semir",
            "original_price": 199.0,
            "final_price": 139.0,
            "specs": {"waterproof_mm": 3000, "weight_g": 370},
            "highlights": ["3000mm防小雨泼水", "370g常规整衣重量", "撞色插肩袖时尚百搭", "可收纳进袋"],
            "visual_description": "米白拼海军蓝撞色连帽风衣夹克，下摆抽绳调节，胸前简约字母印花，偏日常休闲廓形。",
            "search_query": "撞色连帽防风夹克 户外休闲外套 商品图",
        },
        {
            "sku_id": "SKU-APP-006",
            "category": "apparel",
            "sub_category": "windbreaker",
            "title": "骆驼 高海拔专业三合一硬壳冲锋衣 15000mm防水抗寒防风外套",
            "brand": "CAMEL",
            "original_price": 799.0,
            "final_price": 569.0,
            "specs": {"waterproof_mm": 15000, "weight_g": 395},
            "highlights": ["15000mm专业高山硬壳防水", "三合一可拆卸内胆系统", "YKK双向防水拉链", "耐磨防刮"],
            "visual_description": "曜石黑挺括硬壳冲锋衣，高立领防风帽带抽绳卡扣，肩肘部加厚耐磨拼接，配可拆卸内胆挂载扣。",
            "search_query": "骆驼 黑色 三合一冲锋衣 户外外套 商品图",
        },

        # --- 2.2 hoodie (4 SKUs: SKU-APP-003, 004, 007, 008) ---
        # 【R2 修复】：面料克重明确标注为 g/㎡，避免与整衣重量(g)混淆；SKU-APP-008 移除'开衫'字样避免跨类目歧义
        {
            "sku_id": "SKU-APP-003",
            "category": "apparel",
            "sub_category": "hoodie",
            "title": "日系重磅 300g/㎡ 新疆长绒棉宽松落肩圆领卫衣",
            "brand": "UniqCraft",
            "original_price": 169.0,
            "final_price": 129.0,
            "specs": {"fabric_weight_gm2": 300},
            "highlights": ["300g/㎡重磅面料不透肉", "领口双层螺纹久洗不变形", "落肩版型微阔显瘦", "不起球"],
            "visual_description": "高级花灰色无帽圆领套头卫衣，领口加密粗罗纹加固，落肩微阔版型，纯棉毛圈底纹理细腻。",
            "search_query": "日系重磅 花灰色 圆领套头卫衣 商品图",
        },
        {
            "sku_id": "SKU-APP-004",
            "category": "apparel",
            "sub_category": "hoodie",
            "title": "美式复古 400g/㎡ 超重磅三层加绒连帽卫衣 立体挺括帽型",
            "brand": "CleanFit",
            "original_price": 219.0,
            "final_price": 159.0,
            "specs": {"fabric_weight_gm2": 400},
            "highlights": ["400g/㎡重磅硬挺不塌帽", "三层加绒保暖锁温", "落肩版型", "抗静电不起球"],
            "visual_description": "深藏青色重磅连帽卫衣，双层立体大帽兜无抽绳极简设计，腹前袋鼠兜，内层厚实复合银狐绒。",
            "search_query": "美式重磅 藏青色 连帽卫衣 商品图",
        },
        {
            "sku_id": "SKU-APP-007",
            "category": "apparel",
            "sub_category": "hoodie",
            "title": "早秋轻薄 220g/㎡ 纯棉透气基础款圆领卫衣 打底外穿皆宜",
            "brand": "BasicLab",
            "original_price": 99.0,
            "final_price": 69.0,
            "specs": {"fabric_weight_gm2": 220},
            "highlights": ["220g/㎡面料轻盈透气不闷汗", "落肩版型", "柔顺亲肤不起球"],
            "visual_description": "燕麦杏色轻薄圆领长袖卫衣，合身微松剪裁，袖口与下摆弹性罗纹收口，适合春秋季单穿或叠穿。",
            "search_query": "燕麦色 基础款 圆领长袖卫衣 商品图",
        },
        {
            "sku_id": "SKU-APP-008",
            "category": "apparel",
            "sub_category": "hoodie",
            "title": "街头廓形 360g/㎡ 重磅水洗做旧拉链连帽卫衣外套",
            "brand": "StreetVibe",
            "original_price": 189.0,
            "final_price": 139.0,
            "specs": {"fabric_weight_gm2": 360},
            "highlights": ["360g/㎡高克重面料挺括有型", "复古水洗炭灰工艺", "落肩版型", "双向金属拉链不起球"],
            "visual_description": "水洗炭灰色拉链连帽卫衣，做旧复古炒色质感，宽大落肩袖，正面古银色金属拉链贯通。",
            "search_query": "水洗炭灰 拉链连帽卫衣 商品图",
        },

        # --- 2.3 knit_cardigan (4 SKUs: SKU-APP-009, 010, 011, 012) ---
        {
            "sku_id": "SKU-APP-009",
            "category": "apparel",
            "sub_category": "knit_cardigan",
            "title": "慵懒风 310g/㎡ 软糯绵羊毛混纺 V领单排扣针织开衫外套",
            "brand": "WarmKnit",
            "original_price": 199.0,
            "final_price": 149.0,
            "specs": {"fabric_weight_gm2": 310},
            "highlights": ["310g/㎡厚实针织面料", "软糯不扎贴身穿", "抗起球纱线不易起球", "复古琥珀牛角扣"],
            "visual_description": "焦糖驼色V领宽松针织开衫，表面细腻绒感，门襟五粒琥珀纹纽扣，两侧对称插袋。",
            "search_query": "驼色 V领针织开衫 毛衣外套 商品图",
        },
        {
            "sku_id": "SKU-APP-010",
            "category": "apparel",
            "sub_category": "knit_cardigan",
            "title": "春季薄款 200g/㎡ 冰丝棉透气空调衫针织开衫",
            "brand": "UniqCraft",
            "original_price": 129.0,
            "final_price": 89.0,
            "specs": {"fabric_weight_gm2": 200},
            "highlights": ["200g/㎡轻薄透气针织", "软糯不扎", "机洗不易起球不变形"],
            "visual_description": "雾霾蓝细针平纹薄款针织开衫，垂坠感强，简约同色小圆扣，适合办公室空调房叠穿。",
            "search_query": "雾霾蓝 薄款针织开衫 外套 商品图",
        },
        {
            "sku_id": "SKU-APP-011",
            "category": "apparel",
            "sub_category": "knit_cardigan",
            "title": "学院风 380g/㎡ 重磅粗针麻花编织加厚毛衣针织开衫",
            "brand": "CampusClub",
            "original_price": 259.0,
            "final_price": 189.0,
            "specs": {"fabric_weight_gm2": 380},
            "highlights": ["380g/㎡加厚重磅针织保暖", "立体复古麻花绞花织法", "软糯不扎", "不易起球"],
            "visual_description": "奶油白粗针绞花针织开衫，领口袖口藏青撞色条纹装饰，厚实饱满有分量感。",
            "search_query": "学院风 奶油白 麻花针织开衫 毛衣 商品图",
        },
        {
            "sku_id": "SKU-APP-012",
            "category": "apparel",
            "sub_category": "knit_cardigan",
            "title": "日系极简 260g/㎡ 纯棉华夫格肌理立领拉链针织开衫",
            "brand": "BasicLab",
            "original_price": 169.0,
            "final_price": 119.0,
            "specs": {"fabric_weight_gm2": 260},
            "highlights": ["260g/㎡立体华夫格透气肌理", "软糯不扎", "精梳棉不易起球"],
            "visual_description": "炭黑色华夫格凹凸纹理针织开衫，微立领拉链款式，干练利落不显臃肿。",
            "search_query": "黑色 华夫格 拉链针织开衫 商品图",
        },

        # --- 2.4 cargo_pants (4 SKUs: SKU-APP-013, 014, 015, 016) ---
        {
            "sku_id": "SKU-APP-013",
            "category": "apparel",
            "sub_category": "cargo_pants",
            "title": "日系军绿纯棉斜纹多口袋直筒工装裤 水洗耐磨长裤",
            "brand": "Military Lab",
            "original_price": 179.0,
            "final_price": 129.0,
            "specs": {"material": "cotton", "fit": "regular"},
            "highlights": ["100%纯棉加密斜纹布", "大腿立体风琴多口袋", "合身直筒修饰腿型", "耐磨耐洗"],
            "visual_description": "经典橄榄军绿色直筒工装长裤，两侧带翻盖暗扣大口袋，纯棉水洗哑光质感，裤型挺括。",
            "search_query": "军绿色 纯棉直筒多口袋工装裤 商品图",
        },
        {
            "sku_id": "SKU-APP-014",
            "category": "apparel",
            "sub_category": "cargo_pants",
            "title": "户外机能防撕裂尼龙宽松阔腿伞兵工装裤 抽绳束脚两穿",
            "brand": "PioneerCamp",
            "original_price": 219.0,
            "final_price": 159.0,
            "specs": {"material": "nylon", "fit": "loose"},
            "highlights": ["防撕裂格子尼龙耐磨面料", "膝部立体打褶宽松阔腿", "裤脚抽绳束脚垂感阔腿两穿", "多口袋"],
            "visual_description": "暗夜黑宽松廓形机能伞兵裤，膝盖处立体折叠剪裁，微光泽防泼水尼龙面料，裤脚配弹力猪鼻扣抽绳。",
            "search_query": "黑色 尼龙宽松伞兵工装裤 商品图",
        },
        {
            "sku_id": "SKU-APP-015",
            "category": "apparel",
            "sub_category": "cargo_pants",
            "title": "都市通勤弹力棉混纺修身束脚工装九分裤 显高利落",
            "brand": "CleanFit",
            "original_price": 149.0,
            "final_price": 99.0,
            "specs": {"material": "cotton_blend", "fit": "slim"},
            "highlights": ["高弹力棉混纺蹲坐不紧绷", "上宽下窄修身束脚", "隐形拉链多口袋", "耐磨抗皱"],
            "visual_description": "卡其色修身小脚工装裤，罗纹弹力束脚口，侧面贴合式斜插拉链袋，线条干净利落。",
            "search_query": "卡其色 修身束脚工装裤 商品图",
        },
        {
            "sku_id": "SKU-APP-016",
            "category": "apparel",
            "sub_category": "cargo_pants",
            "title": "美式复古高密涤纶抗皱宽松阔腿多袋工装长裤",
            "brand": "StreetVibe",
            "original_price": 139.0,
            "final_price": 89.0,
            "specs": {"material": "polyester", "fit": "loose"},
            "highlights": ["免烫抗皱聚酯纤维", "垂坠阔腿大廓形", "大容量多口袋", "耐磨不粘毛"],
            "visual_description": "深灰色宽松阔腿工装裤，垂感顺滑抗皱面料，双膝加固贴布与立体工装侧袋设计。",
            "search_query": "深灰色 宽松阔腿工装裤 商品图",
        },

        # --- 2.5 tshirt (4 SKUs: SKU-APP-017, 018, 019, 020) ---
        {
            "sku_id": "SKU-APP-017",
            "category": "apparel",
            "sub_category": "tshirt",
            "title": "重磅 280g/㎡ 新疆长绒棉纯白短袖T恤 防凸点不透肉打底衫",
            "brand": "UniqCraft",
            "original_price": 89.0,
            "final_price": 59.0,
            "specs": {"fabric_weight_gm2": 280},
            "highlights": ["280g/㎡高克重重磅不透", "二本针加固领口不变形", "100%新疆长绒棉"],
            "visual_description": "纯净本白色重磅圆领短袖T恤，加宽粗罗纹高密领口，面料厚实硬挺不贴身。",
            "search_query": "重磅纯白圆领短袖T恤 男款女款 商品图",
        },
        {
            "sku_id": "SKU-APP-018",
            "category": "apparel",
            "sub_category": "tshirt",
            "title": "夏日凉感 190g/㎡ 精梳棉透气吸汗基础款短袖T恤",
            "brand": "BasicLab",
            "original_price": 59.0,
            "final_price": 39.0,
            "specs": {"fabric_weight_gm2": 190},
            "highlights": ["190g/㎡轻薄透气排汗", "领口不变形", "亲肤柔软打底"],
            "visual_description": "浅雾蓝基础款合身短袖T恤，丝光柔顺棉质表面，车线工整细腻。",
            "search_query": "浅蓝色 纯棉基础款短袖T恤 商品图",
        },
        {
            "sku_id": "SKU-APP-019",
            "category": "apparel",
            "sub_category": "tshirt",
            "title": "美式高街 310g/㎡ 超重磅水洗炭黑落肩廓形短袖T恤",
            "brand": "StreetVibe",
            "original_price": 119.0,
            "final_price": 79.0,
            "specs": {"fabric_weight_gm2": 310},
            "highlights": ["310g/㎡殿堂级重磅不透", "复古酵洗炭黑质感", "三本针领口不变形"],
            "visual_description": "复古水洗炭黑色五分袖落肩T恤，粗犷美式肌理棉感，立体廓形藏肉显瘦。",
            "search_query": "水洗炭黑 重磅落肩短袖T恤 商品图",
        },
        {
            "sku_id": "SKU-APP-020",
            "category": "apparel",
            "sub_category": "tshirt",
            "title": "索罗娜 Sorona 240g/㎡ 凉感抗皱重磅短袖T恤 久穿不垮",
            "brand": "CleanFit",
            "original_price": 99.0,
            "final_price": 69.0,
            "specs": {"fabric_weight_gm2": 240},
            "highlights": ["240g/㎡黄金克重重磅不透", "Sorona纤维冰感抗皱", "机洗50次领口不变形"],
            "visual_description": "森林绿微宽松短袖T恤，面料自带微弹光泽与垂坠感，久折无折痕。",
            "search_query": "墨绿色 重磅纯色短袖T恤 商品图",
        },

        # ==================== 3. 美妆个护 (15 SKUs) ====================
        # --- 3.1 sunscreen (3 SKUs: SKU-BEA-001, 002, 003) ---
        # C5 Path A: final_price, spf, volume_ml 由 conflict_pairs.json[C5].path_a.expect 注入
        {
            "sku_id": "SKU-BEA-001",
            "category": "beauty",
            "sub_category": "sunscreen",
            "title": "珀莱雅 轻享阳光水感防晒霜 60ml 大容量户外高倍防晒乳 SPF50+ PA++++",
            "brand": "PROYA",
            "original_price": 119.0,
            "final_price": None,
            "specs": {"pa": "pa_plus_plus_plus_plus"},
            "highlights": ["SPF50+ PA++++全波段高倍防晒", "60ml全身涂抹大瓶装", "水感成膜快不搓泥", "不泛白"],
            "visual_description": "鹅黄色扁圆流线型软管瓶身，金色旋盖，正面印有醒目SPF50+ PA++++字样，乳液质地轻盈水润。",
            "search_query": "防晒霜 SPF50+ PA++++ 60ml 黄色瓶 商品图",
        },
        # C5 Path B: final_price, spf, volume_ml 由 conflict_pairs.json[C5].path_b.expect 注入
        {
            "sku_id": "SKU-BEA-002",
            "category": "beauty",
            "sub_category": "sunscreen",
            "title": "蜜丝婷 小黄帽清爽防晒乳 30ml 便携装高倍面部防晒霜 SPF50+ PA++++",
            "brand": "Mistine",
            "original_price": 79.0,
            "final_price": None,
            "specs": {"pa": "pa_plus_plus_plus_plus"},
            "highlights": ["SPF50+ PA++++热带级防晒力", "30ml随身便携", "跟妆服帖不搓泥", "清爽不泛白"],
            "visual_description": "白色管身搭配活力亮黄色斜面瓶盖（小黄帽造型），小巧30ml管体方便放进随身化妆包。",
            "search_query": "蜜丝婷 小黄帽防晒霜 SPF50+ 商品图",
        },
        {
            "sku_id": "SKU-BEA-003",
            "category": "beauty",
            "sub_category": "sunscreen",
            "title": "薇诺娜 清透温和日常通勤防晒乳 30ml 敏感肌物理防晒 SPF30 PA+++",
            "brand": "Winona",
            "original_price": 69.0,
            "final_price": 45.0,
            "specs": {"spf": 30, "pa": "pa_plus_plus_plus", "volume_ml": 30},
            "highlights": ["SPF30 PA+++日常室内通勤", "温和免卸妆", "不搓泥", "不泛白"],
            "visual_description": "医美极简红白配色细长按压管，真空泵头干净卫生，适合日常低强度紫外线防护。",
            "search_query": "薇诺娜 清透防晒乳 红白瓶 商品图",
        },

        # --- 3.2 facial_cream (4 SKUs: SKU-BEA-004, 005, 006, 007) ---
        {
            "sku_id": "SKU-BEA-004",
            "category": "beauty",
            "sub_category": "facial_cream",
            "title": "适乐肤 C霜 三重神经酰胺NP深层保湿修护面霜 50ml 干敏皮救星",
            "brand": "CeraVe",
            "original_price": 109.0,
            "final_price": 79.0,
            "specs": {"key_ingredient": "ceramide_np", "skin_type": "dry_sensitive", "volume_ml": 50},
            "highlights": ["三重神经酰胺NP强韧屏障", "MVE渐进缓释24小时保湿", "干敏皮换季修护屏障", "无香精不闷痘"],
            "visual_description": "蓝白医疗感广口罐装面霜，膏体呈绵密冰淇淋质地，哑光清爽不泛油光。",
            "search_query": "适乐肤 C霜 神经酰胺保湿面霜 蓝白罐 商品图",
        },
        {
            "sku_id": "SKU-BEA-005",
            "category": "beauty",
            "sub_category": "facial_cream",
            "title": "优色林 依克多因舒缓修护霜 50ml 混油皮维稳退红保湿乳霜",
            "brand": "Eucerin",
            "original_price": 169.0,
            "final_price": 129.0,
            "specs": {"key_ingredient": "ectoin", "skin_type": "combination", "volume_ml": 50},
            "highlights": ["高纯度依克多因抵御光老泛红", "轻盈凝乳质地适合混合肌", "修护屏障", "不闷痘"],
            "visual_description": "白色磨砂真空按压瓶配暗红品牌标，乳白色半透明凝霜质地，一抹化水易吸收。",
            "search_query": "舒缓修护面霜 真空按压瓶 护肤品 商品图",
        },
        {
            "sku_id": "SKU-BEA-006",
            "category": "beauty",
            "sub_category": "facial_cream",
            "title": "理肤泉 B5多效修护霜 40ml 泛醇积雪草厚敷急救面霜",
            "brand": "LaRochePosay",
            "original_price": 119.0,
            "final_price": 85.0,
            "specs": {"key_ingredient": "vitamin_b5", "skin_type": "dry_sensitive", "volume_ml": 40},
            "highlights": ["5%高浓度维生素B5泛醇", "修护屏障淡化红印", "干敏皮安心配方"],
            "visual_description": "蓝白药妆风铝塑软管包装，尖嘴细口精准控量，丰润白色乳霜质地。",
            "search_query": "理肤泉 B5修复霜 40ml 软管 商品图",
        },
        {
            "sku_id": "SKU-BEA-007",
            "category": "beauty",
            "sub_category": "facial_cream",
            "title": "科颜氏 高保湿清爽水杨酸控油果冻面霜 60ml 油痘肌无油啫喱",
            "brand": "Kiehls",
            "original_price": 189.0,
            "final_price": 139.0,
            "specs": {"key_ingredient": "salicylic_acid", "skin_type": "oily_acne", "volume_ml": 60},
            "highlights": ["微脂水杨酸温和控油疏通毛孔", "无油果冻啫喱不闷痘", "油痘肌夏季清爽保湿"],
            "visual_description": "冰蓝色半透明磨砂罐，打开为晶莹剔透的水蓝色果冻啫喱凝露，触肤冰凉清爽。",
            "search_query": "蓝色果冻啫喱保湿面霜 60ml 罐装 商品图",
        },

        # --- 3.3 amino_cleanser (3 SKUs: SKU-BEA-008, 009, 010) ---
        # 【R2 修复】：SKU-BEA-008 统一为 100ml 净含量口径，消除 100g 标题 vs 100ml 规格的物理量纲错配
        {
            "sku_id": "SKU-BEA-008",
            "category": "beauty",
            "sub_category": "amino_cleanser",
            "title": "芙丽芳丝 净润氨基酸温和洁面乳液 100ml 日本进口敏感肌洗面奶",
            "brand": "Freeplus",
            "original_price": 119.0,
            "final_price": 79.0,
            "specs": {"volume_ml": 100},
            "highlights": ["100ml温和氨基酸液体洁面", "温和不假滑洗后不紧绷", "无皂基配方"],
            "visual_description": "蓝白极简泵装洁面乳液瓶，白色按压头设计，挤出为细腻珍珠白凝乳液体，加水揉搓泡沫绵密。",
            "search_query": "芙丽芳丝 氨基酸洁面 100ml 蓝白瓶 商品图",
        },
        {
            "sku_id": "SKU-BEA-009",
            "category": "beauty",
            "sub_category": "amino_cleanser",
            "title": "至本 舒颜修护氨基酸洁面泡 150ml 按压自动起泡慕斯",
            "brand": "ZHIBEN",
            "original_price": 68.0,
            "final_price": 49.0,
            "specs": {"volume_ml": 150},
            "highlights": ["150ml大容量一按出云朵泡", "可卸防晒与淡妆", "温和不假滑"],
            "visual_description": "纯白极简圆柱起泡瓶，宽大防滑按压泵头，按压即出绵密丰盈的白色云朵慕斯泡沫。",
            "search_query": "至本 氨基酸洁面慕斯 白色起泡瓶 商品图",
        },
        {
            "sku_id": "SKU-BEA-010",
            "category": "beauty",
            "sub_category": "amino_cleanser",
            "title": "溪木源 山茶花氨基酸洁面乳 80ml 温和控油洗面奶",
            "brand": "Simpcare",
            "original_price": 59.0,
            "final_price": 39.0,
            "specs": {"volume_ml": 80},
            "highlights": ["80ml便携山茶花精粹洗卸合一", "可卸防晒", "温和不假滑"],
            "visual_description": "浅米色磨砂软管配木纹质感瓶盖，管身印有手绘山茶花图案，清新自然风格。",
            "search_query": "溪木源 山茶花氨基酸洗面奶 80ml 商品图",
        },

        # --- 3.4 b5_serum (2 SKUs: SKU-BEA-011, 012) ---
        {
            "sku_id": "SKU-BEA-011",
            "category": "beauty",
            "sub_category": "b5_serum",
            "title": "修丽可 色修B5保湿舒缓精华液 30ml 泛醇透明质酸退红修护",
            "brand": "SkinCeuticals",
            "original_price": 239.0,
            "final_price": 169.0,
            "specs": {"volume_ml": 30},
            "highlights": ["高浓度维生素B5协同小分子玻尿酸", "急救舒缓泛红", "无油水感质地清爽"],
            "visual_description": "透明玻璃滴管瓶，内置淡翠绿色澄澈精华原液，配黑色胶头玻璃吸管精准取量。",
            "search_query": "绿色玻璃滴管瓶 B5舒缓精华液 30ml 商品图",
        },
        {
            "sku_id": "SKU-BEA-012",
            "category": "beauty",
            "sub_category": "b5_serum",
            "title": "乐敦 CC/B5积雪草屏障修护精华液 50ml 大容量院线装",
            "brand": "Mentholatum",
            "original_price": 129.0,
            "final_price": 89.0,
            "specs": {"volume_ml": 50},
            "highlights": ["50ml大碗耐用", "换季舒缓泛红干痒", "质地清爽不粘腻"],
            "visual_description": "医用级深蓝色避光玻璃滴管瓶，50ml大容量瓶身，清透水状精华流动性极佳。",
            "search_query": "蓝色玻璃滴管瓶 B5保湿精华液 50ml 商品图",
        },

        # --- 3.5 salicylic_pads (2 SKUs: SKU-BEA-013, 014) ---
        {
            "sku_id": "SKU-BEA-013",
            "category": "beauty",
            "sub_category": "salicylic_pads",
            "title": "施泰福 Stridex 水杨酸棉片 0.5%温和型 绿色装新手刷酸去闭口",
            "brand": "Stridex",
            "original_price": 69.0,
            "final_price": 45.0,
            "specs": {"concentration_pct": 0.5},
            "highlights": ["0.5%低浓度水杨酸温和建立耐受", "含芦荟舒缓不拔干", "控油去闭口黑头"],
            "visual_description": "白底鲜绿色标签圆罐包装，内含双面纹理纯棉圆片（一面珍珠纹二次清洁、一面平纹湿敷），附防尘小镊子。",
            "search_query": "Stridex 绿色 0.5% 水杨酸棉片 商品图",
        },
        {
            "sku_id": "SKU-BEA-014",
            "category": "beauty",
            "sub_category": "salicylic_pads",
            "title": "博乐达 2.0%超分子水杨酸祛痘调理棉片 进阶强效控油去痘印",
            "brand": "Broda",
            "original_price": 109.0,
            "final_price": 75.0,
            "specs": {"concentration_pct": 2.0},
            "highlights": ["2.0%超分子水杨酸深入毛孔溶脂", "强效控油去闭口脓包痘", "无酒精配方"],
            "visual_description": "红白专业院线风圆盒包装，内浸满高浓度超分子水杨酸精华液的加厚网格棉片。",
            "search_query": "红色 2% 水杨酸祛痘棉片 圆罐 商品图",
        },

        # --- 3.6 lip_balm (1 SKU: SKU-BEA-015) ---
        {
            "sku_id": "SKU-BEA-015",
            "category": "beauty",
            "sub_category": "lip_balm",
            "title": "依泉 柔润护唇膏 4ml 小白管角鲨烷保湿滋润防干裂润唇膏",
            "brand": "Uriage",
            "original_price": 49.0,
            "final_price": 32.0,
            "specs": {"volume_ml": 4},
            "highlights": ["牛油果树果脂修护干裂死皮", "哑光打底不粘腻不猪油嘴", "无香精日夜可用"],
            "visual_description": "纯白色纤细圆管旋转唇膏（小白管），斜切面乳白色膏体，小巧便携。",
            "search_query": "依泉 小白管 柔润护唇膏 商品图",
        },
    ]


def _sync_sku_text_with_spec(sku: Dict[str, Any], ek: str, ev: Any) -> None:
    """
    【F1 / D1 / D2 / D3 修复】：当 conflict_pairs.json 中的规格真源发生变更时，同步更新路径 SKU 的
    title / highlights / visual_description / search_query 受控展示文本，消除机器规格与文本事实的双值漂移：
      - D1: 同步 ports 时联动更新 XC YA 组合编码 (如 2C1A -> 3C1A) 与视觉描述中的 Type-C + USB-A 数量；
      - D2: 同步 capacity_mah 时同时覆盖 mAh、毫安、毫安时、万毫安；
      - D3: 同步 weight_g 时同时覆盖 g/克 与 0.001*g 的 kg/千克/公斤 换算标注（忽略大小写）。
    """
    val_str = f"{int(ev)}" if isinstance(ev, int) or float(ev).is_integer() else f"{ev:g}"

    def _transform(text: str) -> str:
        if ek == "screen_size":
            return re.sub(r"(?<!\d)\d+(?:\.\d+)?(?=\s*(?:寸|英寸))", val_str, text)
        if ek in ("power_w", "output_w"):
            return re.sub(r"(?<!\d)\d+(?:\.\d+)?(?=\s*[wW瓦](?![hH]))", val_str, text)
        if ek == "ports":
            p_int = int(ev)
            cn_port = {1: "单", 2: "双", 3: "三", 4: "四", 5: "五", 6: "六"}.get(p_int, val_str)
            if p_int <= 1:
                c_num, a_num = 1, 0
                ca_code = "1C"
            else:
                c_num, a_num = p_int - 1, 1
                ca_code = f"{c_num}C{a_num}A"
            t = re.sub(r"(?<!\d)\d+\s*[cC]\s*\d+\s*[aA](?![a-zA-Z])", ca_code, text)
            if p_int > 1:
                t = re.sub(r"(?<!\d)1\s*[cC](?=\s*单口)", ca_code, t)
            t = re.sub(r"(?<!\d)\d+(?=\s*(?:快充)?口)", val_str, t)
            t = re.sub(r"[单双两一二三四五六](?=\s*(?:快充)?口)", cn_port, t)
            if p_int >= 2:
                t = re.sub(
                    r"(\d+)(\s*个\s*Type-C(?:接口|口)?\s*(?:与|和|加|\+)\s*)(\d+)(\s*个[^，。；]*?USB-A(?:接口|口)?)",
                    lambda m: f"{c_num}{m.group(2)}{a_num}{m.group(4)}",
                    t,
                    flags=re.IGNORECASE,
                )
                t = re.sub(
                    r"单\s*Type-C\s*(?:接口|口)",
                    f"{c_num}个Type-C口与{a_num}个USB-A口",
                    t,
                    flags=re.IGNORECASE,
                )
            else:
                t = re.sub(
                    r"\d+\s*个\s*Type-C(?:接口|口)?\s*(?:与|和|加|\+)\s*\d+\s*个[^，。；]*?USB-A(?:接口|口)?",
                    "单Type-C接口",
                    t,
                    flags=re.IGNORECASE,
                )
            return t
        if ek == "capacity_mah":
            t = re.sub(
                r"(?<!\d)\d+(?:\.\d+)?(?=\s*(?:mAh|毫安时|毫安)(?![a-zA-Z]))",
                val_str,
                text,
                flags=re.IGNORECASE,
            )
            wan_rep = f"{int(ev) // 10000}万毫安" if int(ev) % 10000 == 0 else f"{val_str}毫安"
            return re.sub(r"(?<!\d)\d+(?:\.\d+)?\s*万\s*(?:mAh|毫安时|毫安)", wan_rep, t, flags=re.IGNORECASE)
        if ek == "weight_g":
            kg_val_str = f"{round(float(ev) / 1000.0, 6):g}"
            t = re.sub(
                r"(?<!\d)\d+(?:\.\d+)?(?=\s*(?:g|克)(?!\s*/\s*㎡|[a-zA-Z]))",
                val_str,
                text,
                flags=re.IGNORECASE,
            )
            return re.sub(
                r"(?<!\d)\d+(?:\.\d+)?(?=\s*(?:kg|千克|公斤)(?!\s*/\s*㎡|[a-zA-Z]))",
                kg_val_str,
                t,
                flags=re.IGNORECASE,
            )
        if ek == "waterproof_mm":
            return re.sub(r"(?<!\d)\d+(?:\.\d+)?(?=\s*mm)", val_str, text, flags=re.IGNORECASE)
        if ek == "spf":
            return re.sub(r"(?<=SPF)\s*\d+(?:\.\d+)?", val_str, text, flags=re.IGNORECASE)
        if ek == "volume_ml":
            return re.sub(
                r"(?<!\d)\d+(?:\.\d+)?(?=\s*(?:ml|毫升)(?![a-zA-Z]))",
                val_str,
                text,
                flags=re.IGNORECASE,
            )
        return text

    for fn in ("title", "visual_description", "search_query"):
        if isinstance(sku.get(fn), str):
            sku[fn] = _transform(sku[fn])
    if isinstance(sku.get("highlights"), list):
        sku["highlights"] = [_transform(str(x)) for x in sku["highlights"]]


def apply_conflict_pairs_truth(
    raw_skus: List[Dict[str, Any]],
    conflict_data: Dict[str, Any],
    spec_registry: Dict[str, Any],
) -> None:
    """
    【R3 / F1 修复】：将 data/conflict_pairs.json 作为 C1~C5 路径商品的唯一真源注入 raw_skus。
    1. 强制校验 path_a/path_b.expect 的键集合严格等于 {'final_price'} ∪ 当前冲突对非价格 hard_constraints.key；
    2. 当 conflict_pairs.json 中修改了 expect 的价格或规格时，同步更新 specs 与全部受控展示文本字段。
    """
    if not isinstance(conflict_data, dict) or not isinstance(conflict_data.get("pairs"), list):
        raise ValueError("conflict_pairs.json 顶层结构非法：缺少 pairs 列表")

    pairs = conflict_data["pairs"]
    expected_ids = ["C1", "C2", "C3", "C4", "C5"]
    actual_ids = [p.get("id") for p in pairs if isinstance(p, dict)]
    if actual_ids != expected_ids:
        raise ValueError(f"conflict_pairs.json 的冲突对 ID 必须严格为 {expected_ids}，实际为 {actual_ids}")

    sku_map: Dict[str, Dict[str, Any]] = {s["sku_id"]: s for s in raw_skus}
    sub_cats_meta = spec_registry["sub_categories"]
    seen_path_skus: set[str] = set()

    for pair in pairs:
        cid = pair["id"]
        sub_cat = pair.get("sub_category")
        if sub_cat not in sub_cats_meta:
            raise ValueError(f"[{cid}] 未知的 sub_category: {sub_cat}")
        declared_specs = sub_cats_meta[sub_cat]["specs"]
        hard_constraints = pair.get("hard_constraints")
        if not isinstance(hard_constraints, list) or len(hard_constraints) < 2:
            raise ValueError(f"[{cid}] hard_constraints 必须为至少包含预算与 1 项规格的列表")
        required_expect_keys = {"final_price"} | {
            hc.get("key") for hc in hard_constraints if isinstance(hc, dict) and isinstance(hc.get("key"), str)
        }

        for branch_name in ("path_a", "path_b"):
            branch = pair.get(branch_name)
            if not isinstance(branch, dict):
                raise ValueError(f"[{cid}] 缺少 {branch_name} 定义")
            sid = branch.get("sku_id")
            expect = branch.get("expect")
            if not sid or sid not in sku_map:
                raise ValueError(f"[{cid}.{branch_name}] 引用了不存在的 sku_id: {sid}")
            if sid in seen_path_skus:
                raise ValueError(f"[{cid}.{branch_name}] 路径 SKU 重复使用: {sid}")
            seen_path_skus.add(sid)

            target_sku = sku_map[sid]
            if target_sku["sub_category"] != sub_cat:
                raise ValueError(
                    f"[{cid}.{branch_name}] SKU {sid} 的 sub_category({target_sku['sub_category']}) != {sub_cat}"
                )
            if not isinstance(expect, dict) or set(expect.keys()) != required_expect_keys:
                raise ValueError(
                    f"[{cid}.{branch_name}.expect] 键集合必须严格等于 {sorted(required_expect_keys)} (F1)，实际为 {sorted(expect.keys()) if isinstance(expect, dict) else expect!r}"
                )

            for ek, ev in expect.items():
                if not isinstance(ev, (int, float)) or isinstance(ev, bool) or not math.isfinite(float(ev)):
                    raise ValueError(f"[{cid}.{branch_name}] expect.{ek}={ev!r} 必须为有限纯数字")
                if ek == "final_price":
                    if float(ev) <= 0 or float(ev) > float(target_sku["original_price"]):
                        raise ValueError(
                            f"[{cid}.{branch_name}] 注入的 final_price({ev}) 违反 0 < final_price <= original_price({target_sku['original_price']})"
                        )
                    target_sku["final_price"] = float(ev)
                else:
                    if ek not in declared_specs:
                        raise ValueError(
                            f"[{cid}.{branch_name}] expect 规格键 '{ek}' 未在 {sub_cat} 的 spec_registry 中声明"
                        )
                    sdef = declared_specs[ek]
                    if sdef.get("integer") is True:
                        if not isinstance(ev, int) or isinstance(ev, bool):
                            raise ValueError(f"[{cid}.{branch_name}] 离散规格 {ek}={ev!r} 必须为整数(int)")
                        target_sku["specs"][ek] = int(ev)
                    else:
                        target_sku["specs"][ek] = ev
                    _sync_sku_text_with_spec(target_sku, ek, target_sku["specs"][ek])

    # 校验所有 SKU 的 final_price 与 specs 均已完备
    for s in raw_skus:
        sid = s["sku_id"]
        if s.get("final_price") is None:
            raise ValueError(f"[{sid}] final_price 未赋值（可能被移出了 conflict_pairs.json）")
        req_keys = set(sub_cats_meta[s["sub_category"]]["specs"].keys())
        act_keys = set(s["specs"].keys())
        if req_keys != act_keys:
            raise ValueError(f"[{sid}] specs 键不完整: 期望 {req_keys}，实际 {act_keys}")


def build_platform_rules() -> List[Dict[str, Any]]:
    """
    生成平台售后与价保规则 JSON（与 deploy/init.sql 完全对齐）。
    【R6 修复】：
      1. 冻结唯一确定性时间契约：统一按精确时间戳滚动小时差判定（7天=168小时，15天=360小时，含端点 <=），移除“次日零时”与“滚动小时”并存的矛盾表述；
      2. 补齐结构化判定字段（anchor_field、fallback_anchor_field、window_hours、include_boundary、allow_unsigned、source_reference），使自然语言文案与机器参数 100% 同构。
    """
    return [
        {
            "rule_id": "RULE-AFTER-001",
            "rule_type": "aftersales",
            "rule_version": "v2026.10",
            "title": "7天无理由退换货规则（受控基准简化自淘宝公开规则）",
            "max_days": 7,
            "window_hours": 168,
            "anchor_field": "signed_at",
            "fallback_anchor_field": None,
            "include_boundary": True,
            "allow_unsigned": False,
            "require_unopened": True,
            "content": (
                "【确定性核验契约】仅限已签收订单（order_status='delivered' 且 signed_at 非空）："
                "自实际签收时刻（signed_at，Asia/Shanghai 时区）起算滚动 168 小时内（0 <= elapsed_hours <= 168.0，含 168 小时整点边界），"
                "且商品完好未拆封（is_opened=false），可申请7天无理由退货；"
                "若订单尚未签收（pending/shipped）、已拆封（is_opened=true）或距签收时刻超过 168 小时（elapsed_hours > 168.0），则不支持7天无理由退货。"
            ),
            "source_reference": {
                "platform": "淘宝网公开规则中心",
                "source_title": "《淘宝网七天无理由退货规范》",
                "source_url": "https://rulechannel.taobao.com/",
                "retrieved_at": "2026-10-10",
                "mock_simplification_note": "原公开规则以签收次日零时起算满168小时且按类目区分完好标准；本项目作为求职演示基准，统一简化为自 signed_at 精确时刻起算滚动 168.0 小时（含端点）且统一以布尔字段 is_opened=false 表征未拆封完好，消除跨日零时与自然日折算歧义。"
            },
        },
        {
            "rule_id": "RULE-PRICE-001",
            "rule_type": "price_guarantee",
            "rule_version": "v2026.10",
            "title": "15天全程价格保护规则（受控基准简化自淘宝公开规则）",
            "max_days": 15,
            "window_hours": 360,
            "anchor_field": "signed_at",
            "fallback_anchor_field": "paid_at",
            "include_boundary": True,
            "allow_unsigned": True,
            "require_unopened": False,
            "content": (
                "【确定性核验契约】购买带价保标识（has_price_guarantee=true）的商品后："
                "已签收订单自实际签收时刻（signed_at）起算滚动 360 小时内（0 <= elapsed_hours <= 360.0，含边界）；"
                "未签收订单（pending/shipped 且 signed_at 为空）自实际付款时刻（paid_at）起算滚动 360 小时内（0 <= elapsed_hours <= 360.0）。"
                "在有效期内若同一 SKU 当前到手价（final_price）低于历史实付金额（pay_amount），无论是否拆封（is_opened 不限），均可申请退还正差额（refund_diff = pay_amount - final_price > 0）。"
            ),
            "source_reference": {
                "platform": "淘宝网公开规则中心",
                "source_title": "《淘宝网价格保护服务规范》",
                "source_url": "https://rulechannel.taobao.com/",
                "retrieved_at": "2026-10-10",
                "mock_simplification_note": "原公开规则排除限时秒杀/满减凑单/百亿补贴等复杂除外场景；本项目已砍掉算券凑单模块，统一简化为对比订单历史实付价 pay_amount 与商品当前 final_price 的直接差额，所有演示订单默认 has_price_guarantee=true。"
            },
        },
        {
            "rule_id": "RULE-SHIP-001",
            "rule_type": "shipping",
            "rule_version": "v2026.10",
            "title": "退货运费险与极速退款服务说明（受控基准简化自淘宝公开规则）",
            "depends_on_rule_id": "RULE-AFTER-001",
            "max_days": 7,
            "window_hours": 168,
            "anchor_field": "signed_at",
            "fallback_anchor_field": None,
            "include_boundary": True,
            "allow_unsigned": False,
            "require_unopened": True,
            "content": (
                "【确定性核验契约】凡持有运费险标识（has_freight_insurance=true）且通过 RULE-AFTER-001（7天无理由退货资格核验，168 小时窗口内未拆封）的订单，"
                "选择官方合作快递上门取件可直接抵扣首重退货运费；快递员揽收上传有效物流单号后触发极速垫付退款。"
            ),
            "source_reference": {
                "platform": "淘宝网公开规则中心",
                "source_title": "《退货运费险服务规范》",
                "source_url": "https://rulechannel.taobao.com/",
                "retrieved_at": "2026-10-10",
                "mock_simplification_note": "简化为布尔字段 has_freight_insurance=true 且前置依赖 RULE-AFTER-001 判定通过，不引入真实保险理赔风控中台。"
            },
        },
    ]


def build_mock_orders(sku_by_id: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    生成基准测试订单 JSON（覆盖 init.sql 种子订单 + R6(d) 要求的全部独立售后与价保边界隔离用例）。
    【R6 & S11 修复】：
      1. 固定 reference_time = '2026-10-10T12:00:00+08:00'；
      2. 补齐 paid_offset_hours 与确定性物化的 created_at / paid_at / signed_at；
      3. 补齐 has_price_guarantee / has_freight_insurance 与 expected_eligibility 确定性预期真值；
      4. S11 修复：price_diff_amount 采用独立硬编码字面量（40.0 / 130.0），不与闸门减法算式同源。
    """
    raw_orders = [
        # 1. init.sql 种子订单 1：运输中未签收 (不支持7天无理由退货；15天价保按 paid_at 起算但无差价)
        {
            "order_id": "ORD-20261001-001",
            "scenario_tag": "shipped_unsigned_no_price_diff",
            "user_id": "U1001",
            "sku_id": "SKU-3C-008",
            "pay_amount": float(sku_by_id["SKU-3C-008"]["final_price"]),
            "order_status": "shipped",
            "is_opened": False,
            "has_price_guarantee": True,
            "has_freight_insurance": True,
            "rule_version": "v2026.10",
            "logistics_company": "顺丰速运",
            "tracking_number": "SF142857001",
            "latest_logistics_event": "【北京市】快件已到达北京海淀集散中心，正在分拣中",
            "created_offset_hours": -48.0,
            "paid_offset_hours": -47.5,
            "signed_offset_hours": None,
            "expected_eligibility": {
                "return_7d_eligible": False,
                "return_7d_reason_code": "NOT_SIGNED_YET",
                "price_guarantee_15d_eligible": False,
                "price_guarantee_15d_reason_code": "NO_PRICE_DROP",
                "price_diff_amount": 0.0,
            },
        },
        # 2. init.sql 种子订单 2：已签收 72h 且未拆封 (7天无理由退货通过)
        {
            "order_id": "ORD-20261001-002",
            "scenario_tag": "delivered_within_7d_unopened",
            "user_id": "U1001",
            "sku_id": "SKU-3C-002",
            "pay_amount": float(sku_by_id["SKU-3C-002"]["final_price"]),
            "order_status": "delivered",
            "is_opened": False,
            "has_price_guarantee": True,
            "has_freight_insurance": True,
            "rule_version": "v2026.10",
            "logistics_company": "中通快递",
            "tracking_number": "ZT889900112",
            "latest_logistics_event": "【已签收】已由小区丰巢快递柜代收，取件码 5842",
            "created_offset_hours": -120.0,
            "paid_offset_hours": -119.5,
            "signed_offset_hours": -72.0,
            "expected_eligibility": {
                "return_7d_eligible": True,
                "return_7d_reason_code": "ELIGIBLE",
                "price_guarantee_15d_eligible": False,
                "price_guarantee_15d_reason_code": "NO_PRICE_DROP",
                "price_diff_amount": 0.0,
            },
        },
        # 3. init.sql 种子订单 3：待揽收未发货
        {
            "order_id": "ORD-20261001-003",
            "scenario_tag": "pending_unshipped",
            "user_id": "U1001",
            "sku_id": "SKU-APP-003",
            "pay_amount": float(sku_by_id["SKU-APP-003"]["final_price"]),
            "order_status": "pending",
            "is_opened": False,
            "has_price_guarantee": True,
            "has_freight_insurance": True,
            "rule_version": "v2026.10",
            "logistics_company": "待揽收",
            "tracking_number": None,
            "latest_logistics_event": "商家正在打包备货中",
            "created_offset_hours": -6.0,
            "paid_offset_hours": -5.8,
            "signed_offset_hours": None,
            "expected_eligibility": {
                "return_7d_eligible": False,
                "return_7d_reason_code": "NOT_SIGNED_YET",
                "price_guarantee_15d_eligible": False,
                "price_guarantee_15d_reason_code": "NO_PRICE_DROP",
                "price_diff_amount": 0.0,
            },
        },
        # 4. 综合拒绝订单：已签收 200h (>168h) 且已拆封
        {
            "order_id": "ORD-20261001-004",
            "scenario_tag": "delivered_expired_7d_and_opened",
            "user_id": "U1001",
            "sku_id": "SKU-BEA-001",
            "pay_amount": float(sku_by_id["SKU-BEA-001"]["final_price"]),
            "order_status": "delivered",
            "is_opened": True,
            "has_price_guarantee": True,
            "has_freight_insurance": True,
            "rule_version": "v2026.10",
            "logistics_company": "圆通速递",
            "tracking_number": "YT667788990",
            "latest_logistics_event": "【已签收】本人签收，感谢使用圆通速递",
            "created_offset_hours": -240.0,
            "paid_offset_hours": -239.5,
            "signed_offset_hours": -200.0,
            "expected_eligibility": {
                "return_7d_eligible": False,
                "return_7d_reason_code": "EXPIRED_AND_OPENED",
                "price_guarantee_15d_eligible": False,
                "price_guarantee_15d_reason_code": "NO_PRICE_DROP",
                "price_diff_amount": 0.0,
            },
        },
        # 5. 【R6(d) 隔离分支 1】：窗口内 (48h <= 168h) 但已拆封 (is_opened=True)，单独验证拆封拒绝原因
        {
            "order_id": "ORD-20261001-005",
            "scenario_tag": "delivered_within_7d_but_opened",
            "user_id": "U1001",
            "sku_id": "SKU-BEA-004",
            "pay_amount": float(sku_by_id["SKU-BEA-004"]["final_price"]),
            "order_status": "delivered",
            "is_opened": True,
            "has_price_guarantee": True,
            "has_freight_insurance": True,
            "rule_version": "v2026.10",
            "logistics_company": "京东快递",
            "tracking_number": "JD009988776",
            "latest_logistics_event": "【已签收】前台本人签收",
            "created_offset_hours": -96.0,
            "paid_offset_hours": -95.5,
            "signed_offset_hours": -48.0,
            "expected_eligibility": {
                "return_7d_eligible": False,
                "return_7d_reason_code": "OPENED_NOT_ALLOWED",
                "price_guarantee_15d_eligible": False,
                "price_guarantee_15d_reason_code": "NO_PRICE_DROP",
                "price_diff_amount": 0.0,
            },
        },
        # 6. 【R6(d) 隔离分支 2】：未拆封完好 (is_opened=False) 但超出7天窗口 (180h > 168h)，单独验证超时拒绝原因
        {
            "order_id": "ORD-20261001-006",
            "scenario_tag": "delivered_expired_7d_but_unopened",
            "user_id": "U1001",
            "sku_id": "SKU-3C-019",
            "pay_amount": float(sku_by_id["SKU-3C-019"]["final_price"]),
            "order_status": "delivered",
            "is_opened": False,
            "has_price_guarantee": True,
            "has_freight_insurance": True,
            "rule_version": "v2026.10",
            "logistics_company": "顺丰速运",
            "tracking_number": "SF142857099",
            "latest_logistics_event": "【已签收】顺丰智能柜签收",
            "created_offset_hours": -220.0,
            "paid_offset_hours": -219.5,
            "signed_offset_hours": -180.0,
            "expected_eligibility": {
                "return_7d_eligible": False,
                "return_7d_reason_code": "WINDOW_EXPIRED_7D",
                "price_guarantee_15d_eligible": False,
                "price_guarantee_15d_reason_code": "NO_PRICE_DROP",
                "price_diff_amount": 0.0,
            },
        },
        # 7. 【R6(d) 边界分支 3】：7天无理由正好 168.0 小时精确到期边界 (未拆封 -> 含端点通过)
        {
            "order_id": "ORD-20261001-007",
            "scenario_tag": "delivered_exact_168h_boundary_unopened",
            "user_id": "U1001",
            "sku_id": "SKU-3C-015",
            "pay_amount": float(sku_by_id["SKU-3C-015"]["final_price"]),
            "order_status": "delivered",
            "is_opened": False,
            "has_price_guarantee": True,
            "has_freight_insurance": True,
            "rule_version": "v2026.10",
            "logistics_company": "中通快递",
            "tracking_number": "ZT889900777",
            "latest_logistics_event": "【已签收】驿站签收",
            "created_offset_hours": -210.0,
            "paid_offset_hours": -209.5,
            "signed_offset_hours": -168.0,
            "expected_eligibility": {
                "return_7d_eligible": True,
                "return_7d_reason_code": "ELIGIBLE",
                "price_guarantee_15d_eligible": False,
                "price_guarantee_15d_reason_code": "NO_PRICE_DROP",
                "price_diff_amount": 0.0,
            },
        },
        # 8. 【R6(d) & S11 价保正差额分支 4】：大促前以原价 ¥169 购入 SKU-3C-010，签收 120h 已拆封，当前直降至 ¥129，独立字面量预期退差价 40.0
        {
            "order_id": "ORD-20261001-008",
            "scenario_tag": "price_guarantee_eligible_with_positive_diff",
            "user_id": "U1001",
            "sku_id": "SKU-3C-010",
            "pay_amount": 169.0,
            "order_status": "delivered",
            "is_opened": True,
            "has_price_guarantee": True,
            "has_freight_insurance": True,
            "rule_version": "v2026.10",
            "logistics_company": "顺丰速运",
            "tracking_number": "SF142857888",
            "latest_logistics_event": "【已签收】本人签收",
            "created_offset_hours": -150.0,
            "paid_offset_hours": -149.5,
            "signed_offset_hours": -120.0,
            "expected_eligibility": {
                "return_7d_eligible": False,
                "return_7d_reason_code": "OPENED_NOT_ALLOWED",
                "price_guarantee_15d_eligible": True,
                "price_guarantee_15d_reason_code": "ELIGIBLE",
                "price_diff_amount": 40.0,
            },
        },
        # 9. 【R6(d) & S11 15天价保精确边界分支 5】：签收正好 360.0 小时整点边界，历史实付原价 ¥529，当前 ¥399，独立字面量预期退差价 130.0
        {
            "order_id": "ORD-20261001-009",
            "scenario_tag": "price_guarantee_exact_360h_boundary",
            "user_id": "U1001",
            "sku_id": "SKU-APP-001",
            "pay_amount": 529.0,
            "order_status": "delivered",
            "is_opened": True,
            "has_price_guarantee": True,
            "has_freight_insurance": True,
            "rule_version": "v2026.10",
            "logistics_company": "韵达快递",
            "tracking_number": "YD334455667",
            "latest_logistics_event": "【已签收】门卫签收",
            "created_offset_hours": -400.0,
            "paid_offset_hours": -399.5,
            "signed_offset_hours": -360.0,
            "expected_eligibility": {
                "return_7d_eligible": False,
                "return_7d_reason_code": "EXPIRED_AND_OPENED",
                "price_guarantee_15d_eligible": True,
                "price_guarantee_15d_reason_code": "ELIGIBLE",
                "price_diff_amount": 130.0,
            },
        },
        # 10. 【R6(d) 15天价保超时拒绝分支 6】：签收 370.0 小时 (>360h)，虽有历史差价但超出15天价保窗口
        {
            "order_id": "ORD-20261001-010",
            "scenario_tag": "price_guarantee_expired_over_360h",
            "user_id": "U1001",
            "sku_id": "SKU-APP-004",
            "pay_amount": 219.0,
            "order_status": "delivered",
            "is_opened": True,
            "has_price_guarantee": True,
            "has_freight_insurance": True,
            "rule_version": "v2026.10",
            "logistics_company": "圆通速递",
            "tracking_number": "YT667788010",
            "latest_logistics_event": "【已签收】丰巢签收",
            "created_offset_hours": -410.0,
            "paid_offset_hours": -409.5,
            "signed_offset_hours": -370.0,
            "expected_eligibility": {
                "return_7d_eligible": False,
                "return_7d_reason_code": "EXPIRED_AND_OPENED",
                "price_guarantee_15d_eligible": False,
                "price_guarantee_15d_reason_code": "WINDOW_EXPIRED_15D",
                "price_diff_amount": 0.0,
            },
        },
    ]

    materialized_orders: List[Dict[str, Any]] = []
    for od in raw_orders:
        sid = od["sku_id"]
        sku_title = sku_by_id[sid]["title"]
        created_at = format_offset_iso(REFERENCE_DT, od["created_offset_hours"])
        paid_at = format_offset_iso(REFERENCE_DT, od["paid_offset_hours"])
        signed_at = format_offset_iso(REFERENCE_DT, od["signed_offset_hours"])
        materialized_orders.append(
            {
                "order_id": od["order_id"],
                "scenario_tag": od["scenario_tag"],
                "user_id": od["user_id"],
                "sku_id": sid,
                "sku_title": sku_title,
                "pay_amount": od["pay_amount"],
                "order_status": od["order_status"],
                "is_opened": od["is_opened"],
                "has_price_guarantee": od["has_price_guarantee"],
                "has_freight_insurance": od["has_freight_insurance"],
                "rule_version": od["rule_version"],
                "logistics_company": od["logistics_company"],
                "tracking_number": od["tracking_number"],
                "latest_logistics_event": od["latest_logistics_event"],
                "reference_time": REFERENCE_TIME_ISO,
                "created_offset_hours": od["created_offset_hours"],
                "paid_offset_hours": od["paid_offset_hours"],
                "signed_offset_hours": od["signed_offset_hours"],
                "created_at": created_at,
                "paid_at": paid_at,
                "signed_at": signed_at,
                "expected_eligibility": od["expected_eligibility"],
            }
        )
    return materialized_orders


def load_image_manifest_and_cos_urls(catalog_path: Path) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Optional[str]]]:
    """读取 data/image_manifest.json（若存在）与已有 COS 地址，支持模块二图文校准状态与资产无损合并。"""
    manifest_map: Dict[str, Dict[str, Any]] = {}
    cos_map: Dict[str, Optional[str]] = {}
    manifest_path = DATA_DIR / "image_manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(manifest, dict):
                for sid, meta in manifest.items():
                    if isinstance(meta, dict):
                        manifest_map[sid] = meta
                        if meta.get("cos_image_url"):
                            cos_map[sid] = meta["cos_image_url"]
        except Exception:
            pass
    if catalog_path.exists():
        try:
            existing = json.loads(catalog_path.read_text(encoding="utf-8"))
            if isinstance(existing, list):
                for item in existing:
                    if isinstance(item, dict) and item.get("sku_id") and item.get("cos_image_url"):
                        cos_map.setdefault(item["sku_id"], item["cos_image_url"])
        except Exception:
            pass
    return manifest_map, cos_map


def main() -> None:
    spec_registry_path = DATA_DIR / "spec_registry.json"
    conflict_pairs_path = DATA_DIR / "conflict_pairs.json"

    spec_registry = json.loads(spec_registry_path.read_text(encoding="utf-8"))
    conflict_data = json.loads(conflict_pairs_path.read_text(encoding="utf-8"))
    sub_categories_meta = spec_registry["sub_categories"]

    raw_skus = get_raw_skus()
    # 从唯一真源 conflict_pairs.json 动态合并 C1~C5 路径商品的价格与规格
    apply_conflict_pairs_truth(raw_skus, conflict_data, spec_registry)

    # 按 sku_id 排序，保持文件整洁稳定
    raw_skus.sort(key=lambda x: x["sku_id"])

    catalog_path = DATA_DIR / "ecommerce_catalog_60skus.json"
    manifest_map, existing_cos_map = load_image_manifest_and_cos_urls(catalog_path)

    enriched_skus: List[Dict[str, Any]] = []
    for item in raw_skus:
        sub_cat = item["sub_category"]
        sub_meta = sub_categories_meta[sub_cat]
        sid = item["sku_id"]

        m_entry = manifest_map.get(sid, {})
        has_valid_manifest = bool(
            isinstance(m_entry, dict)
            and m_entry.get("sha256")
            and m_entry.get("source_url")
            and m_entry.get("magic_format") in ("JPEG", "PNG")
        )
        if has_valid_manifest and m_entry.get("calibrated_visual_description"):
            item["visual_description"] = str(m_entry["calibrated_visual_description"])

        v_source = (
            "calibrated_from_retained_image"
            if has_valid_manifest
            else "synthetic_draft_pending_image_calibration"
        )

        sku_obj: Dict[str, Any] = {
            "sku_id": sid,
            "category": item["category"],
            "sub_category": sub_cat,
            "title": item["title"],
            "brand": item["brand"],
            "original_price": item["original_price"],
            "final_price": item["final_price"],
            "specs": item["specs"],
            "highlights": item["highlights"],
            "visual_description": item["visual_description"],
            "visual_description_source": v_source,
            "is_synthetic_benchmark": True,
            "search_query": item["search_query"],
            "retrieval_text": build_retrieval_text(item, sub_meta),
            "image_path": f"/static/images/{sid}.jpg",
            "cos_image_url": existing_cos_map.get(sid),
            "inventory": 100,
            "status": "active",
            "warehouse_city": "广州自营数码仓" if item["category"] == "3c_digital" else ("华东自营服饰仓" if item["category"] == "apparel" else "广州自营美妆保税仓"),
            "shipping_sla_hours": 24,
            "inventory_status": "in_stock",
        }
        enriched_skus.append(sku_obj)

    sku_by_id = {s["sku_id"]: s for s in enriched_skus}

    catalog_path.write_text(
        json.dumps(enriched_skus, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    rules_path = DATA_DIR / "platform_rules.json"
    rules_path.write_text(
        json.dumps(build_platform_rules(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    orders_path = DATA_DIR / "mock_orders.json"
    orders_path.write_text(
        json.dumps(build_mock_orders(sku_by_id), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"[OK] Generated {len(enriched_skus)} SKUs (with conflict_pairs.json single source of truth) -> {catalog_path}")
    print(f"[OK] Generated platform rules -> {rules_path}")
    print(f"[OK] Generated mock orders -> {orders_path}")


if __name__ == "__main__":
    main()
