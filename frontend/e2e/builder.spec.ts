import { BuilderPage, MANUAL, expect, makeWorkflow, test } from "./fixtures";

test("build a workflow from scratch: add, wire, configure, save, test, publish, activate", async ({ page, api, org }) => {
  // Create the workflow from the Automations screen.
  await page.goto("/automations");
  await page.getByLabel("Organization").selectOption({ label: org.name });
  await page.getByRole("button", { name: "New workflow" }).first().click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Name").fill("Greeting flow");
  await dialog.getByRole("button", { name: "Create and open builder" }).click();
  await page.waitForURL(/\/builder\/[0-9a-f-]{36}$/);
  const workflowId = page.url().split("/").pop()!;

  const b = new BuilderPage(page);
  await expect(b.nodes).toHaveCount(1);
  await expect(b.header).toContainText("Never published");

  // Add a step from the palette. It must not land on top of the trigger.
  await b.addStep("Transform Data");
  await expect(b.nodes).toHaveCount(2);
  await expect(b.header).toContainText("Unsaved changes");
  const t = (await b.node("trigger").boundingBox())!;
  const x = (await b.node("transform_1").boundingBox())!;
  const overlap = !(x.x > t.x + t.width || x.x + x.width < t.x || x.y > t.y + t.height || x.y + x.height < t.y);
  expect(overlap, "new step overlaps the trigger").toBe(false);

  // Configure it in the side panel.
  await b.panel.getByLabel("Step name").fill("Shape greeting");
  await expect(b.node("transform_1")).toContainText("Shape greeting");
  await b.panel.getByRole("button", { name: "Add field" }).click();
  await b.panel.getByLabel("Key").fill("greeting");
  await b.panel.getByLabel("Value").fill("Hello {{ trigger.name }}");

  // Checks never claim success for unsaved work; after saving, the unconnected step is flagged.
  await b.deselect();
  await expect(b.panel).toContainText("save the draft to re-check");
  await expect(b.panel).not.toContainText("can be published");
  await b.save();
  await expect(b.panel).toContainText("Node is not connected to any input");

  // Wire trigger -> transform by dragging the handle.
  await b.connect("trigger", "transform_1");
  await expect(b.edges).toHaveCount(1);
  await b.save();
  await expect(b.panel).toContainText("No problems found");

  // The draft is persisted on the server, not only in the browser.
  const saved = await api("GET", `/workflows/${workflowId}`, undefined, org.id);
  expect(saved.definition.edges).toEqual([{ source: "trigger", target: "transform_1", source_handle: "out" }]);
  expect(saved.definition.nodes.find((n: any) => n.id === "transform_1").config).toMatchObject({ fields: { greeting: "Hello {{ trigger.name }}" } });
  await page.reload();
  await expect(b.nodes).toHaveCount(2);
  await expect(b.edges).toHaveCount(1);

  // Test-run the draft from the builder.
  await b.header.getByRole("button", { name: "Test" }).click();
  const test = page.getByRole("dialog");
  await test.getByLabel("Trigger payload (JSON)").fill('{"name": "Ada"}');
  await test.getByRole("button", { name: "Run test" }).click();
  await expect(test).toContainText("COMPLETED", { timeout: 20_000 });
  await test.locator("summary", { hasText: "Output" }).last().click();
  await expect(test).toContainText("Hello Ada");
  await test.getByRole("button", { name: "Close" }).last().click();

  // Publish and activate.
  await b.header.getByRole("button", { name: "Publish" }).click();
  await expect(b.banner).toContainText("Published version 1");
  await expect(b.header).toContainText("Published v1");
  await b.header.getByRole("button", { name: "Activate" }).click();
  await expect(b.header.getByRole("button", { name: "Deactivate" })).toBeVisible();

  // The published version runs.
  const ex = await api("POST", `/workflows/${workflowId}/execute`, { input: { name: "Grace" } }, org.id);
  await expect.poll(async () => (await api("GET", `/executions/${ex.id}`, undefined, org.id)).status, { timeout: 20_000 }).toBe("COMPLETED");
  const done = await api("GET", `/executions/${ex.id}`, undefined, org.id);
  expect(done.output).toEqual({ transform_1: { greeting: "Hello Grace" } });
});

test("validation blocks publishing and points at the faulty step", async ({ page, api, org }) => {
  const id = await makeWorkflow(api, org.id, "Broken flow");
  const b = new BuilderPage(page);
  await b.open(id);

  await b.addStep("If / Else"); // a condition is required and missing
  await b.connect("trigger", "if_1", "out");
  await b.save();
  await expect(b.header).toContainText("1 error");

  await b.header.getByRole("button", { name: "Publish" }).click();
  await expect(b.banner).toContainText("validation errors");
  await expect(b.header).toContainText("Never published");

  // Clicking the issue selects the step it belongs to.
  await b.deselect();
  await b.panel.getByRole("button", { name: /if_1/ }).click();
  await expect(b.panel).toContainText("Follow 'true' or 'false'");
  await expect(b.node("if_1")).toHaveClass(/selected/);
});

test("duplicate and delete steps; switch cases become outputs", async ({ page, api, org }) => {
  const transform = { id: "shape", type: "logic.transform", name: "Shape", config: { fields: { a: 1 } }, position: { x: 460, y: 160 } };
  const id = await makeWorkflow(api, org.id, "Editing flow", [MANUAL, transform], [{ source: "trigger", target: "shape", source_handle: "out" }]);
  const b = new BuilderPage(page);
  await b.open(id);
  await expect(b.nodes).toHaveCount(2);

  await b.select("shape");
  await b.panel.getByRole("button", { name: "Duplicate" }).click();
  await expect(b.nodes).toHaveCount(3);
  await expect(b.node("transform_1")).toBeVisible();

  await b.select("transform_1");
  await b.panel.getByRole("button", { name: "Delete" }).click();
  await expect(b.nodes).toHaveCount(2);

  // A Switch step grows one output per case.
  await b.select("shape");
  await b.addStep("Switch");
  await expect(b.panel).toContainText("Switch");
  await b.panel.getByPlaceholder("One per line").fill("vip\nregular");
  const sw = b.node("switch_1");
  for (const h of ["vip", "regular", "default"]) await expect(sw.locator(`.react-flow__handle.source[data-handleid="${h}"]`)).toHaveCount(1);
  await b.connect("shape", "switch_1");
  await b.save();

  const saved = await api("GET", `/workflows/${id}`, undefined, org.id);
  expect(saved.definition.nodes.map((n: any) => n.id).sort()).toEqual(["shape", "switch_1", "trigger"]);
  expect(saved.definition.nodes.find((n: any) => n.id === "switch_1").config.cases).toEqual(["vip", "regular"]);
  expect(saved.definition.edges).toHaveLength(2);
});

test("a concurrent edit is reported instead of silently overwritten", async ({ page, api, org }) => {
  const id = await makeWorkflow(api, org.id, "Shared flow");
  const b = new BuilderPage(page);
  await b.open(id);

  // Someone else saves a change while this editor is open.
  const current = await api("GET", `/workflows/${id}`, undefined, org.id);
  await api("PUT", `/workflows/${id}/draft`, {
    definition: { nodes: [...current.definition.nodes, { id: "theirs", type: "logic.transform", name: "Theirs", config: {}, position: { x: 500, y: 400 } }], edges: [] },
    base_revision: current.draft_revision,
  }, org.id);

  await b.addStep("Transform Data");
  await b.header.getByRole("button", { name: "Save draft" }).click();
  await expect(b.banner).toContainText("changed by someone else");
  await expect(b.header).toContainText("Unsaved changes");

  const after = await api("GET", `/workflows/${id}`, undefined, org.id);
  expect(after.definition.nodes.map((n: any) => n.id)).toContain("theirs"); // their change survived
});
