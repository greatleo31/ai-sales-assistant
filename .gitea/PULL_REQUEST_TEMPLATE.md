## 变更概述

简要描述本次合并请求（Pull Request）所引入的架构模块设计、业务功能或缺陷修复范围。

- **关联 Issue**: 关闭 #<!-- 填入关联 Issue 编号 -->
- **分支类型**: `feat/*` (特性) | `fix/*` (修复) | `test/*` (测试) | `docs/*` (文档)

---

## 核心变更点

- 
- 
- 

---

## 工程规范与契约自查清单

- [ ] **确定性事实接地 (Deterministic Grounding)**：商品价格、规格参数、售后与价保核验严格取自底层结构化真源，严禁出现编造型降价优惠或未经核准的陈述（`0` 幻觉率）。
- [ ] **Fail-Closed 闭环安全与 SLA 兜底**：外部接口调用超时（如 System One Jev `>800ms` 或主模型连续异常熔断）时，无缝降级至确定性本地规则层与静态兜底话术。
- [ ] **272K 前缀缓存对齐 (Prompt Cache)**：`SYSTEM_PROMPT_DYNAMIC_BOUNDARY` 分界线以上的静态系统宪法在多轮对话中保持字节级一致（`static_prefix_md5` 不变）。
- [ ] **流式净水器零开发者噪声 (Zero Developer Noise)**：C 端 SSE 流中绝不出现未闭合的 HTML 标签（`<br>`、`<div>` 等）、裸 LaTeX 公式代码以及内部调试协议标签。
- [ ] **纯净工作区**：严禁提交 `.env` 真实密钥、严禁包含 `__pycache__` 或 `.pyc` 字节码文件，所有文件均使用 UTF-8 编码。

---

## 本地自动化验证与测试输出

```bash
# 请粘贴使用 python -B 实际执行并通过的验证命令与退出码：
python -B scripts/validate_catalog.py
```
