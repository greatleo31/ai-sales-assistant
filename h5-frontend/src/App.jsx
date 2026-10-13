import React, { useState, useRef, useEffect } from 'react';
import { CustomerRichText } from './utils/streamSanitizer';

/* ==========================================================================
   0. 全局唯一 ID 生成器与生产级数值精度防漂移工具函数
   ========================================================================== */
let globalMessageCounter = 0;
const nextSafeId = (prefix) =>
  `${prefix}-${Date.now()}-${(++globalMessageCounter).toString(36)}`;

function safeFormatPrice(val, defaultVal = '0') {
  const num = Number(val);
  if (!Number.isFinite(num)) return defaultVal;
  return num.toFixed(1).replace(/\.0$/, '');
}

function safeCalcDiscount(orig, final) {
  const o = Math.round((Number(orig) || 0) * 100);
  const f = Math.round((Number(final) || 0) * 100);
  const diff = Math.max(0, o - f);
  return (diff / 100).toFixed(1).replace(/\.0$/, '');
}

/**
 * 浏览器端 Canvas 自适应图片压缩：
 *   - 小于 350KB 的 JPEG/PNG 保留原始字节（保障 60-SKU 官方商品图 SHA-256 0ms 精确指纹匹配）；
 *   - 手机拍照/高清相册大图自动等比缩放至 <= 1024px 并以 0.82 质量编码为 JPEG Base64，实现秒级上传。
 */
function compressImageToDataUrl(file, maxEdge = 1024, quality = 0.82) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error('读取图片失败'));
    reader.onload = () => {
      const rawDataUrl = reader.result;
      if (
        typeof rawDataUrl === 'string' &&
        file.size <= 350 * 1024 &&
        /^data:image\/(jpeg|jpg|png|webp);base64,/i.test(rawDataUrl)
      ) {
        resolve(rawDataUrl);
        return;
      }
      const img = new Image();
      img.onerror = () => {
        if (typeof rawDataUrl === 'string') resolve(rawDataUrl);
        else reject(new Error('解码图片失败'));
      };
      img.onload = () => {
        let w = img.naturalWidth || img.width || 800;
        let h = img.naturalHeight || img.height || 800;
        if (w > maxEdge || h > maxEdge) {
          const ratio = Math.min(maxEdge / w, maxEdge / h);
          w = Math.max(1, Math.round(w * ratio));
          h = Math.max(1, Math.round(h * ratio));
        }
        const canvas = document.createElement('canvas');
        canvas.width = w;
        canvas.height = h;
        const ctx = canvas.getContext('2d');
        if (!ctx) {
          resolve(rawDataUrl);
          return;
        }
        ctx.fillStyle = '#FFFFFF';
        ctx.fillRect(0, 0, w, h);
        ctx.drawImage(img, 0, 0, w, h);
        resolve(canvas.toDataURL('image/jpeg', quality));
      };
      img.src = rawDataUrl;
    };
    reader.readAsDataURL(file);
  });
}

/* ==========================================================================
   1. 精简矢量图标库（严格克制，仅用于顶栏、卡片底部拇指/重答、输入框与订单弹窗）
   ========================================================================== */
const IconMore = () => (
  <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
    <circle cx="5" cy="12" r="1.8" />
    <circle cx="12" cy="12" r="1.8" />
    <circle cx="19" cy="12" r="1.8" />
  </svg>
);

const IconBack = () => (
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M19 12H5M12 19l-7-7 7-7" />
  </svg>
);

const IconSearchBadge = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
    <circle cx="11" cy="11" r="7" />
    <path d="m20 20-3.5-3.5" />
  </svg>
);

const IconOrderBadge = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z" />
    <path d="m3.3 7 8.7 5 8.7-5" />
  </svg>
);

const IconCouponBadge = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.3" strokeLinecap="round" strokeLinejoin="round">
    <path d="M8 7h8M8 11h8M12 7v10M9 4h6l3 3v13a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V7l3-3Z" />
  </svg>
);

const IconMsgBadge = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z" />
  </svg>
);

const IconThumbUp = () => (
  <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
    <path d="M7 10v12" />
    <path d="M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 2a3.13 3.13 0 0 1 3 3.88Z" />
  </svg>
);

const IconThumbDown = () => (
  <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
    <path d="M17 14V2" />
    <path d="M9 18.12 10 14H4.17a2 2 0 0 1-1.92-2.56l2.33-8A2 2 0 0 1 6.5 2H20a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-2.76a2 2 0 0 0-1.79 1.11L12 22a3.13 3.13 0 0 1-3-3.88Z" />
  </svg>
);

const IconRetry = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" />
    <path d="M3 3v5h5" />
  </svg>
);

const IconArrowDown = () => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M12 5v14M19 12l-7 7-7-7" />
  </svg>
);

const IconVoice = () => (
  <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#23262B" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
    <circle cx="12" cy="12" r="9" />
    <path d="M9.5 9.5a3.5 3.5 0 0 1 0 5M12.5 7.5a6 6 0 0 1 0 9" />
    <circle cx="7.5" cy="12" r="0.8" fill="#23262B" />
  </svg>
);

const IconBagPlus = ({ size = 22 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="#23262B" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
    <path d="M6 2 3 6v14a2 2 0 0 0 2 2h9" />
    <path d="M3 6h18" />
    <path d="M16 10a4 4 0 0 1-8 0" />
    <path d="M19 15v6M16 18h6" />
  </svg>
);

const IconCamera = ({ size = 26 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="#23262B" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
    <path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3l-2.5-3z" />
    <circle cx="12" cy="13" r="3.5" />
  </svg>
);

const IconPhoto = ({ size = 26 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="#23262B" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
    <rect x="3" y="3" width="18" height="18" rx="2.5" ry="2.5" />
    <circle cx="8.5" cy="8.5" r="1.6" />
    <path d="m21 15-5-5L5 21" />
  </svg>
);

const IconPlusCircle = () => (
  <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#23262B" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
    <circle cx="12" cy="12" r="9" />
    <path d="M12 8v8M8 12h8" />
  </svg>
);

const IconCloseCircle = () => (
  <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#23262B" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
    <circle cx="12" cy="12" r="9" />
    <path d="m15 9-6 6M9 9l6 6" />
  </svg>
);

/* 首屏大插画与回答卡片左上角圆形助手头像 */
const HuigouAssistantIllustration = () => (
  <svg className="hg-mascot-svg" viewBox="0 0 150 130" fill="none" xmlns="http://www.w3.org/2000/svg">
    <circle cx="84" cy="72" r="46" fill="#FFEFE5" />
    <path d="M30 48L33 41L36 48L43 51L36 54L33 61L30 54L23 51L30 48Z" fill="#FFA928" />
    <circle cx="124" cy="36" r="14" fill="#FF661A" />
    <text x="124" y="40" textAnchor="middle" fill="#FFFFFF" fontSize="11" fontWeight="700" fontFamily="Outfit, sans-serif">Hi</text>
    <path d="M50 124C52 98 66 86 84 86C102 86 116 98 118 124H50Z" fill="#FFFFFF" stroke="#E7E9EE" strokeWidth="1.5" />
    <path d="M58 92C66 85 102 85 110 92L103 114L84 103L65 114L58 92Z" fill="#FF7315" />
    <path d="M77 98L72 122H82L84 104L86 122H96L91 98H77Z" fill="#F25D07" />
    <rect x="78" y="72" width="12" height="16" rx="6" fill="#FAD5C0" />
    <ellipse cx="84" cy="54" rx="21" ry="23" fill="#FCE0CE" />
    <circle cx="84" cy="24" r="9" fill="#232226" />
    <path d="M76 28C81 26 87 26 92 28" stroke="#FF7315" strokeWidth="3" strokeLinecap="round" />
    <path d="M63 52C63 35 73 27 84 27C95 27 105 35 105 52C102 42 93 37 84 37C75 37 66 42 63 52Z" fill="#232226" />
    <path d="M73 49C75 47 78 47 80 49" stroke="#232226" strokeWidth="1.8" strokeLinecap="round" />
    <path d="M88 49C90 47 93 47 95 49" stroke="#232226" strokeWidth="1.8" strokeLinecap="round" />
    <circle cx="76.5" cy="53.5" r="2" fill="#232226" />
    <circle cx="91.5" cy="53.5" r="2" fill="#232226" />
    <path d="M79 63C82 66 86 66 89 63" stroke="#D96B4E" strokeWidth="2" strokeLinecap="round" />
    <path d="M41 74C44 68 49 69 51 75L55 89L43 93L38 80C37 77 39 75 41 74Z" fill="#FCE0CE" />
  </svg>
);

const AssistantSmallAvatar = () => (
  <div className="hg-avatar-circle">
    <svg width="30" height="30" viewBox="52 18 64 88" fill="none" xmlns="http://www.w3.org/2000/svg">
      <circle cx="84" cy="62" r="32" fill="#FFEFE5" />
      <path d="M56 106C58 88 68 80 84 80C100 80 110 88 112 106H56Z" fill="#FFFFFF" />
      <path d="M62 84C68 79 100 79 106 84L100 102L84 94L68 102L62 84Z" fill="#FF7315" />
      <rect x="79" y="68" width="10" height="14" rx="5" fill="#FAD5C0" />
      <ellipse cx="84" cy="52" rx="18" ry="20" fill="#FCE0CE" />
      <circle cx="84" cy="26" r="8" fill="#232226" />
      <path d="M66 50C66 35 74 28 84 28C94 28 102 35 102 50C99 41 92 37 84 37C76 37 69 41 66 50Z" fill="#232226" />
      <circle cx="77.5" cy="52" r="1.8" fill="#232226" />
      <circle cx="90.5" cy="52" r="1.8" fill="#232226" />
      <path d="M80 60C82.5 62.5 85.5 62.5 88 60" stroke="#D96B4E" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  </div>
);

/* ==========================================================================
   2. 首屏横向商品导购卡片与「已购买 / 购物车」半屏弹窗真实数据源
   ========================================================================== */
const getSkuImage = (skuId) => `/static/images/${skuId}.jpg`;

const HOME_PRODUCT_HOOKS = [
  {
    id: 'hook-headphones',
    tag: '看过',
    image: getSkuImage('SKU-3C-019'),
    question: '这款漫步者W820NB耳机和索尼差别大吗',
    buttonText: '看亮点',
    prompt: '这款漫步者 W820NB 双金标头戴式降噪耳机有哪些核心亮点？200多元值得买吗？',
  },
  {
    id: 'hook-keyboard',
    tag: '看过',
    image: getSkuImage('SKU-3C-015'),
    question: '有没有类似风格的三模机械键盘平价款推荐？',
    buttonText: '试一试',
    prompt: '我想买一把无线三模热插拔机械键盘，预算 150 元以内，有平价款推荐吗？',
  },
  {
    id: 'hook-jacket',
    tag: '看过',
    image: getSkuImage('SKU-APP-001'),
    question: '同价位有没有更值得入手的防水冲锋衣推荐？',
    buttonText: '试一试',
    prompt: '帮我看看拓路者暴雨级防水超轻冲锋衣的亮点和到手好价',
  },
  {
    id: 'hook-wishlist',
    tag: '看过',
    image: getSkuImage('SKU-3C-001'),
    question: '想买一台家用空气炸锅，目前有现货吗？',
    buttonText: '试一试',
    prompt: '想买一台家用空气炸锅，目前有现货吗？',
  },
];

/* 半屏弹窗：购物车 & 已购买列表（1:1 复刻截图6 选择商品布局） */
const CART_ITEMS = [
  {
    id: 'cart-1',
    sku_id: 'SKU-3C-004',
    title: '绿联 13.3寸轻薄笔记本防刮内胆包 适配MacBook Air',
    spec: '13.3寸【防泼水绒里+便携轻薄】深空灰 >',
    price: 15.9,
    orig_price: 25.0,
    shop: '绿联数码官方旗舰店',
    image: getSkuImage('SKU-3C-004'),
  },
  {
    id: 'cart-2',
    sku_id: 'SKU-3C-019',
    title: '漫步者 W820NB 双金标版 头戴式主动降噪蓝牙耳机',
    spec: '云岩白【-43dB深度降噪+50h长续航】 >',
    price: 239.0,
    orig_price: 299.0,
    shop: '漫步者官方自营旗舰店',
    image: getSkuImage('SKU-3C-019'),
  },
  {
    id: 'cart-3',
    sku_id: 'SKU-APP-017',
    title: '重磅 280g/㎡ 新疆长绒棉纯白短袖T恤 防凸点不透肉打底衫',
    spec: 'L码【280g重磅纯棉+久洗不变形】本白 >',
    price: 59.0,
    orig_price: 79.0,
    shop: '惠购精选服饰自营店',
    image: getSkuImage('SKU-APP-017'),
  },
  {
    id: 'cart-4',
    sku_id: 'SKU-BEA-004',
    title: '适乐肤 C霜 三重神经酰胺NP深层保湿修护面霜 50ml',
    spec: '50ml标准装【干敏皮修护屏障】 >',
    price: 79.0,
    orig_price: 98.0,
    shop: '适乐肤官方海外旗舰店',
    image: getSkuImage('SKU-BEA-004'),
  },
  {
    id: 'cart-5',
    sku_id: 'SKU-3C-001',
    title: '安克 Anker 30W 氮化镓超能充 Nano3 折叠插脚单口快充头',
    spec: '冰晶蓝【30W疾速快充+折叠插脚】 >',
    price: 35.0,
    orig_price: 49.0,
    shop: 'Anker安克官方旗舰店',
    image: getSkuImage('SKU-3C-001'),
  },
];

const PURCHASED_ITEMS = [
  {
    id: 'ord-1',
    order_id: 'ORD-20261001-001',
    sku_id: 'SKU-APP-001',
    title: '拓路者 10000mm暴雨级防水超轻冲锋衣 280g便携防风外套',
    spec: 'XL码【暴雨级防水+支持7天无理由】苍山绿 >',
    price: 399.0,
    orig_price: 459.0,
    shop: '订单号 ORD-20261001-001 · 已签收',
    image: getSkuImage('SKU-APP-001'),
  },
  {
    id: 'ord-2',
    order_id: 'ORD-20261001-002',
    sku_id: 'SKU-3C-015',
    title: 'VGN V98Pro 三模无线客制化机械键盘 TTC金粉轴V2',
    spec: '极光蓝【15天价保期内·可退差价¥100】 >',
    price: 249.0,
    orig_price: 349.0,
    shop: '订单号 ORD-20261001-002 · 已签收',
    image: getSkuImage('SKU-3C-015'),
  },
  {
    id: 'ord-3',
    order_id: 'ORD-20261001-005',
    sku_id: 'SKU-BEA-011',
    title: '修丽可 色修B5保湿舒缓精华液 30ml 泛醇透明质酸退红修护',
    spec: '30ml【顺丰派送中·SF1420981234】 >',
    price: 169.0,
    orig_price: 199.0,
    shop: '订单号 ORD-20261001-005 · 运输中',
    image: getSkuImage('SKU-BEA-011'),
  },
];

/* ==========================================================================
   3. 卡片内嵌浅灰底 Generative UI 组件群（1:1 复刻截图2/3/4/5）
   ========================================================================== */

function AssistantPresetGrayTabs({ presetType, onSendPrompt, onOpenSheet }) {
  if (presetType === 'buy_assistant') {
    const items = [
      {
        title: '天凉了该添件外套',
        desc: '最近降温明显，帮你挑几件保暖防风又好看的外套与开衫',
        image: getSkuImage('SKU-APP-001'),
        prompt: '最近降温了，帮我推荐几件保暖防风又好看的冲锋衣或针织开衫外套',
      },
      {
        title: '换季皮肤干痒怎么救',
        desc: '秋冬换季皮肤容易缺水，挑款保湿力强的神经酰胺面霜',
        image: getSkuImage('SKU-BEA-004'),
        prompt: '秋冬换季皮肤干燥敏感，帮我推荐一款保湿力强又不闷痘的修护面霜',
      },
      {
        title: '差旅通勤数码好装备',
        desc: '降噪耳机与轻便快充现在买更划算，帮你看看哪款省心好用',
        image: getSkuImage('SKU-3C-019'),
        prompt: '帮我推荐一套适合通勤差旅的头戴式降噪耳机和氮化镓快充头',
      },
    ];
    return (
      <div className="hg-gray-tabs-stack">
        {items.map((it) => (
          <button
            key={it.title}
            type="button"
            className="hg-gray-feature-card"
            onClick={() => onSendPrompt(it.prompt)}
          >
            <div className="hg-gray-feature-top">
              <span className="hg-gray-feature-title">
                <span className="hg-star-red">✦</span>
                <span>{it.title}</span>
              </span>
              <span className="hg-arrow-ne">↗</span>
            </div>
            <div className="hg-gray-feature-bottom">
              <span className="hg-gray-feature-desc">{it.desc}</span>
              <img className="hg-gray-feature-thumb" src={it.image} alt={it.title} />
            </div>
          </button>
        ))}
      </div>
    );
  }

  if (presetType === 'order_assistant') {
    return (
      <div className="hg-gray-pill-list">
        <button
          type="button"
          className="hg-gray-action-pill"
          onClick={() =>
            onSendPrompt('帮我总结下最近订单 ORD-20261001-005 和 ORD-20261001-001 的物流进度')
          }
        >
          <span>帮我总结下最近订单的物流进度</span>
          <span className="hg-arrow-ne">↗</span>
        </button>
        <button
          type="button"
          className="hg-gray-action-pill"
          onClick={() =>
            onSendPrompt('查一下我最近的购买记录，看看哪笔订单能退差价或7天无理由退货')
          }
        >
          <span>查一下我最近的购买记录</span>
          <span className="hg-arrow-ne">↗</span>
        </button>
        <button
          type="button"
          className="hg-gray-action-pill"
          onClick={() => onOpenSheet('purchased')}
        >
          <span>找一笔订单发起服务</span>
          <span className="hg-arrow-ne">&gt;</span>
        </button>
      </div>
    );
  }

  if (presetType === 'save_assistant') {
    return (
      <div className="hg-gray-tabs-stack">
        <button
          type="button"
          className="hg-gray-full-btn"
          onClick={() => onOpenSheet('cart')}
        >
          选择商品
        </button>
      </div>
    );
  }

  if (presetType === 'msg_assistant') {
    return (
      <div className="hg-gray-pill-list">
        <button
          type="button"
          className="hg-gray-action-pill"
          onClick={() => onSendPrompt('总结当前对话与订单需要注意的地方')}
        >
          <span>总结当前对话与订单需要注意的地方</span>
          <span className="hg-arrow-ne">↗</span>
        </button>
        <button
          type="button"
          className="hg-gray-action-pill"
          onClick={() => onSendPrompt('总结物流、售后等消息')}
        >
          <span>总结物流、售后等消息</span>
          <span className="hg-arrow-ne">↗</span>
        </button>
      </div>
    );
  }

  return null;
}

/**
 * 商品卡片（浅灰底内嵌在 AI 回答卡片中）
 */
function ProductGenUICard({ products, priceCards }) {
  const [expandedSku, setExpandedSku] = useState(null);
  const [drawerMode, setDrawerMode] = useState('highlights');

  if (!products || products.length === 0) return null;

  const priceMap = {};
  (priceCards || []).forEach((pc) => {
    if (pc.sku_id) priceMap[pc.sku_id] = pc;
  });

  const toggleDrawer = (skuId, mode) => {
    if (expandedSku === skuId && drawerMode === mode) {
      setExpandedSku(null);
    } else {
      setExpandedSku(skuId);
      setDrawerMode(mode);
    }
  };

  return (
    <div className="hg-inbubble-section">
      {products.slice(0, 3).map((item) => {
        const skuId = item.sku_id;
        const rawFinal = item.final_price ?? item.price ?? 0;
        const rawOrig = item.original_price ?? priceInfo?.original_price ?? rawFinal;
        const finalPrice = safeFormatPrice(rawFinal);
        const origPrice = safeFormatPrice(rawOrig);
        const discount = safeCalcDiscount(rawOrig, rawFinal);
        const highlights = item.highlights || item.matched_tags || [];
        const isExpanded = expandedSku === skuId;

        return (
          <div key={skuId} className="hg-prod-item">
            <img
              className="hg-prod-thumb"
              src={item.image_path || item.image_url || getSkuImage(item.sku_id || 'SKU-3C-001')}
              alt={item.title}
              loading="lazy"
            />
            <div className="hg-prod-info">
              <div className="hg-prod-name">{item.title}</div>
              <div className="hg-prod-price">¥{finalPrice}</div>
              <div className="hg-prod-actions">
                <button
                  type="button"
                  className="hg-primary-btn"
                  onClick={() => toggleDrawer(skuId, 'highlights')}
                >
                  {isExpanded && drawerMode === 'highlights' ? '收起亮点' : '看亮点'}
                </button>
                <button
                  type="button"
                  className="hg-secondary-btn"
                  style={{ background: '#FFFFFF' }}
                  onClick={() => toggleDrawer(skuId, 'price')}
                >
                  {isExpanded && drawerMode === 'price' ? '收起好价' : '算好价'}
                </button>
              </div>
            </div>

            {isExpanded && drawerMode === 'highlights' && (
              <div className="hg-prod-drawer">
                {highlights.length > 0
                  ? `核心亮点：${highlights.slice(0, 4).join(' · ')}`
                  : '官方自营正品保障，支持 7 天无理由退货与 15 天自动价保。'}
              </div>
            )}

            {isExpanded && drawerMode === 'price' && (
              <div className="hg-prod-drawer">
                <div className="hg-order-kv">
                  <span className="hg-order-kv-label">日常价</span>
                  <span className="hg-order-kv-val">¥{origPrice}</span>
                </div>
                <div className="hg-order-kv">
                  <span className="hg-order-kv-label">官方直降立减</span>
                  <span className="hg-order-kv-val" style={{ color: 'var(--accent)' }}>
                    -¥{discount}
                  </span>
                </div>
                <div className="hg-order-kv">
                  <span className="hg-order-kv-label">预估到手好价</span>
                  <span className="hg-order-kv-val" style={{ color: 'var(--accent)' }}>
                    ¥{finalPrice}
                  </span>
                </div>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

const SUB_CATEGORY_ZH_MAP = {
  laptop_bag: '笔记本电脑包',
  gan_charger: '氮化镓快充充电器',
  mechanical_keyboard: '机械键盘',
  nc_headphones: '头戴式降噪耳机',
  power_bank: '移动电源',
  usb_hub_cable: 'Type-C拓展坞',
  hoodie: '卫衣',
  knit_cardigan: '针织开衫',
  windbreaker: '户外冲锋衣',
  cargo_pants: '工装裤',
  tshirt: '短袖T恤',
  facial_cream: '保湿修护面霜',
  amino_cleanser: '氨基酸洁面',
  sunscreen: '防晒霜',
  b5_serum: 'B5修护精华液',
  salicylic_pads: '水杨酸棉片',
  lip_balm: '润唇膏',
};

/**
 * 双路径冲突松弛浅灰块 + 流内预算滑块
 */
function RelaxationGenUICard({ products, maxBudget, subCategory, onSendPrompt }) {
  const [activeTab, setActiveTab] = useState('path_a');
  const initialBudget = Number(maxBudget) > 0 ? Number(maxBudget) : 150;
  const [sliderBudget, setSliderBudget] = useState(initialBudget);

  if (!products || products.length < 2) return null;
  const pathA = products[0];
  const pathB = products[1];
  const current = activeTab === 'path_a' ? pathA : pathB;
  const rawSub = subCategory || current.sub_category || pathA.sub_category || '';
  const targetSubCat = SUB_CATEGORY_ZH_MAP[rawSub] || rawSub || '款式';

  return (
    <div className="hg-inbubble-section">
      <div className="hg-path-tabs">
        <button
          type="button"
          className={`hg-path-tab ${activeTab === 'path_a' ? 'active' : ''}`}
          onClick={() => setActiveTab('path_a')}
        >
          方案A · 保规格 (¥{pathA.final_price})
        </button>
        <button
          type="button"
          className={`hg-path-tab ${activeTab === 'path_b' ? 'active' : ''}`}
          onClick={() => setActiveTab('path_b')}
        >
          方案B · 保预算 (¥{pathB.final_price})
        </button>
      </div>

      <div className="hg-prod-item">
        <img
          className="hg-prod-thumb"
          src={current.image_url || current.image_path || getSkuImage(current.sku_id || 'SKU-3C-015')}
          alt={current.title}
        />
        <div className="hg-prod-info">
          <div className="hg-prod-name">{current.title}</div>
          <div className="hg-prod-price">¥{current.final_price}</div>
        </div>
      </div>

      <div className="hg-slider-box">
        <div className="hg-slider-top">
          <span>拖动调整预算上限</span>
          <span className="hg-slider-val">¥{sliderBudget}</span>
        </div>
        <input
          type="range"
          min="30"
          max="600"
          step="10"
          value={sliderBudget}
          onChange={(e) => setSliderBudget(Number(e.target.value))}
          className="hg-range-input"
        />
        <button
          type="button"
          className="hg-primary-btn"
          onClick={() =>
            onSendPrompt(
              `把预算放宽到 ${sliderBudget} 元以内，再帮我找一轮最合适的${targetSubCat}`
            )
          }
        >
          放宽到 ¥{sliderBudget} 再找一轮 ↗
        </button>
      </div>
    </div>
  );
}

/**
 * 缺货品类心愿单浅灰钩子块
 */
function WishlistGenUICard({ card, sessionId }) {
  const defaultOptions = ['¥100以内', '¥100-300', '¥300-800', '¥800以上'];
  const budgetOptions =
    Array.isArray(card.budget_options) && card.budget_options.length > 0
      ? card.budget_options
      : defaultOptions;
  const [selectedBudget, setSelectedBudget] = useState(
    budgetOptions[1] || budgetOptions[0] || '¥100-300'
  );
  const [submitted, setSubmitted] = useState(false);

  const handleSubscribe = async () => {
    try {
      await fetch('/api/v1/wishlist', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          missing_item: card.missing_item,
          user_query: card.query || '',
          session_id: sessionId,
          expected_budget: selectedBudget,
          user_subscribed: true,
        }),
      });
    } catch {
      // ignore error
    }
    setSubmitted(true);
  };

  return (
    <div className="hg-inbubble-section">
      <div style={{ fontWeight: 700, fontSize: '15px' }}>
        {card.title || `暂无「${card.missing_item}」`}
      </div>
      <div className="hg-chip-row">
        {budgetOptions.map((opt) => (
          <button
            key={opt}
            type="button"
            disabled={submitted}
            className={`hg-chip-btn ${selectedBudget === opt ? 'active' : ''}`}
            onClick={() => setSelectedBudget(opt)}
          >
            {opt}
          </button>
        ))}
      </div>
      {submitted ? (
        <div className="hg-done-banner">
          已登记「{card.missing_item}（{selectedBudget}）」心愿单，上架后第一时间提醒您
        </div>
      ) : (
        <button type="button" className="hg-primary-btn" onClick={handleSubscribe}>
          暂无当前商品 · 点击登记尽快上架
        </button>
      )}
    </div>
  );
}

/**
 * 订单物流与售后核验浅灰块
 */
function OrderAftersalesGenUICard({ orderCards, aftersalesCard }) {
  const [actionDone, setActionDone] = useState('');
  const order = orderCards && orderCards[0];
  if (!order && !aftersalesCard) return null;

  const refundAmount = Number(aftersalesCard?.refund_amount ?? 0);
  const eligible = aftersalesCard ? Boolean(aftersalesCard.eligible) : true;

  return (
    <div className="hg-inbubble-section">
      {order && (
        <>
          <div className="hg-order-kv">
            <span className="hg-order-kv-label">订单编号</span>
            <span className="hg-order-kv-val">{order.order_id}</span>
          </div>
          <div className="hg-order-kv">
            <span className="hg-order-kv-label">商品名称</span>
            <span className="hg-order-kv-val">{order.sku_title || order.title}</span>
          </div>
          {order.carrier && (
            <div className="hg-order-kv">
              <span className="hg-order-kv-label">物流单号</span>
              <span className="hg-order-kv-val">
                {order.carrier} · {order.tracking_no}
              </span>
            </div>
          )}
        </>
      )}

      {aftersalesCard && refundAmount > 0 && (
        <div className="hg-order-kv">
          <span className="hg-order-kv-label">核定可退差额</span>
          <span className="hg-order-kv-val" style={{ color: 'var(--accent)' }}>
            ¥{refundAmount.toFixed(2)}
          </span>
        </div>
      )}

      {eligible && (
        <div>
          {actionDone ? (
            <div className="hg-done-banner">{actionDone}</div>
          ) : refundAmount > 0 ? (
            <button
              type="button"
              className="hg-primary-btn"
              onClick={() =>
                setActionDone(`已受理 ¥${refundAmount.toFixed(2)} 价保退差，预计 2 小时内退回原账户`)
              }
            >
              一键申领 ¥{refundAmount.toFixed(2)} 差价
            </button>
          ) : (
            <button
              type="button"
              className="hg-gray-full-btn"
              style={{ background: '#FFFFFF' }}
              onClick={() => setActionDone('已为您生成极速退换服务单，快递员将预约上门取件')}
            >
              申请退换货服务
            </button>
          )}
        </div>
      )}
    </div>
  );
}

/* ==========================================================================
   4. 惠购 (Huigou) 主应用组件
   ========================================================================== */
export default function App() {
  const [sessionId, setSessionId] = useState(() => `hg-${Date.now().toString(36)}`);
  const [messages, setMessages] = useState([]);
  const [inputText, setInputText] = useState('');
  const [isStreaming, setIsStreaming] = useState(false);

  // 多模态图片上传与相机预览状态
  const [pendingImage, setPendingImage] = useState(null); // { dataUrl, name } | null
  const [cameraModalOpen, setCameraModalOpen] = useState(false);

  // 页面导航与弹窗状态
  const [subPage, setSubPage] = useState(null); // null | 'settings' | 'disclaimer'
  const [showPlusTray, setShowPlusTray] = useState(false);
  const [sheetOpen, setSheetOpen] = useState(false);
  const [sheetTab, setSheetTab] = useState('cart'); // 'cart' | 'purchased'
  const [selectedIds, setSelectedIds] = useState([]);
  const [toastMsg, setToastMsg] = useState('');
  const [showScrollFab, setShowScrollFab] = useState(false);

  const mainScrollRef = useRef(null);
  const activePacerRef = useRef(null);
  const abortControllerRef = useRef(null);
  const albumInputRef = useRef(null);
  const cameraInputRef = useRef(null);
  const videoRef = useRef(null);
  const mediaStreamRef = useRef(null);

  const stopCameraStream = () => {
    if (mediaStreamRef.current) {
      mediaStreamRef.current.getTracks().forEach((track) => track.stop());
      mediaStreamRef.current = null;
    }
  };

  const stopCurrentStream = () => {
    if (activePacerRef.current) {
      clearInterval(activePacerRef.current);
      activePacerRef.current = null;
    }
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
    setIsStreaming(false);
  };

  useEffect(() => {
    return () => {
      stopCurrentStream();
      stopCameraStream();
    };
  }, []);

  const triggerToast = (msg) => {
    setToastMsg(msg);
    setTimeout(() => {
      setToastMsg('');
    }, 1800);
  };

  /**
   * 选图/拍照文件回调：通过 Canvas 压缩至 <= 1024px 后挂载到输入框上方待发送预览条
   */
  const handleImageFileChange = async (e) => {
    const file = e.target.files && e.target.files[0];
    e.target.value = '';
    if (!file) return;
    if (!file.type.startsWith('image/')) {
      triggerToast('请选择图片格式文件');
      return;
    }
    try {
      const dataUrl = await compressImageToDataUrl(file);
      setPendingImage({
        dataUrl,
        name: file.name || 'photo.jpg',
      });
      setShowPlusTray(false);
    } catch {
      triggerToast('图片处理失败，请重试');
    }
  };

  /**
   * 点击「照片」：唤醒手机系统相册（iOS 照片图库 / Android 相册）或桌面文件选择器
   */
  const handlePickAlbum = () => {
    setShowPlusTray(false);
    if (albumInputRef.current) {
      albumInputRef.current.click();
    }
  };

  /**
   * 点击「拍照」：
   *   - 移动端直接通过 capture="environment" 唤醒手机后置摄像头；
   *   - 桌面端优先拉起 WebRTC 实时取景框，若无摄像头权限则平滑回退至文件选择。
   */
  const handleTakePhoto = async () => {
    setShowPlusTray(false);
    const isMobile = /Android|iPhone|iPad|iPod|Mobile/i.test(
      navigator.userAgent || ''
    );
    if (isMobile || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      if (cameraInputRef.current) {
        cameraInputRef.current.click();
      }
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: 'environment' },
        audio: false,
      });
      mediaStreamRef.current = stream;
      setCameraModalOpen(true);
      setTimeout(() => {
        if (videoRef.current) {
          videoRef.current.srcObject = stream;
          videoRef.current.play().catch(() => {});
        }
      }, 60);
    } catch {
      if (cameraInputRef.current) {
        cameraInputRef.current.click();
      }
    }
  };

  const closeCameraModal = () => {
    stopCameraStream();
    setCameraModalOpen(false);
  };

  const captureWebRTCFrame = () => {
    const video = videoRef.current;
    if (!video) return;
    const rawW = video.videoWidth || 640;
    const rawH = video.videoHeight || 480;
    const maxEdge = 1024;
    const ratio = Math.min(1, maxEdge / Math.max(rawW, rawH));
    const w = Math.max(1, Math.round(rawW * ratio));
    const h = Math.max(1, Math.round(rawH * ratio));
    const canvas = document.createElement('canvas');
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext('2d');
    if (ctx) {
      ctx.drawImage(video, 0, 0, w, h);
      const dataUrl = canvas.toDataURL('image/jpeg', 0.85);
      setPendingImage({ dataUrl, name: `camera-${Date.now()}.jpg` });
    }
    closeCameraModal();
  };

  const scrollToBottom = () => {
    if (mainScrollRef.current) {
      mainScrollRef.current.scrollTo({
        top: mainScrollRef.current.scrollHeight,
        behavior: 'smooth',
      });
    }
    setShowScrollFab(false);
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages.length]);

  const handleMainScroll = () => {
    const el = mainScrollRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    setShowScrollFab(distanceFromBottom > 120);
  };

  /**
   * 点 3：人眼舒适速度的动态字流节拍器（带生命周期保护与定时器防泄漏）
   */
  const playSmoothText = (msgId, fullText, extraPatch = {}) => {
    stopCurrentStream();
    setIsStreaming(true);
    let cursor = 0;
    const totalLen = fullText.length;

    const timer = setInterval(() => {
      const remaining = totalLen - cursor;
      const step = remaining > 90 ? 3 : remaining > 40 ? 2 : 1;
      cursor = Math.min(totalLen, cursor + step);
      const currentSlice = fullText.slice(0, cursor);

      setMessages((prev) =>
        prev.map((m) =>
          m.id === msgId
            ? {
                ...m,
                text: currentSlice,
                ...(cursor >= totalLen ? extraPatch : {}),
              }
            : m
        )
      );

      if (mainScrollRef.current) {
        const el = mainScrollRef.current;
        if (el.scrollHeight - el.scrollTop - el.clientHeight < 140) {
          el.scrollTop = el.scrollHeight;
        }
      }

      if (cursor >= totalLen) {
        clearInterval(timer);
        activePacerRef.current = null;
        setIsStreaming(false);
      }
    }, 28);
    activePacerRef.current = timer;
  };

  /**
   * 点 7 & 8：点击三个助手入口或底部快捷胶囊 -> 发送对应用户气泡 + AI 流式回复与浅灰 Tab
   */
  const triggerAssistantTab = (type) => {
    if (isStreaming) return;
    setShowPlusTray(false);

    const configs = {
      buy: {
        userText: 'Hi，惠购选品顾问！',
        aiText:
          '您好！我是惠购官方首席选品顾问，专注为您提供无套路的硬核选品决策：\n- 参数级严选：按功率、容量、克重、成分硬指标精准锁定靠谱好物\n- 双路径折中：遇到预算与高配规格冲突，自动生成「保规格 vs 保预算」对比方案\n- 滑块微调预算：动动手指拖动预算上限，秒级重算最优平替\n- 跨品类搭齐：3C数码、换季服饰、美妆个护一站式严选对比\n点选下方推荐方向，或直接告诉我您的预算和具体参数要求吧！',
        presetType: 'buy_assistant',
      },
      order: {
        userText: '嗨，订单与售后顾问',
        aiText: '您好！我是惠购订单与售后专员，支持实时物流轨迹查询、7天无理由退货（168h）及15天自动价保（360h）秒级核验，点下方快捷项立即体验：',
        presetType: 'order_assistant',
      },
      save: {
        userText: '帮我核算直降好价',
        aiText: '您好！惠购坚持「官方直营·无套路真直降」，废除复杂满减凑单。勾选您心仪的商品，我立即为您核算日常原价与当前直降到手差额：',
        presetType: 'save_assistant',
      },
      msg: {
        userText: '嗨，消息与权益助手',
        aiText: '您好！我是惠购消息与权益助手，可为您一键汇总当前对话的选品结论、订单物流进展及15天价保可退差额提醒：',
        presetType: 'msg_assistant',
      },
    };

    const cfg = configs[type] || configs.buy;
    const uId = nextSafeId('u');
    const aId = nextSafeId('a');

    setMessages((prev) => [
      ...prev,
      { id: uId, role: 'user', text: cfg.userText },
      {
        id: aId,
        serverMessageId: aId,
        role: 'assistant',
        userPrompt: cfg.userText,
        assistantPreset: type,
        text: '',
        presetType: null,
        uiCards: {},
        followupPills: [],
        feedback: null,
      },
    ]);

    playSmoothText(aId, cfg.aiText, { presetType: cfg.presetType });
  };

  /**
   * 点 9：消息助手长对话总结（检测长对话中的预算卡点、售后时效与选购注意事项，无定时任务）
   */
  const runLongConversationSummary = (userTriggerText) => {
    const uId = nextSafeId('u');
    const aId = nextSafeId('a');
    const userTurns = messages.filter((m) => m.role === 'user');

    let summaryReply = '';
    if (userTurns.length <= 1) {
      summaryReply =
        '嘿！消息和当前对话都帮您看过了，暂时没有需要您特别留意的风险项～\n需要注意的地方已帮您划重点：\n- 您的订单 ORD-20261001-002（VGN机械键盘）正处于 15 天价保期内，存在 ¥100.00 差价可随时申领；\n- 订单 ORD-20261001-005（修丽可精华）顺丰正在派送途中，签收后享 7 天无理由退货保障。';
    } else {
      const topics = userTurns
        .slice(-4)
        .map((t) => t.text)
        .join('、');
      summaryReply = `已为您汇总本场对话（共 ${userTurns.length} 轮咨询，涉及：${topics}）需要注意的重点事项：\n- 选购与预算提醒：若遇到预算与高配参数冲突，建议优先参考卡片中的「方案A · 保预算」或微调预算滑块，避免买错规格；\n- 订单与价保提醒：您名下订单 ORD-20261001-002 核定可退差价 ¥100.00，请及时在价保有效期内点击申领；\n- 隐私与安全说明：对话中的商品对比与订单核验仅在当前会话生效，不会跨场次留存您的个人画像。`;
    }

    setMessages((prev) => [
      ...prev,
      { id: uId, role: 'user', text: userTriggerText },
      {
        id: aId,
        serverMessageId: aId,
        role: 'assistant',
        userPrompt: userTriggerText,
        text: '',
        uiCards: {},
        followupPills: [],
        feedback: null,
      },
    ]);

    playSmoothText(aId, summaryReply);
  };

  /**
   * 发送普通或带图消息并按人眼可读速度动态流式渲染 SSE
   * 支持 AbortController 中断、30s 响应超时、事件状态复位与原地重答机制
   */
  const sendMessage = async (
    rawQuery,
    attachedProducts = null,
    targetMsgId = null,
    overrideHistory = null,
    explicitImage = null
  ) => {
    const rawTrimmed = (rawQuery ?? inputText).trim();
    const imageToSend = explicitImage ?? pendingImage?.dataUrl ?? null;
    if (!rawTrimmed && !imageToSend) return;
    if (isStreaming && !targetMsgId) return;

    const query =
      rawTrimmed || '请帮我识别这张图片中的商品并推荐合适的款式';

    // 终止前序流与旧定时器，防止状态冲突
    stopCurrentStream();
    setInputText('');
    setPendingImage(null);
    setShowPlusTray(false);

    if (
      !imageToSend &&
      (query === '总结当前对话与订单需要注意的地方' ||
        query === '总结物流、售后等消息')
    ) {
      runLongConversationSummary(query);
      return;
    }

    const isRetry = Boolean(targetMsgId);
    const assistantMsgId = targetMsgId || nextSafeId('a');
    const userMsgId = nextSafeId('u');

    let historyPayload = [];
    if (overrideHistory) {
      historyPayload = overrideHistory;
    } else {
      historyPayload = messages
        .filter((m) => m.text)
        .slice(-6)
        .map((m) => ({ role: m.role, content: m.text }));
    }

    if (isRetry) {
      // 原地重试：保留原用户提问，重置对应 AI 气泡所有动态状态
      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantMsgId
            ? {
                ...m,
                text: '',
                decisionMeta: null,
                uiCards: {},
                followupPills: [],
                feedback: null,
              }
            : m
        )
      );
    } else {
      const userMsg = {
        id: userMsgId,
        role: 'user',
        text: query,
        sentProducts: attachedProducts,
        sentImage: imageToSend,
      };
      const placeholderAssistant = {
        id: assistantMsgId,
        serverMessageId: assistantMsgId,
        role: 'assistant',
        userPrompt: query,
        userImage: imageToSend,
        text: '',
        decisionMeta: null,
        uiCards: {},
        followupPills: [],
        feedback: null,
      };
      setMessages((prev) => [...prev, userMsg, placeholderAssistant]);
    }

    setIsStreaming(true);

    const controller = new AbortController();
    abortControllerRef.current = controller;

    // 30s 响应超时兜底定时器
    const timeoutId = setTimeout(() => {
      controller.abort('Network timeout (30s)');
    }, 30000);

    const charQueue = [];
    let sseFinished = false;
    let displayedText = '';

    const pacer = setInterval(() => {
      if (charQueue.length > 0) {
        const takeCount = charQueue.length > 80 ? 3 : charQueue.length > 35 ? 2 : 1;
        const chunk = charQueue.splice(0, takeCount).join('');
        displayedText += chunk;
        setMessages((prev) =>
          prev.map((m) => (m.id === assistantMsgId ? { ...m, text: displayedText } : m))
        );
        if (mainScrollRef.current) {
          const el = mainScrollRef.current;
          if (el.scrollHeight - el.scrollTop - el.clientHeight < 140) {
            el.scrollTop = el.scrollHeight;
          }
        }
      } else if (sseFinished) {
        clearInterval(pacer);
        activePacerRef.current = null;
        setIsStreaming(false);
      }
    }, 28);
    activePacerRef.current = pacer;

    try {
      const reqBody = {
        query,
        session_id: sessionId,
        tenant_id: 'tenant_all',
        history: historyPayload,
      };
      if (imageToSend) {
        reqBody.image_base64 = imageToSend;
      }

      const response = await fetch('/api/v1/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(reqBody),
        signal: controller.signal,
      });

      clearTimeout(timeoutId);

      if (!response.ok || !response.body) {
        throw new Error(`HTTP ${response.status}`);
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder('utf-8');
      let buffer = '';
      let currentEvent = 'message';

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';

        for (const rawLine of lines) {
          const line = rawLine.trim();
          if (!line) {
            // W3C SSE 规范：空行标志着一个事件块的结束，重置事件为默认值
            currentEvent = 'message';
            continue;
          }
          if (line.startsWith('event:')) {
            currentEvent = line.slice(6).trim();
          } else if (line.startsWith('data:')) {
            const dataStr = line.slice(5).trim();
            if (dataStr === '[DONE]') {
              sseFinished = true;
              break;
            }
            let parsed = null;
            try {
              parsed = JSON.parse(dataStr);
            } catch {
              continue;
            }

            if (currentEvent === 'decision_meta') {
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === assistantMsgId
                    ? {
                        ...m,
                        decisionMeta: parsed,
                        serverMessageId: parsed.message_id || m.serverMessageId,
                      }
                    : m
                )
              );
            } else if (currentEvent === 'ui_cards') {
              setMessages((prev) =>
                prev.map((m) => (m.id === assistantMsgId ? { ...m, uiCards: parsed || {} } : m))
              );
            } else if (currentEvent === 'text_delta') {
              const delta = parsed?.delta || '';
              if (delta) {
                charQueue.push(...Array.from(delta));
              }
            } else if (currentEvent === 'followup_pills') {
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === assistantMsgId ? { ...m, followupPills: parsed?.pills || [] } : m
                )
              );
            }
          }
        }
        if (sseFinished) break;
      }
      sseFinished = true;
    } catch (err) {
      clearTimeout(timeoutId);
      if (activePacerRef.current) {
        clearInterval(activePacerRef.current);
        activePacerRef.current = null;
      }
      setIsStreaming(false);

      if (err.name === 'AbortError') {
        // 请求由用户主动中断或发生新请求，静默退出
        return;
      }

      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantMsgId
            ? { ...m, text: '服务连接繁忙，请点击右下角「重答」再试一次。' }
            : m
        )
      );
    }
  };

  /**
   * 点击「↻ 重答」：原地重置历史消息，杜绝上下文污染与重复提问气泡堆叠
   */
  const handleRetry = (msg) => {
    if (isStreaming) return;
    if (msg.assistantPreset) {
      triggerAssistantTab(msg.assistantPreset);
      return;
    }
    if (!msg.userPrompt && !msg.userImage) return;

    const msgIdx = messages.findIndex((m) => m.id === msg.id);
    const truncatedHistory = (msgIdx > 0 ? messages.slice(0, msgIdx) : messages)
      .filter((m) => m.text && m.id !== msg.id)
      .slice(-6)
      .map((m) => ({ role: m.role, content: m.text }));

    sendMessage(msg.userPrompt, null, msg.id, truncatedHistory, msg.userImage || null);
  };

  const handleFeedback = async (msg, rating) => {
    setMessages((prev) =>
      prev.map((m) => (m.id === msg.id ? { ...m, feedback: rating } : m))
    );
    try {
      await fetch('/api/v1/feedback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message_id: msg.serverMessageId || msg.id,
          session_id: sessionId,
          tenant_id: 'tenant_all',
          rating,
          reason_tags: rating === 'like' ? ['回答有帮助'] : ['回答待改进'],
          user_query: msg.userPrompt || '',
          assistant_response: msg.text,
        }),
      });
    } catch {
      // ignore
    }
  };

  /**
   * 打开「选择商品 / 订单服务」半屏弹窗（只保留「购物车」与「已购买」）
   */
  const openProductOrderSheet = (defaultTab = 'cart') => {
    setSheetTab(defaultTab);
    setSelectedIds([]);
    setSheetOpen(true);
    setShowPlusTray(false);
  };

  const toggleSelectSheetItem = (id) => {
    setSelectedIds((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]
    );
  };

  /**
   * 在「购物车 / 已购买」半屏弹窗点击「确认选择」
   */
  const handleConfirmSheetSelection = () => {
    const pool = sheetTab === 'cart' ? CART_ITEMS : PURCHASED_ITEMS;
    const chosen = pool.filter((it) => selectedIds.includes(it.id));
    if (chosen.length === 0) return;
    setSheetOpen(false);

    if (sheetTab === 'cart') {
      // 消除浮点数累加漂移：采用分级整数运算
      const totalOrigCents = chosen.reduce(
        (acc, c) => acc + Math.round((Number(c.orig_price) || 0) * 100),
        0
      );
      const totalFinalCents = chosen.reduce(
        (acc, c) => acc + Math.round((Number(c.price) || 0) * 100),
        0
      );
      const savedCents = Math.max(650, totalOrigCents - totalFinalCents);
      const savedAmount = (savedCents / 100).toFixed(2);
      const uId = nextSafeId('u');
      const aId = nextSafeId('a');
      const replyText = `已生成省钱方案，最多**共可省${savedAmount}元**\n现处于官方大促直降期，已为你从活动会场、百亿补贴与店铺立减通道搜寻优惠。检测到你勾选的 ${chosen.length} 款商品可享官方直降优惠，已为你生成最划算的到手方案：`;

      setMessages((prev) => [
        ...prev,
        {
          id: uId,
          role: 'user',
          text: '帮我生成省钱方案',
          sentProducts: chosen,
        },
        {
          id: aId,
          serverMessageId: aId,
          role: 'assistant',
          userPrompt: '帮我生成省钱方案',
          text: '',
          uiCards: {
            product_cards: chosen.map((c) => ({
              sku_id: c.sku_id,
              title: c.title,
              final_price: c.price,
              original_price: c.orig_price,
              image_path: c.image,
              highlights: [c.spec.replace(' >', ''), c.shop],
            })),
          },
          followupPills: [],
          feedback: null,
        },
      ]);
      playSmoothText(aId, replyText);
    } else {
      // 已购买订单：直接针对选中的订单发起物流与价保/退换核验
      const firstOrd = chosen[0];
      sendMessage(
        `帮我核验订单 ${firstOrd.order_id}（${firstOrd.title}）的物流状态、7天无理由退货资格与15天价保差额`,
        chosen
      );
    }
  };

  /**
   * 点 2：右上角「新对话」：确保前序流与定时器被彻底中断
   */
  const handleStartNewChat = () => {
    stopCurrentStream();
    setMessages([]);
    setPendingImage(null);
    setSessionId(`hg-${Date.now().toString(36)}`);
    setShowPlusTray(false);
    setShowScrollFab(false);
    setSubPage(null);
  };

  const activeSheetItems = sheetTab === 'cart' ? CART_ITEMS : PURCHASED_ITEMS;

  return (
    <div className="huigou-shell">
      {/* 隐藏的系统相册与相机文件选择器（兼容 iOS Safari / Android Chrome / 微信 WebView / PC 浏览器） */}
      <input
        ref={albumInputRef}
        type="file"
        accept="image/*"
        style={{ display: 'none' }}
        onChange={handleImageFileChange}
      />
      <input
        ref={cameraInputRef}
        type="file"
        accept="image/*"
        capture="environment"
        style={{ display: 'none' }}
        onChange={handleImageFileChange}
      />

      {/* ====================================================================
          1. 顶部导航栏：去掉左箭头，居中「惠购」，右侧「新对话」+「•••」
         ==================================================================== */}
      <header className="hg-topbar">
        <div className="hg-topbar-left" />
        <h1 className="hg-topbar-title">惠购</h1>
        <div className="hg-topbar-right">
          <button
            type="button"
            className="hg-newchat-btn"
            onClick={handleStartNewChat}
          >
            新对话
          </button>
          <button
            type="button"
            className="hg-icon-btn"
            aria-label="打开设置界面"
            onClick={() => setSubPage('settings')}
          >
            <IconMore />
          </button>
        </div>
      </header>

      {/* ====================================================================
          2. 主内容滚动区（首屏模块 + 对话流）
         ==================================================================== */}
      <main
        className="hg-main"
        ref={mainScrollRef}
        onScroll={handleMainScroll}
      >
        {/* 首屏 Hero 欢迎区与 3 个核心助手入口卡片 */}
        <section className="hg-hero">
          <div>
            <div className="hg-hero-greeting">
              <div>Hi~</div>
              <div>我是你的购物助手</div>
            </div>
            <p className="hg-hero-sub">试着和我聊聊这些话题吧~</p>
          </div>
          <div className="hg-hero-mascot">
            <HuigouAssistantIllustration />
          </div>
        </section>

        <section className="hg-entry-grid" aria-label="核心助手入口">
          <button
            type="button"
            className="hg-entry-card"
            onClick={() => triggerAssistantTab('buy')}
          >
            <div className="hg-entry-top">
              <span className="hg-entry-title-wrap">
                <span className="hg-entry-badge orange">
                  <IconSearchBadge />
                </span>
                <span className="hg-entry-title">AI帮你买</span>
              </span>
              <span className="hg-entry-arrow">→</span>
            </div>
            <p className="hg-entry-desc">想买什么，告诉我帮你找</p>
          </button>

          <button
            type="button"
            className="hg-entry-card"
            onClick={() => triggerAssistantTab('order')}
          >
            <div className="hg-entry-top">
              <span className="hg-entry-title-wrap">
                <span className="hg-entry-badge green">
                  <IconOrderBadge />
                </span>
                <span className="hg-entry-title">AI查订单</span>
              </span>
              <span className="hg-entry-arrow">→</span>
            </div>
            <p className="hg-entry-desc">查物流、退换货，更省心</p>
          </button>

          <button
            type="button"
            className="hg-entry-card hg-entry-wide"
            onClick={() => triggerAssistantTab('save')}
          >
            <span className="hg-entry-title-wrap">
              <span className="hg-entry-badge red">
                <IconCouponBadge />
              </span>
              <span className="hg-entry-title">AI省钱</span>
            </span>
            <p className="hg-entry-desc">全网好价，帮你划算下单</p>
            <span className="hg-entry-arrow">→</span>
          </button>
        </section>

        {/* 未发起对话时展示横向商品钩子卡片 */}
        {messages.length === 0 && (
          <>
            <div className="hg-section-hint">可以这样问我哦</div>
            <section className="hg-hook-gallery" aria-label="推荐话题卡片">
              {HOME_PRODUCT_HOOKS.map((hook) => (
                <article key={hook.id} className="hg-hook-card">
                  <div>
                    <div className="hg-hook-img-wrap">
                      <span className="hg-hook-seen-tag">{hook.tag}</span>
                      <img
                        className="hg-hook-img"
                        src={hook.image}
                        alt={hook.question}
                        loading="lazy"
                      />
                    </div>
                    <h3 className="hg-hook-question">{hook.question}</h3>
                  </div>
                  <button
                    type="button"
                    className="hg-primary-btn"
                    onClick={() => sendMessage(hook.prompt)}
                  >
                    {hook.buttonText}
                  </button>
                </article>
              ))}
            </section>
          </>
        )}

        {/* 对话消息流 */}
        {messages.length > 0 && (
          <div className="hg-chat-stream">
            {messages.map((msg) => {
              if (msg.role === 'user') {
                return (
                  <div key={msg.id} className="hg-msg-row user">
                    {msg.sentImage && (
                      <div className="hg-user-sent-image-wrap">
                        <img
                          className="hg-user-sent-image"
                          src={msg.sentImage}
                          alt="用户上传图片"
                        />
                        <span className="hg-user-vision-badge">V4.1 视觉识图</span>
                      </div>
                    )}
                    <div className="hg-bubble-user">{msg.text}</div>
                    {msg.sentProducts && msg.sentProducts.length > 0 && (
                      <div className="hg-user-sent-skus">
                        <div className="hg-user-sku-thumbs">
                          {msg.sentProducts.map((sp) => (
                            <div key={sp.id} className="hg-user-sku-box">
                              <img src={sp.image} alt={sp.title} />
                              <span className="hg-user-sku-price">¥{sp.price}</span>
                            </div>
                          ))}
                        </div>
                        <span className="hg-user-sku-caption">
                          发送{msg.sentProducts.length}款商品
                        </span>
                      </div>
                    )}
                  </div>
                );
              }

              const cards = msg.uiCards || {};
              const isRelaxed = msg.decisionMeta?.constraint_status === 'relaxed';
              const maxBudget = msg.decisionMeta?.extracted_constraints?.max_price;

              return (
                <div key={msg.id} className="hg-msg-row assistant">
                  {/* 点 4：回答的人物头像和昵称 */}
                  <div className="hg-assistant-header">
                    <AssistantSmallAvatar />
                    <span className="hg-assistant-name">AI购物助手</span>
                  </div>

                  {/* 白色回答卡片（含正文 + 浅灰 Generative UI Tab + 底部点赞/不满意/重答） */}
                  <div className="hg-bubble-assistant">
                    <div className="hg-clean-text">
                      <CustomerRichText
                        text={msg.text || ''}
                        isStreaming={isStreaming && !msg.text}
                      />
                    </div>

                    {/* 三个助手预设的浅灰底 Generative UI Tab */}
                    {msg.presetType && (
                      <AssistantPresetGrayTabs
                        presetType={msg.presetType}
                        onSendPrompt={(p) => sendMessage(p)}
                        onOpenSheet={(tab) => openProductOrderSheet(tab)}
                      />
                    )}

                    {/* 缺货心愿单浅灰钩子卡 */}
                    {cards.wishlist_card && (
                      <WishlistGenUICard
                        card={cards.wishlist_card}
                        sessionId={sessionId}
                      />
                    )}

                    {/* 双路径松弛或商品导购浅灰卡 */}
                    {isRelaxed && (cards.product_cards || []).length >= 2 ? (
                      <RelaxationGenUICard
                        products={cards.product_cards}
                        maxBudget={maxBudget}
                        subCategory={msg.decisionMeta?.extracted_constraints?.sub_category}
                        onSendPrompt={(p) => sendMessage(p)}
                      />
                    ) : (
                      (cards.product_cards || []).length > 0 && (
                        <ProductGenUICard
                          products={cards.product_cards}
                          priceCards={cards.price_explain_cards}
                        />
                      )
                    )}

                    {/* 订单物流与售后核验浅灰卡 */}
                    {((cards.order_cards && cards.order_cards.length > 0) ||
                      cards.aftersales_card) && (
                      <OrderAftersalesGenUICard
                        orderCards={cards.order_cards}
                        aftersalesCard={cards.aftersales_card}
                      />
                    )}

                    {/* 点 7：精简的卡片底部操作栏（左纯图标 👍 / 👎，右 ↻ 重答） */}
                    {msg.text && (
                      <div className="hg-card-footer-bar">
                        <div className="hg-card-footer-left">
                          <button
                            type="button"
                            className={`hg-icon-square-btn ${
                              msg.feedback === 'like' ? 'active' : ''
                            }`}
                            aria-label="点赞"
                            onClick={() => handleFeedback(msg, 'like')}
                          >
                            <IconThumbUp />
                          </button>
                          <button
                            type="button"
                            className={`hg-icon-square-btn ${
                              msg.feedback === 'dislike' ? 'active' : ''
                            }`}
                            aria-label="不满意"
                            onClick={() => handleFeedback(msg, 'dislike')}
                          >
                            <IconThumbDown />
                          </button>
                        </div>

                        <button
                          type="button"
                          className="hg-retry-btn"
                          onClick={() => handleRetry(msg)}
                        >
                          <IconRetry />
                          <span>重答</span>
                        </button>
                      </div>
                    )}
                  </div>

                  {/* 追问短语气泡 */}
                  {msg.followupPills && msg.followupPills.length > 0 && (
                    <div className="hg-followup-row">
                      {msg.followupPills.slice(0, 2).map((pill, i) => (
                        <button
                          key={i}
                          type="button"
                          className="hg-followup-btn"
                          onClick={() => sendMessage(pill)}
                        >
                          {pill}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </main>

      {/* ====================================================================
          3. 长对话快速回到最新消息的下箭头悬浮按钮 ↓ (点 9)
         ==================================================================== */}
      {showScrollFab && messages.length > 0 && (
        <button
          type="button"
          className="hg-scroll-bottom-fab"
          aria-label="回到最新消息"
          onClick={scrollToBottom}
        >
          <IconArrowDown />
        </button>
      )}

      {/* ====================================================================
          4. 底部快捷胶囊条 + 待发送图片预览条 + 输入框 + 右侧加号展开面板
         ==================================================================== */}
      <footer className="hg-footer">
        {!showPlusTray && !pendingImage && (
          <div className="hg-quick-pills">
            <button
              type="button"
              className="hg-quick-pill"
              onClick={() => triggerAssistantTab('save')}
            >
              <span style={{ color: '#FF5500', display: 'inline-flex' }}>
                <IconCouponBadge />
              </span>
              <span>AI省钱</span>
            </button>
            <button
              type="button"
              className="hg-quick-pill"
              onClick={() => triggerAssistantTab('order')}
            >
              <span style={{ color: '#FF5500', display: 'inline-flex' }}>
                <IconOrderBadge />
              </span>
              <span>订单服务</span>
            </button>
            <button
              type="button"
              className="hg-quick-pill"
              onClick={() => triggerAssistantTab('msg')}
            >
              <span style={{ color: '#FF5500', display: 'inline-flex' }}>
                <IconMsgBadge />
              </span>
              <span>消息助手</span>
            </button>
          </div>
        )}

        {/* 待发送图片缩略图预览与快捷识图提问标签条 */}
        {pendingImage && (
          <div className="hg-image-preview-bar">
            <div className="hg-image-preview-thumb-wrap">
              <img
                src={pendingImage.dataUrl}
                alt="待发送图片预览"
                className="hg-image-preview-thumb"
              />
              <button
                type="button"
                className="hg-image-preview-remove"
                aria-label="移除图片"
                onClick={() => setPendingImage(null)}
              >
                ✕
              </button>
            </div>
            <div className="hg-image-preview-meta">
              <div className="hg-image-preview-hint">
                已就绪 · 点击右侧「发送」或点选快捷指令识图：
              </div>
              <div className="hg-image-preview-tags">
                <button
                  type="button"
                  className="hg-image-tag-chip"
                  onClick={() =>
                    sendMessage('帮我识别图片中的商品，推荐同款或高性价比平替')
                  }
                >
                  找同款/平替
                </button>
                <button
                  type="button"
                  className="hg-image-tag-chip"
                  onClick={() =>
                    sendMessage('帮我识别图中商品并拆解核心规格参数与选购要点')
                  }
                >
                  看核心参数
                </button>
                <button
                  type="button"
                  className="hg-image-tag-chip"
                  onClick={() =>
                    sendMessage('这是我的订单商品实拍凭证，请帮我核验7天无理由退货与15天价保资格')
                  }
                >
                  上传售后凭证
                </button>
              </div>
            </div>
          </div>
        )}

        <form
          className="hg-input-box"
          onSubmit={(e) => {
            e.preventDefault();
            sendMessage();
          }}
        >
          {/* 点 5：点击语音输入提示「暂不支持该功能」 */}
          <button
            type="button"
            className="hg-icon-btn"
            aria-label="语音输入"
            onClick={() => triggerToast('暂不支持该功能')}
          >
            <IconVoice />
          </button>

          <input
            type="text"
            className="hg-text-input"
            placeholder={
              pendingImage
                ? '补充图片问题（如：预算150以内有同款吗）或直接点发送...'
                : '发消息或点右侧 + 号拍照/传图识物...'
            }
            value={inputText}
            onChange={(e) => setInputText(e.target.value)}
            disabled={isStreaming}
          />

          {/* 点 9：订单/购物袋图标直接打开「已购买 / 购物车」半屏弹窗 */}
          <button
            type="button"
            className="hg-icon-btn"
            aria-label="打开订单服务与商品选择"
            onClick={() => openProductOrderSheet('purchased')}
          >
            <IconBagPlus />
          </button>

          {inputText.trim() || pendingImage ? (
            <button type="submit" className="hg-send-btn" disabled={isStreaming}>
              发送
            </button>
          ) : (
            <button
              type="button"
              className="hg-icon-btn"
              aria-label="展开更多面板"
              onClick={() => setShowPlusTray((v) => !v)}
            >
              {showPlusTray ? <IconCloseCircle /> : <IconPlusCircle />}
            </button>
          )}
        </form>

        {/* 点击加号展开的多模态扩展面板：拍照 + 照片（手机相册） + 订单服务 */}
        {showPlusTray && (
          <div className="hg-plus-tray">
            <button
              type="button"
              className="hg-plus-tray-item"
              onClick={handleTakePhoto}
            >
              <div className="hg-plus-tray-icon">
                <IconCamera size={26} />
              </div>
              <span className="hg-plus-tray-label">拍照</span>
            </button>

            <button
              type="button"
              className="hg-plus-tray-item"
              onClick={handlePickAlbum}
            >
              <div className="hg-plus-tray-icon">
                <IconPhoto size={26} />
              </div>
              <span className="hg-plus-tray-label">照片</span>
            </button>

            <button
              type="button"
              className="hg-plus-tray-item"
              onClick={() => openProductOrderSheet('purchased')}
            >
              <div className="hg-plus-tray-icon">
                <IconBagPlus size={26} />
              </div>
              <span className="hg-plus-tray-label">订单服务</span>
            </button>
          </div>
        )}

        <div className="hg-ai-footnote">内容由 AI 生成</div>
      </footer>

      {/* 桌面端 WebRTC 实时摄像头拍照弹窗（移动端直接调起系统相机） */}
      {cameraModalOpen && (
        <div className="hg-sheet-backdrop" onClick={closeCameraModal}>
          <div
            className="hg-camera-modal"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="hg-sheet-header">
              <div className="hg-sheet-title">拍照识商品</div>
              <button
                type="button"
                className="hg-sheet-close"
                aria-label="关闭相机"
                onClick={closeCameraModal}
              >
                ✕
              </button>
            </div>
            <div className="hg-camera-Stage">
              <video
                ref={videoRef}
                className="hg-camera-video"
                playsInline
                muted
              />
            </div>
            <div className="hg-camera-actions">
              <button
                type="button"
                className="hg-secondary-btn"
                onClick={() => {
                  closeCameraModal();
                  handlePickAlbum();
                }}
              >
                从相册选图
              </button>
              <button
                type="button"
                className="hg-primary-btn"
                onClick={captureWebRTCFrame}
              >
                立即拍照
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ====================================================================
          5. 非全覆盖半屏弹窗：「订单服务 / 选择商品」（只保留「已购买」和「购物车」）
         ==================================================================== */}
      {sheetOpen && (
        <div
          className="hg-sheet-backdrop"
          onClick={() => setSheetOpen(false)}
        >
          <div
            className="hg-sheet-panel"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="hg-sheet-header">
              <div className="hg-sheet-title">订单服务</div>
              <button
                type="button"
                className="hg-sheet-close"
                aria-label="关闭弹窗"
                onClick={() => setSheetOpen(false)}
              >
                ✕
              </button>
            </div>

            <div className="hg-sheet-tabs">
              <button
                type="button"
                className={`hg-sheet-tab ${sheetTab === 'purchased' ? 'active' : ''}`}
                onClick={() => {
                  setSheetTab('purchased');
                  setSelectedIds([]);
                }}
              >
                已购买
              </button>
              <button
                type="button"
                className={`hg-sheet-tab ${sheetTab === 'cart' ? 'active' : ''}`}
                onClick={() => {
                  setSheetTab('cart');
                  setSelectedIds([]);
                }}
              >
                购物车
              </button>
            </div>

            <div className="hg-sheet-list">
              {activeSheetItems.map((item) => {
                const isChecked = selectedIds.includes(item.id);
                return (
                  <div
                    key={item.id}
                    className="hg-sheet-row"
                    onClick={() => toggleSelectSheetItem(item.id)}
                  >
                    <div className={`hg-radio-circle ${isChecked ? 'checked' : ''}`}>
                      {isChecked && '✓'}
                    </div>
                    <img
                      className="hg-sheet-thumb"
                      src={item.image}
                      alt={item.title}
                    />
                    <div className="hg-sheet-meta">
                      <div className="hg-sheet-item-title">{item.title}</div>
                      <div className="hg-sheet-item-spec">{item.spec}</div>
                      <div className="hg-sheet-item-price">¥{item.price}</div>
                      <div className="hg-sheet-item-shop">{item.shop}</div>
                    </div>
                  </div>
                );
              })}
            </div>

            <div className="hg-sheet-footer">
              <button
                type="button"
                className="hg-sheet-confirm-btn"
                disabled={selectedIds.length === 0}
                onClick={handleConfirmSheetSelection}
              >
                确认选择
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ====================================================================
          6. 右上角「•••」设置界面与「关于：免责声明」独立页面 (点 6)
         ==================================================================== */}
      {subPage === 'settings' && (
        <div className="hg-subpage">
          <div className="hg-subpage-header">
            <button
              type="button"
              className="hg-icon-btn"
              onClick={() => setSubPage(null)}
            >
              <IconBack />
            </button>
            <span className="hg-subpage-title">惠购</span>
          </div>

          <div className="hg-settings-group-label">功能</div>
          <button
            type="button"
            className="hg-settings-cell"
            onClick={() => {
              setMessages([]);
              setSubPage(null);
              triggerToast('已清空聊天记录');
            }}
          >
            <span>清空聊天记录</span>
          </button>

          <div className="hg-settings-group-label">关于</div>
          <button
            type="button"
            className="hg-settings-cell"
            onClick={() => setSubPage('disclaimer')}
          >
            <span>免责声明</span>
            <span style={{ color: 'var(--muted)' }}>&gt;</span>
          </button>
        </div>
      )}

      {subPage === 'disclaimer' && (
        <div className="hg-subpage">
          <div className="hg-subpage-header">
            <button
              type="button"
              className="hg-icon-btn"
              onClick={() => setSubPage('settings')}
            >
              <IconBack />
            </button>
            <span className="hg-subpage-title">免责声明</span>
          </div>

          <div className="hg-disclaimer-card">
            <div className="hg-disclaimer-section">
              <h3>一、软件性质</h3>
              <p>
                「惠购」系基于人工智能技术构建的全平台电商导购与订单售后服务助手，旨在为用户提供商品参数筛选、好价试算、物流查询与售后资格核验参考。
              </p>
            </div>
            <div className="hg-disclaimer-section">
              <h3>二、使用限制</h3>
              <p>
                AI 生成的导购建议、穿搭与护肤参考基于平台商品公开数据整理，不构成医疗、法律或专业领域承诺；具体商品实时库存、促销活动与最终结算金额以实际下单页面为准。
              </p>
            </div>
            <div className="hg-disclaimer-section">
              <h3>三、用户责任</h3>
              <p>
                请勿在对话中输入银行卡密码、验证码等高度敏感个人隐私信息。用户在采纳导购建议或发起退换货、价保申请前，应核对订单号与商品规格信息并自行承担相应交易决策责任。
              </p>
            </div>
          </div>
        </div>
      )}

      {/* 居中轻提示 Toast */}
      {toastMsg && <div className="hg-toast">{toastMsg}</div>}
    </div>
  );
}
