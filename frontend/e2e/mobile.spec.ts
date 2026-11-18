import { MANUAL, chooseOrg, expect, makeWorkflow, seedRuns, test, type Api } from "./fixtures";
import type { Page } from "@playwright/test";

const PHONE = { width: 390, height: 844 };

/**
 * Elements that stick out past the right edge of the screen, other than inside a container that
 * scrolls sideways on purpose (wide tables, code, the builder canvas). Empty means the page fits.
 */
async function overflowing(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const width = document.documentElement.clientWidth;
    const scrollsSideways = (el: Element | null): boolean => {
      for (let p = el?.parentElement; p && p !== document.body && p.tagName !== "MAIN"; p = p.parentElement) {
        const s = getComputedStyle(p);
        if (["auto", "scroll", "hidden", "clip"].includes(s.overflowX) && p.getBoundingClientRect().right <= width + 1) return true;
      }
      return false;
    };
    const out: string[] = [];
    if (document.documentElement.scrollWidth > width + 1) out.push(`document scrolls sideways (${document.documentElement.scrollWidth}px)`);
    const main = document.querySelector("main");
    if (main && main.scrollWidth > main.clientWidth + 1) out.push(`main scrolls sideways (${main.scrollWidth}px in ${main.clientWidth}px)`);
    for (const el of Array.from(document.querySelectorAll("body *"))) {
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.height === 0 || r.right <= width + 1 || scrollsSideways(el)) continue;
      const s = getComputedStyle(el);
      if (s.position === "fixed" || s.visibility === "hidden") continue;
      const label = `${el.tagName.toLowerCase()}${el.id ? "#" + el.id : ""}.${String(el.className).split(" ").slice(0, 4).join(".")} "${(el.textContent ?? "").trim().slice(0, 40)}" right=${Math.round(r.right)}`;
      out.push(label);
      if (out.length > 8) break;
    }
    return out;
  });
}

async function arrange(api: Api, orgId: string) {
  const runs = await seedRuns(api, orgId);
  const agent = await api("POST", "/agents", { name: "Phone agent", system_instructions: "Answer briefly." }, orgId);
  return { ...runs, agentId: agent.id };
}

test.use({ viewport: PHONE });

test("on a phone every screen fits the width and the menu opens as a drawer", async ({ page, api, org }) => {
  const { ok, badRun, agentId } = await arrange(api, org.id);
  await page.goto("/");
  await expect(page.getByRole("navigation", { name: "Main" })).toBeHidden(); // tucked away behind the menu button
  await page.getByRole("button", { name: "Open menu" }).click();
  await chooseOrg(page, org.name);
  await expect(page.getByRole("navigation", { name: "Main" })).toBeHidden(); // choosing an organization closes the drawer

  const screens: [string, RegExp | string][] = [
    ["/", org.name], ["/organizations", "Organizations"], [`/organizations/${org.id}`, org.name], ["/automations", "Automations"],
    ["/builder", "Workflow Builder"], ["/agents", "AI Agents"], [`/agents/${agentId}`, "Phone agent"], ["/integrations", "Integrations"],
    ["/templates", "Templates"], ["/solutions", "Industry Solutions"], ["/executions", "Executions"], [`/executions/${badRun}`, "Bad flow"],
    ["/teams", "AI Teams"], ["/content", "Content"], ["/studio", "Creative Studio"], ["/files", "Files"],
    ["/analytics", "Analytics"], ["/billing", "Usage & Billing"], ["/notifications", "Notifications"], ["/audit", "Audit Logs"], ["/settings", "Settings"],
  ];
  const problems: string[] = [];
  for (const [path, heading] of screens) {
    await page.goto(path);
    await expect(page.getByRole("heading", { level: 1, name: heading }).first()).toBeVisible();
    await page.waitForLoadState("networkidle");
    for (const p of await overflowing(page)) problems.push(`${path}: ${p}`);
  }
  for (const tab of ["Team", "Roles", "Platform"]) {
    await page.goto("/settings");
    await page.getByRole("tab", { name: tab }).click();
    await page.waitForLoadState("networkidle");
    for (const p of await overflowing(page)) problems.push(`/settings (${tab}): ${p}`);
  }
  expect(problems).toEqual([]);

  // Navigate with the drawer, the way a person on a phone does.
  await page.getByRole("button", { name: "Open menu" }).click();
  const nav = page.getByRole("navigation", { name: "Main" });
  await expect(nav).toBeVisible();
  await nav.getByRole("link", { name: "Executions" }).click();
  await page.waitForURL((u) => u.pathname === "/executions");
  await expect(nav).toBeHidden();
  await page.getByRole("button", { name: "Open menu" }).click();
  await page.keyboard.press("Escape");
  await expect(nav).toBeHidden();
  await expect(page.getByRole("button", { name: "Open menu" })).toBeFocused();

  // The builder fits the screen; steps are added from a drawer and edited in a bottom sheet.
  await page.goto(`/builder/${ok}`);
  const nodes = page.locator(".react-flow__node");
  await expect(nodes.first()).toBeVisible();
  expect(await overflowing(page)).toEqual([]);
  const before = await nodes.count();
  await page.getByRole("button", { name: "Add step" }).click();
  const palette = page.getByRole("complementary", { name: "Steps" });
  await expect(palette).toBeVisible();
  await palette.getByRole("button", { name: "Transform Data", exact: true }).click();
  await expect(palette).toBeHidden();
  await expect(nodes).toHaveCount(before + 1);
  await expect(page.getByLabel("Step name")).toBeVisible(); // the new step is selected and its settings slide up
  expect(await overflowing(page)).toEqual([]);
  // The selected step stays in view above the sheet instead of hiding behind it.
  const sheetTop = (await page.locator("#builder-panel").boundingBox())!.y;
  await expect.poll(async () => { const b = await page.locator(".react-flow__node.selected").boundingBox(); return b ? b.y + b.height <= sheetTop && b.y >= 0 : false; }).toBe(true);
  const first = (await nodes.first().boundingBox())!; // the canvas is zoomed out on a phone and the step may be partly under the toolbar
  await nodes.first().click({ position: { x: first.width / 3, y: first.height - 5 } }); // tapping another step brings that one into view
  await expect.poll(async () => { const b = await nodes.first().boundingBox(); return b ? b.y + b.height <= sheetTop && b.y >= 0 : false; }).toBe(true);
  await page.getByRole("button", { name: "Close panel" }).click();
  await expect(page.getByLabel("Step name")).toBeHidden();
  await page.getByRole("button", { name: /^Workflow details/ }).click();
  await expect(page.getByRole("button", { name: "Close panel" })).toBeVisible();
  await page.getByRole("button", { name: "Save draft" }).click();
  await expect(page.getByText("Unsaved changes")).toHaveCount(0);
  expect((await api("GET", `/workflows/${ok}`, undefined, org.id)).definition.nodes).toHaveLength(before + 1);
});

test.describe("tablet", () => {
  test.use({ viewport: { width: 768, height: 1024 } });
  test("on a tablet every screen fits the width", async ({ page, api, org }) => {
    const { ok, badRun } = await seedRuns(api, org.id);
    await page.goto("/");
    await page.getByRole("button", { name: "Open menu" }).click();
    await chooseOrg(page, org.name);
    const problems: string[] = [];
    for (const path of ["/", "/automations", "/executions", `/executions/${badRun}`, "/analytics", "/billing", "/integrations", "/templates", "/settings", `/builder/${ok}`]) {
      await page.goto(path);
      await page.waitForLoadState("networkidle");
      for (const p of await overflowing(page)) problems.push(`${path}: ${p}`);
    }
    expect(problems).toEqual([]);
  });
});

test("on a phone a new workflow can be started from the builder list", async ({ page, api, org }) => {
  await makeWorkflow(api, org.id, "Pocket flow", [MANUAL]);
  await page.goto("/builder");
  await page.getByRole("button", { name: "Open menu" }).click();
  await chooseOrg(page, org.name);
  await expect(page.getByText("Pocket flow")).toBeVisible();
  expect(await overflowing(page)).toEqual([]);
});
