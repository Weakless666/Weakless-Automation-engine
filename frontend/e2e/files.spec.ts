import { readFileSync } from "node:fs";
import { chooseOrg, expect, test } from "./fixtures";

test("upload a file, download it back intact, and delete it", async ({ page, api, org }) => {
  const bytes = Buffer.from(Array.from({ length: 2048 }, (_, i) => i % 256)); // binary content
  await page.goto("/files");
  await chooseOrg(page, org.name);
  await expect(page.getByText("No files yet")).toBeVisible();

  await page.getByLabel("Choose files to upload").setInputFiles({ name: "price list.pdf", mimeType: "application/pdf", buffer: bytes });
  const row = page.getByRole("row").filter({ hasText: "price list.pdf" });
  await expect(row).toContainText("2.0 KB");
  await expect(row).toContainText("Uploaded");
  await expect(row).toContainText("Until deleted");
  await expect(page.getByText(/2\.0 KB of .* used/)).toBeVisible();

  const [download] = await Promise.all([page.waitForEvent("download"), row.getByRole("button", { name: "Download price list.pdf" }).click()]);
  expect(download.suggestedFilename()).toBe("price list.pdf");
  expect(readFileSync(await download.path()).equals(bytes)).toBe(true);

  // The id is what a workflow step uses; the API agrees with what the page shows.
  const [file] = (await api("GET", "/files", undefined, org.id)).items;
  expect(file.size).toBe(2048);

  await page.getByLabel("Search files").fill("nothing-like-this");
  await expect(page.getByText("No files match")).toBeVisible();
  await page.getByLabel("Search files").fill("price");
  await row.getByRole("button", { name: "Delete price list.pdf" }).click();
  await expect(row).toHaveCount(0);
  expect((await api("GET", "/files", undefined, org.id)).total).toBe(0);
});
