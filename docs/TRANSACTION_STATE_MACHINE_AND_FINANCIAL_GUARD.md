# 交易状态机演进与生产资损防控设计规范 (Transaction State Machine & Financial Guard)

> **版本**：v2026.10  
> **文档定位**：真实电商主站交易与履约系统接入目标规范（由首席交易架构师 / Delivery QA 归档制定）  
> **当前状态**：`TARGET_ARCHITECTURE`（后续演进目标；当前导购 Agent 聚焦选品、规格约束诊断、松弛推荐与只读数据核验）

---

## 一、背景与设计诉求

在基于 LLM 的电商导购与智能客服应用中，大模型生成能力必须被强力物理约束。
在当前系统阶段：
1. 本系统已完成 60 SKU 完整商品目录、硬约束冲突对（Path A 提预算 vs Path B 放规格）、gemini-embedding-2 向量预计算、Jev System One 意图裁决以及只读订单核验的确定性设计；
2. 当前阶段聚焦于**导购 Agent 核心智能链路**，尚未直接挂载真实生产交易扣款与退款微服务；
3. 为防范未来系统接入生产时发生资金损失（Financial Loss / 资损），特制定本文档作为**生产交易履约闭环的标准指引**。

---

## 二、交易履约状态机完整拓扑 (Order Lifecycle State Machine)

生产环境的订单状态必须遵循严格的单向有限状态机（FSM），严禁逆向流转与未决穿越：

```
                ┌───────────────┐
                │    pending    │ (已下单未支付 / 待揽收备货)
                └───────┬───────┘
                        │ 物流揽收发货
                        ▼
                ┌───────────────┐
                │    shipped    │ (运输在途中，signed_at 为空)
                └───────┬───────┘
                        │ 签收确认
                        ▼
                ┌───────────────┐
                │   delivered   │ (已签收，启动 168h 售后 / 360h 价保滚动计时)
                └───────┬───────┘
                        │ 发生退货/退款/拒收
                        ▼
               ┌─────────────────┐
               │    refunded     │ (【终态】已全额退款 / 订单关闭)
               └─────────────────┘
```

---

## 三、生产资损防控三大铁律 (Financial Risk Rules)

### 1. 终态订单退款防二次套利（Anti-Double-Dipping）
- **资损场景**：用户已针对某订单退货全款（状态置为 `refunded`），随后商品发生大促降价，用户若再次申请 15 天价保，由于系统判定 `paid_amount > current_price`，若无状态机终态校验，可能再次触发退差价。
- **物理拦截铁律**：
  - 凡 `order_status in ('refunded', 'closed', 'cancelled')` 的订单，在进入任何售后退款或价格保护核验函数时，**第一时间前置熔断拒绝**；
  - 统一返回状态码：`return_reason_code = "ALREADY_REFUNDED"`，`diff_amt = 0.0`；
  - 严禁任何已全额退款订单流向退差计算流水。

### 2. 7天无理由退货法定起算时序（168h Rolling Clock vs Legal Standard）
- **法定口径**：依据《网络购买商品七日无理由退货暂行办法》，法定期限为**自签收次日零时（00:00:00）起算满 168 小时**；
- **沙箱基准口径与生产映射**：
  - 当前导购演示沙箱按 `signed_at` 精确时刻滚动计算 168.0 小时，以消除自然日折算歧义；
  - 接入生产交易系统时，应提供双时序核验器：
    ```python
    # 生产法定判定标准：
    signed_next_day_midnight = (signed_dt + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    elapsed_legal_hours = (ref_dt - signed_next_day_midnight).total_seconds() / 3600.0
    is_within_7d = 0.0 <= elapsed_legal_hours <= 168.0
    ```

### 3. 价保退差强校验原始支付流水（Payment Ledger Binding）
- **资损场景**：防范前台篡改实付金额 `pay_amount`。
- **物理拦截铁律**：
  - 导购 Agent 必须通过后端内部受信 RPC 查询账务系统的最终净实付流水（扣除店铺红包、现金券、退款后的真实资金占款）；
  - 退差上限封顶：`max_refund = actual_net_paid - sku_current_final_price`，且当且仅当 `max_refund > 0` 且 `order.is_refunded == false` 时允许生成退款结算单。

---

## 四、未来生产环境接入架构图 (Future Integration Architecture)

```
  [买家自然语言提问 / 诉求]
              │
              ▼
  [导购 Agent / Jev System One] ──(意图分流: aftersales_qa / order_query)
              │
              ▼
  [只读核验层 OrderTools] ──── 校验本地快照与合规时效
              │
              ▼ (当用户确认申请售后时)
  ┌────────────────────────────────────────────────────────┐
  │         【后续开发目标：生产交易风控前置网关】         │
  │  1. 状态机非终态校验 (Check NOT in 'refunded'/'closed')│
  │  2. 支付结算中心真实流水核验 (Payment Audit Hook)      │
  │  3. 幂等防重放分布式锁 (Redis Idempotency Token)      │
  │  4. 人行反洗钱与高频退款风控拦截 (Risk Engine)         │
  └──────────────────────────┬─────────────────────────────┘
                             │
                             ▼
              [主站履约与退款结算微服务]
```

---

## 五、演进结论

1. **当前阶段**：严格落实导购 Agent 设计标准，确保推荐算法（Path A/B）、检索重排、前缀缓存（272K Prefix Cache）与纯粹只读规则校验的高内聚与零幻觉；
2. **后续阶段**：依据本规范，对接主站交易中台 FSM 状态机网关与分布式事务，完成从“智能导购”到“交易闭环”的无缝切换。
