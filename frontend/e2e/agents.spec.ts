import { chooseOrg, expect, test } from "./fixtures";

test("create an agent, configure it, allow a scoped tool, and see an honest error without an AI key", async ({ page, api, org }) => {
  await page.goto("/agents");
  await chooseOrg(page, org.name);
  await page.getByRole("button", { name: "New agent" }).first().click();
  const create = page.getByRole("dialog", { name: "New agent" });
  await create.getByLabel("Name").fill("Front Desk");
  await create.getByLabel("Type").selectOption({ label: "Receptionist Agent" });
  await create.getByRole("button", { name: "Create agent" }).click();
  await page.waitForURL(/\/agents\/[0-9a-f-]{36}$/);
  const agentId = page.url().split("/").pop()!;

  // Configuration.
  await expect(page.getByLabel(/^Model\b/)).toHaveValue("claude-opus-5-5");
  await page.getByLabel("System instructions").fill("You help hotel guests politely.");
  await page.getByLabel("Effort").selectOption("low");
  await page.getByRole("button", { name: "Save agent" }).click();
  await expect(page.getByText("Saved", { exact: true })).toBeVisible();
  const saved = await api("GET", `/agents/${agentId}`, undefined, org.id);
  expect(saved.system_instructions).toBe("You help hotel guests politely.");
  expect(saved.provider_options).toEqual({ effort: "low" });

  // Tools: only what is explicitly allowed, scoped.
  await page.getByRole("tab", { name: /Tools/ }).click();
  await expect(page.getByText("This agent has no tools")).toBeVisible();
  await page.getByRole("button", { name: "Add tool" }).click();
  const tool = page.getByRole("dialog", { name: "Add tool" });
  await tool.getByLabel("Tool").selectOption({ label: "HTTP request" });
  await tool.getByLabel("Name shown to the model").fill("fetch_rates");
  await tool.getByLabel("Allowed domains").fill("api.partner.com");
  await tool.getByRole("button", { name: "Add tool" }).click();
  const row = page.getByRole("row").filter({ hasText: "fetch_rates" });
  await expect(row).toContainText("allowed_domains=api.partner.com");
  await expect(row).toContainText("Automatic");
  await row.getByRole("button", { name: "Require approval" }).click();
  await expect(row).toContainText("Human approval");

  // Chat: no AI provider is connected, so the run fails and says why.
  await page.getByRole("tab", { name: "Test" }).click();
  await page.getByLabel("Message").fill("Hello");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText("No credentials for AI provider 'anthropic'")).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText(/Last run:/)).toContainText(/failed/i);
});

test("knowledge base: add a document and find it by searching", async ({ page, org }) => {
  await page.goto("/agents");
  await chooseOrg(page, org.name);
  await page.getByRole("tab", { name: "Knowledge" }).click();
  await page.getByLabel("New knowledge base name").fill("Hotel facts");
  await page.getByRole("button", { name: "Add", exact: true }).click();
  await page.getByRole("button", { name: /Hotel facts/ }).click();

  await page.getByLabel("Title").fill("Spa");
  await page.getByLabel("Content").fill("The spa is open daily from 09:00 to 21:00.\n\nThe pool closes at 20:00.");
  await page.getByRole("button", { name: "Add document" }).click();
  await expect(page.getByRole("row").filter({ hasText: "Spa" })).toBeVisible();

  await page.getByLabel("Search query").fill("when does the spa open");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(page.getByText("The spa is open daily from 09:00 to 21:00.")).toBeVisible();
});
