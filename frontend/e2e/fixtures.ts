import { expect, test as base, type Locator, type Page } from "@playwright/test";

export const ADMIN = {
  email: process.env.E2E_ADMIN_EMAIL ?? "e2e-admin@weakless.pro",
  password: process.env.E2E_ADMIN_PASSWORD ?? "E2e-Admin-Passw0rd",
};

export type Api = <T = any>(method: string, path: string, body?: unknown, org?: string) => Promise<T>;

/**
 * `api` signs the browser context in as the e2e administrator (the session cookie is shared
 * with the page) and returns a helper for arranging and checking state through the real API.
 */
export const test = base.extend<{ api: Api; org: { id: string; name: string } }>({
  api: async ({ page }, use) => {
    const login = await page.request.post("/api/v1/auth/login", { data: ADMIN });
    expect(login.ok(), await login.text()).toBeTruthy();
    const { csrf_token } = await login.json();
    await use(async (method, path, body, org) => {
      const res = await page.request.fetch(`/api/v1${path}`, {
        method,
        data: body,
        headers: { "X-WAE-CSRF": csrf_token, ...(org ? { "X-Organization-Id": org } : {}) },
      });
      const text = await res.text();
      if (!res.ok()) throw new Error(`${method} ${path} -> ${res.status()}: ${text}`);
      return text ? JSON.parse(text) : null;
    });
  },
  org: async ({ api }, use) => {
    const name = `E2E ${test.info().title.slice(0, 40)} ${Date.now().toString(36)}`;
    const org = await api("POST", "/organizations", { name, mode: "agency" });
    await use({ id: org.id, name });
  },
});

export { expect };

export const MANUAL = { id: "trigger", type: "trigger.manual", name: "Manual Trigger", config: {}, position: { x: 120, y: 160 } };

/** Create a workflow through the API and return its id. */
export async function makeWorkflow(api: Api, orgId: string, name: string, nodes: any[] = [MANUAL], edges: any[] = []): Promise<string> {
  const wf = await api("POST", "/workflows", { name, definition: { nodes, edges } }, orgId);
  return wf.id;
}

export class BuilderPage {
  readonly nodes: Locator;
  readonly edges: Locator;
  readonly header: Locator;
  readonly panel: Locator;
  readonly banner: Locator;

  constructor(readonly page: Page) {
    this.nodes = page.locator(".react-flow__node");
    this.edges = page.locator(".react-flow__edge");
    this.header = page.locator("header");
    this.panel = page.locator("aside").last();
    this.banner = page.getByRole("status");
  }

  async open(workflowId: string) {
    await this.page.goto(`/builder/${workflowId}`);
    await expect(this.page.locator(".react-flow")).toBeVisible();
    await expect(this.nodes.first()).toBeVisible();
  }

  node(id: string): Locator {
    return this.page.locator(`.react-flow__node[data-id="${id}"]`);
  }

  async addStep(label: string) {
    await this.page.getByRole("button", { name: label, exact: true }).click();
  }

  async select(id: string) {
    await this.node(id).click({ position: { x: 40, y: 12 } });
    await expect(this.panel.getByLabel("Step name")).toBeVisible();
  }

  async deselect() {
    await this.page.locator(".react-flow__pane").click({ position: { x: 20, y: 20 } });
  }

  /** Drag from a step's output handle to another step's input, like a person would. */
  async connect(from: string, to: string, handle = "out") {
    const src = this.node(from).locator(`.react-flow__handle.source[data-handleid="${handle}"]`);
    const dst = this.node(to).locator(".react-flow__handle.target");
    const a = (await src.boundingBox())!;
    const b = (await dst.boundingBox())!;
    await this.page.mouse.move(a.x + a.width / 2, a.y + a.height / 2);
    await this.page.mouse.down();
    await this.page.mouse.move(b.x + b.width / 2, b.y + b.height / 2, { steps: 15 });
    await this.page.mouse.up();
  }

  async save() {
    await this.header.getByRole("button", { name: "Save draft" }).click();
    await expect(this.header).not.toContainText("Unsaved changes");
  }
}
