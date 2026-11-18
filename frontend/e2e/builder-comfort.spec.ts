import { BuilderPage, MANUAL, expect, makeWorkflow, test } from "./fixtures";

const SHAPE = { id: "shape", type: "logic.transform", name: "Shape", config: { fields: { greeting: "Hello {{ trigger.name }}" } }, position: { x: 420, y: 160 } };
const EDGE = { source: "trigger", target: "shape", source_handle: "out" };

test("undo, redo, add-next, copy/paste and tidy up", async ({ page, api, org }) => {
  const id = await makeWorkflow(api, org.id, "Comfort flow", [MANUAL, SHAPE], [EDGE]);
  const b = new BuilderPage(page);
  await b.open(id);
  const undo = b.header.getByRole("button", { name: "Undo" });
  const redo = b.header.getByRole("button", { name: "Redo" });
  await expect(undo).toBeDisabled();

  // "Add next step" connects the new step from the selected one.
  await b.select("shape");
  await b.panel.getByRole("button", { name: "Add next step" }).click();
  const palette = page.getByRole("complementary", { name: "Steps" });
  await expect(palette.getByRole("status")).toContainText("Adding after Shape");
  await expect(palette.getByRole("button", { name: "Manual Trigger", exact: true })).toHaveCount(0); // a trigger cannot follow a step
  await palette.getByLabel("Search steps").fill("summar");
  await page.keyboard.press("Enter"); // Enter adds the first match
  await expect(b.nodes).toHaveCount(3);
  await expect(b.edges).toHaveCount(2);
  await expect(b.node("aggregate_1")).toBeVisible();

  // Undo removes the step and its connection; redo brings both back.
  await undo.click();
  await expect(b.nodes).toHaveCount(2);
  await expect(b.edges).toHaveCount(1);
  await redo.click();
  await expect(b.nodes).toHaveCount(3);
  await expect(b.edges).toHaveCount(2);

  // Typing in a setting is one undo step, and Ctrl+Z works from the canvas.
  await b.select("shape");
  await b.panel.getByLabel("Step name").fill("Greeting builder");
  await expect(b.node("shape")).toContainText("Greeting builder");
  await b.deselect();
  await page.keyboard.press("Control+z");
  await expect(b.node("shape")).toContainText("Shape");
  await expect(b.node("shape")).not.toContainText("Greeting builder");

  // Copy and paste: the copy gets its own id.
  await b.select("shape");
  await b.deselect();
  await b.node("shape").click({ position: { x: 40, y: 12 } });
  await page.locator(".react-flow__pane").focus().catch(() => {});
  await page.keyboard.press("Control+c");
  await expect(b.banner).toContainText("Copied 1 step");
  await page.keyboard.press("Control+v");
  await expect(b.nodes).toHaveCount(4);
  await expect(b.node("transform_1")).toBeVisible();

  // Tidy up lays the steps out left to right in flow order without overlaps.
  await b.header.getByRole("button", { name: "Tidy up" }).click();
  const box = async (nid: string) => (await b.node(nid).boundingBox())!;
  const [t, s, a] = [await box("trigger"), await box("shape"), await box("aggregate_1")];
  expect(t.x + t.width).toBeLessThan(s.x);
  expect(s.x + s.width).toBeLessThan(a.x);

  // Ctrl+S saves; the shortcuts list opens with "?".
  await page.keyboard.press("Control+s");
  await expect(b.header).not.toContainText("Unsaved changes");
  const saved = await api("GET", `/workflows/${id}`, undefined, org.id);
  expect(saved.definition.nodes.map((n: any) => n.id).sort()).toEqual(["aggregate_1", "shape", "transform_1", "trigger"]);
  await page.keyboard.press("?");
  await expect(page.getByRole("dialog", { name: "Keyboard shortcuts" })).toContainText("Duplicate the selected step");
});

test("insert data from earlier steps and see the last test run on each step", async ({ page, api, org }) => {
  const use = { id: "use", type: "logic.transform", name: "Use it", config: { fields: { note: "" } }, position: { x: 760, y: 160 } };
  const id = await makeWorkflow(api, org.id, "Data flow", [MANUAL, SHAPE, use], [EDGE, { source: "shape", target: "use", source_handle: "out" }]);
  const b = new BuilderPage(page);
  await b.open(id);

  await b.select("use");
  await expect(b.panel.getByRole("region", { name: "Last test run" })).toContainText("Run a test to see what this step received and produced.");

  // Run a test: every step now shows its own result, and the picker shows real values.
  await b.header.getByRole("button", { name: "Test" }).click();
  const test = page.getByRole("dialog", { name: "Test run" });
  await test.getByLabel("Trigger payload (JSON)").fill('{"name": "Ada"}');
  await test.getByRole("button", { name: "Run test" }).click();
  await expect(test).toContainText("COMPLETED", { timeout: 20_000 });
  await test.getByRole("button", { name: "Close" }).last().click();

  await b.select("shape");
  await expect(b.panel.getByRole("region", { name: "Last test run" })).toContainText("Hello Ada");

  await b.select("use");
  await b.panel.getByLabel("Value").fill("Say: ");
  await b.panel.getByRole("button", { name: "Insert data" }).first().click();
  const menu = page.getByRole("dialog", { name: "Available data" });
  await expect(menu).toContainText("Shape");
  await expect(menu).toContainText("Hello Ada"); // the value from the last test run
  await expect(menu).toContainText("Trigger");
  await menu.getByLabel("Search data").fill("greeting");
  await menu.getByRole("button", { name: /greeting/ }).click();
  await expect(b.panel.getByLabel("Value")).toHaveValue("Say: {{ nodes.shape.greeting }}");

  await b.save();
  const ex = await api("POST", `/workflows/${id}/test`, { input: { name: "Grace" } }, org.id);
  await expect.poll(async () => (await api("GET", `/executions/${ex.id}`, undefined, org.id)).status, { timeout: 20_000 }).toBe("COMPLETED");
  expect((await api("GET", `/executions/${ex.id}`, undefined, org.id)).output).toEqual({ use: { note: "Say: Hello Grace" } });
});
