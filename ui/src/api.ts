import type { ClusterInfo, EndEvent, LogEvent, PlanResponse, Run, RunRequest } from "./types";

const BASE = "/api";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(BASE + path, {
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    ...init,
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body?.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

export const api = {
  clusterInfo: (url: string, auth: string) =>
    request<ClusterInfo>("/clusters/info", { method: "POST", body: JSON.stringify({ url, auth }) }),

  plan: (body: { source_url: string; source_auth: string; target_url: string; target_auth: string }) =>
    request<PlanResponse>("/plan", { method: "POST", body: JSON.stringify(body) }),

  startRun: (body: RunRequest) => request<Run>("/runs", { method: "POST", body: JSON.stringify(body) }),
  listRuns: () => request<Run[]>("/runs"),
  getRun: (id: string) => request<Run>(`/runs/${id}`),
  cancelRun: (id: string) =>
    request<{ cancelled: boolean; reason?: string }>(`/runs/${id}/cancel`, { method: "POST" }),

  /** Tail a run's log via SSE. Yields log lines, then a single {type:"end"} event. */
  stream: async function* (id: string, signal: AbortSignal): AsyncGenerator<LogEvent | EndEvent> {
    const res = await fetch(`${BASE}/runs/${id}/stream`, { headers: { Accept: "text/event-stream" }, signal });
    if (!res.ok || !res.body) throw new Error(`stream failed: ${res.status}`);
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) return;
      buf += decoder.decode(value, { stream: true });
      let idx: number;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const frame = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        for (const line of frame.split("\n")) {
          if (line.startsWith("data: ")) yield JSON.parse(line.slice(6));
        }
      }
    }
  },
};
