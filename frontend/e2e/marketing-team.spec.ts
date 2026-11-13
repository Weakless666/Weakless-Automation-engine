import { chooseOrg, expect, test } from "./fixtures";

test("install the AI Marketing Team, see the team board, and approve content before it goes out", async ({ page, api, org }) => {
  await page.goto("/solutions");
  await chooseOrg(page, org.name);
  const card = page.locator("div").filter({ has: page.getByRole("heading", { name: "AI Marketing Team" }) }).filter({ has: page.getByRole("button") }).last();
  await card.getByRole("button", { name: "View package" }).click();
  const dialog = page.getByRole("dialog", { name: "AI Marketing Team" });
  await expect(dialog.getByRole("region", { name: "Team members" })).toContainText("Marketing Director");
  await expect(dialog.getByRole("region", { name: "Team members" })).toContainText("Performance Analyst");
  const install = dialog.getByRole("button", { name: /Install team and 5 automations/ });
  await expect(install).toBeDisabled(); // the brief is required
  await dialog.getByLabel("Business name").fill("Sunny Yoga");
  await dialog.getByLabel("What the business sells").fill("A yoga studio in Varna with beginner and prenatal classes.");
  await dialog.getByLabel("Who the customers are").fill("Busy professionals and expecting mothers in Varna.");
  await install.click();
  await expect(dialog.getByText("10 AI agents are ready")).toBeVisible();
  await dialog.getByRole("link", { name: "Open the team" }).click();

  await page.waitForURL((u) => u.pathname === "/teams");
  await expect(page.getByRole("heading", { level: 2, name: "AI Marketing Team" })).toBeVisible();
  await expect(page.getByText("Marketing Director").first()).toBeVisible();
  for (const module of ["Research & Content", "Email & Nurture", "Leads & Booking", "Search & Website", "Analytics Loop"]) {
    await expect(page.getByText(module, { exact: true })).toBeVisible();
  }
  await expect(page.getByRole("link", { name: /Post Writer/ })).toBeVisible();

  // A specialist's proposal (created here through the API) waits for a person.
  const when = new Date(Date.now() + 2 * 86400_000).toISOString();
  await api("POST", "/content", { title: "Your first class", channel: "instagram", body: "Nervous about your first class? Here is what to expect.",
    media_notes: "Bright studio photo", scheduled_for: when, status: "awaiting_approval" }, org.id);
  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Content" }).click();
  await page.waitForURL((u) => u.pathname === "/content");
  await expect(page.getByRole("button", { name: /Your first class/ }).first()).toBeVisible(); // on the week calendar
  await page.getByRole("tab", { name: /Awaiting approval/ }).click();
  await page.getByRole("button", { name: /Your first class/ }).click();
  const item = page.getByRole("dialog", { name: "Your first class" });
  await item.getByLabel("Note (optional)").fill("Great, keep the tone");
  await item.getByRole("button", { name: "Approve", exact: true }).click();
  await expect(item).toBeHidden();
  await expect(page.getByText("Nothing is waiting for approval")).toBeVisible();

  const [approved] = (await api("GET", "/content?status=approved", undefined, org.id)).items;
  expect(approved.title).toBe("Your first class");
  expect(approved.decision_note).toBe("Great, keep the tone");
  await page.getByRole("tab", { name: "Recently changed" }).click();
  await page.getByRole("button", { name: /Your first class/ }).click();
  await item.getByLabel("Link to the live post").fill("https://instagram.com/p/abc");
  await item.getByRole("button", { name: "Mark published" }).click();
  await expect(item).toBeHidden();
  expect((await api("GET", `/content/${approved.id}`, undefined, org.id)).status).toBe("published");
});
