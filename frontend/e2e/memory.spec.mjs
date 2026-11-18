import { test, expect } from "@playwright/test";

for (const language of ["en", "zh-CN"]) {
  test(`Memory card controls keep non-first targets isolated (${language})`, async ({ page }) => {
    const zh = language === "zh-CN";
    const labels = {
      settings: zh ? "设置" : "Settings", tab: zh ? "管理长期记忆" : "Manage long-term Memory",
      edit: zh ? "编辑" : "Edit", save: zh ? "保存记忆" : "Save Memory",
      reject: zh ? "拒绝…" : "Reject…", confirmReject: zh ? "确认拒绝" : "Confirm reject",
      forget: zh ? "遗忘…" : "Forget…", confirmForget: zh ? "确认遗忘" : "Confirm forget",
      remove: zh ? "彻底删除…" : "Permanently delete…", confirmRemove: zh ? "确认彻底删除" : "Confirm permanent delete",
      cancel: zh ? "取消" : "Cancel", refresh: zh ? "刷新" : "Refresh",
    };
    const writes = [], operations = new Map();
    const originals = [1, 2, 3].map(number => ({
      id: `fixture-memory-${number}`, memory: `Synthetic fact ${number}`, kind: "semantic", scope: "session",
      state: "active", subject: "user", predicate: "prefers", object: `Object ${number}`, topic: "",
      session_id: `source-session-${number}`, importance: 0.5, revision: 1, provenance: [],
    }));
    let records = originals.map(item => ({ ...item }));
    await page.route(/https?:\/\/(?!127\.0\.0\.1:59992(?:\/|$))/, route => {
      throw new Error(`Unexpected external request: ${route.request().url()}`);
    });
    await page.addInitScript(locale => localStorage.setItem("shovel.locale", locale), language);
    await page.route("**/api/v1/memories**", async route => {
      const request = route.request(), url = new URL(request.url());
      if (request.method() === "GET") return route.fulfill({ json: { items: records, total: records.length } });
      const id = decodeURIComponent(url.pathname.split("/")[4]), body = request.postDataJSON();
      writes.push({ id, body, method: request.method(), hard: url.searchParams.get("hard") });
      const item = records.find(record => record.id === id);
      expect(item).toBeTruthy();
      if (request.method() === "DELETE") {
        if (url.searchParams.get("hard") === "true") records = records.filter(record => record.id !== id);
        else item.state = "deleted";
        return route.fulfill({ json: { deleted: true } });
      }
      if (request.method() === "PUT") item.memory = body.text;
      else item.state = "rejected";
      const operation = { id: `operation-${writes.length}`, kind: "memory.update", state: "succeeded", result: {} };
      operations.set(operation.id, operation);
      return route.fulfill({ status: 202, json: operation });
    });
    await page.route("**/api/v1/operations/*", route => route.fulfill({
      json: operations.get(new URL(route.request().url()).pathname.split("/").at(-1)),
    }));
    await page.goto("/");
    await page.getByRole("button", { name: labels.settings, exact: true }).click();
    await page.getByRole("tab", { name: labels.tab, exact: true }).click();
    const card = id => page.locator(".memory-row").filter({ has: page.getByText(id, { exact: true }) });
    await expect(page.locator(".memory-row")).toHaveCount(3);
    for (const original of originals.slice(1)) {
      const target = card(original.id);
      await target.getByRole("button", { name: labels.edit, exact: true }).click();
      await expect(target.locator("textarea")).toHaveValue(original.memory);
      await expect(card(originals[0].id).locator("form, .confirmation")).toHaveCount(0);
      await target.locator("textarea").fill(`Updated ${original.memory}`);
      records = [...records].reverse();
      await page.locator(".memory-controls").getByRole("button", { name: labels.refresh, exact: true }).click();
      await expect(target.locator("textarea")).toHaveValue(`Updated ${original.memory}`);
      await target.getByRole("button", { name: labels.save, exact: true }).click();
      await expect(target.locator(".preserve").first()).toHaveText(`Updated ${original.memory}`);
      expect(writes.at(-1)).toMatchObject({
        id: original.id, method: "PUT", body: { text: `Updated ${original.memory}`, object: original.object, session_id: original.session_id },
      });
      for (const [trigger, confirmation] of [
        [labels.reject, labels.confirmReject], [labels.forget, labels.confirmForget], [labels.remove, labels.confirmRemove],
      ]) {
        const before = writes.length;
        await target.getByRole("button", { name: trigger, exact: true }).click();
        await expect(target.locator(".confirmation")).toBeVisible();
        await expect(card(originals[0].id).locator(".confirmation")).toHaveCount(0);
        await target.getByRole("button", { name: labels.cancel, exact: true }).click();
        expect(writes).toHaveLength(before);
        await target.getByRole("button", { name: trigger, exact: true }).click();
        await target.getByRole("button", { name: confirmation, exact: true }).click();
        await expect.poll(() => writes.length).toBe(before + 1);
        expect(writes.at(-1).id).toBe(original.id);
        if (trigger === labels.reject) {
          expect(writes.at(-1).body).toEqual({ decision: "reject", conversation_id: original.session_id });
          await expect(target.getByRole("button", { name: labels.reject, exact: true })).toHaveCount(0);
        } else if (trigger === labels.forget) {
          expect(writes.at(-1).method).toBe("DELETE");
          expect(writes.at(-1).hard).toBeNull();
          await expect(target.getByRole("button", { name: labels.forget, exact: true })).toHaveCount(0);
        } else {
          expect(writes.at(-1).hard).toBe("true");
          await expect(target).toHaveCount(0);
        }
      }
    }
    expect(records).toEqual([originals[0]]);
    expect(writes).toHaveLength(8);
    await expect(card(originals[0].id).locator(".preserve").first()).toHaveText(originals[0].memory);
    const controls = await page.locator(".memory-controls").boundingBox();
    const remaining = await card(originals[0].id).boundingBox();
    const pagination = await page.locator(".memory-pagination").boundingBox();
    expect(remaining.y).toBeGreaterThan(controls.y + controls.height);
    expect(pagination.y).toBeGreaterThan(remaining.y + remaining.height);
    expect(await card(originals[0].id).evaluate(element => getComputedStyle(element).borderTopStyle)).toBe("solid");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.locator(".memory-list").screenshot({ path: test.info().outputPath(`memory-cards-${language}.png`) });
  });
}
