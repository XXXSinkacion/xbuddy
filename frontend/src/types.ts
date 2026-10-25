export type Block = { type: string; [k: string]: any };

export interface Message {
  id: string;
  thread_id: string;
  run_id?: string | null;
  role: "user" | "assistant";
  blocks: Block[];
  created_at: number;
}

export interface Task {
  id: string;
  idx: number;
  title: string;
  objective: string;
  depends_on: string[];
  tools: string[];
  acceptance?: string;
  status: string;
  result?: any;
  error?: string;
  started_at?: number;
  ended_at?: number;
}

export interface Approval {
  id: string;
  run_id: string;
  thread_id: string;
  task_id?: string | null;
  kind: string;
  title: string;
  payload: any;
  status: string;
  reason?: string;
  created_at: number;
  decided_at?: number;
}

export interface Usage {
  llm_calls: number;
  llm_errors: number;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  tool_calls: number;
  tool_errors: number;
  tool_cache_hits: number;
  elapsed_ms: number;
  compressions: number;
  tokens_saved: number;
  llm_latency_ms: number;
  tool_latency_ms: number;
}

export interface Run {
  id: string;
  thread_id: string;
  goal: string;
  status: string;
  phase: string;
  mode: string;
  stop_reason?: string | null;
  error?: string | null;
  created_at: number;
  finished_at?: number | null;
  budget: Record<string, any>;
  usage: Usage;
  remaining: Record<string, { used: number; max: number }>;
  degrade_reasons: string[];
  working_memory: Record<string, any>;
  step: number;
  tasks: Task[];
  approvals: Approval[];
  artifacts: { id: string; title: string; type: string; created_at: number; lint?: any }[];
}

export interface Evidence {
  id: string;
  run_id: string;
  task_id?: string | null;
  label: string;
  tool: string;
  args: Record<string, any>;
  source: string;
  status: string;
  fetched_at: number;
  data_timestamp?: number | null;
  unit_notes?: string;
  summary?: string;
  raw_hash?: string;
  request_id?: string;
  latency_ms?: number;
  error?: string;
  raw?: any;
}

export interface XEvent {
  seq: number;
  type: string;
  run_id: string | null;
  thread_id: string;
  payload: any;
  created_at: number;
}

export interface ThreadRow {
  id: string;
  title: string;
  created_at: number;
  updated_at: number;
  last_status?: string;
  run_count?: number;
}

export interface Config {
  llm: { available: boolean; mode: string; primary?: any; fallback?: any };
  fuyao: { mode: string; configured: boolean };
  ifind: { ok: boolean; count: number; error?: string | null; configured: boolean };
  tools: any[];
  budget_defaults: Record<string, any>;
  fault_injection: boolean;
  faults: Record<string, any>;
  breaker: Record<string, any>;
}
