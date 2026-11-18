import { chooseOrg, expect, test } from "./fixtures";

// 1x1 PNG
const PNG = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==", "base64");

test("attach media to a post, connect a channel, and see why publishing failed", async ({ page, api, org }) => {
  // The account exists but cannot connect (there is no real Telegram here), which is exactly what a person sees when a token is wrong.
  const bot = await api("POST", "/integrations", { provider_key: "telegram", name: "Studio channel", values: { bot_token: "123456789:" + "A".repeat(35), chat_id: "@studio" } }, org.id);
  expect(bot.status).not.toBe("connected");

  await page.goto("/content");
  await chooseOrg(page, org.name);
  await page.getByRole("button", { name: "New content" }).click();
  const dialog = page.getByRole("dialog", { name: "New content" });
  await dialog.getByLabel("Title").fill("Autumn timetable");
  await dialog.getByLabel("Channel").selectOption("other");
  await dialog.getByLabel("Text").fill("The autumn timetable is out.");
  await dialog.getByLabel("Publish at").fill("2031-05-06T09:30");
  await dialog.getByLabel("Upload media").setInputFiles({ name: "timetable.png", mimeType: "image/png", buffer: PNG });
  await expect(dialog.getByRole("img", { name: "timetable.png" })).toBeVisible(); // previewed from the stored file
  await dialog.getByRole("button", { name: "Add to calendar" }).click();
  await expect(dialog).toBeHidden();
  const [item] = (await api("GET", "/content", undefined, org.id)).items;
  expect(item.media).toHaveLength(1);
  expect(item.media[0].name).toBe("timetable.png");

  // Connect the channel to the account.
  await page.getByRole("tab", { name: "Publishing" }).click();
  await expect(page.getByText("Where approved content is published")).toBeVisible();
  await page.getByLabel("Account for Other").selectOption(bot.id);
  await expect(page.getByLabel("Account for Other")).toHaveValue(bot.id);
  const channels = (await api("GET", "/content/publishing", undefined, org.id)).items;
  expect(channels.find((c: any) => c.channel === "other").integration_id).toBe(bot.id);
  expect(channels.find((c: any) => c.channel === "blog").native).toBe(false);

  // Approve; it is scheduled for later, so nothing goes out yet. Publishing now reports the real reason it failed.
  await page.getByRole("tab", { name: /Awaiting approval/ }).click();
  await page.getByRole("button", { name: /Autumn timetable/ }).click();
  const open = page.getByRole("dialog", { name: "Autumn timetable" });
  await open.getByRole("button", { name: "Approve", exact: true }).click();
  await expect(open).toBeHidden();
  await page.getByRole("tab", { name: "Recently changed" }).click();
  await page.getByRole("button", { name: /Autumn timetable/ }).click();
  await expect(open.getByText(/It will be published to Studio channel automatically/)).toBeVisible();
  await expect(open.getByRole("img", { name: "timetable.png" })).toBeVisible();
  await open.getByRole("button", { name: "Publish now" }).click();
  await expect(open.getByText(/Publishing to Studio channel failed: .*not connected/)).toBeVisible();
  const after = await api("GET", `/content/${item.id}`, undefined, org.id);
  expect(after.status).toBe("failed");
  expect(after.published_url).toBeNull();
});

test("creative studio explains what is missing when no AI provider is connected", async ({ page, api, org }) => {
  await page.goto("/studio");
  await chooseOrg(page, org.name);
  await expect(page.getByRole("heading", { level: 1, name: "Creative Studio" })).toBeVisible();
  await expect(page.getByText(/No AI provider that makes images or videos is connected/)).toBeVisible();
  await page.getByLabel("Describe it").fill("A bright yoga studio at sunrise");
  await expect(page.getByRole("button", { name: "Generate image" })).toBeDisabled();
  await page.getByRole("button", { name: "Video", exact: true }).click();
  await expect(page.getByLabel("Length")).toBeVisible();
  await expect(page.getByRole("button", { name: "Generate video" })).toBeDisabled();
  await expect(page.getByText("Nothing created yet")).toBeVisible();
  // The API agrees: it refuses instead of queueing a request that could never run.
  await expect(api("POST", "/studio/generations", { prompt: "A bright yoga studio at sunrise" }, org.id)).rejects.toThrow(/422/);
});
