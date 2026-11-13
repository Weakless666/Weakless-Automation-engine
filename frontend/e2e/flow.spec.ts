import { BuilderPage, MANUAL, expect, makeWorkflow, test } from "./fixtures";

test("the builder offers sub-workflows, data steps and webhook replies", async ({ page, api, org }) => {
  await makeWorkflow(api, org.id, "Shared: format address", [MANUAL]);
  const id = await makeWorkflow(api, org.id, "Order intake", [MANUAL]);
  const b = new BuilderPage(page);
  await b.open(id);

  const palette = page.getByRole("complementary", { name: "Steps" });
  await expect(palette.getByText("Data", { exact: true })).toBeVisible();
  for (const label of ["Sort and Limit", "Remove Duplicates", "Summarize", "Date & Time", "Find CRM Records", "Find Database Records", "Respond to Webhook"]) {
    await expect(palette.getByRole("button", { name: label, exact: true })).toBeVisible();
  }

  await b.addStep("Run Workflow");
  const picker = b.panel.locator("select").filter({ has: page.locator("option", { hasText: "Shared: format address" }) });
  await expect(picker).toHaveCount(1);
  await picker.selectOption({ label: "Shared: format address" });
  await expect(b.panel.getByLabel("Pause until the called workflow finishes and use its result")).toBeChecked();
  await expect(b.panel.getByText("Failure handling")).toHaveCount(0); // logic steps report failures through their own 'error' output

  await b.addStep("Summarize");
  await expect(b.panel.getByLabel("Step name")).toHaveValue("Summarize");
  await b.save();
  const saved = await api("GET", `/workflows/${id}`, undefined, org.id);
  const call = saved.definition.nodes.find((n: any) => n.type === "logic.run_workflow");
  expect(call.config.workflow_id).toBeTruthy();
});
