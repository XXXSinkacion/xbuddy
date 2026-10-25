import { useState } from "react";
import { api, fmtTime } from "../api";
import type { Approval, Block, Task } from "../types";
import KlineChart from "./KlineChart";
import Markdown from "./Markdown";

export function StatusDot({ status }: { status: string }) {
  const icon: Record<string, string> = { done: "✓", running: "…", failed: "✕", partial: "!", skipped: "–", pending: "" };
  return <span className={`st ${status}`}>{icon[status] ?? ""}</span>;
}

export function PlanBlock({ b, tasks }: { b: Block; tasks?: Task[] }) {
  const live = new Map((tasks || []).map((t) => [t.id, t]));
  return (
    <div className="plan">
      <div className="hd">
        <span>📋 研究计划：{b.title}</span>
        <span className={`chip ${b.mode}`}>{b.mode === "llm" ? "模型规划" : "模板规划（降级）"}</span>
      </div>
      {(b.tasks || []).map((t: Task) => {
        const cur = live.get(t.id) || t;
        return (
          <div className="task" key={t.id}>
            <StatusDot status={cur.status} />
            <div>
              <div><b>{t.title}</b> <span className="hint">{t.depends_on?.length ? `依赖 ${t.depends_on.join(",")}` : ""}</span></div>
              <div className="obj">{t.objective}</div>
              {t.acceptance && <div className="obj">验收：{t.acceptance}</div>}
              <div className="tools">{(t.tools || []).map((n: string) => <span className="tool-chip" key={n}>{n}</span>)}</div>
            </div>
          </div>
        );
      })}
      {b.assumptions?.length ? <div className="task"><span /><div className="obj">假设：{b.assumptions.join("；")}</div></div> : null}
    </div>
  );
}

export function MetricsBlock({ b, onEvidence }: { b: Block; onEvidence: (l: string) => void }) {
  return (
    <div>
      <div className="block-title"><span>{b.title}{b.timestamp ? ` · 时点 ${b.timestamp}` : ""}</span>{b.evidence && <a className="evref" onClick={() => onEvidence(b.evidence)}>{b.evidence}</a>}</div>
      <div className="metrics">
        {(b.items || []).map((it: any, i: number) => (
          <div className={`metric ${it.trend || ""}`} key={i}>
            <div className="l">{it.label}</div>
            <div className="v">{it.value ?? "--"}</div>
            {(it.sub || it.note) && <div className="s">{[it.sub, it.note].filter(Boolean).join(" · ")}</div>}
          </div>
        ))}
      </div>
    </div>
  );
}

export function TableBlock({ b, onEvidence }: { b: Block; onEvidence: (l: string) => void }) {
  return (
    <div>
      <div className="block-title"><span>{b.title}{b.timestamp ? ` · 时点 ${b.timestamp}` : ""}</span>{b.evidence && <a className="evref" onClick={() => onEvidence(b.evidence)}>{b.evidence}</a>}</div>
      <div className="table-wrap">
        <table>
          <thead><tr>{(b.columns || []).map((c: string, i: number) => <th key={i}>{c}</th>)}</tr></thead>
          <tbody>{(b.rows || []).map((r: any[], i: number) => <tr key={i}>{r.map((c, j) => <td key={j}>{c === null || c === undefined ? "--" : String(c)}</td>)}</tr>)}</tbody>
        </table>
      </div>
    </div>
  );
}

export function CalloutBlock({ b, onEvidence }: { b: Block; onEvidence: (l: string) => void }) {
  const icon: Record<string, string> = { warning: "⚠️", error: "⛔", missing: "∅", info: "ℹ️", fact: "✓", uncertain: "?" };
  return (
    <div className={`callout ${b.level || "info"}`}>
      {icon[b.level] || "ℹ️"} {b.text} {b.evidence && <a className="evref" onClick={() => onEvidence(b.evidence)}>{b.evidence}</a>}
    </div>
  );
}

export function ApprovalBlock({ b, live, onDecided }: { b: Block; live?: Approval; onDecided: () => void }) {
  const a: Approval = live || b.approval;
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const decide = async (ok: boolean) => {
    setBusy(true);
    setErr(null);
    try {
      await api.decide(a.id, ok, reason);
      onDecided();
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };
  const p = a.payload || {};
  return (
    <div className={`approval ${a.status !== "pending" ? "decided" : ""}`}>
      <div className="hd">🛡️ 需要你的确认：{a.title} <span className="badge">{a.kind}</span></div>
      {a.kind === "plan" && (
        <div className="hint">原因：{p.reason}。任务：{(p.tasks || []).map((t: any) => `${t.id} ${t.title}`).join("；")}<br />{p.hint}</div>
      )}
      {a.kind === "tool" && (
        <div>
          <div className="hint">风险级别 {p.risk}（{p.risk_label}）：{p.description}</div>
          <pre>{JSON.stringify(p.arguments, null, 2)}</pre>
        </div>
      )}
      {a.kind === "budget" && <div className="hint">{p.warning}。{p.proposal}</div>}
      {a.kind === "memory_write" && (
        <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>{(p.proposals || []).map((m: any, i: number) => <li key={i}><span className="badge">{m.kind}</span> {m.content}</li>)}</ul>
      )}
      {a.status === "pending" ? (
        <div className="actions">
          <input placeholder="备注 / 理由（可选，如：去掉 t5）" value={reason} onChange={(e) => setReason(e.target.value)} style={{ flex: 1 }} />
          <button className="primary" disabled={busy} onClick={() => decide(true)}>批准</button>
          <button className="danger" disabled={busy} onClick={() => decide(false)}>拒绝</button>
        </div>
      ) : (
        <div className="hint">已{a.status === "approved" ? "批准" : "拒绝"}{a.reason ? `：${a.reason}` : ""} · {fmtTime(a.decided_at)}</div>
      )}
      {err && <div className="callout error">{err}</div>}
    </div>
  );
}

export function ArtifactBlock({ b, onOpen }: { b: Block; onOpen: (id: string) => void }) {
  const lint = b.lint || {};
  return (
    <div className="artifact">
      <div>
        <div><b>📄 {b.title}</b> <span className={`chip ${b.gen_mode}`}>{b.gen_mode === "llm" ? "模型撰写" : "模板生成"}</span></div>
        <div className="hint">合规检查 {lint.ok ? "通过" : "已修正"} · 移除违规 {lint.stats?.forbidden_removed ?? 0} · 无引用结论 {lint.stats?.uncited ?? 0}</div>
      </div>
      <div style={{ display: "flex", gap: 6 }}>
        <button className="primary sm" onClick={() => onOpen(b.artifact_id)}>查看报告</button>
        <a href={`/api/artifacts/${b.artifact_id}/download.md`}><button className="sm">下载 .md</button></a>
      </div>
    </div>
  );
}

export default function BlockView({ b, tasks, approvals, onEvidence, onOpenArtifact, onDecided }: { b: Block; tasks?: Task[]; approvals?: Approval[]; onEvidence: (l: string) => void; onOpenArtifact: (id: string) => void; onDecided: () => void }) {
  switch (b.type) {
    case "text":
      return <Markdown text={b.text} onEvidence={onEvidence} />;
    case "plan":
      return <PlanBlock b={b} tasks={tasks} />;
    case "metrics":
      return <MetricsBlock b={b} onEvidence={onEvidence} />;
    case "table":
      return <TableBlock b={b} onEvidence={onEvidence} />;
    case "kline":
      return (
        <div>
          <div className="block-title"><span>{b.title}{b.timestamp ? ` · 时点 ${b.timestamp}` : ""}</span>{b.evidence && <a className="evref" onClick={() => onEvidence(b.evidence)}>{b.evidence}</a>}</div>
          <KlineChart series={b.series || []} />
          {b.unit && <div className="hint">{b.unit}</div>}
        </div>
      );
    case "callout":
      return <CalloutBlock b={b} onEvidence={onEvidence} />;
    case "approval":
      return <ApprovalBlock b={b} live={approvals?.find((a) => a.id === b.approval?.id)} onDecided={onDecided} />;
    case "artifact_ref":
      return <ArtifactBlock b={b} onOpen={onOpenArtifact} />;
    case "status":
      return <div className="status-line"><StatusDot status={b.status} /> {b.text}</div>;
    default:
      return <pre>{JSON.stringify(b, null, 2)}</pre>;
  }
}
