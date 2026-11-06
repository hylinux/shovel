import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import { fileURLToPath } from "node:url";

const loader = createRequire(import.meta.url)("@tailwindcss/turbopack");

test("Layout stylesheet changes remain fresh across incremental loader calls", async () => {
  const resource = fileURLToPath(new URL("../app/globals.css", import.meta.url));
  const source = readFileSync(resource, "utf8");
  function transform(css) {
    return new Promise((resolve, reject) => {
      loader.call({
        resource, resourcePath: resource,
        getOptions: () => ({ base: fileURLToPath(new URL("../", import.meta.url)), optimize: false }),
        addDependency() {},
        addContextDependency() {},
        async: () => (error, output) => error ? reject(error) : resolve(output),
      }, css).catch(reject);
    });
  }
  await transform(source);
  const updated = await transform(`${source}\n.layout-refresh-probe { margin: 13px; }\n`);
  assert.ok(/\.layout-refresh-probe\s*\{/.test(updated), "The loader returned stale CSS instead of the updated layout.");
});
