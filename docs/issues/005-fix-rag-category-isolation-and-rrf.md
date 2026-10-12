# Issue #5: [缺陷] 解决跨类目 BM25 词频统计污染与 SQLite 全候选池统一 RRF 融合断层

- **Issue 编号**: #5
- **关联工程分支**: `fix/rag-category-isolation-and-rrf`
- **标签类型**: `bug`
- **当前状态**: 已关闭 (Closed)

---

## 一、问题背景与动机
详见 `docs/PR_AND_ISSUE_RECORDS.md` 对应章节。
该 Issue 在系统全生命周期演进中承担核心架构或缺陷收敛职责。

## 二、交付文件与设计约束
- **主交付分支**: `fix/rag-category-isolation-and-rrf`
- **硬性约束**: 严格杜绝事实幻觉，保证多轮对话前缀对齐与无开发者噪声。

## 三、验收标准与防回归测试
必须通过对应自动化测试套件且退出码为 0，经代码审查与基准测试核准后由 Pull Request #5 合并至主干分支。
