import { defineConfig, devices } from "@playwright/test";

/**
 * Browser end-to-end tests. `npm run e2e` starts a throwaway API (fresh SQLite database,
 * port 8100) and a production build of the web app (port 3100), then drives the UI.
 *
 * Locally on Windows the installed Microsoft Edge is used, so no browser download is needed.
 * In CI (or with PW_CHANNEL=chromium) Playwright's own Chromium is used:
 *   npx playwright install chromium
 */
const API_PORT = 8100;
const WEB_PORT = 3100;
const python = process.env.E2E_PYTHON ?? (process.platform === "win32" ? "..\\backend\\.venv\\Scripts\\python.exe" : "python");
const channel = process.env.PW_CHANNEL ?? (process.env.CI ? undefined : process.platform === "win32" ? "msedge" : undefined);

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    ...devices["Desktop Chrome"],
    baseURL: `http://localhost:${WEB_PORT}`,
    viewport: { width: 1440, height: 900 },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    ...(channel && channel !== "chromium" ? { channel } : {}),
  },
  webServer: [
    {
      command: `${python} ../backend/scripts/e2e_server.py`,
      url: `http://127.0.0.1:${API_PORT}/api/v1/health/ready`,
      timeout: 120_000,
      reuseExistingServer: false,
      stdout: "ignore",
      stderr: "pipe",
    },
    {
      command: `npm run build && npx next start -p ${WEB_PORT}`,
      url: `http://localhost:${WEB_PORT}/login`,
      timeout: 300_000,
      reuseExistingServer: false,
      env: { BACKEND_URL: `http://127.0.0.1:${API_PORT}`, NEXT_DIST_DIR: ".next-e2e", NEXT_STANDALONE: "0", NEXT_TELEMETRY_DISABLED: "1" },
    },
  ],
});
