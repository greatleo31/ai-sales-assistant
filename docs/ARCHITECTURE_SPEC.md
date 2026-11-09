# 惠购电商导购智能体：系统架构与确定性状态机技术规范 (ARCHITECTURE_SPEC.md)

> **版本**：v2026.10 生产级架构基线  
> **状态**：Active / Production Ready  
> **归档溯源**：历史指令推演与原始蓝图已安全归档至 [`docs/archive/架构重构-argon指南.md`](./archive/架构重构-argon指南.md)

---

## 一、系统架构与分层门禁拓扑 (Architecture Topology)

系统基于 **FastAPI + Jev System One + Hybrid RAG + DeepSeek-V4.1-Flash + Typed SSE** 构建，采用 **6 级确定性受控门禁流水线（6-Stage Deterministic Guarded Pipeline）**，彻底解耦概率推理与确定性业务状态：

```
[User Request (Query + SessionID)]
       │
       ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Stage 0: L0 零时延域围栏与安全熔断器 (DomainConfinementGate, < 1ms)     │
│  ├─ 命中 CTF/渗透/逆向Prompt/环境探测/非电商代码生成？                   │
│  │   └─ YES -> [Short-Circuit!] Strike+1, 触发 TopicShiftDecay 清空品类, │
│  │             直接下发静态拒答 SSE + 空卡片 [] (不调 RAG, 不调 LLM)     │
└─────────────────────────────────────────────────────────────────────────┘
       │ PASS
       ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Stage 1: 会话账本加载与元纠错/实体锚定 (Ledger & Entity/Meta Linker)     │
│  ├─ 识别元对话质疑 ("和上一轮不一样" / "你的卡片呢") -> META_CORRECTION   │
│  └─ 精确匹配在售 SKU 型号 ("漫步者 W820NB") -> 标记 target_single_sku   │
└─────────────────────────────────────────────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Stage 2: 上下文感知意图门控 (Context-Aware Jev Router)                  │
│  ├─ 结合 spec_registry 属性词典 + 上一轮 FSM 状态偏置矩阵修正意图        │
│  └─ 导购态下短词 ("通勤" / "换黑色" / "250元") -> 强制归入 SHOPPING_REFINE│
└─────────────────────────────────────────────────────────────────────────┘
       │
       ├──► [分支 A: 售后/订单意图 (AFTERSALES)]
       │      └─ Stage 3A: 强契约工具门禁 (Anti-Hallucination Tool Guard)
       │           ├─ 正则提取 ORD-\d{8}-\d{3} + 校验所有权
       │           ├─ 无单号 -> Short-Circuit! 状态置为 AWAITING_ORDER_ID，强制反问索单
       │           └─ 有单号且核验通过 -> 查询真实订单并渲染
       │
       └──► [分支 B: 导购选品意图 (EXPLORATION / CANDIDATE_LOCKED / FULFILLMENT)]
              └─ Stage 3B: 候选集集合代数引擎 (Candidate Lock Algebra Engine)
                   ├─ 单品命中 (FULFILLMENT) -> 物理截断候选集为 [target_sku] (长度=1)
                   ├─ 属性收窄 ("通勤") -> Sticky Intersection (原候选集满足项置顶 + 差额替补)
                   ├─ 约束放宽 ("250元") -> Pinned Union (原推荐合规项保护 + 增量高分补充)
                   └─ 纠错恢复 ("卡片呢") -> 直接恢复上一轮渲染快照
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Stage 4: 受控生成与流式字节级对齐引擎 (StreamOrderAligner + Typed SSE)  │
│  ├─ Prompt 注入候选白名单与 [[SKU:id]] 锚点协议 + 变更 Diff 元数据      │
│  ├─ 滑动窗口实时剥离锚点并推送 text_delta，按首次出现偏移量记录顺序队列 │
│  └─ 文本流结束帧前，按队列字节级重排 product_cards 并下发 ui_cards       │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 二、状态生命周期与转移真值表 (Dialogue FSM)

会话账本（`DecisionLedgerState`）维持 7 个正交状态，任何时刻会话必居其一：

| **当前状态 ($S_{t-1}$)** | **触发事件 / 门控信号 ($E_t$)** | **目标状态 ($S_t$)** | **候选集与槽位动作（Deterministic Action）** |
| :--- | :--- | :--- | :--- |
| `ANY` | L0 命中安全/黑客/环境探测且 `strikes < 2` | `TOPIC_SHIFT_DECAYED` | 清空 `active_category` 与 `locked_sku_ids`，返回中性边界声明 + 空卡片 |
| `ANY` | L0 命中安全探测且 `strikes >= 2` | `CIRCUIT_BROKEN` | 锁定会话 15 分钟，拒绝处理，直接返回风控熔断提示 |
| `IDLE` / `DECAYED` | 宽泛导购需求（如“20元内17寸电脑包”） | `CANDIDATE_LOCKED` | 初始化类目与硬槽位，执行 Hybrid RAG，将 Top-$K$ 写入候选集 |
| `CANDIDATE_LOCKED` | **增量属性收窄**（如短词“通勤”、“要防水”） | `CANDIDATE_LOCKED` | **粘性交集（Sticky Intersection）**：保留旧锁定集中满足新属性的 SKU 置顶，仅剔除不满足项并从 RAG 替补 |
| `CANDIDATE_LOCKED` | **约束放宽**（如“预算放宽到250元”） | `CANDIDATE_LOCKED` | **锚定并集（Pinned Union）**：将旧锁定集中原本合规的 SKU 白名单锁定，补充新价格带高分 SKU |
| `CANDIDATE_LOCKED` | **元对话纠错/追问**（“卡片呢”、“怎么和上轮不一样”） | `CANDIDATE_LOCKED` | **快照回滚（Snapshot Rollback）**：禁止重新检索，直接复用上一轮渲染卡片快照 |
| `ANY_SHOPPING` | **单品精准锚定**（“漫步者 W820NB 亮点”、“第2款几天到”） | `FULFILLMENT` | **单品物理截断（Singleton Truncation）**：`locked_sku_ids = [target_sku_id]`，严禁下发竞品 |
| `ANY` | 售后/查单意图，但未提取到合法 `order_id` | `AFTERSALES_AWAITING_ID` | **阻断工具调用**，挂起导购快照，强制下发标准反问索单模板 |
| `AFTERSALES_*` | 提取到合法 `order_id` 且通过核验 | `AFTERSALES_VERIFIED` | 执行受保护的订单查询工具，返回真实物流与售后卡片 |

---

## 三、候选集物理锁定算法 (Candidate Lock Algebra)

为杜绝多轮推荐大洗牌，后端在 `CANDIDATE_LOCKED` 态下严禁直接用 RAG 覆盖历史候选集，严格执行确定性三步集合代数运算：

设上一轮锁定展示的商品列表为 $L_{t-1} = [s_1, s_2, \dots, s_k]$，本轮合法 SKU 集合为 $\Omega(C_t)$，本轮 Hybrid RAG 召回列表为 $R_t$：

1. **粘性保留集（Sticky Retained Sequence）**：
   $$P_t = \big[\, s \in L_{t-1} \;\big\vert{}\; s \in \Omega(C_t) \,\big]$$
   *说明*：上一轮已展示且仍符合当前约束的款式，**按原有相对顺序强制保留在列表头部**。

2. **增量替补集（Scoped Backfill Sequence）**：
   $$B_t = \big[\, r \in R_t \;\big\vert{}\; r \in \Omega(C_t) \;\land\; r \notin P_t \,\big]$$

3. **最终锁定候选集（Final Locked Candidates，容量上限 $K=3$）**：
   - **属性收窄（如追加“通勤”）**：优先保留 $P_t$，不足 $K$ 个时用 $B_t$ 补齐：
     $$L_t = \text{Slice}_{0:K}\big(P_t \oplus B_t\big)$$
   - **约束放宽（如“放宽到 250 元”）**：保留 $P_t$ 前 $\min(|P_t|, K-1)$ 款，引入新价格带高分商品 $B_t$，并在 `diff_meta` 中标注 `retained_skus` 与 `added_skus`。

---

## 四、Prompt 动静二元隔离与前缀缓存协议 (KV Cache Protocol)

为保障大模型服务商（DeepSeek-V4.1-Flash / OpenAI / vLLM）的 Prefix Caching 达到 100% 缓存命中，系统提示词严格按照 `BOUNDARY_MARKER` 二元切分：

```python
BOUNDARY_MARKER = "==================== SYSTEM_PROMPT_DYNAMIC_BOUNDARY ===================="
```

1. **边界之上（Static Constitution）**：
   - 包含自营老店长人设、导购守则、售后政策红线、合规禁令；
   - 保证多轮次与多用户之间 **0 字节漂移（MD5 绝对一致）**，换取 80%+ 的 KV Cache 命中与首字延迟优化。
2. **边界之下（Dynamic Context）**：
   - 包含 `< 100 Tokens` 的结构化决策账本（`DecisionLedgerState`）；
   - 经过剪枝修剪的活跃轮次对话（最近 2~3 轮）；
   - 当前锁定候选卡片详情及订单事实。

---

## 五、三层阶梯断言与测试基线 (Assertion Pyramid)

| 层级 | 断言类型 | 校验机制 | 容忍阈值 |
| :--- | :--- | :--- | :--- |
| **L1** | 结构化事实硬断言 | O(1) 主键点查、价格/规格/状态码一致性校验 | 0 违规 |
| **L2** | 轨迹与状态机断言 | 意图分类转移、单品截断、候选锁置顶一致性 | 0 漂移 |
| **L3** | 语义逆向闭包断言 | 文本商业数值逆向包含核验（$S_{text} \subseteq S_{context}$）、违规承诺与内部标记零泄露 | 0 幻觉 |
