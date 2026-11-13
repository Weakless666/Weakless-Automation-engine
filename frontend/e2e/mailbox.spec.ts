import { BuilderPage, MANUAL, expect, makeWorkflow, test } from "./fixtures";

test("the Mailbox Trigger offers only IMAP mailboxes and explains when it starts", async ({ page, api, org }) => {
  // Neither host exists, so both integrations are stored in an error state - enough to be selectable.
  await api("POST", "/integrations", { provider_key: "imap", name: "Front desk inbox", values: { host: "imap.invalid", username: "desk@example.com", password: "app-password-1" } }, org.id);
  await api("POST", "/integrations", { provider_key: "smtp", name: "Outgoing mail", values: { host: "smtp.invalid", port: "587", from_address: "desk@example.com" } }, org.id);
  const id = await makeWorkflow(api, org.id, "Inbox flow", [MANUAL]);

  const b = new BuilderPage(page);
  await b.open(id);
  await b.addStep("Mailbox Trigger");
  await expect(b.panel.getByText("Only mail that arrives after activation starts it.")).toBeVisible();
  const picker = b.panel.locator("select").filter({ has: page.locator("option", { hasText: "Front desk inbox" }) });
  await expect(picker).toHaveCount(1);
  await expect(picker.locator("option", { hasText: "Outgoing mail" })).toHaveCount(0);
  await expect(b.panel.getByText(/^Mailbox\s*\*?$/)).toBeVisible(); // a readable label, not "Integration Id"
  await expect(b.panel.getByLabel("Folder")).toHaveValue("INBOX");
  await expect(b.panel.getByLabel("Mark handled messages as read in the mailbox")).not.toBeChecked();
});
