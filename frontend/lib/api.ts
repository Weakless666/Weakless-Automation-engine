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

function fail(res: Response, data: any): never {
  const err = data?.error;
  throw new ApiError(res.status, err?.code ?? "error", err?.message ?? `Request failed (${res.status})`, err?.details);
}

/** Upload one file as multipart form data (the JSON client above cannot carry binary bodies). */
export async function apiUpload<T = any>(path: string, file: File): Promise<T> {
  const body = new FormData();
  body.append("file", file);
  const headers: Record<string, string> = {};
  if (csrfToken) headers["X-WAE-CSRF"] = csrfToken;
  if (organizationId) headers["X-Organization-Id"] = organizationId;
  let res: Response;
  try {
    res = await fetch(`/api/v1${path}`, { method: "POST", headers, credentials: "same-origin", body });
  } catch {
    throw new ApiError(0, "network_error", "Cannot reach the server. Check your connection and try again.");
  }
  const data = await res.json().catch(() => null);
  if (!res.ok) fail(res, data);
  return data as T;
}

/** Fetch a stored file as a typed blob for an inline preview. Only images and videos are ever given their real type. */
export async function apiBlob(path: string, contentType: string): Promise<Blob> {
  const headers: Record<string, string> = {};
  if (organizationId) headers["X-Organization-Id"] = organizationId;
  const res = await fetch(`/api/v1${path}`, { headers, credentials: "same-origin" });
  if (!res.ok) fail(res, await res.json().catch(() => null));
  const safe = /^(image\/(png|jpeg|gif|webp)|video\/(mp4|webm|quicktime))$/.test(contentType) ? contentType : "application/octet-stream";
  return new Blob([await res.arrayBuffer()], { type: safe });
}

/** Fetch a binary response and hand it to the browser as a download. */
export async function apiDownload(path: string, filename: string): Promise<void> {
  const headers: Record<string, string> = {};
  if (organizationId) headers["X-Organization-Id"] = organizationId;
  const res = await fetch(`/api/v1${path}`, { headers, credentials: "same-origin" });
  if (!res.ok) fail(res, await res.json().catch(() => null));
  const url = URL.createObjectURL(await res.blob());
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
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
