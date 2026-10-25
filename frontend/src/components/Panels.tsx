import { useEffect, useState } from "react";
import { api, fmtDur, fmtTime } from "../api";
import type { Evidence, Run, XEvent } from "../types";
import Markdown from "./Markdown";
import { StatusDot } from "./Blocks";

export function TasksPanel({ run }: { run: Run | null }) {
  if (!run) return <div className="empty">选择或发起一次研究后，这里显示任务拆解与执行状态。</div>;
  return (
    <div className="panel">
      <div className="card">
        <div className="hd"><span>运行 {run.id}</span><span className={`chip ${run.status}`}>{run.status}</span></div>
        <div className="kv" style={{ marginTop: 6 }}>
          <b>目标</b><span>{run.goal}</span>
          <b>阶段</b><span>{run.phase} · 步骤 {run.step}</span>
          <b>模式</b><span>{run.mode === "llm" ? "模型驱动" : "模板模式（降级）"}</span>
          {run.stop_reason && <><b>停止原因</b><span>{run.stop_reason}</span></>}
          {run.error && <><b>错误</b><span>{run.error}</span></>}
          {run.degrade_reasons?.length ? <><b>降级</b><span>{run.degrade_reasons.join("；")}</span></> : null}
        </div>
      </div>
      {run.tasks.map((t) => (
        <div className="card" key={t.id}>
          <div className="hd"><span><StatusDot status={t.status} /> {t.title}</span><span className="badge">{t.id.split(":")[1]}</span></div>
          <div className="sub">{t.objective}</div>
          {t.acceptance && <div className="sub">验收：{t.acceptance}</div>}
          {t.depends_on?.length ? <div className="sub">依赖：{t.depends_on.join(", ")}</div> : null}
          <div style={{ display: "flex", gap: 4, flexWrap: "wrap", marginTop: 4 }}>{t.tools.map((n) => <span className="tool-chip" key={n}>{n}</span>)}</div>
          {t.result?.evidence_labels?.length ? <div className="sub">证据：{t.result.evidence_labels.join(", ")}</div> : null}
          {t.error && <div className="callout error" style={{ marginTop: 6 }}>{t.error}</div>}
          {t.ended_at && t.started_at && <div className="sub">耗时 {fmtDur(t.ended_at - t.started_at)}</div>}
        </div>
      ))}
      {Object.keys(run.working_memory || {}).length > 0 && (
        <div className="card">
          <div className="hd">工作记忆</div>
          <pre style={{ maxHeight: 200 }}>{JSON.stringify(run.working_memory, null, 2)}</pre>
        </div>
      )}
    </div>
  );
}

export function EvidencePanel({ evidence, onOpen }: { evidence: Evidence[]; onOpen: (e: Evidence) => void }) {
  if (!evidence.length) return <div className="empty">每一次工具调用（成功或失败）都会生成证据记录：来源、时点、单位/口径、原始字段。</div>;
  return (
    <div className="panel">
      {evidence.map((e) => (
        <div className="card ev-item" key={e.id} onClick={() => onOpen(e)}>
          <div className="hd"><span><span className="evref">{e.label}</span> {e.tool}</span><span className={`badge ${e.status}`}>{e.status}</span></div>
          <div className="sub">{e.summary || e.error}</div>
          <div className="kv" style={{ marginTop: 4 }}>
            <b>来源</b><span>{e.source}</span>
            <b>数据时点</b><span>{e.data_timestamp ? fmtTime(e.data_timestamp) : "接口未提供"}</span>
            <b>抓取</b><span>{fmtTime(e.fetched_at)} · {e.latency_ms}ms</span>
            {e.unit_notes && <><b>单位/口径</b><span>{e.unit_notes}</span></>}
          </div>
        </div>
      ))}
    </div>
  );
}

export function EvidenceModal({ ev, onClose }: { ev: Evidence | null; onClose: () => void }) {
  const [full, setFull] = useState<Evidence | null>(null);
  useEffect(() => {
    setFull(null);
    if (ev) api.evidenceOne(ev.id).then(setFull).catch(() => setFull(ev));
  }, [ev?.id]);
  if (!ev) return null;
  const e = full || ev;
  return (
    <div className="modal-bg" onClick={onClose}>
      <div className="modal" onClick={(s) => s.stopPropagation()}>
        <div className="hd"><b><span className="evref">{e.label}</span> {e.tool} <span className={`badge ${e.status}`}>{e.status}</span></b><button className="sm" onClick={onClose}>关闭</button></div>
        <div className="kv">
          <b>来源</b><span>{e.source}</span>
          <b>数据时点</b><span>{e.data_timestamp ? fmtTime(e.data_timestamp) : "接口未提供（以抓取时间为准）"}</span>
          <b>抓取时间</b><span>{fmtTime(e.fetched_at)}（{e.latency_ms}ms）</span>
          <b>单位/口径</b><span>{e.unit_notes || "—"}</span>
          <b>request_id</b><span>{e.request_id || "—"}</span>
          <b>原始哈希</b><span>{e.raw_hash || "—"}</span>
          <b>摘要</b><span>{e.summary}</span>
          {e.error && <><b>错误</b><span style={{ color: "var(--err)" }}>{e.error}</span></>}
        </div>
        <div className="hint">调用参数</div>
        <pre>{JSON.stringify(e.args, null, 2)}</pre>
        <div className="hint">原始数据（结论可回溯到以下字段）</div>
        <pre>{full ? JSON.stringify(full.raw, null, 2) : "加载中…"}</pre>
      </div>
    </div>
  );
}

export function ArtifactsPanel({ run, onOpen }: { run: Run | null; onOpen: (id: string) => void }) {
  if (!run?.artifacts?.length) return <div className="empty">研究完成后生成报告产物（Markdown），每条结论带 [E#] 引用，可下载。</div>;
  return (
    <div className="panel">
      {run.artifacts.map((a) => (
        <div className="card" key={a.id}>
          <div className="hd"><span>📄 {a.title}</span><span className="badge">{a.type}</span></div>
          <div className="sub">{fmtTime(a.created_at)} · 合规 {a.lint?.ok ? "通过" : "已修正"}{a.lint?.stats ? ` · 移除违规 ${a.lint.stats.forbidden_removed} · 无引用 ${a.lint.stats.uncited}` : ""}</div>
          <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
            <button className="primary sm" onClick={() => onOpen(a.id)}>查看</button>
            <a href={`/api/artifacts/${a.id}/download.md`}><button className="sm">下载 .md</button></a>
            <a href={`/api/artifacts/${a.id}/bundle.json`} target="_blank" rel="noreferrer"><button className="sm">证据包 .json</button></a>
          </div>
        </div>
      ))}
    </div>
  );
}

export function ArtifactModal({ id, onClose, onEvidence }: { id: string | null; onClose: () => void; onEvidence: (l: string) => void }) {
  const [a, setA] = useState<any>(null);
  useEffect(() => {
    setA(null);
    if (id) api.artifact(id).then(setA);
  }, [id]);
  if (!id) return null;
  return (
    <div className="modal-bg" onClick={onClose}>
      <div className="modal" onClick={(s) => s.stopPropagation()}>
        <div className="hd"><b>{a?.title || "报告"}</b><div style={{ display: "flex", gap: 6 }}><a href={`/api/artifacts/${id}/download.md`}><button className="sm">下载 .md</button></a><button className="sm" onClick={onClose}>关闭</button></div></div>
        {a?.lint && !a.lint.ok && <div className="callout warning">合规 Linter 已自动修正：{a.lint.violations.filter((v: any) => v.type === "forbidden").map((v: any) => `「${v.match}」`).join(" ")}</div>}
        {a ? <Markdown text={a.content_md} onEvidence={onEvidence} /> : <div className="empty">加载中…</div>}
      </div>
    </div>
  );
}

export function TracePanel({ run, events }: { run: Run | null; events: XEvent[] }) {
  const [trace, setTrace] = useState<any>(null);
  useEffect(() => {
    if (!run) return;
    api.trace(run.id).then(setTrace).catch(() => {});
  }, [run?.id, run?.status, run?.step]);
  if (!run) return <div className="empty">观测：每次模型 / 工具调用的时延、token、成本、状态；检查点、压缩、降级与熔断事件。</div>;
  const u = run.usage;
  const spans = trace?.spans || [];
  return (
    <div className="panel">
      <div className="card">
        <div className="hd">用量与成本</div>
        <div className="kv" style={{ marginTop: 6 }}>
          <b>模型调用</b><span>{u.llm_calls}（失败 {u.llm_errors}）· {u.input_tokens} in / {u.output_tokens} out tokens</span>
          <b>估算成本</b><span>${u.cost_usd.toFixed(4)} / 上限 ${run.budget.max_cost_usd}</span>
          <b>工具调用</b><span>{u.tool_calls}（失败 {u.tool_errors}，缓存命中 {u.tool_cache_hits}）</span>
          <b>时延</b><span>模型 {fmtDur(u.llm_latency_ms)} · 工具 {fmtDur(u.tool_latency_ms)} · 总 {fmtDur(u.elapsed_ms)}</span>
          <b>上下文压缩</b><span>{u.compressions} 次，节省约 {u.tokens_saved} tokens</span>
          <b>检查点</b><span>{trace?.checkpoints?.length ?? "—"} 个</span>
        </div>
      </div>
      <div className="card">
        <div className="hd">停止规则余量</div>
        <div className="meters" style={{ marginTop: 6 }}>
          {Object.entries(run.remaining).map(([k, v]) => (
            <div className="meter" key={k}><span>{k}: {v.used}/{v.max}</span><div className="bar"><i className={v.used / v.max > 0.8 ? "hot" : ""} style={{ width: `${Math.min(100, (v.used / (v.max || 1)) * 100)}%` }} /></div></div>
          ))}
        </div>
      </div>
      {(Object.keys(trace?.breaker || {}).length > 0 || Object.keys(trace?.faults || {}).length > 0) && (
        <div className="card">
          <div className="hd">熔断 / 故障注入</div>
          <pre style={{ maxHeight: 120 }}>{JSON.stringify({ breaker: trace?.breaker, faults: trace?.faults }, null, 2)}</pre>
        </div>
      )}
      <div className="card">
        <div className="hd">时间线（{spans.length} spans）</div>
        <div className="timeline" style={{ marginTop: 6 }}>
          {spans.slice(-150).map((s: any, i: number) => (
            <div className={`span ${s.kind} ${s.status === "error" ? "error" : ""}`} key={i}>
              <span className="k">{s.kind}</span>
              <span>{s.name}{s.evidence ? ` → ${s.evidence}` : ""}{s.provider ? ` · ${s.provider}${s.fallback ? "(备用)" : ""}` : ""}{s.tokens_in != null ? ` · ${s.tokens_in}/${s.tokens_out} tok · $${(s.cost_usd || 0).toFixed(5)}` : ""}{s.detail ? ` · ${JSON.stringify(s.detail).slice(0, 120)}` : ""}{s.error ? ` · ${s.error}` : ""}</span>
              <span className="k">{s.duration_ms != null ? fmtDur(s.duration_ms) : s.status || ""}</span>
            </div>
          ))}
        </div>
      </div>
      <div className="card">
        <div className="hd">实时事件（最近 {Math.min(events.length, 60)} 条）</div>
        <div className="timeline" style={{ marginTop: 6 }}>
          {events.slice(-60).reverse().map((e) => (
            <div className="span system" key={e.seq}><span className="k">#{e.seq}</span><span>{e.type} {summarize(e)}</span><span className="k">{fmtTime(e.created_at).slice(11)}</span></div>
          ))}
        </div>
      </div>
    </div>
  );
}

function summarize(e: XEvent) {
  const p = e.payload || {};
  switch (e.type) {
    case "tool.end": return `${p.tool} ${p.status} ${p.latency_ms}ms ${p.evidence_label || ""}`;
    case "tool.start": return `${p.tool}(${JSON.stringify(p.args).slice(0, 60)})`;
    case "llm.end": return `${p.purpose} ${p.input_tokens}/${p.output_tokens} tok ${p.latency_ms}ms`;
    case "llm.error": return `${p.provider}: ${p.error}`;
    case "task.start": case "task.end": return `${p.title} ${p.status || ""}`;
    case "run.checkpoint": return `step ${p.step} ${p.phase} ${p.note || ""}`;
    case "context.compressed": return `${p.before}→${p.after} tokens`;
    case "run.degraded": return p.reason;
    case "approval.requested": return `${p.kind}: ${p.title}`;
    default: return "";
  }
}

export function MemoryPanel() {
  const [mems, setMems] = useState<any[]>([]);
  const [content, setContent] = useState("");
  const [kind, setKind] = useState("note");
  const [q, setQ] = useState("");
  const load = () => api.memories(q || undefined).then((r) => setMems(r.memories));
  useEffect(() => { load(); }, [q]);
  return (
    <div className="panel">
      <div className="card">
        <div className="hd">长期记忆（匿名构造数据）</div>
        <div className="sub">规划时按关键词检索注入；研究结束后的写入需你审批。</div>
        <input placeholder="检索记忆…" value={q} onChange={(e) => setQ(e.target.value)} style={{ width: "100%", marginTop: 8 }} />
        <div style={{ display: "flex", gap: 6, marginTop: 8 }}>
          <select value={kind} onChange={(e) => setKind(e.target.value)}><option value="note">note</option><option value="preference">preference</option><option value="watchlist">watchlist</option><option value="conclusion">conclusion</option></select>
          <input placeholder="新增一条记忆…" value={content} onChange={(e) => setContent(e.target.value)} style={{ flex: 1 }} />
          <button className="primary sm" onClick={async () => { if (!content.trim()) return; await api.addMemory(kind, content); setContent(""); load(); }}>添加</button>
        </div>
      </div>
      {mems.map((m) => (
        <div className="card" key={m.id}>
          <div className="hd"><span><span className="badge">{m.kind}</span> {m.content}</span><button className="sm ghost" onClick={async () => { await api.deleteMemory(m.id); load(); }}>删除</button></div>
          <div className="sub">{m.tags} · {fmtTime(m.created_at)}{m.source_run_id ? ` · 来自 ${m.source_run_id}` : ""}</div>
        </div>
      ))}
    </div>
  );
}
