import { NextResponse, type NextRequest } from "next/server";

/**
 * Content-Security-Policy for every page, with a fresh nonce per request.
 *
 * Only scripts carrying the nonce run (Next.js adds it to its own scripts when it sees the
 * policy on the request), and 'strict-dynamic' lets those load the app's chunks. Injected
 * markup can therefore not execute script, even if some data were rendered unescaped.
 * Inline *styles* stay allowed: the builder canvas and charts position elements with style
 * attributes, and styles cannot run code. The API (/api) sets its own stricter policy.
 */
export function middleware(request: NextRequest) {
  const nonce = btoa(crypto.randomUUID());
  const dev = process.env.NODE_ENV === "development";
  const policy = [
    "default-src 'self'",
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${dev ? " 'unsafe-eval'" : ""}`,
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "media-src 'self' blob:", // previews of generated and uploaded videos
    "font-src 'self' data:",
    `connect-src 'self'${dev ? " ws: wss:" : ""}`,
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
  ].join("; ");

  const headers = new Headers(request.headers);
  headers.set("x-nonce", nonce);
  headers.set("Content-Security-Policy", policy);
  const response = NextResponse.next({ request: { headers } });
  response.headers.set("Content-Security-Policy", policy);
  return response;
}

export const config = {
  // Pages only: not the API proxy, static assets or prefetches.
  matcher: [{ source: "/((?!api/|_next/static|_next/image|favicon.ico).*)", missing: [{ type: "header", key: "next-router-prefetch" }] }],
};
