# 自动化评测体系重构与 Admin UI/UX 工业级升级规约

> **文档定位**：本文档为惠购 AI 购物助手「自动化评测体系重构」与「Admin 管理后台评测中心」的最终落地技术设计规范。
> **双规约基准**：严格贯彻项目根目录 [`DESIGN.md`](../DESIGN.md)（反 AI 味网页设计规约）与 [`humanizer-zh`](file:///D:/projectCollection/xiaohongshu/.agents/skills/humanizer-zh/SKILL.md)（去除 AI 痕迹指南）。
> **核心目标**：彻底解决当前 Admin 评测模块“假动态、纯静态读文件、缺接口、缺展示、小字拥挤紧凑、辞藻堆砌、人机割裂”的深层缺陷，建立管理员可视、可触发、可下钻、可修订的专业级大模型评测中台。

---

## 一、 评测机理与人类协同定位（Human-in-the-Loop）

### 1. 当前自动化评测的底层运作机理
当前评测流水线（`scripts/run_eval.py`）是一个**高精度的确定性回归测试沙箱**：
- **测试用例输入**：50 道单轮业务基准题（覆盖规格冲突松弛、精准选品、订单物流、7天退货/保价、12315维权） + 10 组多轮对话会话树（共 30 轮交互，考核上下文继承、代词锁定、跨品类转移、连续发怒状态机）。
- **三层断言金字塔（691 项断言）**：
  - **L1 物理事实断言（211项）**：到手价与直降差额、发货仓与 24h 时效、7天无理由（168h滚动窗口+未拆封）、15天价保（360h+签收次日0点起算）；
  - **L2 决策状态机断言（160项）**：意图路由、双路径松弛（Path A / Path B）数学唯一解、连续催促与升级触发 `handoff_guard`；
  - **L3 语义反向闭包断言（320项）**：模型生成文本中出现的所有价格与参数严格取自候选池（100% 零幻觉）、无内部 prompt 标记泄露、无虚构神券、无生硬公文八股小标题。

### 2. 人类专家在体系中的 4 大核心定位
在重构后的体系中，人绝不是被动看客，而是主导质检的核心闭环：
1. **题库出题官**：通过后台可视化界面增改测试题，将大厂蒸馏出的典型业务对话直接入库；
2. **评测发令官**：在界面一键发起评测，掌控快速沙箱与实机测试模式；
3. **Badcase 仲裁法官**：线上真实用户产生的点踩（Dislike）或转人工诉求，一键审阅并收录为官方回归测试集，确保“同坑不摔两次”；
4. **话术质量把关人**：脚本只能断言“事实对错”，无法判定话术的温度与得体程度，由管理员进行人工复核与标准答案修订。

---

## 二、 严格遵循的双规约红线（严禁违背）

### 1. 文案规约：严格按 `humanizer-zh` 脱敏去 AI 味
- **绝对禁用词表（出现即返工）**：
  - ❌ 严禁出现低幼、标签化或过度修饰的词汇：“人情味”、“老店长人话”、“生硬AI腔”、“推销感重”、“AI味浓”、“覆灭幻觉”、“神级”。
- **统一规范用语**：
  - 评测打标统一采用中台质检专业工程词汇：
    - “人工复核” (`Human Audit`)：状态清晰界定为 **已确认** / **待修正**；
    - “话术修正” (`Response Revision`)：管理员直接录入标准修订内容；
    - “收录为基准用例” (`Promote to Benchmark`)：将线上案例收录进官方评测集；
    - “事实一致率” (`Fact Consistency: 100.0%`)、“断言通过” (`691/691`)。
  - 删除所有填充性短语、夸大性修饰，直接陈述客观数据与动作。

### 2. 视觉排版规约：严格按 `DESIGN.md` 反 AI 味设计
- **视觉去玩具化**：
  - ❌ 彻底清退导航栏和卡片中的所有 Emoji 图标（如 `🧪`、`🏷️`、`📊`、`⚡`、`📈`、`👍`、`👎`）；
  - 采用极简中性文字与标准几何指示点，保持专业控制台品质。
- **根除“AI 布局小字紧凑”**：
  - ❌ 严禁在每个指标下方挂载密密麻麻的 11px 灰字解释与注释；
  - **辅助性小字每屏不超过 1 处**；依靠字重、大字号与主数据分量建立清晰层级，不靠小字说明填补空间；
  - 拒绝机械等分的小卡片三列堆叠，改用单列清晰、阅读舒适的横向数据行。
- **留白经济学**：
  - 区块间距必须有呼吸感梯度，列表行垂直 padding ≥ 1.2rem；
  - 默认禁用多余行分割线与层层嵌套的卡片描边，纯靠留白与背景对比自然分组。

---

## 三、 模块架构与页面重构设计

### 1. 导航合并（盘活现有半成品模块）
- 将左侧导航原有的两个分散 Tab（`ab_experiments` 与 `benchmark`）合并为一个统一的顶层模块：
  - 路由标识：`key: 'evaluation'`
  - 菜单标签：`评测基准与回归中心`
  - 原 `ab_experiments` 和 `benchmark` 导航项正式废除下线。

### 2. 页面内三重视图组织
- **视图 1：评测总览与架构对照大屏**：
  - 顶部指标：基准用例数 (50)、断言通过率 (100.0%)、Prefix Cache 恒定率 (100.0%)、P95 响应时延 (5.99ms)；
  - 运行控制条：运行模式选择（`⚡ 快速沙箱回归` / `🌐 端到端模型实测`） + 【执行全量评测】按钮 + 实时任务进度条；
  - 架构对照：三组 A/B 实验实测结果（检索模式对比、前缀切分稳定性对比、决策门控时延对比）。
- **视图 2：基准题库与质检工作台**：
  - 支持单轮 50 题与多轮 10 组会话整合展示，支持按场景分类（冲突松弛/精准搜索/订单物流/售后退换/服务安全）筛选；
  - 横向列表布局：用例编号、提问文本、场景分类、实测耗时、断言状态、操作入口；
  - **下钻抽屉（极简利落，无多余小字）**：
    - 呈现用户提问、实测回复文本与命中的 Generative UI 卡片；
    - 呈现三层断言核验结果（物理事实/状态机/语义零幻觉）；
    - 呈现多行文本编辑区，支持管理员录入官方标准答案，点击【保存并收录为基准】。
- **视图 3：线上反馈待复核池**：
  - 直通用户端点踩反馈（Dislike）与转人工工单流水；
  - 管理员直接查看客户原始抱怨与 AI 当时的实际输出，支持一键点击【转为回归测试用例】。

---

## 四、 数据持久化与双写方案（SQLite + JSON 镜像）

### 1. SQLite 数据库表设计 (`huigou_analytics.db`)
在现有的分析数据库中新建 `eval_benchmark_cases` 表：

```sql
CREATE TABLE IF NOT EXISTS eval_benchmark_cases (
    case_id TEXT PRIMARY KEY,               -- 用例编号，如 Q01, SES-01-T1
    case_type TEXT NOT NULL,                -- single_turn / multi_turn
    scenario_group TEXT NOT NULL,           -- conflict_relaxation / multimodal_exact_search / order_query / aftersales_verify / safety_and_escalation
    query_text TEXT NOT NULL,               -- 用户输入文本
    expected_intent TEXT,                   -- 期望意图
    expected_constraint_status TEXT,        -- satisfied / relaxed / none
    expected_skus TEXT,                     -- 期望推荐 SKU 列表 (JSON 字符串)
    hard_constraints TEXT,                  -- 规格与预算约束 (JSON 字符串)
    standard_response TEXT,                 -- 管理员录入的人类专家标准回复
    audit_status TEXT DEFAULT 'verified',   -- verified (已确认) / pending_review (待复核)
    source_type TEXT DEFAULT 'benchmark',   -- benchmark (官方基准) / online_feedback (线上差评沉淀)
    source_ref_id TEXT,                     -- 关联的反馈 ID 或工单 ID
    last_run_status TEXT,                   -- pass / fail
    last_run_latency_ms REAL,               -- 最近一次评测时延
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_eval_cases_group ON eval_benchmark_cases (scenario_group);
CREATE INDEX IF NOT EXISTS idx_eval_cases_status ON eval_benchmark_cases (audit_status);
```

### 2. 双写与镜像同步机制
- **写机制**：管理员在后台界面增改用例、修订标准回答或采纳线上反馈时，事务写入 SQLite；
- **镜像机制**：写入成功后自动触发全量导出，刷新覆盖 `data/eval_benchmark_50queries.json`；
- **兼容性保障**：原有离线评测脚本 `scripts/run_eval.py` 无需修改底层数据读取逻辑，完全保持平滑兼容。

---

## 五、 后端 API 接口契约规范

### 1. `POST /api/v1/admin/eval/trigger`
- **说明**：管理员在后台一键触发全量自动化评测。
- **请求体**：
  ```json
  {
    "mode": "sandbox",  // "sandbox" (快速沙箱回归, ~1.5s) 或 "e2e_llm" (真实模型调用, ~25s)
    "scenario_filter": null // 可选指定评测场景组
  }
  ```
- **响应体**：
  ```json
  {
    "status": "started",
    "task_id": "eval-task-20261023-001",
    "mode": "sandbox",
    "message": "评测任务已在后台启动"
  }
  ```

### 2. `GET /api/v1/admin/eval/progress/{task_id}`
- **说明**：轮询当前正在执行的评测进度。
- **响应体**：
  ```json
  {
    "task_id": "eval-task-20261023-001",
    "status": "running", // "running" / "completed" / "failed"
    "progress_pct": 68,
    "completed_queries": 34,
    "total_queries": 50,
    "current_scenario": "aftersales_verify",
    "error": null
  }
  ```

### 3. `GET /api/v1/admin/eval/cases`
- **说明**：分页查询评测用例列表与实测详情。
- **查询参数**：
  - `scenario_group`: 场景过滤；
  - `audit_status`: 状态过滤 (`verified` / `pending_review`)；
  - `page`: 页码（默认 1）；
  - `page_size`: 每页条数（默认 20）。
- **响应体**：返回用例列表、实测回复、断言通过详情及标准回复。

### 4. `PATCH /api/v1/admin/eval/cases/{case_id}`
- **说明**：管理员在线修订单题标准回答与复核状态。
- **请求体**：
  ```json
  {
    "standard_response": "修润后的官方自营标准回复内容",
    "audit_status": "verified"
  }
  ```

### 5. `POST /api/v1/admin/eval/adopt-feedback`
- **说明**：从 C 端差评反馈或转人工工单中，一键收录为新的基准用例。
- **请求体**：
  ```json
  {
    "source_type": "feedback", // "feedback" 或 "handoff"
    "source_id": "FB-20261023-012",
    "scenario_group": "aftersales_verify",
    "standard_response": "管理员核定的标准答复"
  }
  ```

---

## 六、 阶段落地与验收标准

| 阶段 | 实施范围 | 核心交付物 | 验收标准 |
| :--- | :--- | :--- | :--- |
| **阶段一（当前）** | 架构与设计规约固化 | 本文档 (`docs/EVAL_SYSTEM_REDESIGN_AND_ADMIN_SPEC.md`) | 用户正式审阅并验收方案与规约 |
| **阶段二** | 后端数据层与 API 落地 | SQLite 表初始化、数据初始化脚本、`app/main.py` 5 个核心评测 API | 接口全通，单元测试通过，双写镜像无缝同步 |
| **阶段三** | 前端界面重构与极简落地 | `admin-frontend/src/App.jsx` 导航合并、A/B 大屏整合、抽屉工作台落地 | `npm run build` 打包成功，无 Emoji，无密集小字，留白有呼吸感 |
| **阶段四** | 端到端全链路验收 | 实机触发评测、下钻修订用例、Badcase 收录验证 | 评测通过率 100%，所有功能真实联动 |

---

*本规约已一次性沉淀完毕，后续所有开发实施严格以本文档为唯一基准。*
