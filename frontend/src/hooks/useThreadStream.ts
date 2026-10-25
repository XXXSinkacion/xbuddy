import { useEffect, useRef } from "react";
import type { XEvent } from "../types";

/** 订阅线程 SSE；断线自动重连并从最后 seq 续传。 */
export function useThreadStream(threadId: string | null, sinceRef: React.MutableRefObject<number>, onEvent: (e: XEvent) => void, onStatus?: (s: "open" | "closed" | "error") => void) {
  const cbRef = useRef(onEvent);
  cbRef.current = onEvent;
  useEffect(() => {
    if (!threadId) return;
    let es: EventSource | null = null;
    let closed = false;
    let retry = 1000;
    const connect = () => {
      if (closed) return;
      es = new EventSource(`/api/threads/${threadId}/stream?since=${sinceRef.current}`);
      es.onopen = () => {
        retry = 1000;
        onStatus?.("open");
      };
      es.onmessage = (m) => handle(m);
      // 服务端使用命名事件（event: tool.end）；EventSource 不支持通配，逐个注册已知事件名
      for (const n of KNOWN_EVENTS) es.addEventListener(n, (m: any) => handle(m));
      es.onerror = () => {
        onStatus?.("error");
        es?.close();
        if (!closed) {
          setTimeout(connect, retry);
          retry = Math.min(retry * 2, 15000);
        }
      };
    };
    const handle = (m: MessageEvent) => {
      try {
        const evt = JSON.parse(m.data) as XEvent;
        if (evt.seq > sinceRef.current) sinceRef.current = evt.seq;
        cbRef.current(evt);
      } catch {}
    };
    connect();
    return () => {
      closed = true;
      es?.close();
      onStatus?.("closed");
    };
  }, [threadId]);
}

export const KNOWN_EVENTS = [
  "run.created", "run.started", "run.resumed", "run.degraded", "run.checkpoint", "run.waiting_approval", "run.interrupted", "run.stopping", "run.stopped", "run.completed", "run.failed",
  "plan.created", "plan.edited",
  "task.start", "task.end", "task.note",
  "tool.start", "tool.end", "tool.retry", "tool.fallback", "tool.fallback_args", "tool.stale_cache", "tool.rejected", "tool.loop_blocked",
  "llm.start", "llm.end", "llm.error", "llm.fallback",
  "evidence.recorded", "artifact.created", "approval.requested", "approval.decided", "context.compressed", "memory.recalled", "memory.written", "message.created", "compliance.lint", "compliance.rewrite", "budget.extended",
];
