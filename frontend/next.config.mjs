/** @type {import('next').NextConfig} */
const backend = process.env.BACKEND_URL || "http://localhost:8000";

const nextConfig = {
  // Browser tests build into their own folder so they never clash with a running dev server.
  distDir: process.env.NEXT_DIST_DIR || ".next",
  output: process.env.NEXT_STANDALONE === "0" ? undefined : "standalone",
  reactStrictMode: true,
  poweredByHeader: false,
  // The browser only ever talks to this origin; /api is proxied to the FastAPI backend,
  // which keeps the session cookie first-party (HttpOnly, SameSite=Lax).
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${backend}/api/:path*` }];
  },
  async headers() {
    return [
      {
        source: "/((?!api/).*)",
        headers: [
          { key: "X-Frame-Options", value: "DENY" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
        ],
      },
    ];
  },
};

export default nextConfig;
