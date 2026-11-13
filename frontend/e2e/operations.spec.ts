import { MANUAL, chooseOrg, expect, makeWorkflow, seedRuns, test, uniqueEmail } from "./fixtures";

test("executions: filter by status, read the failing step, retry it", async ({ page, api, org }) => {
  const { badRun } = await seedRuns(api, org.id);
  await page.goto("/executions");
  await chooseOrg(page, org.name);
  await expect(page.getByRole("row").filter({ hasText: "Good flow" })).toBeVisible();

  await page.getByLabel("Status").selectOption("FAILED");
  await expect(page.getByRole("row").filter({ hasText: "Good flow" })).toHaveCount(0);
  const failed = page.getByRole("row").filter({ hasText: "Bad flow" });
  await expect(failed).toContainText("blocked");
  await failed.getByRole("link").first().click();

  await page.waitForURL(new RegExp(`/executions/${badRun}$`));
  await expect(page.getByText("Call partner", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("ssrf_blocked").first()).toBeVisible();
  await expect(page.getByRole("button", { name: "Run again from the start" })).toBeVisible();
  await page.getByRole("button", { name: "Retry from failed step" }).click();
  await page.waitForURL((u) => u.pathname.startsWith("/executions/") && !u.pathname.endsWith(badRun));
  await expect(page.getByText("This is a retry of")).toBeVisible();
  await expect(page.getByText(/continued from the failed step \(call\); earlier steps were not repeated/)).toBeVisible();
});

test("a run waiting for an external signal can be resumed from its page", async ({ page, api, org }) => {
  const wf = await makeWorkflow(api, org.id, "Waiting flow",
    [MANUAL, { id: "hold", type: "logic.wait", name: "Wait for customer", config: {}, position: { x: 400, y: 160 } },
      { id: "after", type: "logic.transform", name: "After", config: { fields: { done: true } }, position: { x: 700, y: 160 } }],
    [{ source: "trigger", target: "hold", source_handle: "out" }, { source: "hold", target: "after", source_handle: "out" }]);
  await api("POST", `/workflows/${wf}/publish`, {}, org.id);
  const ex = await api("POST", `/workflows/${wf}/execute`, { input: {} }, org.id);
  await expect.poll(async () => (await api("GET", `/executions/${ex.id}`, undefined, org.id)).status, { timeout: 20_000 }).toBe("WAITING");

  await page.goto(`/executions/${ex.id}`);
  await expect(page.getByText("Paused", { exact: true })).toBeVisible();
  await expect(page.locator("main").getByText("waiting for an external signal")).toBeVisible();
  await page.getByRole("button", { name: "Resume now" }).click();
  await expect(page.getByText("COMPLETED").first()).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText("Paused", { exact: true })).toHaveCount(0);
});

test("notifications: a failure reaches the inbox; preferences and channels are saved", async ({ page, api, org }) => {
  await seedRuns(api, org.id);
  await page.goto("/notifications");
  await chooseOrg(page, org.name);

  // Inbox
  const item = page.getByRole("listitem").filter({ hasText: "Workflow failed: Bad flow" }).first();
  await expect(item).toBeVisible();
  await item.getByRole("button", { name: "Mark read" }).click();
  await expect(item.getByRole("button", { name: "Mark read" })).toHaveCount(0);

  // Preferences persist.
  await page.getByRole("tab", { name: "Preferences" }).click();
  const box = page.getByLabel("Workflow completed by email");
  const before = await box.isChecked();
  await box.click();
  await page.reload();
  await page.getByRole("tab", { name: "Preferences" }).click();
  await expect(page.getByLabel("Workflow completed by email")).toBeChecked({ checked: !before });

  // A webhook channel: its URL is stored encrypted and never shown again. (No test message is sent here.)
  await page.getByRole("tab", { name: "Channels" }).click();
  await page.getByRole("button", { name: "Add channel" }).click();
  const dlg = page.getByRole("dialog", { name: "Add notification channel" });
  await dlg.getByLabel("Type").selectOption("webhook");
  await dlg.getByLabel("Name").fill("Ops hook");
  await dlg.getByLabel("Webhook URL (https)").fill("https://1.1.1.1/e2e-channel-secret");
  await dlg.getByLabel("Workflow failed").check();
  await dlg.getByRole("button", { name: "Add channel" }).click();
  const row = page.getByRole("row").filter({ hasText: "Ops hook" });
  await expect(row).toContainText("workflow.failed");
  expect(await page.content()).not.toContain("e2e-channel-secret");
  await row.getByRole("button", { name: "Turn off" }).click();
  await expect(row).toContainText("Off");
  await row.getByRole("button", { name: "Delete Ops hook" }).click();
  await expect(page.getByRole("row").filter({ hasText: "Ops hook" })).toHaveCount(0);
});

test("settings: invite a staff member and see their pending invitation", async ({ page, api }) => {
  const email = uniqueEmail("staff");
  await page.goto("/settings");
  await page.getByRole("tab", { name: "Team" }).click();
  await page.getByRole("button", { name: "Add team member" }).click();
  const dlg = page.getByRole("dialog", { name: "Add team member" });
  await dlg.getByLabel("Email").fill(email);
  await dlg.getByLabel("Full name").fill("New Engineer");
  await dlg.getByLabel("Role").selectOption("AUTOMATION_ENGINEER");
  await dlg.getByRole("button", { name: "Send invitation" }).click();
  await expect(page.getByRole("dialog", { name: `Invitation for ${email}` }).getByLabel("Link", { exact: true })).toHaveValue(/\/accept-invite\?token=/);
  await page.getByRole("button", { name: "Done" }).click();
  const row = page.getByRole("row").filter({ hasText: email });
  await expect(row).toContainText(/invited/i);
  await expect(row.getByRole("button", { name: "Resend invite" })).toBeVisible();

  // The platform tab shows configuration status, never secret values.
  await page.getByRole("tab", { name: "Platform" }).click();
  await expect(page.getByText("Platform email (SMTP)")).toBeVisible();
  await expect(page.getByText("Vault active key")).toBeVisible();
  expect(await page.content()).not.toContain("e2e-vault-key");
});

test("settings: change the data retention policy and apply it", async ({ page, api }) => {
  const before = (await api("GET", "/settings")).items.find((i: any) => i.key === "data.retention").value;
  try {
    await page.goto("/settings");
    await page.getByRole("tab", { name: "Platform" }).click();
    const events = page.getByLabel("Inbound events");
    await expect(events).toHaveValue(String(before.events_days));
    await expect(page.getByLabel("Audit log and sign-in history")).toHaveValue(""); // kept forever by default

    await events.fill("0");
    await expect(page.getByRole("button", { name: "Save retention" })).toBeDisabled();
    await events.fill("45");
    await page.getByLabel("Audit log and sign-in history").fill("10"); // below the 30-day minimum
    await page.getByRole("button", { name: "Save retention" }).click();
    await expect(page.getByText(/'audit_days' must be between 30/)).toBeVisible();
    await page.getByLabel("Audit log and sign-in history").fill("");
    await page.getByRole("button", { name: "Save retention" }).click();
    await expect(page.getByText("Saved", { exact: true })).toBeVisible();
    const saved = (await api("GET", "/settings")).items.find((i: any) => i.key === "data.retention").value;
    expect(saved).toEqual({ ...before, events_days: 45, audit_days: null });

    await page.getByRole("button", { name: "Apply now" }).click();
    await expect(page.getByRole("status").filter({ hasText: /Nothing was old enough|deleted|removed|merged/ })).toBeVisible();
    await page.reload();
    await page.getByRole("tab", { name: "Platform" }).click();
    await expect(page.getByLabel("Inbound events")).toHaveValue("45");
  } finally {
    await api("PUT", "/settings/data.retention", { value: before });
  }
});
