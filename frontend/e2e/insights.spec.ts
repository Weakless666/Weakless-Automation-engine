import { PASSWORD, cardFor, chooseOrg, expect, makeWorkflow, newSession, seedRuns, test, uniqueEmail } from "./fixtures";

test("analytics are computed from real runs and follow the automation filter", async ({ page, api, org }) => {
  const { bad } = await seedRuns(api, org.id);
  await page.goto("/analytics");
  await chooseOrg(page, org.name);

  const success = cardFor(page, page.getByText("Success rate", { exact: true })).or(page.locator("div").filter({ has: page.getByText("Success rate", { exact: true }) }).last());
  await expect(success.first()).toContainText("50%");
  const byAutomation = page.locator("div").filter({ has: page.getByRole("heading", { name: "By automation" }) }).filter({ has: page.getByRole("table") }).last();
  await expect(byAutomation.getByRole("row").filter({ hasText: "Good flow" })).toBeVisible();
  await expect(byAutomation.getByRole("row").filter({ hasText: "Bad flow" })).toBeVisible();

  await page.getByLabel("Automation").selectOption({ label: "Bad flow" });
  await expect(success.first()).toContainText("0%");
  await expect(page.getByLabel("Automation")).toHaveValue(bad);
});

test("plans: staff create and apply a plan, downgrades are scheduled, clients can only request upgrades", async ({ page, api, org, browser }) => {
  await page.goto("/billing");
  await chooseOrg(page, org.name);
  await expect(page.getByText("Plan allowance")).toBeVisible();

  // Staff create a plan through the editor.
  const key = `e2e-${Date.now().toString(36)}`;
  await page.getByRole("tab", { name: "Plans" }).click();
  await page.getByRole("button", { name: "New plan" }).click();
  const editor = page.getByRole("dialog", { name: "New plan" });
  await editor.getByLabel("Key").fill(key);
  await editor.getByLabel("Name").fill(`E2E Plan ${key}`);
  await editor.getByLabel("Monthly price (cents)").fill("9900");
  await editor.getByLabel("Executions Per Month").fill("1000");
  await editor.getByRole("button", { name: "Save plan" }).click();
  await expect(editor).toBeHidden();
  const card = cardFor(page, page.getByRole("heading", { name: `E2E Plan ${key}` }));
  await expect(card).toContainText("1K");

  // Apply it to the organization, then schedule a downgrade.
  await card.getByRole("button", { name: "Apply to organization" }).click();
  await expect(page.getByText("The new plan is active.")).toBeVisible();
  await expect(card).toContainText("Current");
  const starter = cardFor(page, page.getByRole("heading", { name: "Starter", exact: true }));
  await starter.getByRole("button", { name: "Apply to organization" }).click();
  await expect(page.getByText("scheduled for the end of the current billing period")).toBeVisible();
  await page.getByRole("tab", { name: "Usage" }).click();
  await expect(page.getByText(/Changes to\s+Starter/)).toBeVisible();

  // A client admin can only request an upgrade: nothing is charged, nothing changes.
  const email = uniqueEmail("billing");
  await api("POST", "/members", { email, full_name: "Client Owner", role: "CLIENT_ADMIN", password: PASSWORD }, org.id);
  const client = await newSession(browser, email);
  await client.goto("/billing");
  await client.getByRole("tab", { name: "Plans" }).click();
  await expect(client.getByText("Online payment is not enabled")).toBeVisible();
  await expect(client.getByRole("heading", { name: "Internal (agency managed)" })).toHaveCount(0); // internal plans are hidden from clients
  await cardFor(client, client.getByRole("heading", { name: "Scale", exact: true })).getByRole("button", { name: "Request upgrade" }).click();
  await expect(client.getByText("Nothing was charged")).toBeVisible();
  const sub = await api("GET", "/billing/subscription", undefined, org.id);
  expect(sub.plan.key).toBe(key);

  await page.getByRole("tab", { name: "History" }).click();
  await expect(page.getByText("Change Requested")).toBeVisible();
});

test("audit log records who did what and can be filtered", async ({ page, api, org }) => {
  await makeWorkflow(api, org.id, "Audited flow");
  await page.goto("/audit");
  await chooseOrg(page, org.name);
  await page.getByLabel("Area").selectOption("workflow");
  const row = page.getByRole("row").filter({ hasText: "workflow.created" });
  await expect(row).toContainText("e2e-admin@weakless.pro");
  await expect(page.getByRole("row").filter({ hasText: "organization.created" })).toHaveCount(0); // filtered out
  await row.click();
  await expect(page.getByText('"name": "Audited flow"')).toBeVisible();

  await page.getByLabel("Actor").fill("nobody-matches");
  await expect(page.getByText("No audit entries match these filters")).toBeVisible();
});

