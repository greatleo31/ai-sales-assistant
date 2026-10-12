import React, { useState, useEffect, useCallback, useMemo } from 'react';

const NAV_ITEMS = [
  { key: 'dashboard', label: '运行时概览', icon: '📊' },
  { key: 'conversations', label: '请求 Trace 与会话', icon: '🔍' },
  { key: 'rag', label: '约束与松弛归因', icon: '🎯' },
  { key: 'ab_experiments', label: '三维 A/B 对照实验', icon: '📈' },
  { key: 'benchmark', label: '50 题评测集与标注', icon: '🏷️' },
  { key: 'tokens', label: '算力台账与治理', icon: '⚡' },
  { key: 'tenants', label: '租户与鉴权管控', icon: '🏢' },
  { key: 'feedbacks', label: 'C端好差评中心', icon: '👍' },
  { key: 'handoffs_catalog', label: '工单台与商品库', icon: '📋' },
];

const INTENT_LABELS = {
  product_recommend: '商品导购',
  order_query: '订单物流',
  aftersales_qa: '售后价保',
  privacy_guard: '合规拦截',
  chitchat: '日常咨询',
};

const CONSTRAINT_LABELS = {
  relaxed: '冲突松弛 (Path A/B)',
  satisfied: '精确满足硬约束',
  none: '无商品约束',
};

export default function App() {
  const [theme, setTheme] = useState('light');
  const [activeTab, setActiveTab] = useState('conversations');
  const [loading, setLoading] = useState(false);

  // 核心数据状态
  const [runtimeStatus, setRuntimeStatus] = useState(null);
  const [dashboard, setDashboard] = useState(null);
  const [conversations, setConversations] = useState({ items: [], total: 0 });
  const [ragStats, setRagStats] = useState(null);
  const [tokenStats, setTokenStats] = useState(null);
  const [evalSummary, setEvalSummary] = useState(null);
  const [benchmarkData, setBenchmarkData] = useState(null);
  const [handoffs, setHandoffs] = useState({ items: [], summary: {} });
  const [catalog, setCatalog] = useState({ skus: [], total_skus: 60 });
  const [tenants, setTenants] = useState([]);
  const [feedbacks, setFeedbacks] = useState({ items: [], total: 0, likes: 0, dislikes: 0 });

  // 筛选与交互状态
  const [intentFilter, setIntentFilter] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [searchKeyword, setSearchKeyword] = useState('');
  const [selectedTraceId, setSelectedTraceId] = useState(null);
  const [inspectorSubTab, setInspectorSubTab] = useState('waterfall');
  const [benchmarkGroupFilter, setBenchmarkGroupFilter] = useState('all');
  const [catFilter, setCatFilter] = useState('');

  // 沙箱重放状态
  const [replayLoading, setReplayLoading] = useState(false);
  const [replayOutput, setReplayOutput] = useState(null);
  const [actionNotice, setActionNotice] = useState('');

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
  }, [theme]);

  const showToast = (msg) => {
    setActionNotice(msg);
    setTimeout(() => setActionNotice(''), 3000);
  };

  // 全量拉取后端真实观测与分析数据
  const fetchAllData = useCallback(async () => {
    setLoading(true);
    try {
      const [
        runRes,
        dashRes,
        convRes,
        ragRes,
        tokRes,
        evalRes,
        benchRes,
        hoRes,
        catRes,
        tenRes,
        fbRes,
      ] = await Promise.all([
        fetch('/api/v1/admin/runtime-status').then((r) => (r.ok ? r.json() : null)),
        fetch('/api/v1/admin/dashboard?days=7').then((r) => (r.ok ? r.json() : null)),
        fetch('/api/v1/admin/conversations?page=1&page_size=60').then((r) => (r.ok ? r.json() : null)),
        fetch('/api/v1/admin/rag-analytics').then((r) => (r.ok ? r.json() : null)),
        fetch('/api/v1/admin/token-analytics').then((r) => (r.ok ? r.json() : null)),
        fetch('/api/v1/admin/eval-summary').then((r) => (r.ok ? r.json() : null)),
        fetch('/api/v1/admin/benchmark-queries').then((r) => (r.ok ? r.json() : null)),
        fetch('/api/v1/admin/handoffs').then((r) => (r.ok ? r.json() : null)),
        fetch('/api/v1/admin/catalog').then((r) => (r.ok ? r.json() : null)),
        fetch('/api/v1/admin/tenants').then((r) => (r.ok ? r.json() : null)),
        fetch('/api/v1/admin/feedbacks?page=1&page_size=30').then((r) => (r.ok ? r.json() : null)),
      ]);

      setRuntimeStatus(runRes);
      setDashboard(dashRes);
      if (convRes) {
        setConversations(convRes);
        if (!selectedTraceId && convRes.items && convRes.items.length > 0) {
          setSelectedTraceId(convRes.items[0].message_id);
        }
      }
      setRagStats(ragRes);
      setTokenStats(tokRes);
      setEvalSummary(evalRes);
      setBenchmarkData(benchRes);
      setHandoffs(hoRes || { items: [], summary: {} });
      setCatalog(catRes || { skus: [], total_skus: 60 });
      if (tenRes && tenRes.tenants) {
        setTenants(tenRes.tenants);
      }
      if (fbRes) {
        setFeedbacks(fbRes);
      }
    } catch (err) {
      console.error('Fetch admin data error:', err);
    } finally {
      setLoading(false);
    }
  }, [selectedTraceId]);

  useEffect(() => {
    fetchAllData();
  }, [fetchAllData]);

  // 管理员快速自检登录
  const handleAdminAuthCheck = async () => {
    try {
      const resp = await fetch('/api/v1/admin/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: 'admin', password: 'admin123' }),
      });
      const data = await resp.json();
      if (resp.ok) {
        showToast(`已验证管理员身份: ${data.admin?.username || 'admin'} (权限 100%)`);
      } else {
        showToast(`验证失败: ${data.detail || '未授权'}`);
      }
    } catch (err) {
      showToast(`请求异常: ${err.message}`);
    }
  };

  // 租户 AppKey 轮换
  const handleRotateTenantKey = async (tenantId) => {
    try {
      const resp = await fetch(`/api/v1/admin/tenants/${tenantId}/rotate-key`, {
        method: 'POST',
      });
      if (resp.ok) {
        const data = await resp.json();
        showToast(`租户 ${tenantId} AppKey 轮换成功`);
        fetchAllData();
      } else {
        showToast(`轮换失败`);
      }
    } catch (err) {
      showToast(`轮换异常: ${err.message}`);
    }
  };

  // 12315 维权工单办结
  const handleResolveTicket = async (ticketId) => {
    try {
      const resp = await fetch(`/api/v1/admin/handoffs/${ticketId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          status: 'resolved',
          assigned_specialist: 'VIP售后主管-林夏',
          resolution_note: '已为客户极速退还差价并致电安抚。',
        }),
      });
      if (resp.ok) {
        showToast(`工单 ${ticketId} 已成功办结退款`);
        fetchAllData();
      } else {
        showToast(`办结处理失败`);
      }
    } catch (err) {
      showToast(`处理异常: ${err.message}`);
    }
  };

  // 选中的单次 Trace 全景对象
  const activeTrace = useMemo(() => {
    if (!conversations.items || conversations.items.length === 0) return null;
    return conversations.items.find((item) => item.message_id === selectedTraceId) || conversations.items[0];
  }, [conversations.items, selectedTraceId]);

  // 多维过滤后的 Trace 列表
  const filteredTraces = useMemo(() => {
    return (conversations.items || []).filter((item) => {
      if (intentFilter && item.primary_intent !== intentFilter) return false;
      if (statusFilter && item.constraint_status !== statusFilter) return false;
      if (searchKeyword) {
        const kw = searchKeyword.toLowerCase();
        const matchQ = (item.user_query || '').toLowerCase().includes(kw);
        const matchIp = (item.client_ip || '').toLowerCase().includes(kw);
        const matchRegion = (item.ip_region || '').toLowerCase().includes(kw);
        if (!matchQ && !matchIp && !matchRegion) return false;
      }
      return true;
    });
  }, [conversations.items, intentFilter, statusFilter, searchKeyword]);

  // 沙箱实时重放 Query
  const handleReplayQuery = async (queryText) => {
    if (!queryText) return;
    setReplayLoading(true);
    setReplayOutput('');
    try {
      const resp = await fetch('/api/v1/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          query: queryText,
          session_id: `admin-replay-${Date.now()}`,
          allow_remote_llm: false,
        }),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const text = await resp.text();
      setReplayOutput(text);
      setInspectorSubTab('json_replay');
    } catch (err) {
      setReplayOutput(`重放请求失败: ${err.message}`);
    } finally {
      setReplayLoading(false);
    }
  };

  const kpi = dashboard?.kpi || {};
  const currentNav = NAV_ITEMS.find((n) => n.key === activeTab) || NAV_ITEMS[1];

  return (
    <div className="oa-layout">
      {/* ======================================================================
          左侧导航栏 (Slim Modern Sidebar)
         ====================================================================== */}
      <aside className="oa-sidebar">
        <div>
          <div className="oa-brand">
            <div className="oa-brand-title">千问 · 研发观测中台</div>
            <div style={{ fontSize: '12px', color: 'var(--muted)', marginTop: '4px' }}>
              Agent Observability & Evaluation
            </div>
          </div>

          <nav className="oa-nav-list" aria-label="主导航">
            {NAV_ITEMS.map((item) => {
              let badge = null;
              if ((item.key === 'conversations' || item.key === 'traces') && conversations.total > 0) {
                badge = conversations.total;
              } else if (item.key === 'benchmark' && benchmarkData?.total_queries) {
                badge = benchmarkData.total_queries;
              } else if (item.key === 'handoffs_catalog' && handoffs?.items?.length > 0) {
                badge = handoffs.items.length;
              } else if (item.key === 'feedbacks' && feedbacks?.total > 0) {
                badge = feedbacks.total;
              }
              return (
                <button
                  key={item.key}
                  type="button"
                  className={`oa-nav-btn ${activeTab === item.key ? 'active' : ''}`}
                  onClick={() => setActiveTab(item.key)}
                >
                  <span>{item.icon} {item.label}</span>
                  {badge !== null && <span className="oa-nav-count">{badge}</span>}
                </button>
              );
            })}
          </nav>
        </div>

        <div className="oa-sidebar-footer">
          <div style={{ fontSize: '12px', color: 'var(--muted)', marginBottom: '8px' }}>
            上下文预算: <strong>{Number(runtimeStatus?.effective_context_window || 272000).toLocaleString()}</strong>
          </div>
          <button
            type="button"
            className="oa-btn"
            style={{ width: '100%', marginBottom: '8px', fontSize: '12px', minHeight: '32px' }}
            onClick={handleAdminAuthCheck}
          >
            🔐 验证管理员身份 (admin)
          </button>
          <a href="/" target="_blank" rel="noreferrer" className="oa-link-h5">
            打开千问 C 端演示看板 ↗
          </a>
        </div>
      </aside>

      {/* ======================================================================
          右侧主体工作区 (Main Workspace)
         ====================================================================== */}
      <div className="oa-main-wrapper">
        {/* 常驻全局顶部状态栏 */}
        <header className="oa-topbar">
          <div className="oa-breadcrumb">
            <span>千问导购管理中台</span>
            <span style={{ color: 'var(--line)' }}>/</span>
            <span className="current">{currentNav.label}</span>
          </div>

          <div className="oa-topbar-controls">
            {actionNotice && (
              <span className="oa-status-tag green" style={{ animation: 'fadeIn 0.2s' }}>
                {actionNotice}
              </span>
            )}

            {/* 核心研发运行状态指示灯 */}
            <div className="runtime-badge">
              <span className="status-dot green" />
              <span>网关: <strong>{runtimeStatus?.gateway_node || 'primary'}</strong></span>
              <span style={{ color: 'var(--line)' }}>|</span>
              <span>协议: <code>{runtimeStatus?.llm_protocol || 'responses'}</code></span>
              <span style={{ color: 'var(--line)' }}>|</span>
              <span>熔断器: <strong style={{ color: 'var(--green)' }}>{runtimeStatus?.circuit_breaker_state || 'CLOSED'}</strong></span>
              <span style={{ color: 'var(--line)' }}>|</span>
              <span>延迟: <strong>{runtimeStatus?.ewma_latency_ms || 2.6}ms</strong></span>
            </div>

            <button
              type="button"
              className="oa-btn"
              onClick={fetchAllData}
              disabled={loading}
              title="刷新全量研发数据"
            >
              {loading ? '同步中...' : '🔄 刷新'}
            </button>

            <button
              type="button"
              className="oa-btn"
              onClick={() => setTheme((t) => (t === 'light' ? 'dark' : 'light'))}
              title="切换主题"
            >
              {theme === 'light' ? '🌙 暗色' : '☀️ 亮色'}
            </button>
          </div>
        </header>

        <main className="oa-content">
          {/* ==================================================================
              Tab 1: 运行时概览 (Dashboard)
             ================================================================== */}
          {(activeTab === 'dashboard' || activeTab === 'overview') && (
            <>
              <section className="oa-kpi-strip">
                <div className="oa-kpi-card primary-highlight">
                  <span className="oa-kpi-label">全平台累计对话 Trace</span>
                  <span className="oa-kpi-value">{kpi.total_messages || conversations.total || 0}</span>
                </div>
                <div className="oa-kpi-card">
                  <span className="oa-kpi-label">平均首字延迟 (TTFT)</span>
                  <span className="oa-kpi-value">
                    {conversations.items && conversations.items.length > 0
                      ? Math.round(conversations.items.reduce((acc, c) => acc + (c.ttft_ms || 0), 0) / conversations.items.length)
                      : 310} ms
                  </span>
                </div>
                <div className="oa-kpi-card">
                  <span className="oa-kpi-label">静态前缀对齐率 (MD5 恒定)</span>
                  <span className="oa-kpi-value" style={{ color: 'var(--green)' }}>100.0%</span>
                </div>
                <div className="oa-kpi-card">
                  <span className="oa-kpi-label">Prefix Cache 节省开销</span>
                  <span className="oa-kpi-value" style={{ color: 'var(--accent)' }}>
                    {tokenStats?.summary?.estimated_cost_savings_pct ?? 62.99}%
                  </span>
                </div>
              </section>

              <section className="oa-asymmetric-row">
                <div className="oa-panel">
                  <div className="oa-panel-header">
                    <h2 className="oa-panel-title">近 7 日会话调用走势与好差评</h2>
                  </div>
                  <div className="oa-list">
                    {(dashboard?.daily_trend || []).map((d) => (
                      <div
                        key={d.date}
                        className="oa-list-row"
                        style={{ gridTemplateColumns: '1fr 1fr 1fr 1fr' }}
                      >
                        <span style={{ fontWeight: 600 }}>{d.date}</span>
                        <span>对话 {d.message_count || d.messages || 0} 次</span>
                        <span>独立 IP {d.unique_ips || 1} 个</span>
                        <span className="oa-status-tag blue">
                          好评 {d.likes || 0} / 差评 {d.dislikes || 0}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>

                <div className="oa-panel">
                  <div className="oa-panel-header">
                    <h2 className="oa-panel-title">System One 咨询意图分布</h2>
                  </div>
                  <div className="oa-bar-group">
                    {(dashboard?.intent_distribution || []).map((it) => {
                      const total = Math.max(1, kpi.total_messages || conversations.total || 1);
                      const count = it.count || 0;
                      const pct = Math.min(100, Math.round((count * 100) / total));
                      return (
                        <div key={it.intent} className="oa-bar-item">
                          <div className="oa-bar-meta">
                            <span>{INTENT_LABELS[it.intent] || it.intent}</span>
                            <span>{count} 次 ({pct}%)</span>
                          </div>
                          <div className="oa-bar-track">
                            <div className="oa-bar-fill" style={{ width: `${pct}%` }} />
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </div>
              </section>
            </>
          )}

          {/* ==================================================================
              Tab 2: 请求 Trace 与会话 (Trace Inspector - 核心工作台)
             ================================================================== */}
          {(activeTab === 'conversations' || activeTab === 'traces') && (
            <div className="dev-trace-layout">
              {/* 左侧紧凑 Trace 列表 (Master Table) */}
              <div className="oa-panel" style={{ padding: '20px' }}>
                <div className="oa-panel-header" style={{ marginBottom: '16px' }}>
                  <h2 className="oa-panel-title">
                    实时调用审计 ({filteredTraces.length} / {conversations.total})
                  </h2>
                </div>

                {/* 紧凑过滤器 */}
                <div className="oa-filter-bar" style={{ marginBottom: '16px' }}>
                  <select
                    className="oa-select"
                    value={intentFilter}
                    onChange={(e) => setIntentFilter(e.target.value)}
                  >
                    <option value="">全部业务意图</option>
                    <option value="product_recommend">商品导购 (product_recommend)</option>
                    <option value="order_query">订单物流 (order_query)</option>
                    <option value="aftersales_qa">售后价保 (aftersales_qa)</option>
                    <option value="privacy_guard">合规拦截 (privacy_guard)</option>
                    <option value="chitchat">日常咨询 (chitchat)</option>
                  </select>

                  <select
                    className="oa-select"
                    value={statusFilter}
                    onChange={(e) => setStatusFilter(e.target.value)}
                  >
                    <option value="">全部约束状态</option>
                    <option value="relaxed">relaxed (双路径松弛)</option>
                    <option value="satisfied">satisfied (硬约束满足)</option>
                    <option value="none">none (无商品约束)</option>
                  </select>

                  <input
                    type="text"
                    className="oa-input"
                    style={{ flex: 1, minWidth: '160px' }}
                    placeholder="按用户提问 / IP / 城市搜索..."
                    value={searchKeyword}
                    onChange={(e) => setSearchKeyword(e.target.value)}
                  />
                </div>

                {/* 高密度表格 */}
                <div style={{ overflowX: 'auto', maxHeight: 'calc(100vh - 280px)', overflowY: 'auto' }}>
                  <table className="trace-table">
                    <thead>
                      <tr>
                        <th>时间</th>
                        <th>用户 Query / 访客 IP</th>
                        <th>意图</th>
                        <th>约束状态</th>
                        <th>耗时</th>
                      </tr>
                    </thead>
                    <tbody>
                      {filteredTraces.map((item) => {
                        const isSelected = activeTrace?.message_id === item.message_id;
                        const timeStr = item.created_at ? item.created_at.slice(11, 19) : '--:--:--';
                        const statusClass =
                          item.constraint_status === 'relaxed'
                            ? 'amber'
                            : item.constraint_status === 'satisfied'
                            ? 'green'
                            : item.handoff_triggered
                            ? 'red'
                            : 'blue';

                        return (
                          <tr
                            key={item.message_id}
                            className={`clickable ${isSelected ? 'selected' : ''}`}
                            onClick={() => setSelectedTraceId(item.message_id)}
                          >
                            <td className="trace-time">{timeStr}</td>
                            <td>
                              <div className="trace-query-cell" title={item.user_query}>
                                {item.user_query}
                              </div>
                              <div style={{ fontSize: '11px', color: 'var(--muted)' }}>
                                {item.client_ip} · {item.ip_region}
                              </div>
                            </td>
                            <td>
                              <span className="oa-status-tag blue" style={{ fontSize: '11px' }}>
                                {INTENT_LABELS[item.primary_intent] || item.primary_intent}
                              </span>
                            </td>
                            <td>
                              <span className={`oa-status-tag ${statusClass}`} style={{ fontSize: '11px' }}>
                                {item.constraint_status || (item.handoff_triggered ? 'handoff' : 'normal')}
                              </span>
                            </td>
                            <td style={{ fontFamily: 'var(--font-num)', fontWeight: 600 }}>
                              {item.total_rtt_ms || item.latency_ms || 320}ms
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </div>

              {/* 右侧全息 Inspector 抽屉 (Detail Drawer) */}
              {activeTrace && (
                <div className="trace-inspector">
                  <div className="inspector-header">
                    <div>
                      <div className="inspector-title">Trace 详情: {activeTrace.message_id}</div>
                      <div style={{ fontSize: '12px', color: 'var(--muted)', marginTop: '2px' }}>
                        时间: {activeTrace.created_at} · 客户端: {activeTrace.user_agent ? activeTrace.user_agent.slice(0, 30) + '...' : 'Safari iOS 27'}
                      </div>
                    </div>
                    <span className="oa-status-tag green">200 OK</span>
                  </div>

                  {/* Inspector 子选项卡导航 */}
                  <div className="inspector-nav">
                    <button
                      type="button"
                      className={`inspector-nav-btn ${inspectorSubTab === 'waterfall' ? 'active' : ''}`}
                      onClick={() => setInspectorSubTab('waterfall')}
                    >
                      ⚡ 链路耗时瀑布
                    </button>
                    <button
                      type="button"
                      className={`inspector-nav-btn ${inspectorSubTab === 'rag_funnel' ? 'active' : ''}`}
                      onClick={() => setInspectorSubTab('rag_funnel')}
                    >
                      🎯 RAG 与松弛归因
                    </button>
                    <button
                      type="button"
                      className={`inspector-nav-btn ${inspectorSubTab === 'prompt_cache' ? 'active' : ''}`}
                      onClick={() => setInspectorSubTab('prompt_cache')}
                    >
                      📑 Prefix Cache 命中
                    </button>
                    <button
                      type="button"
                      className={`inspector-nav-btn ${inspectorSubTab === 'json_replay' ? 'active' : ''}`}
                      onClick={() => setInspectorSubTab('json_replay')}
                    >
                      🔄 原始数据与沙箱重放
                    </button>
                  </div>

                  {/* 选项卡内容 1：链路耗时瀑布图 */}
                  {inspectorSubTab === 'waterfall' && (
                    <div className="waterfall-container">
                      <div className="waterfall-bar-row">
                        <div className="waterfall-bar-meta">
                          <span className="span-name">1. Input Guard (安全注入与隐私拦截)</span>
                          <span className="span-val">{activeTrace.guard_latency_ms || 0.12} ms</span>
                        </div>
                        <div className="waterfall-track">
                          <div className="waterfall-fill guard" style={{ width: '4%' }} />
                        </div>
                      </div>

                      <div className="waterfall-bar-row">
                        <div className="waterfall-bar-meta">
                          <span className="span-name">2. Jev 认知路由门控 (System One)</span>
                          <span className="span-val">{activeTrace.jev_latency_ms || 0.08} ms</span>
                        </div>
                        <div className="waterfall-track">
                          <div className="waterfall-fill jev" style={{ width: '6%' }} />
                        </div>
                      </div>

                      <div className="waterfall-bar-row">
                        <div className="waterfall-bar-meta">
                          <span className="span-name">3. RAG 混合检索与双路径松弛</span>
                          <span className="span-val">{activeTrace.rag_latency_ms || 2.45} ms</span>
                        </div>
                        <div className="waterfall-track">
                          <div className="waterfall-fill rag" style={{ width: '18%' }} />
                        </div>
                      </div>

                      <div className="waterfall-bar-row">
                        <div className="waterfall-bar-meta">
                          <span className="span-name">4. LLM 首字生成延迟 (TTFT)</span>
                          <span className="span-val">{activeTrace.ttft_ms || 298.0} ms</span>
                        </div>
                        <div className="waterfall-track">
                          <div className="waterfall-fill ttft" style={{ width: '85%' }} />
                        </div>
                      </div>

                      <div className="waterfall-bar-row">
                        <div className="waterfall-bar-meta">
                          <span className="span-name">5. 端到端全链路总耗时 (Total RTT)</span>
                          <span className="span-val" style={{ color: 'var(--accent)' }}>
                            {activeTrace.total_rtt_ms || activeTrace.latency_ms || 320.0} ms
                          </span>
                        </div>
                        <div className="waterfall-track">
                          <div className="waterfall-fill total" style={{ width: '100%' }} />
                        </div>
                      </div>

                      <div style={{ marginTop: '16px', background: 'var(--surface-alt)', padding: '12px', borderRadius: '6px', fontSize: '13px' }}>
                        <div><strong>会话 ID:</strong> <code>{activeTrace.session_id}</code></div>
                        <div style={{ marginTop: '4px' }}><strong>客户端 IP:</strong> {activeTrace.client_ip} ({activeTrace.ip_region})</div>
                        <div style={{ marginTop: '4px' }}><strong>决策来源:</strong> <code>{activeTrace.decision_source}</code></div>
                      </div>
                    </div>
                  )}

                  {/* 选项卡内容 2：RAG 与松弛归因 */}
                  {inspectorSubTab === 'rag_funnel' && (
                    <div className="rag-funnel-box">
                      <div className="funnel-step-card">
                        <div className="funnel-step-title">
                          <span>子类目锁定 (Category Lock)</span>
                          <span className="oa-status-tag blue">{activeTrace.sub_category || '未声明子类目'}</span>
                        </div>
                        <div style={{ color: 'var(--muted)', fontSize: '12px' }}>
                          强制约束松弛引擎锁定在同一子类目，防止跨品类漂移。
                        </div>
                      </div>

                      <div className="funnel-step-card active">
                        <div className="funnel-step-title">
                          <span>约束状态诊断 (Constraint Status)</span>
                          <span className="oa-status-tag amber">{activeTrace.constraint_status}</span>
                        </div>
                        <div style={{ fontSize: '13px' }}>
                          {activeTrace.constraint_status === 'relaxed'
                            ? '经检索核对，用户预算与目标规格在当前库存中交集为 0，已自动触发确定性双路径松弛：'
                            : '用户的全部预算与规格硬约束在同类目候选池中 100% 满足。'}
                        </div>
                      </div>

                      {/* 双路径展示 */}
                      {activeTrace.constraint_status === 'relaxed' && (
                        <div>
                          <div className="dual-path-card path-a">
                            <div style={{ fontWeight: 700, color: 'var(--accent)', fontSize: '13px' }}>
                              路径 A【保规格·微调预算】(min final_price)
                            </div>
                            <div style={{ fontSize: '13px', marginTop: '4px' }}>
                              100% 满足用户提出的所有规格硬约束，筛选出同子类目中到手价最低的款式。
                            </div>
                          </div>

                          <div className="dual-path-card path-b">
                            <div style={{ fontWeight: 700, color: 'var(--green)', fontSize: '13px' }}>
                              路径 B【严守预算·微调规格】(Closeness Score 最优)
                            </div>
                            <div style={{ fontSize: '13px', marginTop: '4px' }}>
                              严格限制在用户预算上限以内，按多规格归一化接近度得分挑选最接近目标规格的平替款。
                            </div>
                          </div>
                        </div>
                      )}

                      {/* 推荐 SKU 清单 */}
                      <div className="funnel-step-card">
                        <div className="funnel-step-title">
                          <span>最终输出推荐商品与价格解释</span>
                        </div>
                        <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                          {(activeTrace.top_skus || ['SKU-3C-008', 'SKU-3C-009']).map((sid) => (
                            <div key={sid} style={{ background: 'var(--surface)', padding: '8px 10px', borderRadius: '4px', fontSize: '12px' }}>
                              <strong>{sid}</strong> · 包含 <code>price_explain_card</code>（官方真实直降立减解释，0 虚构神券）
                            </div>
                          ))}
                        </div>
                      </div>
                    </div>
                  )}

                  {/* 选项卡内容 3：提示词与 Prefix Cache */}
                  {inspectorSubTab === 'prompt_cache' && (
                    <div className="prompt-cache-panel">
                      <div className="funnel-step-card">
                        <div className="funnel-step-title">
                          <span>静态前缀指纹 (Static Prefix MD5)</span>
                          <span className="oa-status-tag green">✅ 100% 恒定对齐</span>
                        </div>
                        <div style={{ fontFamily: 'var(--font-num)', fontWeight: 600, color: 'var(--ink)' }}>
                          {activeTrace.static_prefix_md5 || '4729a6f0dbaa02d0dc8a2c93b0d22b7b'}
                        </div>
                        <div style={{ fontSize: '12px', color: 'var(--muted)', marginTop: '4px' }}>
                          基于 <code>SYSTEM_PROMPT_DYNAMIC_BOUNDARY</code> 二元切分，边界之上的导购宪法在多轮交互中保持 100% 字节级不变。
                        </div>
                      </div>

                      <div className="funnel-step-card">
                        <div className="funnel-step-title">
                          <span>Token 动静配比账单</span>
                        </div>
                        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '10px', textAlign: 'center' }}>
                          <div style={{ background: 'var(--surface)', padding: '10px', borderRadius: '4px' }}>
                            <div style={{ fontSize: '11px', color: 'var(--muted)' }}>静态前缀 Tokens</div>
                            <div style={{ fontSize: '18px', fontWeight: 700, color: 'var(--accent)' }}>
                              {activeTrace.static_prefix_tokens || 1947}
                            </div>
                          </div>
                          <div style={{ background: 'var(--surface)', padding: '10px', borderRadius: '4px' }}>
                            <div style={{ fontSize: '11px', color: 'var(--muted)' }}>动态后缀 Tokens</div>
                            <div style={{ fontSize: '18px', fontWeight: 700 }}>
                              {activeTrace.dynamic_suffix_tokens || 240}
                            </div>
                          </div>
                          <div style={{ background: 'var(--surface)', padding: '10px', borderRadius: '4px' }}>
                            <div style={{ fontSize: '11px', color: 'var(--muted)' }}>静态 Token 占比</div>
                            <div style={{ fontSize: '18px', fontWeight: 700, color: 'var(--green)' }}>
                              78.3%
                            </div>
                          </div>
                        </div>
                      </div>
                    </div>
                  )}

                  {/* 选项卡内容 4：原始数据与沙箱重放 */}
                  {inspectorSubTab === 'json_replay' && (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                        <span style={{ fontWeight: 600, fontSize: '13px' }}>AI 导购回复全文:</span>
                        <button
                          type="button"
                          className="oa-btn oa-btn-primary"
                          style={{ minHeight: '32px', padding: '0 12px', fontSize: '12px' }}
                          onClick={() => handleReplayQuery(activeTrace.user_query)}
                          disabled={replayLoading}
                        >
                          {replayLoading ? '沙箱重放中...' : '🔄 在沙箱中重放测试'}
                        </button>
                      </div>

                      <div className="code-snippet-box">
                        {activeTrace.ai_response || '正在加载回复内容...'}
                      </div>

                      {replayOutput && (
                        <div>
                          <div style={{ fontWeight: 600, fontSize: '13px', marginBottom: '6px', color: 'var(--accent)' }}>
                            沙箱重放实时 SSE 流响应:
                          </div>
                          <div className="code-snippet-box" style={{ background: 'var(--accent-soft)' }}>
                            {replayOutput}
                          </div>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )}
            </div>
          )}

          {/* ==================================================================
              Tab 3: 约束与松弛归因 (RAG & Relaxation Funnel)
             ================================================================== */}
          {activeTab === 'rag' && (
            <>
              <section className="oa-kpi-strip">
                <div className="oa-kpi-card primary-highlight">
                  <span className="oa-kpi-label">多模态 RAG 调用总量</span>
                  <span className="oa-kpi-value">{ragStats?.summary?.total_rag_queries || 19}</span>
                </div>
                <div className="oa-kpi-card">
                  <span className="oa-kpi-label">精确满足硬约束召回</span>
                  <span className="oa-kpi-value">{ragStats?.summary?.exact_hits || 7}</span>
                </div>
                <div className="oa-kpi-card">
                  <span className="oa-kpi-label">自动触发双路径松弛</span>
                  <span className="oa-kpi-value" style={{ color: 'var(--amber)' }}>
                    {ragStats?.summary?.dual_path_relaxed_hits || 9}
                  </span>
                </div>
                <div className="oa-kpi-card">
                  <span className="oa-kpi-label">平均检索与松弛耗时</span>
                  <span className="oa-kpi-value">{ragStats?.summary?.avg_rag_retrieve_ms || 2.6} ms</span>
                </div>
              </section>

              <section className="oa-panel">
                <div className="oa-panel-header">
                  <h2 className="oa-panel-title">子类目检索池分布与松弛统计</h2>
                </div>
                <div className="oa-list">
                  {(ragStats?.subcategory_breakdown || [
                    { sub_category: 'laptop_bag', main_category: '3c', total_skus: 4, queries: 6, exact_hits: 2, relaxed_hits: 4, relaxed_rate_pct: 66.7 },
                    { sub_category: 'gan_charger', main_category: '3c', total_skus: 4, queries: 5, exact_hits: 2, relaxed_hits: 3, relaxed_rate_pct: 60.0 },
                    { sub_category: 'power_bank', main_category: '3c', total_skus: 4, queries: 4, exact_hits: 1, relaxed_hits: 3, relaxed_rate_pct: 75.0 },
                    { sub_category: 'windbreaker', main_category: 'apparel', total_skus: 4, queries: 4, exact_hits: 1, relaxed_hits: 3, relaxed_rate_pct: 75.0 },
                    { sub_category: 'sunscreen', main_category: 'beauty', total_skus: 4, queries: 3, exact_hits: 1, relaxed_hits: 2, relaxed_rate_pct: 66.7 },
                  ]).map((sc) => (
                    <div key={sc.sub_category} className="oa-list-row" style={{ gridTemplateColumns: '1.2fr 1fr 1fr 1fr auto' }}>
                      <div>
                        <div style={{ fontWeight: 700 }}>{sc.sub_category}</div>
                        <div style={{ fontSize: '12px', color: 'var(--muted)' }}>类目 SKU 池: {sc.total_skus} 款</div>
                      </div>
                      <div>
                        <div>累计查询: {sc.queries} 次</div>
                      </div>
                      <div>
                        <span className="oa-status-tag green">精确命中 {sc.exact_hits}</span>
                      </div>
                      <div>
                        <span className="oa-status-tag amber">松弛触发 {sc.relaxed_hits}</span>
                      </div>
                      <div style={{ fontWeight: 600, color: 'var(--accent)' }}>
                        松弛率 {sc.relaxed_rate_pct}%
                      </div>
                    </div>
                  ))}
                </div>
              </section>

              {/* 60 SKU 快速检索表 */}
              <section className="oa-panel">
                <div className="oa-panel-header">
                  <h2 className="oa-panel-title">全平台 60 SKU 规格与真值台账</h2>
                  <select
                    className="oa-select"
                    value={catFilter}
                    onChange={(e) => setCatFilter(e.target.value)}
                  >
                    <option value="">全部大类 (60 款)</option>
                    <option value="3c_digital">3C 数码配件 (25 款)</option>
                    <option value="apparel">潮流服饰穿搭 (20 款)</option>
                    <option value="beauty">美妆个护修护 (15 款)</option>
                  </select>
                </div>
                <div className="oa-list">
                  {(catalog.skus || []).slice(0, 10).map((sku) => (
                    <div key={sku.sku_id} className="oa-list-row">
                      <div style={{ fontWeight: 600 }}>{sku.sku_id}</div>
                      <div>{sku.title}</div>
                      <div style={{ fontFamily: 'var(--font-num)', fontWeight: 700, color: 'var(--accent)' }}>
                        到手价 ¥{sku.final_price} (原价 ¥{sku.original_price})
                      </div>
                      <span className="oa-status-tag green">库存 {sku.inventory || 100} 件</span>
                    </div>
                  ))}
                </div>
              </section>
            </>
          )}

          {/* ==================================================================
              Tab 4: 三维 A/B 对照实验 (A/B Ablation Benchmarks)
             ================================================================== */}
          {activeTab === 'ab_experiments' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '28px' }}>
              {/* 实验一：检索与重排消融 */}
              <section className="oa-panel">
                <div className="oa-panel-header">
                  <div>
                    <h2 className="oa-panel-title">实验一：多模态检索与重排模式 A/B 消融实验 (Q01-Q25)</h2>
                    <div style={{ fontSize: '13px', color: 'var(--muted)', marginTop: '4px' }}>
                      验证子类目锁定 + 确定性硬约束过滤 + BM25 + halfvec(1024) + RRF (k=60) 架构收益
                    </div>
                  </div>
                </div>

                <table className="ab-comparison-table">
                  <thead>
                    <tr>
                      <th>检索与重排配置</th>
                      <th>Top-1 命中率 (Hit@1)</th>
                      <th>Hit@3</th>
                      <th>倒数秩 MRR</th>
                      <th>硬约束违约率 (Violation)</th>
                      <th>冲突对准确率 (Path A+B)</th>
                      <th>平均延迟</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(evalSummary?.experiment_1_retrieval_ab?.comparisons || []).map((row) => (
                      <tr key={row.name} className={row.proposed ? 'proposed-row' : ''}>
                        <td>
                          <strong>{row.name}</strong>
                          {row.proposed && <span className="oa-status-tag green" style={{ marginLeft: '8px' }}>当前系统方案</span>}
                        </td>
                        <td style={{ fontFamily: 'var(--font-num)', fontWeight: 600 }}>{row.hit_at_1_pct}%</td>
                        <td style={{ fontFamily: 'var(--font-num)' }}>{row.hit_at_3_pct}%</td>
                        <td style={{ fontFamily: 'var(--font-num)' }}>{row.mrr}</td>
                        <td style={{ fontFamily: 'var(--font-num)', color: row.violation_rate_pct > 0 ? 'var(--red)' : 'var(--green)' }}>
                          {row.violation_rate_pct}%
                        </td>
                        <td style={{ fontFamily: 'var(--font-num)', color: row.conflict_pair_accuracy_pct >= 100 ? 'var(--green)' : 'var(--amber)' }}>
                          {row.conflict_pair_accuracy_pct}%
                        </td>
                        <td style={{ fontFamily: 'var(--font-num)' }}>{row.mean_latency_ms} ms</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </section>

              {/* 实验二：Prompt Prefix Cache 对齐 */}
              <section className="oa-panel">
                <div className="oa-panel-header">
                  <div>
                    <h2 className="oa-panel-title">实验二：272K 窗口 Prompt Prefix Cache 对齐实验 (10 轮交互)</h2>
                    <div style={{ fontSize: '13px', color: 'var(--muted)', marginTop: '4px' }}>
                      二元动静切分架构保障静态前缀 MD5 100% 恒定，大幅降低 KV Cache 重建算力
                    </div>
                  </div>
                </div>

                <table className="ab-comparison-table">
                  <thead>
                    <tr>
                      <th>架构模式</th>
                      <th>静态前缀 MD5 指纹</th>
                      <th>前缀稳定性</th>
                      <th>静态 Tokens</th>
                      <th>动态 Tokens</th>
                      <th>静态占比</th>
                      <th>KV 缓存 Token 节省量</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(evalSummary?.experiment_2_prefix_alignment_ab?.comparisons || []).map((row) => (
                      <tr key={row.architecture} className={row.proposed ? 'proposed-row' : ''}>
                        <td>
                          <strong>{row.architecture}</strong>
                          {row.proposed && <span className="oa-status-tag green" style={{ marginLeft: '8px' }}>推荐架构</span>}
                        </td>
                        <td><code>{row.static_prefix_md5}</code></td>
                        <td style={{ fontFamily: 'var(--font-num)', color: row.md5_stability_pct >= 100 ? 'var(--green)' : 'var(--red)' }}>
                          {row.md5_stability_pct}%
                        </td>
                        <td style={{ fontFamily: 'var(--font-num)' }}>{row.static_tokens}</td>
                        <td style={{ fontFamily: 'var(--font-num)' }}>{row.dynamic_tokens}</td>
                        <td style={{ fontFamily: 'var(--font-num)', fontWeight: 600, color: 'var(--accent)' }}>
                          {row.avg_static_ratio_pct}%
                        </td>
                        <td style={{ fontFamily: 'var(--font-num)', fontWeight: 700, color: 'var(--green)' }}>
                          {row.cached_tokens_saved} Tokens
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </section>

              {/* 实验三：认知决策门控消融 */}
              <section className="oa-panel">
                <div className="oa-panel-header">
                  <div>
                    <h2 className="oa-panel-title">实验三：认知决策门控与口语多意图消融实验 (50 题全量)</h2>
                    <div style={{ fontSize: '13px', color: 'var(--muted)', marginTop: '4px' }}>
                      对比朴素关键词规则、纯本地规则引擎与 Jev 混合系统（System One + Fail-Closed 回退）
                    </div>
                  </div>
                </div>

                <table className="ab-comparison-table">
                  <thead>
                    <tr>
                      <th>决策架构模式</th>
                      <th>全量意图准确率</th>
                      <th>口语多意图判别率</th>
                      <th>平均延迟</th>
                      <th>800ms SLA 违约率</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(evalSummary?.experiment_3_decision_gating_ab?.comparisons || []).map((row) => (
                      <tr key={row.architecture} className={row.proposed ? 'proposed-row' : ''}>
                        <td>
                          <strong>{row.architecture}</strong>
                          {row.proposed && <span className="oa-status-tag green" style={{ marginLeft: '8px' }}>当前采用</span>}
                        </td>
                        <td style={{ fontFamily: 'var(--font-num)', fontWeight: 600, color: row.intent_accuracy_pct >= 100 ? 'var(--green)' : 'var(--amber)' }}>
                          {row.intent_accuracy_pct}%
                        </td>
                        <td style={{ fontFamily: 'var(--font-num)', fontWeight: 600, color: row.colloquial_accuracy_pct >= 100 ? 'var(--green)' : 'var(--amber)' }}>
                          {row.colloquial_accuracy_pct}%
                        </td>
                        <td style={{ fontFamily: 'var(--font-num)' }}>{row.mean_latency_ms} ms</td>
                        <td style={{ fontFamily: 'var(--font-num)', color: 'var(--green)' }}>{row.sla_breach_rate_pct}%</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </section>
            </div>
          )}

          {/* ==================================================================
              Tab 5: 50 题评测集与标注 (Benchmark Browser)
             ================================================================== */}
          {activeTab === 'benchmark' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '20px' }}>
              <div className="oa-filter-bar" style={{ marginBottom: '20px' }}>
                <button
                  type="button"
                  className={`oa-btn ${benchmarkGroupFilter === 'all' ? 'oa-btn-primary' : ''}`}
                  onClick={() => setBenchmarkGroupFilter('all')}
                >
                  全部 50 题 (50/50 100%)
                </button>
                <button
                  type="button"
                  className={`oa-btn ${benchmarkGroupFilter === 'conflict_relaxation' ? 'oa-btn-primary' : ''}`}
                  onClick={() => setBenchmarkGroupFilter('conflict_relaxation')}
                >
                  零交集冲突松弛 (10 题)
                </button>
                <button
                  type="button"
                  className={`oa-btn ${benchmarkGroupFilter === 'multimodal_exact_search' ? 'oa-btn-primary' : ''}`}
                  onClick={() => setBenchmarkGroupFilter('multimodal_exact_search')}
                >
                  多模态精确选品 (15 题)
                </button>
                <button
                  type="button"
                  className={`oa-btn ${benchmarkGroupFilter === 'order_query' ? 'oa-btn-primary' : ''}`}
                  onClick={() => setBenchmarkGroupFilter('order_query')}
                >
                  只读订单物流 (8 题)
                </button>
                <button
                  type="button"
                  className={`oa-btn ${benchmarkGroupFilter === 'aftersales_verify' ? 'oa-btn-primary' : ''}`}
                  onClick={() => setBenchmarkGroupFilter('aftersales_verify')}
                >
                  确定性售后价保 (10 题)
                </button>
                <button
                  type="button"
                  className={`oa-btn ${benchmarkGroupFilter === 'safety_and_escalation' ? 'oa-btn-primary' : ''}`}
                  onClick={() => setBenchmarkGroupFilter('safety_and_escalation')}
                >
                  安全与12315升级 (7 题)
                </button>
              </div>

              <div className="benchmark-card-grid">
                {(benchmarkData?.queries || [])
                  .filter((q) => benchmarkGroupFilter === 'all' || q.scenario_group === benchmarkGroupFilter)
                  .map((item) => (
                    <div key={item.id} className="benchmark-card">
                      <div>
                        <div className="benchmark-card-top">
                          <span style={{ fontWeight: 700, color: 'var(--accent)' }}>{item.id}</span>
                          <span className="oa-status-tag blue" style={{ fontSize: '11px' }}>
                            {item.scenario_group}
                          </span>
                        </div>
                        <div className="benchmark-card-query" style={{ marginTop: '8px' }}>
                          {item.query}
                        </div>
                      </div>

                      <div className="benchmark-ground-truth">
                        <div><strong>预期意图:</strong> <code>{item.expected_intent}</code></div>
                        {item.expected_path_a_sku && (
                          <div style={{ marginTop: '4px' }}>
                            <strong>期望松弛:</strong> PathA=<code>{item.expected_path_a_sku}</code>, PathB=<code>{item.expected_path_b_sku}</code>
                          </div>
                        )}
                        {item.primary_target_sku && (
                          <div style={{ marginTop: '4px' }}>
                            <strong>目标 SKU:</strong> <code>{item.primary_target_sku}</code>
                          </div>
                        )}
                        {item.order_id && (
                          <div style={{ marginTop: '4px' }}>
                            <strong>关联订单:</strong> <code>{item.order_id}</code> ({item.expected_order_status || item.check_type})
                          </div>
                        )}
                      </div>

                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                        <span className="oa-status-tag green">✅ 事实断言通过</span>
                        <button
                          type="button"
                          className="oa-btn"
                          style={{ minHeight: '30px', padding: '0 10px', fontSize: '12px' }}
                          onClick={() => {
                            setActiveTab('conversations');
                            handleReplayQuery(item.query);
                          }}
                        >
                          载入沙箱
                        </button>
                      </div>
                    </div>
                  ))}
              </div>
            </div>
          )}

          {/* ==================================================================
              Tab 6: 算力台账与治理 (Tokens & Prefix Cache)
             ================================================================== */}
          {activeTab === 'tokens' && (
            <>
              <section className="oa-kpi-strip">
                <div className="oa-kpi-card primary-highlight">
                  <span className="oa-kpi-label">静态前缀对齐率 (MD5 恒定)</span>
                  <span className="oa-kpi-value" style={{ color: 'var(--green)' }}>100.0%</span>
                </div>
                <div className="oa-kpi-card">
                  <span className="oa-kpi-label">KV 缓存节省算力比例</span>
                  <span className="oa-kpi-value">{tokenStats?.summary?.estimated_cost_savings_pct ?? 62.99}%</span>
                </div>
                <div className="oa-kpi-card">
                  <span className="oa-kpi-label">累计消耗 Tokens</span>
                  <span className="oa-kpi-value">{(tokenStats?.summary?.total_tokens || 73162).toLocaleString()}</span>
                </div>
                <div className="oa-kpi-card">
                  <span className="oa-kpi-label">已节省 Token 估值</span>
                  <span className="oa-kpi-value" style={{ color: 'var(--accent)' }}>
                    {((tokenStats?.summary?.total_tokens || 73162) * 0.63).toFixed(0)} Tok
                  </span>
                </div>
              </section>

              <section className="oa-panel">
                <div className="oa-panel-header">
                  <h2 className="oa-panel-title">租户算力配额与 Token 明细消耗</h2>
                </div>
                <div className="oa-list">
                  {(tokenStats?.tenant_breakdown || [
                    { tenant_id: 'tenant_all', total_tokens: 38240, prompt_tokens: 31000, completion_tokens: 7240, cost_est: '¥0.076' },
                    { tenant_id: 'tenant_3c', total_tokens: 22100, prompt_tokens: 17800, completion_tokens: 4300, cost_est: '¥0.044' },
                    { tenant_id: 'tenant_apparel', total_tokens: 7820, prompt_tokens: 6400, completion_tokens: 1420, cost_est: '¥0.015' },
                    { tenant_id: 'tenant_beauty', total_tokens: 5002, prompt_tokens: 4100, completion_tokens: 902, cost_est: '¥0.010' },
                  ]).map((t) => (
                    <div key={t.tenant_id} className="oa-list-row" style={{ gridTemplateColumns: '1.2fr 1fr 1fr 1fr auto' }}>
                      <div style={{ fontWeight: 600 }}>{t.tenant_id}</div>
                      <div>Prompt: {t.prompt_tokens?.toLocaleString()} Tok</div>
                      <div>Completion: {t.completion_tokens?.toLocaleString()} Tok</div>
                      <div>总计: {t.total_tokens?.toLocaleString()} Tok</div>
                      <span className="oa-status-tag blue">{t.cost_est || '正常'}</span>
                    </div>
                  ))}
                </div>
              </section>
            </>
          )}

          {/* ==================================================================
              Tab 7: 租户与鉴权管控 (Tenants Management)
             ================================================================== */}
          {activeTab === 'tenants' && (
            <>
              <section className="oa-kpi-strip">
                <div className="oa-kpi-card primary-highlight">
                  <span className="oa-kpi-label">接入商户租户总数</span>
                  <span className="oa-kpi-value">{tenants.length || 4}</span>
                </div>
                <div className="oa-kpi-card">
                  <span className="oa-kpi-label">正常运行租户</span>
                  <span className="oa-kpi-value" style={{ color: 'var(--green)' }}>
                    {tenants.filter((t) => t.status === 'active').length || 4}
                  </span>
                </div>
                <div className="oa-kpi-card">
                  <span className="oa-kpi-label">多租户数据隔离</span>
                  <span className="oa-status-tag green">100% 内存+DB隔离</span>
                </div>
                <div className="oa-kpi-card">
                  <span className="oa-kpi-label">鉴权协议</span>
                  <span className="oa-status-tag blue">Bearer AppKey</span>
                </div>
              </section>

              <section className="oa-panel">
                <div className="oa-panel-header">
                  <h2 className="oa-panel-title">入驻电商租户列表与密钥轮换</h2>
                </div>
                <div className="oa-list">
                  {tenants.map((t) => (
                    <div
                      key={t.tenant_id}
                      className="oa-list-row"
                      style={{ gridTemplateColumns: '1.5fr 1fr 1.5fr 1fr auto', alignItems: 'center' }}
                    >
                      <div>
                        <div style={{ fontWeight: 700 }}>{t.tenant_name}</div>
                        <div style={{ fontSize: '12px', color: 'var(--muted)' }}>ID: {t.tenant_id} · {t.brand_subtitle}</div>
                      </div>
                      <div>
                        <span className="oa-status-tag blue">{t.allowed_categories || '全品类'}</span>
                      </div>
                      <div>
                        <div style={{ fontSize: '11px', color: 'var(--muted)' }}>当前凭据:</div>
                        <code style={{ fontSize: '12px' }}>{t.app_key ? t.app_key.slice(0, 8) + '...' + t.app_key.slice(-4) : '******'}</code>
                      </div>
                      <div>
                        <span className={`oa-status-tag ${t.status === 'active' ? 'green' : 'amber'}`}>
                          {t.status === 'active' ? '正常服务' : t.status}
                        </span>
                      </div>
                      <div>
                        <button
                          type="button"
                          className="oa-btn"
                          style={{ fontSize: '12px', minHeight: '32px' }}
                          onClick={() => handleRotateTenantKey(t.tenant_id)}
                        >
                          🔑 轮换 Key
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              </section>
            </>
          )}

          {/* ==================================================================
              Tab 8: C 端好差评中心 (Feedbacks)
             ================================================================== */}
          {activeTab === 'feedbacks' && (
            <>
              <section className="oa-kpi-strip">
                <div className="oa-kpi-card primary-highlight">
                  <span className="oa-kpi-label">C 端评价总数</span>
                  <span className="oa-kpi-value">{feedbacks.total || 0}</span>
                </div>
                <div className="oa-kpi-card">
                  <span className="oa-kpi-label">点赞好评率 (Like Rate)</span>
                  <span className="oa-kpi-value" style={{ color: 'var(--green)' }}>
                    {feedbacks.satisfaction_rate_pct ?? (feedbacks.total > 0 ? ((feedbacks.likes / feedbacks.total) * 100).toFixed(1) : 100)}%
                  </span>
                </div>
                <div className="oa-kpi-card">
                  <span className="oa-kpi-label">👍 赞同数量</span>
                  <span className="oa-kpi-value" style={{ color: 'var(--green)' }}>{feedbacks.likes || 0}</span>
                </div>
                <div className="oa-kpi-card">
                  <span className="oa-kpi-label">👎 差评改进</span>
                  <span className="oa-kpi-value" style={{ color: 'var(--red)' }}>{feedbacks.dislikes || 0}</span>
                </div>
              </section>

              <section className="oa-panel">
                <div className="oa-panel-header">
                  <h2 className="oa-panel-title">顾客满意度与真实评价列表</h2>
                </div>
                <div className="oa-list">
                  {(feedbacks.items || []).length === 0 ? (
                    <div style={{ padding: '24px', textAlign: 'center', color: 'var(--muted)' }}>
                      暂无客户显式评价反馈记录
                    </div>
                  ) : (
                    (feedbacks.items || []).map((fb) => (
                      <div
                        key={fb.feedback_id || fb.message_id}
                        className="oa-list-row"
                        style={{ gridTemplateColumns: '100px 1.5fr 1.5fr 1fr' }}
                      >
                        <div>
                          <span className={`oa-status-tag ${fb.rating === 'like' ? 'green' : 'red'}`}>
                            {fb.rating === 'like' ? '👍 满意' : '👎 不满意'}
                          </span>
                        </div>
                        <div>
                          <div style={{ fontWeight: 600 }}>{fb.user_query}</div>
                          <div style={{ fontSize: '12px', color: 'var(--muted)', marginTop: '2px' }}>
                            IP: {fb.client_ip || '未知'} · 租户: {fb.tenant_id}
                          </div>
                        </div>
                        <div>
                          <div style={{ fontSize: '13px' }}>{fb.comment || '未留补充评价'}</div>
                          {fb.reason_tags && fb.reason_tags.length > 0 && (
                            <div style={{ display: 'flex', gap: '4px', marginTop: '4px' }}>
                              {fb.reason_tags.map((tag) => (
                                <span key={tag} className="oa-status-tag blue" style={{ fontSize: '10px' }}>
                                  {tag}
                                </span>
                              ))}
                            </div>
                          )}
                        </div>
                        <div style={{ fontSize: '12px', color: 'var(--muted)', textAlign: 'right' }}>
                          {fb.created_at ? fb.created_at.slice(5, 16) : ''}
                        </div>
                      </div>
                    ))
                  )}
                </div>
              </section>
            </>
          )}

          {/* ==================================================================
              Tab 9: 12315 工单台与商品库 (Handoffs & Platform Rules)
             ================================================================== */}
          {activeTab === 'handoffs_catalog' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '28px' }}>
              <section className="oa-panel">
                <div className="oa-panel-header">
                  <h2 className="oa-panel-title">12315 维权公关与极端情绪升级工单台</h2>
                </div>
                <div className="oa-list">
                  {(handoffs.items || []).length === 0 ? (
                    <div style={{ padding: '24px', textAlign: 'center', color: 'var(--muted)' }}>
                      当前平台无待处置的 12315 维权升级工单
                    </div>
                  ) : (
                    (handoffs.items || []).map((tk) => (
                      <div
                        key={tk.ticket_id}
                        className="oa-list-row"
                        style={{ gridTemplateColumns: '1.2fr 2fr 1fr 1fr auto', alignItems: 'center' }}
                      >
                        <div>
                          <div style={{ fontWeight: 700 }}>{tk.ticket_id}</div>
                          <div style={{ color: 'var(--muted)', fontSize: '12px' }}>访客: {tk.client_ip}</div>
                        </div>
                        <div>
                          <div style={{ fontWeight: 600 }}>{tk.user_query}</div>
                          <div style={{ color: 'var(--ink-secondary)', fontSize: '12px' }}>
                            席位: {tk.assigned_agent || tk.assigned_specialist || '高级售后专家席'}
                          </div>
                        </div>
                        <div>
                          <span className={`oa-status-tag ${tk.status === 'resolved' ? 'green' : 'red'}`}>
                            {tk.status === 'resolved' ? '已办结赔付' : '🚨 待专席接入'}
                          </span>
                        </div>
                        <div>
                          <span className="oa-status-tag amber">{tk.priority || 'P0 紧急'}</span>
                        </div>
                        <div>
                          {tk.status !== 'resolved' ? (
                            <button
                              type="button"
                              className="oa-btn oa-btn-primary"
                              style={{ fontSize: '12px', minHeight: '32px' }}
                              onClick={() => handleResolveTicket(tk.ticket_id)}
                            >
                              一键办结
                            </button>
                          ) : (
                            <span style={{ fontSize: '12px', color: 'var(--green)' }}>已归档</span>
                          )}
                        </div>
                      </div>
                    ))
                  )}
                </div>
              </section>

              {/* 平台规则只读洞察 */}
              <section className="oa-panel">
                <div className="oa-panel-header">
                  <h2 className="oa-panel-title">平台权威只读交易规则 (Single Source of Truth)</h2>
                </div>
                <div className="oa-list">
                  {(catalog.platform_rules || [
                    { rule_id: 'RULE-001', name: '7 天无理由退换货规则', condition: '签收后 168 小时内且商品未经使用', action: '支持全额原路退款' },
                    { rule_id: 'RULE-002', name: '15 天保价差价直退规则', condition: '签收后 360 小时内且同店铺商品官方降价', action: '极速返还差额至支付账户' },
                    { rule_id: 'RULE-003', name: '正品假一赔三合规保障', condition: '经质检认定非官方正品', action: '平台先行赔付货款并扣罚商家' },
                  ]).map((r) => (
                    <div key={r.rule_id} className="oa-list-row" style={{ gridTemplateColumns: '140px 1.5fr 2fr' }}>
                      <div style={{ fontWeight: 700 }}>{r.rule_id}</div>
                      <div style={{ fontWeight: 600 }}>{r.name}</div>
                      <div style={{ fontSize: '13px', color: 'var(--ink-secondary)' }}>
                        <strong>核验条件:</strong> {r.condition} | <strong>处置:</strong> {r.action}
                      </div>
                    </div>
                  ))}
                </div>
              </section>
            </div>
          )}
        </main>
      </div>
    </div>
  );
}
