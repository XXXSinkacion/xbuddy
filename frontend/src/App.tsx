import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, fmtDur } from "./api";
import BlockView from "./components/Blocks";
import { ArtifactModal, ArtifactsPanel, EvidenceModal, EvidencePanel, MemoryPanel, TasksPanel, TracePanel } from "./components/Panels";
import { useThreadStream } from "./hooks/useThreadStream";
import { applyTheme, useTheme } from "./theme";
import type { Config, Evidence, Message, Run, ThreadRow, XEvent } from "./types";

const PRESETS = [
  "研究贵州茅台(600519.SH)近3个月行情、估值与最新财务指标，并与白酒板块对比",
  "对比宁德时代与比亚迪的估值与盈利能力，列出事实与不确定项",
  "今日市场情绪复盘：涨停池、连板统计、热股榜、主要指数表现",
  "研究招商银行(600036.SH)估值与近一年走势统计",
];

const ACTIVE = new Set(["planning", "running"]);

export default function App() {
  const [config, setConfig] = useState<Config | null>(null);
  const [threads, setThreads] = useState<ThreadRow[]>([]);
  const [threadId, setThreadId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [run, setRun] = useState<Run | null>(null);
  const [evidence, setEvidence] = useState<Evidence[]>([]);
  const [events, setEvents] = useState<XEvent[]>([]);
  const [activity, setActivity] = useState<string>("");
  const [tab, setTab] = useState<"tasks" | "evidence" | "artifacts" | "trace" | "memory">("tasks");
  const [goal, setGoal] = useState("");
  const [showSettings, setShowSettings] = useState(false);
  const [budget, setBudget] = useState<Record<string, any>>({});
  const [evOpen, setEvOpen] = useState<Evidence | null>(null);
  const [artOpen, setArtOpen] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sse, setSse] = useState<string>("closed");
  const sinceRef = useRef(0);
  const runIdRef = useRef<string | null>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const theme = useTheme();

  const loadThreads = useCallback(() => api.threads().then((r) => setThreads(r.threads)).catch(() => {}), []);
  useEffect(() => {
    api.config().then((c) => { setConfig(c); setBudget(c.budget_defaults); }).catch((e) => setError(String(e)));
    loadThreads();
  }, []);

  const refreshRun = useCallback(async (id: string) => {
    try {
      const r = await api.run(id);
      setRun(r);
    } catch {}
  }, []);

  const openThread = useCallback(async (id: string) => {
    setThreadId(id);
    setError(null);
    const t = await api.thread(id);
    setMessages(t.messages);
    sinceRef.current = 0; // 全量回放事件，便于观测面板
    setEvents([]);
    const last = t.runs[t.runs.length - 1];
    runIdRef.current = last?.id || null;
    if (last) {
      await refreshRun(last.id);
      api.evidence(last.id).then((r) => setEvidence(r.evidence));
    } else {
      setRun(null);
      setEvidence([]);
    }
  }, [refreshRun]);

  // 事件处理
  const onEvent = useCallback((e: XEvent) => {
    setEvents((prev) => (prev.length > 600 ? [...prev.slice(-400), e] : [...prev, e]));
    const p = e.payload || {};
    if (e.type === "message.created") {
      setMessages((prev) => (prev.some((m) => m.id === p.id) ? prev : [...prev, p as Message]));
    }
    if (e.type === "evidence.recorded" && e.run_id === runIdRef.current) {
      setEvidence((prev) => (prev.some((x) => x.id === p.id) ? prev : [...prev, p as Evidence]));
    }
    if (e.type === "run.created") {
      runIdRef.current = e.run_id;
      setEvidence([]);
    }
    if (e.type.startsWith("run.") || e.type.startsWith("task.") || e.type.startsWith("approval.") || e.type.startsWith("artifact.") || e.type === "plan.created" || e.type === "budget.extended") {
      if (e.run_id) refreshRun(e.run_id);
      if (e.type === "run.completed" || e.type === "run.stopped" || e.type === "run.failed") loadThreads();
    }
    if (e.type === "tool.start") setActivity(`调用工具 ${p.tool} ${JSON.stringify(p.args).slice(0, 80)}`);
    if (e.type === "tool.end") setActivity(`${p.tool} → ${p.status} (${p.latency_ms}ms) ${p.evidence_label || ""}`);
    if (e.type === "llm.start") setActivity(`模型思考中（${p.purpose} · ${p.provider}）…`);
    if (e.type === "llm.end") setActivity(`模型返回 ${p.purpose}：${p.input_tokens}/${p.output_tokens} tokens，${p.latency_ms}ms${p.tool_calls?.length ? ` → 调用 ${p.tool_calls.join(", ")}` : ""}`);
    if (e.type === "context.compressed") setActivity(`上下文压缩：${p.before} → ${p.after} tokens`);
    if (e.type === "run.degraded") setActivity(`⚠️ 降级：${p.reason}`);
    if (e.type === "run.completed") setActivity("研究完成");
    if (e.type === "run.waiting_approval") setActivity("等待你的确认…");
  }, [refreshRun, loadThreads]);

  useThreadStream(threadId, sinceRef, onEvent, setSse);

  // 兜底轮询（SSE 断开时）
  useEffect(() => {
    if (!run || !ACTIVE.has(run.status)) return;
    const t = setInterval(() => refreshRun(run.id), 3000);
    return () => clearInterval(t);
  }, [run?.id, run?.status, refreshRun]);

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: "smooth" });
  }, [messages.length]);

  const newThread = async () => {
    const t = await api.createThread();
    await loadThreads();
    await openThread(t.id);
  };

  const submit = async () => {
    if (!goal.trim()) return;
    setError(null);
    try {
      let tid = threadId;
      if (!tid) {
        const t = await api.createThread();
        tid = t.id;
        await openThread(tid);
      }
      const r = await api.startRun(tid!, goal.trim(), budget);
      runIdRef.current = r.id;
      setRun(r);
      setEvidence([]);
      setGoal("");
      setTab("tasks");
      loadThreads();
    } catch (e: any) {
      setError(e.message);
    }
  };

  const openEvidenceByLabel = useCallback((label: string) => {
    const e = evidence.find((x) => x.label === label);
    if (e) setEvOpen(e);
    else if (run) api.evidenceOne(label, run.id).then(setEvOpen).catch(() => setError(`找不到证据 ${label}`));
  }, [evidence, run]);

  const pendingApprovals = run?.approvals.filter((a) => a.status === "pending") || [];
  // 只有未结束的运行可恢复（中断 / 失败 / 审批已决定的等待态）；已停止或已完成的运行已出报告，发起新研究即可
  const canResume = run && run.phase !== "done" && ["interrupted", "failed", "waiting_approval"].includes(run.status) && pendingApprovals.filter((a) => a.kind !== "memory_write").length === 0;
  const u = run?.usage;

  return (
    <div className="shell">
      <header className="appbar">
        <div className="brand">
          <div className="logo">X</div>
          <h1>投资 X Buddy</h1>
          <span className="divider" />
          <span className="sub">投研 Agent 工作台</span>
        </div>
        <div className="spacer" />
        {config && (
          <div className="status">
            <span><span className={`dot ${config.llm.available ? "ok" : "warn"}`} />模型 {config.llm.available ? config.llm.primary?.model : "未配置（模板模式）"}{config.llm.fallback ? " +备用" : ""}</span>
            <span><span className={`dot ${config.fuyao.mode === "live" ? "ok" : "warn"}`} />扶摇 {config.fuyao.mode === "live" ? "实时数据" : "演示数据"}</span>
            <span><span className={`dot ${config.ifind.ok ? "ok" : config.ifind.configured ? "err" : ""}`} />iFinD {config.ifind.ok ? `${config.ifind.count} 个工具` : config.ifind.configured ? "连接失败" : "未配置"}</span>
            <span>工具 {config.tools.length}</span>
            <span><span className={`dot ${sse === "open" ? "ok" : sse === "error" ? "err" : ""}`} />实时推送 {sse === "open" ? "已连接" : sse === "error" ? "重连中" : "未连接"}</span>
          </div>
        )}
        <button className="theme-toggle" onClick={() => applyTheme(theme === "dark" ? "light" : "dark")} title="切换浅色 / 深色终端主题">{theme === "dark" ? "☀ 浅色" : "☾ 终端"}</button>
      </header>
      <div className="app">
      <aside className="sidebar">
        <div className="new"><button className="primary" onClick={newThread}>＋ 新研究</button></div>
        <div className="side-title">研究线程</div>
        <div className="threads">
          {threads.map((t) => (
            <div className={`thread-item ${t.id === threadId ? "active" : ""}`} key={t.id} onClick={() => openThread(t.id)}>
              <span className="t" title={t.title}>{t.title}</span>
              <span className={`chip ${t.last_status || ""}`} style={{ fontSize: 10 }}>{t.last_status || "空"}</span>
            </div>
          ))}
          {!threads.length && <div className="empty">还没有线程。输入研究目标开始。</div>}
        </div>
        <div className="caps">
          <div>红涨绿跌 · 时点与口径见证据</div>
          <div>不预测涨跌，不给买卖建议</div>
        </div>
      </aside>

      <main className="main">
        <div className="topbar">
          <span className="goal">{run ? run.goal : "输入一个研究目标，Agent 将规划任务、调用金融工具、产出带证据的报告"}</span>
          {run && <span className={`chip ${run.status}`}>{run.status}</span>}
          {run && <span className={`chip ${run.mode}`}>{run.mode === "llm" ? "模型驱动" : "模板模式"}</span>}
          {run && u && (
            <div className="meters">
              <div className="meter"><span>成本 ${u.cost_usd.toFixed(4)} / {run.budget.max_cost_usd}</span><div className="bar"><i className={u.cost_usd / run.budget.max_cost_usd > 0.8 ? "hot" : ""} style={{ width: `${Math.min(100, (u.cost_usd / run.budget.max_cost_usd) * 100)}%` }} /></div></div>
              <div className="meter"><span>工具 {u.tool_calls}/{run.budget.max_tool_calls}</span><div className="bar"><i style={{ width: `${Math.min(100, (u.tool_calls / run.budget.max_tool_calls) * 100)}%` }} /></div></div>
              <div className="meter"><span>模型 {u.llm_calls}/{run.budget.max_llm_steps}</span><div className="bar"><i style={{ width: `${Math.min(100, (u.llm_calls / run.budget.max_llm_steps) * 100)}%` }} /></div></div>
              <div className="meter"><span>耗时 {fmtDur(u.elapsed_ms)}</span></div>
            </div>
          )}
          {run && ACTIVE.has(run.status) && <button className="danger sm" onClick={() => api.stop(run.id).then(setRun)}>■ 停止</button>}
          {canResume && <button className="primary sm" onClick={() => api.resume(run!.id).then(setRun).catch((e) => setError(e.message))}>▶ 从检查点恢复</button>}
          {config?.fault_injection && <FaultControls />}
        </div>
        <div className="activity">{run && ACTIVE.has(run.status) && <span className="pulse" />}<span>{activity}</span></div>

        <div className="messages" ref={listRef}>
          {!threadId && (
            <div className="welcome">
              <h2>给出一个研究目标，把研究过程做扎实</h2>
              <p>系统会规划任务、调用扶摇 / iFinD 金融数据工具、记录每条证据的来源 / 时点 / 单位，在需要时请你确认，最后产出每条结论都能回到原始字段的研究报告。</p>
              <div className="steps">
                <div><b>01 规划</b>拆解任务与工具</div>
                <div><b>02 取数</b>行情 / 估值 / 财务 / 板块</div>
                <div><b>03 核验</b>证据 E# 与数值核验</div>
                <div><b>04 确认</b>计划 / 写操作 / 预算</div>
                <div><b>05 报告</b>事实 · 推断 · 不确定</div>
              </div>
              <p className="hint">不输出涨跌预测、目标价与买卖建议。可以从下方预置目标开始。</p>
            </div>
          )}
          {messages.map((m) => (
            <div className={`msg ${m.role}`} key={m.id}>
              <div className="bubble">
                {m.blocks.map((b, i) => (
                  <BlockView key={i} b={b} tasks={run?.tasks} approvals={run?.approvals} onEvidence={openEvidenceByLabel} onOpenArtifact={setArtOpen} onDecided={() => run && refreshRun(run.id)} />
                ))}
              </div>
              <div className="meta">{m.role === "user" ? "你" : "X Buddy"} · {new Date(m.created_at).toLocaleTimeString()}</div>
            </div>
          ))}
        </div>

        <div className="composer">
          {error && <div className="callout error row-between">{error}<button className="sm ghost" onClick={() => setError(null)}>×</button></div>}
          <div className="presets">
            {PRESETS.map((p) => <button key={p} onClick={() => setGoal(p)}>{p.slice(0, 28)}…</button>)}
            <button onClick={() => setShowSettings((s) => !s)}>⚙ 预算与审批</button>
          </div>
          {showSettings && (
            <div className="settings">
              <label>最大模型步数<input type="number" value={budget.max_llm_steps ?? ""} onChange={(e) => setBudget({ ...budget, max_llm_steps: Number(e.target.value) })} /></label>
              <label>最大工具调用<input type="number" value={budget.max_tool_calls ?? ""} onChange={(e) => setBudget({ ...budget, max_tool_calls: Number(e.target.value) })} /></label>
              <label>成本上限 USD<input type="number" step="0.01" value={budget.max_cost_usd ?? ""} onChange={(e) => setBudget({ ...budget, max_cost_usd: Number(e.target.value) })} /></label>
              <label>时长上限 s<input type="number" value={budget.max_duration_s ?? ""} onChange={(e) => setBudget({ ...budget, max_duration_s: Number(e.target.value) })} /></label>
              <label>上下文预算 tokens<input type="number" value={budget.context_budget_tokens ?? ""} onChange={(e) => setBudget({ ...budget, context_budget_tokens: Number(e.target.value) })} /></label>
              <label>计划确认<select value={budget.plan_approval ?? "auto"} onChange={(e) => setBudget({ ...budget, plan_approval: e.target.value })}><option value="auto">auto（任务多/含写操作时）</option><option value="always">always</option><option value="never">never</option></select></label>
              <label>写记忆需审批<select value={String(budget.memory_write_approval ?? true)} onChange={(e) => setBudget({ ...budget, memory_write_approval: e.target.value === "true" })}><option value="true">是</option><option value="false">否</option></select></label>
            </div>
          )}
          <div className="row">
            <textarea rows={2} placeholder="例如：研究贵州茅台近3个月行情、估值与财务，并与白酒板块对比（Ctrl/⌘+Enter 发送）" value={goal} onChange={(e) => setGoal(e.target.value)} onKeyDown={(e) => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter") submit(); }} />
            <button className="primary" onClick={submit} disabled={!goal.trim() || (run != null && ["planning", "running", "waiting_approval"].includes(run.status))}>开始研究</button>
          </div>
        </div>
      </main>

      <aside className="right">
        <div className="tabs">
          <button className={tab === "tasks" ? "active" : ""} onClick={() => setTab("tasks")}>任务{run ? <span className="n">{run.tasks.filter((t) => t.status === "done").length}/{run.tasks.length}</span> : null}</button>
          <button className={tab === "evidence" ? "active" : ""} onClick={() => setTab("evidence")}>证据<span className="n">{evidence.length}</span></button>
          <button className={tab === "artifacts" ? "active" : ""} onClick={() => setTab("artifacts")}>产物{run?.artifacts.length ? <span className="n">{run.artifacts.length}</span> : null}</button>
          <button className={tab === "trace" ? "active" : ""} onClick={() => setTab("trace")}>观测</button>
          <button className={tab === "memory" ? "active" : ""} onClick={() => setTab("memory")}>记忆</button>
        </div>
        {tab === "tasks" && <TasksPanel run={run} />}
        {tab === "evidence" && <EvidencePanel evidence={evidence} onOpen={setEvOpen} />}
        {tab === "artifacts" && <ArtifactsPanel run={run} onOpen={setArtOpen} />}
        {tab === "trace" && <TracePanel run={run} events={events} />}
        {tab === "memory" && <MemoryPanel />}
      </aside>
      </div>

      <EvidenceModal ev={evOpen} onClose={() => setEvOpen(null)} />
      <ArtifactModal id={artOpen} onClose={() => setArtOpen(null)} onEvidence={(l) => { setArtOpen(null); openEvidenceByLabel(l); }} />
    </div>
  );
}

function FaultControls() {
  const [faults, setFaults] = useState<Record<string, any>>({});
  const load = () => api.faults().then((r) => setFaults(r.faults)).catch(() => {});
  useEffect(() => { load(); }, []);
  const set = async (target: string, mode: string) => { await api.setFault(target, mode); load(); };
  return (
    <div className="faults" title="演示用：注入数据源 / 模型故障，观察降级与恢复">
      <span className="hint">故障注入</span>
      {["fuyao", "llm", "ifind"].map((t) => (
        <select key={t} value={faults[t]?.mode || "off"} title={faults[t]?.expires_in_s != null ? `${faults[t].expires_in_s}s 后自动恢复` : ""} onChange={(e) => set(t, e.target.value)}>
          <option value="off">{t}: 正常</option>
          <option value="error">{t}: 报错</option>
          <option value="timeout">{t}: 超时</option>
          <option value="ratelimit">{t}: 限流</option>
        </select>
      ))}
      <button className="sm ghost" onClick={() => set("all", "off")}>重置</button>
      {Object.values(faults).some((f: any) => f?.expires_in_s != null) && <span className="hint">（{Math.max(...Object.values(faults).map((f: any) => f?.expires_in_s ?? 0))}s 后自动恢复）</span>}
    </div>
  );
}
