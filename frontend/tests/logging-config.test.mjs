import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import test from "node:test";

const require = createRequire(import.meta.url);
const loadConfig = require("next/dist/server/config").default;
const { PHASE_DEVELOPMENT_SERVER, PHASE_PRODUCTION_BUILD } = require("next/constants");
const directory = fileURLToPath(new URL("..", import.meta.url));

test("terminal and browser logging are enabled only in the development config", async () => {
  const development = await loadConfig(PHASE_DEVELOPMENT_SERVER, directory, { silent: true });
  const ignored = development.logging.incomingRequests.ignore;
  for (const path of ["/api/v1/status", "/api/v1/resources?limit=100", "/api/v1/jobs", "/api/v1/operations", "/api/v1/resources/id/profiles", "/api/v1/jobs/id/events?after=1"]) {
    assert.ok(ignored.some((pattern) => pattern.test(path)), path);
  }
  for (const path of ["/", "/api/v1/agent", "/api/v1/memories", "/api/v1/resources/id/scans", "/api/v1/jobs/id/cancel", "/api/v1/status-other"]) {
    assert.equal(ignored.some((pattern) => pattern.test(path)), false, path);
  }
  assert.equal(development.logging.browserToTerminal, true);
  const production = await loadConfig(PHASE_PRODUCTION_BUILD, directory, { silent: true });
  assert.equal(production.output, "export");
  assert.notEqual(production.logging?.browserToTerminal, true);
});
