import { PASSWORD, expect, navLinks, newSession, test, uniqueEmail } from "./fixtures";

test("invite a member from the organization page; they accept and get the client view", async ({ page, api, org, browser }) => {
  const email = uniqueEmail("maria");
  await page.goto(`/organizations/${org.id}`);
  await page.getByRole("tab", { name: "Members" }).click();
  await page.getByRole("button", { name: "Add member" }).click();

  const add = page.getByRole("dialog", { name: "Add member" });
  await add.getByLabel("Email").fill(email);
  await add.getByLabel("Full name").fill("Maria Front Desk");
  await add.getByLabel("Role").selectOption("CLIENT_ADMIN");
  await expect(add.getByLabel("Send an invitation")).toBeChecked(); // invitations are the default
  await add.getByRole("button", { name: "Send invitation" }).click();

  const issued = page.getByRole("dialog", { name: `Invitation for ${email}` });
  await expect(issued).toContainText("was not emailed"); // honest about missing SMTP
  const url = await issued.getByLabel("Link", { exact: true }).inputValue();
  expect(url).toContain("/accept-invite?token=");
  await issued.getByRole("button", { name: "Done" }).click();
  const row = page.getByRole("row").filter({ hasText: email });
  await expect(row).toContainText(/invited/i);
  await expect(row.getByRole("button", { name: "Resend invite" })).toBeVisible();

  // The invited person, in their own browser.
  const maria = await newSession(browser);
  await maria.goto(url);
  await expect(maria.getByRole("heading", { name: `Join ${org.name}` })).toBeVisible();
  await expect(maria.getByLabel("Email")).toHaveValue(email);
  await maria.getByLabel("Choose a password").fill(PASSWORD);
  await maria.getByLabel("Repeat password").fill("Something-Else-1");
  await maria.getByRole("button", { name: "Create account and join" }).click();
  await expect(maria.getByText("The passwords do not match.")).toBeVisible();
  await maria.getByLabel("Repeat password").fill(PASSWORD);
  await maria.getByRole("button", { name: "Create account and join" }).click();
  await maria.waitForURL((u) => u.pathname === "/");

  // Signed in, inside the right organization, with the client navigation only.
  await expect(maria.getByLabel("Organization")).toHaveValue(org.id);
  const links = navLinks(maria);
  await expect(links.filter({ hasText: "Automations" })).toBeVisible();
  await expect(links.filter({ hasText: "Workflow Builder" })).toHaveCount(0);
  await expect(links.filter({ hasText: "Settings" })).toBeVisible();

  // The link works once.
  const again = await newSession(browser);
  await again.goto(url);
  await expect(again.getByRole("heading", { name: "Invitation unavailable" })).toBeVisible();

  await page.reload();
  await page.getByRole("tab", { name: "Members" }).click();
  await expect(page.getByRole("row").filter({ hasText: email })).toContainText(/active/i);
});

test("staff-issued reset link: the person chooses a new password and signs in", async ({ page, api, org, browser }) => {
  const email = uniqueEmail("reset");
  await api("POST", "/members", { email, full_name: "Reset Person", role: "CLIENT_MEMBER", password: PASSWORD }, org.id);

  await page.goto(`/organizations/${org.id}`);
  await page.getByRole("tab", { name: "Members" }).click();
  await page.getByRole("row").filter({ hasText: email }).getByRole("button", { name: "Reset link" }).click();
  const issued = page.getByRole("dialog", { name: `Password reset link for ${email}` });
  const url = await issued.getByLabel("Link", { exact: true }).inputValue();

  const person = await newSession(browser);
  await person.goto(url);
  await person.getByLabel("New password", { exact: true }).fill("Fresh-Passw0rd-9");
  await person.getByLabel("Repeat new password").fill("Fresh-Passw0rd-9");
  await person.getByRole("button", { name: "Set new password" }).click();
  await person.waitForURL(/\/login\?reset=1/);
  await expect(person.getByText("Your password was changed")).toBeVisible();

  await person.getByLabel("Email").fill(email);
  await person.getByLabel("Password").fill(PASSWORD); // the old one no longer works
  await person.getByRole("button", { name: "Sign in" }).click();
  await expect(person.getByText("Invalid email or password")).toBeVisible();
  await person.getByLabel("Password").fill("Fresh-Passw0rd-9");
  await person.getByRole("button", { name: "Sign in" }).click();
  await person.waitForURL((u) => u.pathname === "/");
});

test("forgot password tells the truth when email is not configured", async ({ browser }) => {
  const p = await newSession(browser);
  await p.goto("/login");
  await p.getByRole("link", { name: "Forgot your password?" }).click();
  await p.getByLabel("Email").fill("someone@e2e-client.com");
  await p.getByRole("button", { name: "Send reset link" }).click();
  await expect(p.getByText("Email is not set up on this platform yet")).toBeVisible();
});

test("signing in with a wrong password shows one generic error", async ({ browser }) => {
  const p = await newSession(browser);
  await p.goto("/login");
  await p.getByLabel("Email").fill("nobody@e2e-client.com");
  await p.getByLabel("Password").fill("Wrong-Passw0rd");
  await p.getByRole("button", { name: "Sign in" }).click();
  await expect(p.getByText("Invalid email or password")).toBeVisible();
  await expect(p).toHaveURL(/\/login/);
});
