/**
 * Generation tracker — a module-level singleton that owns the SSE progress
 * streams for in-flight POC generations.
 *
 * Generation runs server-side in a background thread and its progress is a
 * replayable per-project event log (see backend progress.py), so the work
 * never depends on any one browser tab staying open. This store keeps the
 * frontend side of that promise: it lives OUTSIDE the React tree, so opening
 * another project (which unmounts the dashboard) no longer tears down the
 * stream, and it persists the set of running project ids to localStorage so a
 * page refresh can reconnect and replay from cursor 0.
 */
import { useSyncExternalStore } from "react";
import { API_BASE, api } from "./api";

export const GENERATION_STALL_MS = 10 * 60 * 1000;

export type GenStatus = "running" | "done" | "failed" | "cancelled";

export interface GenState {
  projectId: string;
  title: string;
  log: string[];
  phase: string;
  status: GenStatus;
  error?: string;
}

const LS_KEY = "apoc_active_generations";

interface Tracker {
  state: GenState;
  es: EventSource | null;
  stall: ReturnType<typeof setTimeout> | null;
  settled: boolean;
}

const trackers = new Map<string, Tracker>();
const listeners = new Set<() => void>();
let snapshot: GenState[] = [];

// Persist only the still-running generations, with their titles, so a refresh
// knows what to reconnect to (and how to label it before the first event).
function persist() {
  const active: Record<string, string> = {};
  for (const t of trackers.values()) {
    if (t.state.status === "running") active[t.state.projectId] = t.state.title;
  }
  try {
    localStorage.setItem(LS_KEY, JSON.stringify(active));
  } catch {
    /* private mode / quota — degrade gracefully */
  }
}

function emit() {
  snapshot = [...trackers.values()].map((t) => t.state);
  persist();
  for (const l of listeners) l();
}

function update(id: string, patch: Partial<GenState>) {
  const t = trackers.get(id);
  if (!t) return;
  t.state = { ...t.state, ...patch };
  emit();
}

function clearStall(t: Tracker) {
  if (t.stall) {
    clearTimeout(t.stall);
    t.stall = null;
  }
}

function settle(id: string, patch: Partial<GenState>) {
  const t = trackers.get(id);
  if (!t) return;
  t.settled = true;
  clearStall(t);
  t.es?.close();
  t.es = null;
  update(id, patch);
}

function armStall(t: Tracker) {
  clearStall(t);
  t.stall = setTimeout(() => {
    settle(t.state.projectId, {
      status: "failed",
      error: "服务器长时间无进度——生成可能仍在后台继续。可刷新页面重连或稍后重试。",
    });
  }, GENERATION_STALL_MS);
}

function connect(id: string) {
  const t = trackers.get(id);
  if (!t) return;
  const es = new EventSource(`${API_BASE}/api/projects/${id}/stream`);
  t.es = es;
  t.settled = false;
  armStall(t);

  es.onmessage = (e) => {
    const tr = trackers.get(id);
    if (!tr) return;
    armStall(tr); // progress arrived — reset the watchdog
    let ev: any;
    try {
      ev = JSON.parse(e.data);
    } catch {
      return;
    }
    const log = [...tr.state.log, `${ev.phase} — ${ev.message ?? ""}`];
    if (ev.phase === "done") settle(id, { phase: "done", status: "done", log });
    else if (ev.phase === "failed")
      settle(id, { phase: "failed", status: "failed", error: ev.message || "生成失败。", log });
    else if (ev.phase === "cancelled")
      settle(id, { phase: "cancelled", status: "cancelled", log });
    else update(id, { phase: ev.phase, log });
  };

  es.onerror = () => {
    const tr = trackers.get(id);
    if (!tr || tr.settled) return; // normal close after a terminal event
    settle(id, { status: "failed", error: "与生成进度的连接中断，可刷新页面重连。" });
  };
}

export const generations = {
  /** Begin tracking a freshly-started generation (project already created + generate() called). */
  start(projectId: string, title: string) {
    const existing = trackers.get(projectId);
    if (existing && existing.state.status === "running") return;
    trackers.set(projectId, {
      state: { projectId, title, log: ["Creating project…"], phase: "queued", status: "running" },
      es: null,
      stall: null,
      settled: false,
    });
    connect(projectId);
    emit();
  },

  /**
   * Reconnect after a page refresh: for each persisted running id, confirm it's
   * still generating, then re-open its stream (which replays from cursor 0).
   */
  async init() {
    let active: Record<string, string> = {};
    try {
      active = JSON.parse(localStorage.getItem(LS_KEY) || "{}");
    } catch {
      active = {};
    }
    for (const [id, title] of Object.entries(active)) {
      if (trackers.has(id)) continue;
      let status: string;
      try {
        status = (await api.project(id)).status;
      } catch {
        continue; // project gone (deleted) — drop it below via re-persist
      }
      if (status === "generating") {
        trackers.set(id, {
          state: { projectId: id, title, log: ["重新连接生成进度…"], phase: "reconnecting", status: "running" },
          es: null,
          stall: null,
          settled: false,
        });
        connect(id);
        emit();
      }
    }
    // Anything in localStorage we did NOT re-track finished while we were away;
    // re-persisting prunes it so it won't be retried on the next refresh.
    persist();
  },

  /** Best-effort server-side cancel; the SSE will surface the "cancelled" result. */
  cancel(projectId: string) {
    api.cancelGenerate(projectId).catch(() => {});
  },

  /** Drop a card (typically a finished/failed one the user has acknowledged). */
  dismiss(projectId: string) {
    const t = trackers.get(projectId);
    if (!t) return;
    clearStall(t);
    t.es?.close();
    trackers.delete(projectId);
    emit();
  },

  get(projectId: string): GenState | undefined {
    return trackers.get(projectId)?.state;
  },

  subscribe(cb: () => void) {
    listeners.add(cb);
    return () => listeners.delete(cb);
  },

  getSnapshot(): GenState[] {
    return snapshot;
  },
};

/** Subscribe a component to all tracked generations. */
export function useGenerations(): GenState[] {
  return useSyncExternalStore(generations.subscribe, generations.getSnapshot, generations.getSnapshot);
}
