import { MANUAL, PASSWORD, cardFor, chooseOrg, expect, makeWorkflow, navLinks, newSession, test, uniqueEmail } from "./fixtures";

test("integrations: unavailable OAuth providers say so; a connection is tested, never shows its secret, and can be revoked", async ({ page, org }) => {
  await page.goto("/integrations");
  await chooseOrg(page, org.name);
  await page.getByRole("tab", { name: /Catalog/ }).click();

  const gmail = cardFor(page, page.getByText("Gmail", { exact: true }));
  await expect(gmail).toContainText("Awaiting platform credentials");

  // A global IP literal: validated by the SSRF guard without any DNS lookup or outbound request.
  const secretUrl = "https://1.1.1.1/e2e-hook-secret-path";
  const hook = cardFor(page, page.getByText("Custom Webhook", { exact: true }));
  await hook.getByRole("button", { name: "Connect" }).click();
  const dlg = page.getByRole("dialog", { name: "Connect Custom Webhook" });
  await dlg.getByLabel("Webhook URL").fill(secretUrl);
  await dlg.getByRole("button", { name: "Connect and test" }).click();
  await expect(dlg).toBeHidden();

  await page.getByRole("tab", { name: /Connections/ }).click();
  const row = page.getByRole("row").filter({ hasText: "Custom Webhook" });
  await expect(row).toContainText(/connected/i);
  await row.getByRole("button", { name: "Test" }).click();
  await expect(row).toContainText("OK");
  expect(await page.content()).not.toContain("e2e-hook-secret-path"); // the stored secret is never sent to the browser

  await row.getByRole("button", { name: "Revoke" }).click();
  await expect(row).toContainText(/revoked/i);
});

test("a client member sees only what their role allows", async ({ page, api, org, browser }) => {
  const email = uniqueEmail("member");
  await api("POST", "/members", { email, full_name: "Client Member", role: "CLIENT_MEMBER", password: PASSWORD }, org.id);
  const wfId = await makeWorkflow(api, org.id, "Member visible flow", [MANUAL]);
  await api("POST", `/workflows/${wfId}/publish`, {}, org.id);
  const ex = await api("POST", `/workflows/${wfId}/execute`, { input: {} }, org.id);

  const member = await newSession(browser, email);
  await member.goto("/");
  const names = await navLinks(member).allInnerTexts();
  expect(names.map((n) => n.trim())).toEqual(expect.arrayContaining(["Overview", "Automations", "Executions", "Templates", "Notifications", "Settings"]));
  for (const hidden of ["Workflow Builder", "Usage & Billing", "Audit Logs"]) expect(names.map((n) => n.trim())).not.toContain(hidden);

  // Integrations are visible read-only: nothing can be connected.
  await member.goto("/integrations");
  await member.getByRole("tab", { name: /Catalog/ }).click();
  await expect(member.getByText("Gmail", { exact: true })).toBeVisible();
  await expect(member.getByRole("button", { name: "Connect" })).toHaveCount(0);

  // They can see their organization's executions...
  await member.goto("/executions");
  await expect(member.getByRole("row").filter({ hasText: "Member visible flow" })).toBeVisible();
  await member.goto(`/executions/${ex.id}`);
  await expect(member.getByRole("heading", { name: "Member visible flow" })).toBeVisible();
  // ...but cannot edit workflows, even by typing the URL.
  await member.goto(`/builder/${wfId}`);
  await expect(member.getByText("Your role cannot edit workflows.")).toBeVisible();
});

test("the control center shows live numbers and every main screen opens without an error", async ({ page, api, org }) => {
  const wfId = await makeWorkflow(api, org.id, "Counted flow", [MANUAL]);
  await api("POST", `/workflows/${wfId}/publish`, {}, org.id);
  const ex = await api("POST", `/workflows/${wfId}/execute`, { input: {} }, org.id);
  await expect.poll(async () => (await api("GET", `/executions/${ex.id}`, undefined, org.id)).status, { timeout: 20_000 }).toBe("COMPLETED");

  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/");
  await chooseOrg(page, org.name);
  await expect(page.getByRole("heading", { name: org.name })).toBeVisible();
  const executions = page.locator("div").filter({ has: page.getByText("Executions", { exact: true }) }).filter({ hasText: "success" }).last();
  await expect(executions).toContainText("1");

  for (const [href, heading] of [["/organizations", "Organizations"], ["/automations", "Automations"], ["/builder", "Workflow Builder"], ["/agents", "AI Agents"],
    ["/integrations", "Integrations"], ["/templates", "Templates"], ["/solutions", "Industry Solutions"], ["/executions", "Executions"], ["/analytics", "Analytics"],
    ["/billing", "Usage & Billing"], ["/notifications", "Notifications"], ["/audit", "Audit Logs"], ["/settings", "Settings"]] as const) {
    await navLinks(page).filter({ hasText: heading }).first().click();
    await page.waitForURL((u) => u.pathname === href);
    await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible();
    await expect(page.locator("main [role=alert]")).toHaveCount(0);
  }
  expect(errors).toEqual([]);
});
