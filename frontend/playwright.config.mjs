import { defineConfig, devices } from "@playwright/test";

const port = 59992;
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  reporter: "list",
  use: { baseURL: `http://127.0.0.1:${port}`, trace: "retain-on-failure", channel: process.env.PLAYWRIGHT_CHANNEL },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
    { name: "mobile-chromium", use: { ...devices["Pixel 7"], defaultBrowserType: "chromium" } },
  ],
  webServer: {
    command: "node e2e/serve.mjs",
    url: `http://127.0.0.1:${port}/__fixture/health`,
    reuseExistingServer: false,
    timeout: 15_000,
  },
});
