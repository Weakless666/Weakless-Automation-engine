import { createHmac } from "node:crypto";
import type { Page } from "@playwright/test";
import { PASSWORD, expect, newSession, test, uniqueEmail } from "./fixtures";

/** RFC 6238 TOTP, as an authenticator app computes it. */
function totp(secret: string, offsetSteps = 0): string {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = 0, value = 0;
  const bytes: number[] = [];
  for (const ch of secret.replace(/[\s=]/g, "").toUpperCase()) {
    value = (value << 5) | alphabet.indexOf(ch);
    bits += 5;
    if (bits >= 8) { bytes.push((value >>> (bits - 8)) & 255); bits -= 8; }
  }
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(Math.floor(Date.now() / 1000 / 30) + offsetSteps));
  const h = createHmac("sha1", Buffer.from(bytes)).update(counter).digest();
  const o = h[h.length - 1] & 0x0f;
  return String((h.readUInt32BE(o) & 0x7fffffff) % 1_000_000).padStart(6, "0");
}

async function scanAndConfirm(p: Page): Promise<{ secret: string; recovery: string[] }> {
  const secret = (await p.getByLabel("Setup key").innerText()).replace(/\s/g, "");
  await expect(p.getByRole("img", { name: "QR code for your authenticator app" })).toBeVisible();
  await p.getByLabel("Code from the app").fill(totp(secret));
  await p.getByRole("button", { name: "Turn on two-factor sign-in" }).click();
  const list = p.getByRole("list", { name: "Recovery codes" });
  await expect(list.getByRole("listitem")).toHaveCount(10);
  const recovery = (await list.getByRole("listitem").allInnerTexts()).map((c) => c.trim());
  const cont = p.getByRole("button", { name: "I saved these codes" });
  await expect(cont).toBeDisabled(); // cannot skip past the codes
  await p.getByLabel("I have stored my recovery codes").check();
  await cont.click();
  return { secret, recovery };
}

async function passwordStep(p: Page, email: string) {
  await p.goto("/login");
  await p.getByLabel("Email").fill(email);
  await p.getByLabel("Password").fill(PASSWORD);
  await p.getByRole("button", { name: "Sign in" }).click();
}

test("turn on two-factor sign-in, then sign in with a code and with a recovery code", async ({ page, api, browser }) => {
  const email = uniqueEmail("twofactor");
  const user = await api("POST", "/users", { email, full_name: "Two Factor", password: PASSWORD, platform_role: "SUPPORT" });
  const me = await newSession(browser, email);

  await me.goto("/settings");
  const card = me.locator("div").filter({ has: me.getByRole("heading", { name: "Two-factor sign-in" }) }).filter({ has: me.getByRole("button") }).last();
  await expect(card).toContainText("Off");
  await card.getByRole("button", { name: "Turn on" }).click();
  const { secret, recovery } = await scanAndConfirm(me);
  await expect(card).toContainText("On");
  await expect(card).toContainText("10 recovery codes left");

  // Sign out; the password alone is no longer enough.
  await me.getByRole("button", { name: "Sign out" }).click();
  await me.waitForURL(/\/login/);
  await passwordStep(me, email);
  await expect(me.getByRole("heading", { name: "Two-factor sign-in" })).toBeVisible();
  await me.getByLabel("Authentication code").fill("000000");
  await me.getByRole("button", { name: "Verify" }).click();
  await expect(me.getByText("That code is not valid")).toBeVisible();
  await me.getByLabel("Authentication code").fill(totp(secret, 1)); // the current step was used during setup
  await me.getByRole("button", { name: "Verify" }).click();
  await me.waitForURL((u) => u.pathname === "/");

  // A recovery code works once.
  await me.getByRole("button", { name: "Sign out" }).click();
  await me.waitForURL(/\/login/);
  await passwordStep(me, email);
  await me.getByRole("button", { name: "Use a recovery code" }).click();
  await me.getByLabel("Recovery code").fill(recovery[0]);
  await me.getByRole("button", { name: "Verify" }).click();
  await me.waitForURL((u) => u.pathname === "/");

  // An administrator resets it for a lost phone; the person is signed out.
  await page.goto("/settings");
  await page.getByRole("tab", { name: "Team" }).click();
  const row = page.getByRole("row").filter({ hasText: email });
  await row.getByRole("button", { name: "Reset 2FA" }).click();
  await expect(row.getByRole("button", { name: "Reset 2FA" })).toHaveCount(0);
  await me.goto("/settings");
  await me.waitForURL(/\/login/);
  expect((await api("GET", "/users?limit=200")).items.find((u: any) => u.id === user.id).mfa_enabled).toBe(false);
});

test("when required, a team member without two-factor sets it up during sign-in", async ({ api, browser }) => {
  const email = uniqueEmail("required");
  await api("POST", "/users", { email, full_name: "Must Enroll", password: PASSWORD, platform_role: "SUPPORT" });
  await api("PUT", "/settings/security.staff_mfa_required", { value: true });
  try {
    const p = await newSession(browser);
    await passwordStep(p, email);
    await expect(p.getByRole("heading", { name: "Set up two-factor sign-in" })).toBeVisible();
    await scanAndConfirm(p);
    await p.waitForURL((u) => u.pathname === "/");
    await p.goto("/settings");
    const card = p.locator("div").filter({ has: p.getByRole("heading", { name: "Two-factor sign-in" }) }).filter({ has: p.getByRole("button") }).last();
    await expect(card).toContainText("Required for Weakless team accounts");
    await expect(card.getByRole("button", { name: "Turn off" })).toHaveCount(0);
  } finally {
    await api("PUT", "/settings/security.staff_mfa_required", { value: false });
  }
});
