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
  assert.equal(development.logging.incomingRequests, true);
  assert.equal(development.logging.browserToTerminal, true);
  const production = await loadConfig(PHASE_PRODUCTION_BUILD, directory, { silent: true });
  assert.equal(production.output, "export");
  assert.notEqual(production.logging?.browserToTerminal, true);
});
