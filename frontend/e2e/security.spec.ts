import { expect, test } from "./fixtures";

test("pages carry a nonce-based Content-Security-Policy that blocks injected script", async ({ page, api }) => {
  const first = await page.goto("/login");
  const csp = first!.headers()["content-security-policy"] ?? "";
  expect(csp).toMatch(/script-src 'self' 'nonce-[A-Za-z0-9+/=]+' 'strict-dynamic'/);
  expect(csp).toContain("frame-ancestors 'none'");
  expect(csp).toContain("object-src 'none'");
  expect(csp).not.toMatch(/script-src[^;]*'unsafe-(inline|eval)'/);
  const second = await page.request.get("/login");
  expect(second.headers()["content-security-policy"]).not.toEqual(csp); // a fresh nonce per response

  // Signed in, on a data-heavy screen: HTML that reached the page (the XSS case) cannot run script -
  // neither inline event handlers nor javascript: links.
  await api("GET", "/auth/me");
  await page.goto("/executions");
  await expect(page.getByRole("heading", { level: 1, name: "Executions" })).toBeVisible();
  await page.evaluate(() => {
    (window as any).__violations = [];
    document.addEventListener("securitypolicyviolation", (e) => (window as any).__violations.push(e.violatedDirective));
    const holder = document.createElement("div");
    holder.innerHTML = `<img src="data:," onload="window.__pwned = 1"><img src="x" onerror="window.__pwned = 2">`
      + `<a id="evil" href="javascript:window.__pwned = 3">x</a><svg><script>window.__pwned = 4</script></svg>`;
    document.body.appendChild(holder);
  });
  await page.locator("#evil").click();
  await page.waitForTimeout(300);
  expect(await page.evaluate(() => (window as any).__pwned ?? null)).toBeNull();
  expect(await page.evaluate(() => (window as any).__violations)).toEqual(expect.arrayContaining([expect.stringMatching(/^script-src/)]));
});
