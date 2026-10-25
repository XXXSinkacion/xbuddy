import type { Approval, Config, Evidence, Run, ThreadRow } from "./types";

async function req<T>(url: string, init?: RequestInit): Promise<T> {
  const r = await fetch(url, { headers: { "Content-Type": "application/json" }, ...init });
  if (!r.ok) {
    let msg = r.statusText;
    try {
      const j = await r.json();
      msg = j.detail || JSON.stringify(j);
    } catch {}
    throw new Error(msg);
  }
  return r.json();
}

export const api = {
  config: () => req<Config>("/api/config"),
  health: () => req<any>("/api/health"),
  threads: () => req<{ threads: ThreadRow[] }>("/api/threads"),
  createThread: (title = "新研究线程") => req<ThreadRow>("/api/threads", { method: "POST", body: JSON.stringify({ title }) }),
  thread: (id: string) => req<any>(`/api/threads/${id}`),
  deleteThread: (id: string) => req<any>(`/api/threads/${id}`, { method: "DELETE" }),
  startRun: (threadId: string, goal: string, budget?: Record<string, any>) => req<Run>(`/api/threads/${threadId}/runs`, { method: "POST", body: JSON.stringify({ goal, budget }) }),
  run: (id: string) => req<Run>(`/api/runs/${id}`),
  stop: (id: string) => req<Run>(`/api/runs/${id}/stop`, { method: "POST" }),
  resume: (id: string) => req<Run>(`/api/runs/${id}/resume`, { method: "POST" }),
  evidence: (runId: string) => req<{ evidence: Evidence[] }>(`/api/runs/${runId}/evidence`),
  evidenceOne: (id: string, runId?: string) => req<Evidence>(`/api/evidence/${id}${runId ? `?run_id=${runId}` : ""}`),
  trace: (runId: string) => req<any>(`/api/runs/${runId}/trace`),
  artifact: (id: string) => req<any>(`/api/artifacts/${id}`),
  decide: (id: string, approved: boolean, reason = "") => req<Run>(`/api/approvals/${id}/decide`, { method: "POST", body: JSON.stringify({ approved, reason }) }),
  approvals: (runId?: string) => req<{ approvals: Approval[] }>(`/api/approvals${runId ? `?run_id=${runId}` : ""}`),
  memories: (q?: string) => req<{ memories: any[] }>(`/api/memories${q ? `?q=${encodeURIComponent(q)}` : ""}`),
  addMemory: (kind: string, content: string, tags = "") => req<any>("/api/memories", { method: "POST", body: JSON.stringify({ kind, content, tags }) }),
  deleteMemory: (id: string) => req<any>(`/api/memories/${id}`, { method: "DELETE" }),
  faults: () => req<any>("/api/dev/faults"),
  setFault: (target: string, mode: string, count?: number) => req<any>("/api/dev/faults", { method: "POST", body: JSON.stringify({ target, mode, count }) }),
  metrics: () => req<any>("/api/metrics"),
};

export function fmtTime(ms?: number | null) {
  if (!ms) return "—";
  const d = new Date(ms);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

export function fmtDur(ms?: number | null) {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.floor(ms / 60000)}m${Math.round((ms % 60000) / 1000)}s`;
}
