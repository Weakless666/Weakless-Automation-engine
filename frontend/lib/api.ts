// Thin API client. Authentication is the HttpOnly session cookie; mutations carry the
// per-session CSRF token; tenant-scoped calls carry the selected organization.

export class ApiError extends Error {
  status: number;
  code: string;
  details: unknown;
  constructor(status: number, code: string, message: string, details?: unknown) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

let csrfToken = "";
let organizationId: string | null = null;

export function setCsrfToken(token: string) {
  csrfToken = token;
}
export function setOrganization(id: string | null) {
  organizationId = id;
}
export function currentOrganization() {
  return organizationId;
}

type Options = { method?: string; body?: unknown; params?: Record<string, unknown>; org?: string | null | false; headers?: Record<string, string> };

export async function api<T = any>(path: string, opts: Options = {}): Promise<T> {
  const method = opts.method ?? "GET";
  const url = new URL(`/api/v1${path}`, window.location.origin);
  for (const [k, v] of Object.entries(opts.params ?? {})) {
    if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, String(v));
  }
  const headers: Record<string, string> = { ...(opts.headers ?? {}) };
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && csrfToken) headers["X-WAE-CSRF"] = csrfToken;
  const org = opts.org === false ? null : opts.org ?? organizationId;
  if (org) headers["X-Organization-Id"] = org;

  let res: Response;
  try {
    res = await fetch(url, { method, headers, credentials: "same-origin", body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined });
  } catch {
    throw new ApiError(0, "network_error", "Cannot reach the server. Check your connection and try again.");
  }
  const text = await res.text();
  let data: any = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = null;
    }
  }
  if (!res.ok) {
    const err = data?.error;
    if (res.status === 401 && !path.startsWith("/auth/") && typeof window !== "undefined") {
      window.dispatchEvent(new Event("wae:unauthorized"));
    }
    throw new ApiError(res.status, err?.code ?? "error", err?.message ?? (res.status >= 500 ? "The server could not process the request." : `Request failed (${res.status})`), err?.details);
  }
  return data as T;
}

export type Page<T> = { items: T[]; total: number; limit: number; offset: number };

export function errorMessage(e: unknown): string {
  if (e instanceof ApiError) {
    if (Array.isArray(e.details) && e.details.length) {
      const first = e.details[0] as any;
      const extra = first?.message ? `${first.field ? first.field + ": " : ""}${first.message}` : typeof first === "string" ? first : "";
      return extra ? `${e.message} - ${extra}` : e.message;
    }
    return e.message;
  }
  return e instanceof Error ? e.message : "Something went wrong";
}
