import { BuilderPage, cardFor, chooseOrg, expect, test } from "./fixtures";

test("preview a template, install it into an organization, then finish its configuration", async ({ page, api, org }) => {
  await page.goto("/templates");
  await chooseOrg(page, org.name);
  await page.getByLabel("Search templates").fill("Webhook Relay");
  await page.getByRole("button", { name: /Webhook Relay/ }).click();

  const preview = page.getByRole("dialog", { name: "Webhook Relay" });
  await expect(preview).toContainText("Forward"); // the steps are listed
  await expect(preview.getByLabel("Target URL")).toBeVisible();
  await preview.getByRole("button", { name: "Install into this organization" }).click();

  // Installed as an independent draft workflow, opened in the builder.
  await page.waitForURL(/\/builder\/[0-9a-f-]{36}$/);
  const b = new BuilderPage(page);
  await expect(b.nodes).toHaveCount(3);
  await expect(b.header).toContainText("Never published");

  await page.goto("/templates");
  await page.getByRole("tab", { name: "Installed" }).click();
  const row = page.getByRole("row").filter({ hasText: "Webhook Relay" });
  await expect(row).toContainText("Missing: Target URL");
  await row.getByRole("button", { name: "Configure" }).click();
  const cfg = page.getByRole("dialog", { name: /Configure Webhook Relay/ });
  await cfg.getByLabel("Target URL").fill("https://example.com/in");
  await cfg.getByRole("button", { name: "Save configuration" }).click();
  await expect(row).toContainText("Complete");

  const installed = await api("GET", "/installed-templates", undefined, org.id);
  const wf = await api("GET", `/workflows/${installed.items[0].workflow_id}`, undefined, org.id);
  expect(wf.variables).toEqual({ target_url: "https://example.com/in" });
});

test("install an industry package; every automation becomes a draft workflow", async ({ page, api, org }) => {
  await page.goto("/solutions");
  await chooseOrg(page, org.name);
  const card = cardFor(page, page.getByRole("heading", { name: "Beauty & Salon Automation" }));
  await card.getByRole("button", { name: "View package" }).click();

  const dlg = page.getByRole("dialog", { name: "Beauty & Salon Automation" });
  await dlg.getByLabel("Business name").fill("Glow Studio");
  await dlg.getByRole("button", { name: "Install 6 automations" }).click();
  await expect(dlg).toContainText("6 automations were installed");
  await dlg.getByRole("button", { name: "Done" }).click();

  const workflows = await api("GET", "/workflows", undefined, org.id);
  expect(workflows.total).toBe(6);
  await page.goto("/automations");
  await expect(page.getByRole("row")).toHaveCount(7); // header + 6

  // Installing again adds nothing.
  await page.goto("/solutions");
  await card.getByRole("button", { name: "View package" }).click();
  await expect(dlg.getByRole("button", { name: /Install/ })).toBeDisabled();
});
