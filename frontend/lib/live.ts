"use client";

// Live execution data: one fetch for the first paint, then a server-sent event stream that pushes
// every change. If the stream cannot be kept open (old proxy, network), the hook falls back to
// polling, so the page keeps updating either way.

import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, currentOrganization } from "./api";

const ACTIVE = ["PENDING", "QUEUED", "RUNNING", "RETRYING", "WAITING"];
export type LiveMode = "connecting" | "live" | "polling" | "done";

/** Read one SSE stream to its end. Resolves with the reason the server gave ("finished" | "recycle"). */
async function readStream(url: string, signal: AbortSignal, onExecution: (data: any) => void, onOpen: () => void): Promise<string> {
  const headers: Record<string, string> = { Accept: "text/event-stream" };
  const org = currentOrganization();
  if (org) headers["X-Organization-Id"] = org;
  const res = await fetch(url, { headers, credentials: "same-origin", signal, cache: "no-store" });
  if (!res.ok || !res.body) throw new Error(`stream ${res.status}`);
  onOpen();
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return "closed";
    buffer += decoder.decode(value, { stream: true });
    let cut: number;
    while ((cut = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      let event = "message", data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event: ")) event = line.slice(7);
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      if (event === "execution" && data) onExecution(JSON.parse(data));
      if (event === "end") return data ? JSON.parse(data).reason : "finished";
    }
  }
}

export function useLiveExecution(queryKey: unknown[], id: string | null | undefined) {
  const qc = useQueryClient();
  const [mode, setMode] = useState<LiveMode>("connecting");
  const query = useQuery({
    queryKey, queryFn: () => api(`/executions/${id}`), enabled: !!id,
    // Only while the stream is not delivering: polling is the fallback, not the norm.
    refetchInterval: (q) => (mode === "polling" && q.state.data && ACTIVE.includes((q.state.data as any).status) ? 2000 : false),
  });
  const status: string | undefined = (query.data as any)?.status;
  const active = !!status && ACTIVE.includes(status);
  const keyText = JSON.stringify(queryKey);

  useEffect(() => {
    if (!id || !status) return;
    if (!active) { setMode("done"); return; }
    const abort = new AbortController();
    let failures = 0;
    let stopped = false;
    (async () => {
      while (!stopped) {
        try {
          const reason = await readStream(`/api/v1/executions/${id}/stream`, abort.signal,
            (data) => qc.setQueryData(queryKey, data), () => { failures = 0; setMode("live"); });
          if (reason === "finished") { setMode("done"); return; }
          // "recycle" or a closed connection: open a new stream straight away.
        } catch {
          if (abort.signal.aborted) return;
          failures += 1;
          if (failures >= 3) { setMode("polling"); return; }
          await new Promise((r) => setTimeout(r, 800 * failures));
        }
      }
    })();
    return () => { stopped = true; abort.abort(); };
  }, [id, active, keyText]); // eslint-disable-line react-hooks/exhaustive-deps

  return { ...query, mode: (active ? mode : "done") as LiveMode };
}
