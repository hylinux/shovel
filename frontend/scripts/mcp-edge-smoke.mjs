import assert from "node:assert/strict";
import { createServer } from "node:http";
import { mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { extname, resolve, sep } from "node:path";
import { chromium } from "playwright-core";

const root = resolve(import.meta.dirname, "..", "out");
const receipt = resolve(import.meta.dirname, "..", ".tools", "mcp-edge-smoke.json");
await rm(receipt, { force: true });
await readFile(resolve(root, "index.html"));
const mime = {
  ".html": "text/html",
  ".js": "application/javascript",
  ".css": "text/css",
  ".svg": "image/svg+xml",
  ".woff2": "font/woff2",
  ".json": "application/json",
};
const errors = [];
const server = createServer(async (request, response) => {
  try {
    const path = new URL(request.url, "http://127.0.0.1").pathname;
    const file = resolve(root, path === "/" ? "index.html" : decodeURIComponent(path).slice(1));
    if (!file.startsWith(root + sep)) {
      response.writeHead(403).end();
      return;
    }
    const data = await readFile(file);
    response.writeHead(200, {
      "Content-Type": mime[extname(file)] || "application/octet-stream",
    }).end(data);
  } catch (error) {
    response.writeHead(error.code === "ENOENT" ? 404 : 500).end();
    errors.push(`Static export request failed: ${error.code || error.name}`);
  }
});
await new Promise((resolve, reject) => {
  server.once("error", reject);
  server.listen(0, "127.0.0.1", resolve);
});
let browser;
let result;
try {
  browser = await chromium.launch({ channel: "msedge", headless: true });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1000 },
    serviceWorkers: "block",
  });
  const origin = `http://127.0.0.1:${server.address().port}`;
  const revision = "a".repeat(64);
  const mutations = [];
  let state = "not_authorized";
  let authId = null;
  let sessionCreated = false;
  const session = {
    id: "fixture-session", title: "Synthetic Session",
    created_at: 1700000000, updated_at: 1700000000,
  };
  const config = {
    path: "C:\\fixture\\mcp.json",
    content: JSON.stringify({ mcpServers: {
      user: {
        url: "https://mcp.example.com/mcp",
        credential_id: "12b9ae42-c658-4177-a3bb-f8b9b616edaf",
        auth: { type: "oauth_user", client_id: "fixture" },
      },
      key: {
        url: "https://mcp.example.com/mcp",
        credential_id: "23b9ae42-c658-4177-a3bb-f8b9b616edaf",
        auth: { type: "bearer", credential_ref: "keyring:fixture" },
      },
    } }, null, 2),
    revision, exists: true,
    items: [
      {
        name: "user", enabled: true, transport: "http", auth_type: "oauth_user",
        credential_refs: [], connection_state: "not_connected",
      },
      {
        name: "key", enabled: true, transport: "http", auth_type: "bearer",
        credential_refs: ["keyring:fixture"], connection_state: "not_connected",
      },
    ],
    warnings: [], validation_error: null, agent_loaded: false,
    agent_state: "not_loaded", restart_required: false,
  };
  await context.route("**/*", async route => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin !== origin) {
      errors.push("Unexpected external request");
      await route.abort();
      return;
    }
    const path = url.pathname;
    const method = request.method();
    if (!path.startsWith("/api/")) {
      await route.continue();
      return;
    }
    let body;
    if (method === "GET") {
      switch (path) {
        case "/api/v1/status":
          body = {
            workspace_id: "mcp-smoke-fixture", service: "shovel",
            embedding_configured: false, model_configured: false,
            model_name: "", memory_configured: true,
          };
          break;
        case "/api/v1/runtime/configuration":
          body = {
            path: "fixture", revision, items: [], pending: [], editable: false, applied: true,
          };
          break;
        case "/api/v1/user/profile":
          body = {
            revision: 1, nickname: "", gender: "", occupation: "", bio: "",
            avatar_url: "", updated_at: null,
          };
          break;
        case "/api/v1/conversations":
          body = { items: sessionCreated ? [session] : [] };
          break;
        case "/api/v1/conversations/fixture-session/messages":
          body = { items: [], next_before: null };
          break;
        case "/api/v1/conversations/fixture-session/working-memory":
          body = { items: [] };
          break;
        case "/api/v1/agent/mcp":
          body = config;
          break;
        case "/api/v1/agent/mcp/user/auth":
          body = {
            state, auth_type: "oauth_user", revision, expires_at: null, authorization_id: authId,
          };
          break;
        case "/api/v1/resources":
        case "/api/v1/jobs":
        case "/api/v1/operations":
        case "/api/v1/notifications":
        case "/api/v1/platform/interactions":
        case "/api/v1/platform/executions":
          body = { items: [] };
          break;
      }
    } else if (method === "POST" && path === "/api/v1/conversations") {
      sessionCreated = true;
      body = session;
    } else if (path.startsWith("/api/v1/agent/mcp/")) {
      const input = request.postDataJSON();
      assert.equal(input.revision, revision);
      if (method === "POST" && path === "/api/v1/agent/mcp/user/auth/start") {
        state = "authorizing";
        authId = `fixture-auth-${mutations.length}`;
        body = {
          authorization_id: authId,
          authorization_url: "https://identity.example/authorize?state=synthetic-fixture",
          expires_at: Date.now() / 1000 + 300,
          issuer: "https://identity.example", scopes: ["tools.read"],
        };
      } else if (method === "POST" && path === "/api/v1/agent/mcp/user/auth/cancel") {
        assert.equal(input.authorization_id, authId);
        state = "not_authorized";
        authId = null;
        body = { state };
      } else if (method === "POST" && path === "/api/v1/agent/mcp/user/auth/disconnect") {
        state = "not_authorized";
        authId = null;
        body = { state, local_cleared: true, provider_revocation: "unsupported" };
      } else if (method === "PUT" && path === "/api/v1/agent/mcp/key/credentials") {
        assert.equal(input.reference, "keyring:fixture");
        assert.equal(input.value, "synthetic-ui-secret");
        body = { saved: true, restart_required: true };
      }
      if (body !== undefined) mutations.push({ path, method });
    }
    if (body === undefined) {
      errors.push(`Unexpected fixture request: ${method} ${path}`);
      await route.fulfill({ status: 501, body: "Unexpected fixture request" });
      return;
    }
    await route.fulfill({
      status: 200, contentType: "application/json", body: JSON.stringify(body),
    });
  });
  const page = await context.newPage();
  page.setDefaultTimeout(15_000);
  page.on("pageerror", error => errors.push(error.message));
  page.on("console", message => {
    if (message.type() === "error") errors.push(message.text());
  });
  await page.goto(origin);
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByRole("tab", { name: "MCP Server Management", exact: true }).click();
  await page.getByRole("button", { name: "Start authorization", exact: true }).click();
  const link = page.getByRole("link", { name: "Open authorization page", exact: true });
  await link.waitFor();
  assert.deepEqual(await link.evaluate(element => ({
    target: element.target, rel: element.rel, policy: element.referrerPolicy,
  })), {
    target: "_blank", rel: "noopener noreferrer", policy: "no-referrer",
  });
  await page.getByRole("button", { name: "Cancel authorization", exact: true }).click();
  await link.waitFor({ state: "hidden" });
  assert.deepEqual(mutations.map(row => row.path), [
    "/api/v1/agent/mcp/user/auth/start", "/api/v1/agent/mcp/user/auth/cancel",
  ]);
  await page.getByRole("button", { name: "Start authorization", exact: true }).click();
  await link.waitFor();
  state = "authorized";
  authId = null;
  await page.getByText("Local tokens available · connection not verified", { exact: true }).waitFor();
  assert.equal(await link.count(), 0);
  assert.equal(await page.getByText("Connected · tools discovered", { exact: true }).count(), 0);
  await page.getByRole("button", { name: "Disconnect authorization", exact: true }).click();
  assert.equal(mutations.length, 3);
  await page.getByRole("button", { name: "Confirm disconnect", exact: true }).click();
  await page.getByText(
    "Local authorization cleared. Provider revocation is unsupported; tokens may remain valid at the provider. Restart Agent.",
    { exact: true },
  ).waitFor();
  const password = page.getByLabel("New keyring credential (write only)", { exact: true });
  assert.equal(await password.inputValue(), "");
  await password.fill("synthetic-ui-secret");
  await page.getByRole("button", { name: "Save credential", exact: true }).click();
  await page.getByText(
    "Credential saved without readback. Restart Agent to replace cached credentials; no connection was tested.",
    { exact: true },
  ).waitFor();
  assert.equal(await password.inputValue(), "");
  const draft = '{"mcpServers":{"draft":{}}}';
  await page.getByLabel("mcp.json configuration", { exact: true }).fill(draft);
  await page.getByRole("tab", { name: "General settings", exact: true }).click();
  await page.getByRole("tab", { name: "MCP Server Management", exact: true }).click();
  assert.equal(await page.getByLabel("mcp.json configuration", { exact: true }).inputValue(), draft);
  assert.equal(await page.getByRole("button", { name: "Authorize again", exact: true }).count(), 0);
  assert.equal(await page.getByRole("button", { name: "Start authorization", exact: true }).isDisabled(), true);
  assert.equal(await page.getByRole("button", { name: "Save credential", exact: true }).isDisabled(), true);
  const persisted = await page.evaluate(() => [
    ...Object.values(localStorage), ...Object.values(sessionStorage),
  ].join("|"));
  assert.ok(!persisted.includes("synthetic-ui-secret") && !persisted.includes("synthetic-fixture"));
  assert.deepEqual(mutations, [
    { path: "/api/v1/agent/mcp/user/auth/start", method: "POST" },
    { path: "/api/v1/agent/mcp/user/auth/cancel", method: "POST" },
    { path: "/api/v1/agent/mcp/user/auth/start", method: "POST" },
    { path: "/api/v1/agent/mcp/user/auth/disconnect", method: "POST" },
    { path: "/api/v1/agent/mcp/key/credentials", method: "PUT" },
  ]);
  assert.deepEqual(errors, []);
  result = {
    result: "PASS", browser: "Microsoft Edge", version: browser.version(), headless: true,
    synthetic_only: true,
    scenarios: [
      "Explicit authorization and safe new-tab link",
      "Cancel binds current authorization ID and hides link",
      "Authorization completion polls without implying a live connection",
      "Disconnect requires confirmation and reports unsupported revocation",
      "Write-only keyring input is cleared after successful save",
      "Tab navigation retains literal JSON draft",
      "Unsaved draft blocks authentication and credential mutation",
      "All mutations use exact saved revision",
      "No authorization state or credential in browser persistent storage",
      "No page/console errors, unexpected API requests or external requests",
    ],
    mutations,
  };
  await context.close();
} finally {
  try {
    if (browser) await browser.close();
  } finally {
    await new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
  }
}
await mkdir(resolve(receipt, ".."), { recursive: true });
await writeFile(receipt, JSON.stringify(result, null, 2) + "\n");
console.log(JSON.stringify(result, null, 2));
