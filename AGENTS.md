# AGENTS.md — 全栈 AI 导购系统「惠购」入口地图（≤120 行）

> **铁律**：本文件是 Agent 进入本仓库的唯一入口地图（严格 ≤120 行）。只放导航与硬约束，详情查链接。

## 1. 仓库全栈地图

```text
ai-sales-assistant/
├── app/                        # FastAPI 后端主服务 (Port: 8000)
│   ├── main.py                 # 路由入口：SSE 流式对话 + 缺货心愿单钩子 + OA 管理接口
│   ├── core/chat_orchestrator.py # 272K 状态机 + 软硬双锁 GenUI 编排器
│   └── services/               # Jev门控 / 规则清洗 / 混合检索与双路径松弛 / 订单核验 / OA统计
├── h5-frontend/                # [C端唯一入口] 「惠购」全平台购物助手 (千问同款清爽风, /h5/)
├── admin-frontend/             # [B端唯一入口] 惠购正式 OA 运营管理后台 (浅蓝简约风+亮暗切换, /admin/)
├── static/showcase.html        # [SUNSET 已日落] 早期单页联调看板，仅保留兼容，禁止新增功能
├── data/                       # 60 SKU真源 / 17子类目注册表 / 冲突松弛对 / SQLite 运营库
├── docs/SUNSET_AND_ROADMAP.md  # 功能演进规划与 Sunset 日落封存规范
├── DESIGN.md                   # 前端生成必读：反 AI 味网页设计规约
└── scripts/                    # 7 套自动化回归与 E2E 门禁脚本 (一律使用 python -B)
```

## 2. 快速启动与验证命令

- **启动全栈服务**：`python -B -m uvicorn app.main:app --host 0.0.0.0 --port 8000`
- **构建 C 端惠购 H5**：`cd h5-frontend && npm run build`（访问 `http://127.0.0.1:8000/h5/`）
- **构建 B 端 OA 后台**：`cd admin-frontend && npm run build`（访问 `http://127.0.0.1:8000/admin/`）
- **全量无缓存门禁**：`python -B scripts/validate_catalog.py && python -B scripts/run_eval.py`

## 3. 核心架构与踩坑强约定速查表

| 领域 | 一句话强约定 | 详见 |
|---|---|---|
| **无字节码污染** | 严禁产生 `__pycache__` 或 `.pyc`，运行 Python 必加 `-B` | `HANDOFF.md` |
| **前端反 AI 味** | 严禁玻璃拟态、紫蓝渐变、满屏解释性小字、Emoji 充当图标 | `DESIGN.md` |
| **C 端定位** | 「惠购」为全平台统一导购，不分多店铺；遇未上架品类必出 `wishlist_card` 缺货钩子 | `chat_orchestrator.py` |
| **启发1：软硬双锁** | LLM 仅负责自然话术（软锁）；价格/差价/退货时效/HTML清洗 100% 走协议层确定性硬锁 | `rule_governance.py` |
| **启发2：上下文缝补** | 外置到 DB/JSON 的规格池、Prompt Hash 与缺货钩子，统一由 `/api/v1/admin/context-manifest` 缝补回视野 | `admin_analytics_service.py` |
| **启发4：优雅日落** | 旧版 `static/showcase.html` 与多租户店铺切换已进入 Sunset 凝固期，见日落文档 | `docs/SUNSET_AND_ROADMAP.md` |
| **主生成网关** | 锁定 `http://127.0.0.1:8367/v1` (`chat` 协议, Key `123`, `deepseek-flash`) | `app/config.py` |

## 4. 四端当前状态快照

| 模块 | 状态 | 关键能力 |
|---|---|---|
| **C 端「惠购」(`/h5/`)** | `ACTIVE` | 1:1 复刻千问导购交互 + 5 类流内交互式 Generative UI + 缺货品类心愿单钩子 |
| **B 端 OA 后台 (`/admin/`)** | `ACTIVE` | 政企浅蓝简约 OA 风（支持亮/暗切换）+ 缺货心愿单统计 + 上下文缝补快照 + IP/反馈审计 |
| **后端引擎 (`app/`)** | `ACTIVE` | System One Jev + BM25/1024维向量 RRF + 双路径松弛 + 168h/360h 订单售后核验 |
| **旧版看板 (`showcase.html`)** | `SUNSET` | 凝固封存，仅服务 `test_e2e_showcase.py` 向后兼容测试 |
