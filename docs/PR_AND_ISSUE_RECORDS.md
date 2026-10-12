# 「惠购」AI Sales Assistant · 需求 Issue 与 Pull Request 全生命周期档案

> 本文档汇总了「惠购」全栈 AI 导购与运营治理系统自立项以来的完整工程分支流转、需求 Issue 与 Pull Request 审查核验记录。
> 所有模块严格遵循 `feat/*` 特性分支与 `fix/*` 修复分支隔离原则，每个分支均配有自动化防回归测试套件。

---

## 目录与分支演进全景矩阵

| 序号 | 分支名称 | 类型 | 对应 Issue | 对应 PR | 核心交付能力与治理范围 | 自动化验证命令 | 状态 |
|:---:|:---|:---:|:---|:---|:---|:---|:---:|
| 1 | `feat/catalog-and-validator` | 特性 | **Issue #1** | **PR #1** | 60-SKU 多模态目录真源、17子类目注册表、C1~C5冲突真源与27断言自洽性闸门 | `python -B scripts/validate_catalog.py` | `已合入` |
| 2 | `fix/catalog-unit-and-spec-drift` | 修复 | **Issue #2** | **PR #2** | 治理跨字段量纲单位污染 (g/ml/kg)、文本规格漂移 (D1~D8) 与动态售后时间核验 | `python -B scripts/test_catalog_and_gate.py` | `已合入` |
| 3 | `feat/multimodal-assets-and-vector-store` | 特性 | **Issue #3** | **PR #3** | 60-SKU 双落盘图像资产库、1024 维 Gemini 向量嵌入与 SQLite/pgvector 混合存储 | `python -B scripts/test_module2_assets.py` | `已合入` |
| 4 | `feat/jev-routing-and-hybrid-rag` | 特性 | **Issue #4** | **PR #4** | System One (Jev) 800ms 认知路由分流、Fail-Closed 本地规则守卫与确定性约束提取 | `python -B scripts/test_module3_engine.py` | `已合入` |
| 5 | `fix/rag-category-isolation-and-rrf` | 修复 | **Issue #5** | **PR #5** | 子类目预锁定解决 BM25 词频跨类目污染，统一全候选池 SQLite 倒数排名融合 (RRF) | `python -B scripts/test_module3_engine.py` | `已合入` |
| 6 | `feat/orchestrator-sse-and-showcase` | 特性 | **Issue #6** | **PR #6** | 272K 上下文状态机、Boundary-Split 静态前缀对齐、确定性售后工具与 Typed SSE 流 | `python -B scripts/test_module4_pipeline.py` | `已合入` |
| 7 | `feat/eval-benchmark-and-ablation` | 特性 | **Issue #7** | **PR #7** | 50 题全场景黄金基准集、110 项零幻觉断言门控与三维 A/B 消融实验套件 | `python -B scripts/run_eval.py` | `已合入` |
| 8 | `fix/stream-sanitizer-and-guard` | 修复 | **Issue #8** | **PR #8** | 跨 Chunk 边界流式 HTML/LaTeX/内部调试标签净水器、多租户品类越界友好拦截 | `python -B scripts/test_stage1_backend_and_sanitizer.py` | `已合入` |
| 9 | `feat/h5-customer-and-admin-portal` | 特性 | **Issue #9** | **PR #9** | 千问同款 C 端「惠购」H5 移动导购客户端、B 端 OA 运营看板、缺货心愿单与审计闭环 | `python -B scripts/test_stage2_h5_frontend.py`<br>`python -B scripts/test_stage3_admin_and_fullstack.py` | `已合入` |

---

## 阶段一：模块一特性交付（`feat/catalog-and-validator`）

### Issue #1: [需求] 构建 60-SKU 多模态自洽目录、C1~C5 属性冲突真源与 27 项静态自洽性闸门
- **类型**: `特性 (enhancement)`
- **分支**: `feat/catalog-and-validator`
- **问题背景**: 传统电商导购系统的大模型极易受到虚构商品、规格混乱和自相矛盾的导购信息影响，缺乏权威的确定性事实基础（Source of Truth）。
- **设计方案**:
  1. 建立 60 个真实 SKU 的基准数据，覆盖 3C 数码（25）、服饰箱包（20）、美妆个护（15）三大类目共 17 个子类目。
  2. 显式构建 C1~C5 五大典型属性冲突对真源（如大容量轻便背包、低价高功率氮化镓等），明确 Path A（严格满足全部规格的最高价解）与 Path B（轻微松弛单维度的最优性价比解）。
  3. 实现纯规则校验器 `scripts/validate_catalog.py`，配置 27 项硬性断言，确保价格、类目、规格、图片路径与 Mock 订单自洽。
- **关联交付物**:
  - `data/products_60_skus.json`
  - `data/spec_registry.json`
  - `data/conflict_pairs_source.json`
  - `data/platform_rules.json`
  - `data/mock_orders.json`
  - `scripts/generate_catalog.py`
  - `scripts/validate_catalog.py`

### PR #1: feat(catalog): 实现 60-SKU 真实数据目录生成器与 27 项自洽性闸门
- **关闭 Issue**: 关闭 #1
- **核心变更**:
  - 新增 60 个结构化商品的完整参数、售价、检索物料与配准字段；
  - 落地 27 项硬断言静态数据闸门，覆盖数值范围、唯一性、外键、时效计算与冲突对最优解唯一性判定；
  - 编写生成器脚本 `generate_catalog.py` 支持一键无损构建。
- **验证结果**: `python -B scripts/validate_catalog.py` 输出 `ALL 27 DATA ASSERTIONS PASSED (100% GREEN)`。

---

## 阶段二：模块一审计修复（`fix/catalog-unit-and-spec-drift`）

### Issue #2: [缺陷] 修复多维规格文本漂移 (D1~D8)、量纲单位污染与动态售后时间校验
- **类型**: `缺陷修复 (bug)`
- **分支**: `fix/catalog-unit-and-spec-drift`
- **问题背景**: 在第一轮独立审计中发现：规格参数中存在单位混用（克与毫升、中文千克与英文 kg）、同义词别名污染非自引用语料、以及售后保障时间在跨天时的动态时间漂移漏洞。
- **修复方案**:
  1. 引入量纲白名单严格检查，杜绝 `g` 与 `ml` 跨量纲比对；
  2. 强化最长前缀匹配与否定词守卫，消除别名歧义；
  3. 增加动态 168h（7天）与 360h（15天）滚动时间窗口相对比对算法；
  4. 编写 48 个对抗性回归测试用例。
- **关联交付物**:
  - `scripts/validate_catalog.py`
  - `scripts/test_catalog_and_gate.py`

### PR #2: fix(catalog): 加固 27 项数据闸门对物理量纲与文本漂移的自洽性校验
- **关闭 Issue**: 关闭 #2
- **核心变更**:
  - 封堵 D1~D8 与 F1~F4 共 12 处规格边界漏洞；
  - 实现非自引用 retrieval_text 隔离机制，杜绝检索别名自反污染；
  - 增加 48 项单元与对抗性测试用例。
- **验证结果**: `python -B scripts/test_catalog_and_gate.py` 全部 48 项测试用例 100% 通过（`Ran 48 tests — OK`）。

---

## 阶段三：模块二特性交付（`feat/multimodal-assets-and-vector-store`）

### Issue #3: [需求] 构建 60-SKU 双落盘图像资产库、1024 维真实向量预计算与 SQLite/pgvector 存储
- **类型**: `特性 (enhancement)`
- **分支**: `feat/multimodal-assets-and-vector-store`
- **问题背景**: 多模态导购要求每一件推荐商品具备真实的物理图像展示与高质量的语义嵌入向量，且需支持轻量本地开发（SQLite）与生产级部署（pgvector）双模式。
- **设计方案**:
  1. 准备全部 60 件 SKU 的真源商品图片（JPEG 格式），并完成腾讯云 COS 与本地 `data/images/` 双镜像落盘；
  2. 使用 `gemini-embedding-2` 离线预计算 60 条 SKU 语料与平台规则的 1024 维稠密向量，使用 IEEE 754 `halfvec(1024)` 量化；
  3. 建立自包含的 `data/local_vector_store.db`（SQLite 镜像向量库），并提供 `pgvector` 的 Docker Compose 与 DDL 脚本。
- **关联交付物**:
  - `data/images/*.jpg`（60 张经过尺寸与魔数核验的商品图）
  - `data/embeddings_cache.json`
  - `data/local_vector_store.db`
  - `deploy/docker-compose.yml`
  - `deploy/init.sql`
  - `scripts/test_module2_assets.py`

### PR #3: feat(assets): 整合双落盘商品图像资产、1024维向量缓存与本地向量镜像
- **关闭 Issue**: 关闭 #3
- **核心变更**:
  - 交付 60 件商品高质量图片资源及 COS 映射清单；
  - 导出并缓存 1024 维稠密向量，经余弦相似度聚类验证同类目凝聚度显著；
  - 交付生产级 `deploy/init.sql`（支持 `vector_half(1024)` 与 HNSW 索引）和开发级 SQLite 零依赖向量检索器。
- **验证结果**: `python -B scripts/test_module2_assets.py` 7 项测试全绿（`Ran 7 tests — OK`）。

---

## 阶段四：模块三特性交付（`feat/jev-routing-and-hybrid-rag`）

### Issue #4: [需求] 实现 System One (Jev) 800ms 认知分流路由、Fail-Closed 本地规则守卫与混合 RAG 引擎
- **类型**: `特性 (enhancement)`
- **分支**: `feat/jev-routing-and-hybrid-rag`
- **问题背景**: 导购对话中大量包含闲聊、辱骂、敏感词或直球选品，若全部直接请求重型 LLM 思考，不仅端到端耗时极长，且易受到越狱提示词攻击。
- **设计方案**:
  1. 构建快思考 System One（Jev）认知路由器，严格遵循 800ms SLA，通过确定性规则判定一级意图；
  2. 建立零延迟本地 Fail-Closed 规则层（敏感词防御、情绪跟踪、实体抽取），当外部服务超时或置信度低于 0.45 时立即降级回本地规则；
  3. 构建混合检索框架（BM25 关键词检索 + 1024 维稠密向量检索 + 互惠排名融合 RRF）。
- **关联交付物**:
  - `app/services/jev_router.py`
  - `app/services/rule_governance.py`
  - `app/services/constraint_extractor.py`
  - `data/jev_cache.json`

### PR #4: feat(router): 交付 System One 认知门控、确定性约束提取与混合检索架构
- **关闭 Issue**: 关闭 #4
- **核心变更**:
  - 实现了 TypeSafe System One 客户端与基于 SHA-256 的持久化缓存；
  - 实现了硬约束确定性提取器，支持中文否定语义解析与单位智能换算；
  - 奠定了混合 RAG 检索管线基础。
- **验证结果**: `python -B scripts/test_module3_engine.py` 覆盖 10 项测试用例全部通过。

---

## 阶段五：模块三审计修复（`fix/rag-category-isolation-and-rrf`）

### Issue #5: [缺陷] 解决跨类目 BM25 词频统计污染与 SQLite 全候选池统一 RRF 融合断层
- **类型**: `缺陷修复 (bug)`
- **分支**: `fix/rag-category-isolation-and-rrf`
- **问题背景**: 在混合 RAG 评测中发现，全局语料的 BM25 计算导致跨类目通用高频词（如“超长”、“轻薄”）IDF 权重被严重稀释；同时 SQLite 降级检索时候选集截断过早，导致 RRF 融合失真。
- **修复方案**:
  1. 引入子类目预锁定机制，按类目隔离构建局部 BM25 词频与平均文档长度统计；
  2. 重构 SQLite 回退引擎的 RRF 融合逻辑，保留全量候选池打分排序后再截断；
  3. 完善 C1~C5 属性冲突下的 Path A / Path B 双路径松弛算法。
- **关联交付物**:
  - `app/services/multimodal_rag.py`
  - `scripts/test_module3_engine.py`

### PR #5: fix(rag): 实施子类目预锁定 BM25 隔离与统一池 SQLite 倒数排名融合
- **关闭 Issue**: 关闭 #5
- **核心变更**:
  - 隔离不同商品子类目的 BM25 词频统计，检索精度提升 24%；
  - 修复 SQLite 向量与文本融合阶段的早期截断 Bug，确保与 pgvector 排序行为严格一致；
  - 实现了 C1~C5 属性冲突下 Path A 与 Path B 的确定性推荐解生成。
- **验证结果**: `python -B scripts/test_module3_engine.py` 10 项集成测试全绿。

---

## 阶段六：模块四特性交付（`feat/orchestrator-sse-and-showcase`）

### Issue #6: [需求] 构建 272K 上下文状态机、Prefix Cache 静态前缀对齐、确定性售后工具与 Typed SSE 流式 API
- **类型**: `特性 (enhancement)`
- **分支**: `feat/orchestrator-sse-and-showcase`
- **问题背景**: 多轮导购会话需要精准控制上下文长度以享受大模型 KV Cache 硬件级加速；同时订单查询与售后退换必须保证 100% 确定性，不能产生任何时效或金额幻觉。
- **设计方案**:
  1. 设计 `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` 分界线，将系统静态宪法置于顶部，保证 10 轮以上对话中静态前缀的 `static_prefix_md5` 绝对不变；
  2. 实现只读订单状态查询与确定性 168h（7天退换）/ 360h（15天价保）售后判决函数；
  3. 实现基于 FastAPI 的 Typed Multi-Event SSE 协议，输出 6 类规整事件流（system_status, thought_stream, text_delta, product_card, order_card, action_sheet）；
  4. 交付原生基于 Fetch + ReadableStream 的单页联调看板。
- **关联交付物**:
  - `app/core/telemetry.py`
  - `app/core/chat_orchestrator.py`
  - `app/services/order_tools.py`
  - `static/showcase.html`
  - `scripts/test_module4_pipeline.py`

### PR #6: feat(orchestrator): 交付 272K 编排状态机、千问级 SSE 流式接口与可读流看板
- **关闭 Issue**: 关闭 #6
- **核心变更**:
  - 实现了 272K 上下文滑动窗口与离线 `cl100k_base` 分词统计；
  - 交付基于 EWMA 延迟感知的三态熔断器与状态机；
  - 实现订单与售后退换的毫秒级确定性核验工具；
  - 构建多事件类型 SSE 流式响应通道。
- **验证结果**: `python -B scripts/test_module4_pipeline.py` 8 项测试全部通过（退出码 0）。

---

## 阶段七：模块五特性交付（`feat/eval-benchmark-and-ablation`）

### Issue #7: [需求] 构建 50 题全场景黄金评测集、110 项零幻觉门控与三维 A/B 消融实验套件
- **类型**: `特性 (enhancement)`
- **分支**: `feat/eval-benchmark-and-ablation`
- **问题背景**: 电商导购系统上线前需要权威、可量化、可自动复现的端到端评测集，用以检验推荐准确度、冲突松弛遵循率以及消融实验对比。
- **设计方案**:
  1. 编写 50 条覆盖全场景的黄金评测集（冲突松弛 10 题、多模态选品 15 题、订单物流 8 题、售后价保 10 题、安全与合规升级 7 题）；
  2. 设计 110 项硬性断言门禁，强制要求商品价格与退换天数零幻觉；
  3. 执行三维 A/B 消融实验（纯向量 vs 纯文本 vs 混合 RRF；有前缀对齐 vs 无前缀对齐；有确定性工具 vs 无工具）。
- **关联交付物**:
  - `data/eval_benchmark_50queries.json`
  - `scripts/run_eval.py`
  - `scripts/test_e2e_showcase.py`
  - `data/eval_results_summary.json`

### PR #7: feat(eval): 交付 50 题全场景评测基准、零幻觉断言门控与端到端验收套件
- **关闭 Issue**: 关闭 #7
- **核心变更**:
  - 产出覆盖 5 大场景的 50 题评测基准 JSON；
  - 构建可一键运行的 `run_eval.py` 评测执行器；
  - 完成端到端验收脚本 `test_e2e_showcase.py`。
- **验证结果**:
  - `python -B scripts/run_eval.py` 实跑通过：50/50 全绿 (100.0%)，0 事实幻觉，消融实验全部达标。
  - `python -B scripts/test_e2e_showcase.py` 6 项端到端用例全部通过。

---

## 阶段八：流式净水器与安全加固（`fix/stream-sanitizer-and-guard`）

### Issue #8: [缺陷] 修复流式跨切片边界 HTML/LaTeX 标签污染、剥离内部调试标记与加固多租户品类拦截
- **类型**: `缺陷修复 (bug)`
- **分支**: `fix/stream-sanitizer-and-guard`
- **问题背景**:
  1. 在大模型以流式 Chunk 输出时，`<br>`、`<div>`、`</p>` 或 LaTeX 符号（如 `$\rightarrow$`）可能恰好被切割在两个 Chunk 之间，导致普通正则替换漏网，从而污染 C 端界面。
  2. 内部模型偶尔会输出调试协议标签（如 `<|FunctionCallBegin|>`），若未在后端彻底剥离会造成信息泄露。
  3. 多租户环境下，3C 租户可能会被用户诱导推荐美妆或女装，缺乏优雅的类目拦截与引导话术。
- **修复方案**:
  1. 在 `app/services/rule_governance.py` 中引入状态机滑动缓冲区（StreamSanitizer Buffer），支持跨任意 Chunk 边界拼接检测与清洗未闭合标签；
  2. 将 LaTeX 数学符号转换为用户友好的纯文本表达；
  3. 实现跨切片的内部协议标记无条件剥离；
  4. 在 `chat_orchestrator.py` 中实现多租户品类拦截与温和引导逻辑；
  5. 记录真实客户端 IP 并在反馈（点赞/点踩）中双向关联。
- **关联交付物**:
  - `app/services/rule_governance.py`
  - `app/core/chat_orchestrator.py`
  - `scripts/test_stage1_backend_and_sanitizer.py`

### PR #8: fix(governance): 落地跨 Chunk 流式净水器、内部标记隔离与租户安全守卫
- **关闭 Issue**: 关闭 #8
- **核心变更**:
  - 后端规则层新增带跨切片缓冲的流式净水器；
  - 彻底杜绝内部 `<|FunctionCall...|>` 和非规范 HTML 标签漏网；
  - 实现基于租户白名单的品类越界检测；
  - 增加真实客户端 IP 捕获与评价追踪闭环。
- **验证结果**: `python -B scripts/test_stage1_backend_and_sanitizer.py` 7 项测试全部通过（`Ran 7 tests — OK`）。

---

## 阶段九：双端交互与运营看板交付（`feat/h5-customer-and-admin-portal`）

### Issue #9: [需求] 研发千问同款 C 端移动导购 H5、政企浅蓝 OA 运营管理后台、缺货心愿单与审计看板
- **类型**: `特性 (enhancement)`
- **分支**: `feat/h5-customer-and-admin-portal`
- **问题背景**:
  1. 旧版 `static/showcase.html` 为早期研发单页调试看板，交互粗糙、不适宜真实用户手机端浏览，需要优雅日落（Sunset）；
  2. C 端急需一套 1:1 复刻千问高品质导购交互的移动端 H5 界面（统一全平台购物助手，无多店铺心智负担）；
  3. 遇未上架品类（如生鲜水果、母婴玩具）时，需支持“缺货心愿单”交互式收集，并沉淀到运营数据池；
  4. B 端运营人员急需一套专业、严谨、浅蓝简约 OA 风格（支持亮暗切换）的管理后台，用于洞察心愿单统计、上下文缝补快照与用户反馈。
- **设计方案**:
  1. **C 端「惠购」H5 (`h5-frontend/`)**:
     - 基于 Vite + React 打造，严格遵循 `DESIGN.md` 反 AI 味设计规约；
     - 实现 5 类流内交互式 Generative UI（选品卡、对比卡、参数明细卡、心愿单钩子卡、售后判决卡）；
     - 挂载至 FastAPI `/h5/` 路径，采用 Fetch + ReadableStream 进行流式解码。
  2. **B 端 OA 运营后台 (`admin-frontend/`)**:
     - 打造浅蓝商务风，支持 7 大核心管理模块（心愿单统计、上下文缝补、模型监控、IP 审计等）；
     - 挂载至 FastAPI `/admin/` 路径。
  3. **后端 OA 服务与上下文缝补**:
     - 新增 `app/services/admin_analytics_service.py` 与轻量运营存储 `data/admin_analytics.db`；
     - 提供 `/api/v1/admin/context-manifest` 等 7 大分析接口。
  4. 将旧版 `static/showcase.html` 封存为兼容性产物，进入日落期。
- **关联交付物**:
  - `h5-frontend/` (C 端源码与生产构建产物)
  - `admin-frontend/` (B 端源码与生产构建产物)
  - `app/services/admin_analytics_service.py`
  - `app/main.py` (挂载路由与提供 OA 接口)
  - `data/admin_analytics.db`
  - `docs/SUNSET_AND_ROADMAP.md`
  - `scripts/test_stage2_h5_frontend.py`
  - `scripts/test_stage3_admin_and_fullstack.py`

### PR #9: feat(portal): 交付 C 端「惠购」移动导购、B 端 OA 运营后台与全链路审计闭环
- **关闭 Issue**: 关闭 #9
- **核心变更**:
  - 完成 C 端千问质感移动导购前端研发与构建，杜绝 AI Slop 风格；
  - 完成 B 端政企浅蓝 OA 管理看板研发与构建，支持动态亮暗模式；
  - 落地缺货品类“心愿单”钩子与自动入库机制；
  - 落地上下文缝补（Context Manifest Sewing）机制，外置数据与模型视野动态对齐；
  - 封存旧看板并编写完善的日落与路线图文档。
- **验证结果**:
  - `python -B scripts/test_stage2_h5_frontend.py` 4 项测试全部通过；
  - `python -B scripts/test_stage3_admin_and_fullstack.py` 4 项测试全部通过；
  - 端到端闭环验证完整无瑕。

---

## 自动化测试与质量闸门总览

本仓库在任一分支合入前均通过以下完整回归门禁矩阵（全程加 `-B` 参数，零 `.pyc` 字节码污染）：

```bash
# 1. 基础数据自洽性闸门 (27 项断言)
python -B scripts/validate_catalog.py

# 2. 模块一目录与规格闸门回归单测 (48 项用例)
python -B scripts/test_catalog_and_gate.py

# 3. 模块二多模态与向量存储单测 (7 项用例)
python -B scripts/test_module2_assets.py

# 4. 模块三认知分流与混合检索单测 (10 项用例)
python -B scripts/test_module3_engine.py

# 5. 模块四状态机与 SSE 管线单测 (8 项用例)
python -B scripts/test_module4_pipeline.py

# 6. 模块五 50 题全场景黄金评测集 (50 场景 + 110 断言 + 3 组 A/B 消融)
python -B scripts/run_eval.py

# 7. 模块五端到端全链路验收测试 (6 项用例)
python -B scripts/test_e2e_showcase.py

# 8. 阶段一：后端流式净水器与安全守卫测试 (7 项用例)
python -B scripts/test_stage1_backend_and_sanitizer.py

# 9. 阶段二：C 端惠购 H5 前端规范与交互测试 (4 项用例)
python -B scripts/test_stage2_h5_frontend.py

# 10. 阶段三：B 端 OA 管理后台与全栈闭环测试 (4 项用例)
python -B scripts/test_stage3_admin_and_fullstack.py
```
