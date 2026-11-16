import { test, expect } from "@playwright/test";

test("Long history shares expiry work and batches a burst without losing the durable answer", async ({ page }) => {
  await page.addInitScript(() => {
    sessionStorage.setItem("shovel.current-session:browser-fixture", "fixture-session");
    const intervals = new Set();
    const create = window.setInterval.bind(window);
    const remove = window.clearInterval.bind(window);
    window.setInterval = (callback, ms, ...args) => {
      const id = create(callback, ms, ...args);
      if (ms === 30000) intervals.add(id);
      return id;
    };
    window.clearInterval = id => { intervals.delete(id); remove(id); };
    window.renderMetrics = { intervals, updates: 0, longTasks: 0, longestTask: 0 };
    new PerformanceObserver(list => {
      for (const entry of list.getEntries()) {
        window.renderMetrics.longTasks++;
        window.renderMetrics.longestTask = Math.max(window.renderMetrics.longestTask, entry.duration);
      }
    }).observe({ type: "longtask", buffered: true });
  });
  await page.goto("/");
  await page.request.post("/__fixture/control", { data: { historyAttachments: true, streamChunks: 240, streamDelay: 1 } });
  await page.reload();
  await expect(page.locator(".message-attachments")).toHaveCount(100);
  const intervals = await page.evaluate(() => window.renderMetrics.intervals.size);
  await page.evaluate(() => {
    let previous = "";
    new MutationObserver(() => {
      const value = document.querySelector(".pending-answer .assistant .rich-text")?.textContent || "";
      if (value && value !== previous) { window.renderMetrics.updates++; previous = value; }
    }).observe(document.querySelector(".messages"), { subtree: true, childList: true, characterData: true });
  });
  await page.getByRole("textbox", { name: "Message", exact: true }).fill("Synthetic burst");
  await expect(page.getByRole("button", { name: "Send Message", exact: true })).toBeEnabled();
  const started = Date.now();
  await page.getByRole("button", { name: "Send Message", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "Message", exact: true })).toHaveValue("");
  await expect(page.locator("[data-message-id]").last()).toContainText("Synthetic reply chunk 240.");
  const metrics = await page.evaluate(() => ({ updates: window.renderMetrics.updates,
    longTasks: window.renderMetrics.longTasks, longestTask: window.renderMetrics.longestTask }));
  console.log(JSON.stringify({ project: test.info().project.name, expiryIntervals: intervals, elapsedMs: Date.now() - started, ...metrics }));
  if (!process.env.RENDER_BASELINE) {
    expect(intervals).toBe(1);
    expect(metrics.updates).toBeLessThan(80);
  }
});
