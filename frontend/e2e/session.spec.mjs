import { test, expect } from "@playwright/test";

const png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZQmcAAAAASUVORK5CYII=";
const input = page => page.getByRole("textbox", { name: "Message", exact: true });
const send = page => page.getByRole("button", { name: "Send Message", exact: true });
const estimate = page => page.getByRole("region", { name: "Send budget estimate" });
async function control(page, options) { expect((await page.request.post("/__fixture/control", { data: options })).ok()).toBe(true); }
async function fixture(page) { return (await page.request.get("/__fixture/state")).json(); }
async function setDraft(page, text) {
  await input(page).fill(text);
  await expect(estimate(page)).toContainText("Estimated base text:");
}
async function cancelReload(page) {
  const dialog = page.waitForEvent("dialog");
  const reload = page.evaluate(() => { location.reload(); });
  const prompt = await dialog;
  expect(prompt.type()).toBe("beforeunload");
  await prompt.dismiss();
  await reload;
}

test.beforeEach(async ({ page }) => {
  await page.route(/https?:\/\/(?!127\.0\.0\.1:59992(?:\/|$))/, route => {
    throw new Error(`Unexpected external request: ${route.request().url()}`);
  });
  await page.addInitScript(() => {
    const key = "shovel.current-session:browser-fixture";
    if (!sessionStorage.getItem(key)) sessionStorage.setItem(key, "fixture-session");
  });
  await page.goto("/");
  await expect(input(page)).toBeVisible();
  await expect(page.locator(".composer-attach button")).toBeEnabled();
});

test("Dirty drafts prompt on reload even when another Session is selected; clearing the last draft removes the prompt", async ({ page }) => {
  await setDraft(page, "Unsaved original draft");
  await cancelReload(page);
  await expect(input(page)).toHaveValue("Unsaved original draft");
  await page.getByRole("button", { name: "New Session", exact: true }).click();
  await expect(input(page)).toHaveValue("");
  await input(page).click();
  await cancelReload(page);
  await page.getByRole("button", { name: /Fixture Session/ }).click();
  await expect(input(page)).toHaveValue("Unsaved original draft");
  await input(page).fill("");
  let dialogs = 0;
  page.on("dialog", dialog => { dialogs++; void dialog.dismiss(); });
  await page.reload();
  expect(dialogs).toBe(0);
  await expect(input(page)).toHaveValue("");
});

test("Failed sends retain leave protection; a confirmed successful send clears its draft and protection", async ({ page }) => {
  await control(page, { failSend: true });
  await setDraft(page, "Synthetic question");
  await expect(send(page)).toBeEnabled();
  await input(page).press("Control+Enter");
  await expect(page.locator(".composer").getByRole("alert")).toContainText("Synthetic send failure");
  await expect(input(page)).toHaveValue("Synthetic question");
  await cancelReload(page);
  await control(page, { failSend: false });
  await expect(send(page)).toBeEnabled();
  await send(page).click();
  await expect(input(page)).toHaveValue("");
  let dialogs = 0;
  page.on("dialog", dialog => { dialogs++; void dialog.dismiss(); });
  await page.reload();
  expect(dialogs).toBe(0);
});

test("Budget estimates block excess input, never reuse stale success, surface failures and allow explicit retry", async ({ page }) => {
  await setDraft(page, "Small draft 😀");
  await expect(send(page)).toBeEnabled();
  await expect(estimate(page)).toContainText("Candidate history:");
  await input(page).fill("x".repeat(6000));
  await expect(send(page)).toBeDisabled();
  await expect(estimate(page)).toContainText("Text character limit exceeded.");
  await expect(input(page)).toHaveValue("x".repeat(6000));
  await control(page, { failBudget: true });
  await input(page).fill("Recoverable draft");
  await expect(estimate(page)).toContainText("Synthetic budget failure");
  await expect(send(page)).toBeDisabled();
  await control(page, { failBudget: false });
  await page.getByRole("button", { name: "Retry budget estimate" }).click();
  await expect(send(page)).toBeEnabled();
  const snapshots = await fixture(page);
  expect(snapshots.previews.at(-1).content).toBe("Recoverable draft");
  await page.getByLabel("Message format", { exact: true }).selectOption("json");
  await input(page).fill('{"safe":"😀"}');
  await expect(send(page)).toBeEnabled();
  await expect.poll(async () => (await fixture(page)).previews.at(-1).content).toBe('```json\n{"safe":"😀"}\n```');
});

test("Mixed paste preserves selection and file order; cancelled uploads retry the same UUID and expose scope", async ({ page }) => {
  await input(page).fill("Before replace after");
  await input(page).evaluate((element, image) => {
    element.setSelectionRange(7, 14);
    const clipboard = new DataTransfer();
    clipboard.setData("text/plain", "pasted text");
    clipboard.items.add(new File(["x".repeat(9000)], "first.txt", { type: "text/plain" }));
    clipboard.items.add(new File([Uint8Array.from(atob(image), character => character.charCodeAt(0))], "second.png", { type: "image/png" }));
    element.dispatchEvent(new ClipboardEvent("paste", { bubbles: true, cancelable: true, clipboardData: clipboard }));
  }, png);
  await expect(input(page)).toHaveValue("Before pasted text after");
  await expect(page.getByText("Uploaded; validating and saving…").first()).toBeVisible();
  await expect(send(page)).toBeDisabled();
  const uploads = page.locator(".upload-list > li");
  await expect(uploads.nth(0)).toContainText("first.txt");
  await expect(uploads.nth(1)).toContainText("second.png");
  await uploads.nth(0).getByRole("button", { name: "Cancel upload" }).click();
  await expect(uploads.nth(0)).toContainText("The server may have received it");
  await uploads.nth(0).getByRole("button", { name: "Retry" }).click();
  await expect(uploads.nth(0)).toContainText("Prepared excerpt: 8000");
  await expect(uploads.nth(0)).toContainText("Truncated.");
  await expect(uploads.nth(1)).toContainText("Image: 1 × 1");
  await expect(send(page)).toBeDisabled();
  await expect(estimate(page)).toContainText("Text character limit exceeded.");
  const attempts = (await fixture(page)).uploads;
  expect(attempts).toHaveLength(3);
  expect(attempts[2]).toBe(attempts[0]);
  await uploads.nth(0).getByRole("button", { name: "Remove" }).click();
  await expect(send(page)).toBeEnabled();
  await expect(estimate(page)).toContainText("Current images: 1 / 2");
});

test("Media count overflow is visible before sending and removing an image recalculates the budget", async ({ page }) => {
  await page.getByLabel("Attach files", { exact: true }).setInputFiles([0, 1, 2].map(index => ({
    name: `picture-${index}.png`, mimeType: "image/png", buffer: Buffer.from(png, "base64"),
  })));
  await expect(estimate(page)).toContainText("Image count limit exceeded.");
  await expect(send(page)).toBeDisabled();
  await page.locator(".upload-list > li").last().getByRole("button", { name: "Remove" }).click();
  await expect(estimate(page)).toContainText("Current images: 2 / 2");
  await expect(send(page)).toBeEnabled();
});

test("Streaming follows the bottom, pauses while reading, resumes explicitly and restores independent history positions", async ({ page }) => {
  const messages = page.getByLabel("Session messages", { exact: true });
  await setDraft(page, "Stream a synthetic reply");
  await expect(send(page)).toBeEnabled();
  await send(page).click();
  await expect(page.locator(".pending-answer")).toContainText("Synthetic reply chunk");
  await expect.poll(() => messages.evaluate(element => element.scrollHeight - element.scrollTop - element.clientHeight)).toBeLessThan(3);
  const position = await messages.evaluate(element => {
    element.scrollTop = Math.max(100, element.scrollTop - 500);
    element.dispatchEvent(new Event("scroll"));
    return element.scrollTop;
  });
  const streamed = page.locator(".pending-answer .assistant");
  const previous = await streamed.innerText();
  await expect(page.getByRole("button", { name: "New reply · Back to bottom" })).toBeVisible();
  await expect(streamed).not.toHaveText(previous);
  expect(await messages.evaluate(element => element.scrollTop)).toBe(position);
  await page.getByRole("button", { name: "New reply · Back to bottom" }).click();
  await expect(input(page)).toHaveValue("");
  await expect.poll(() => messages.evaluate(element => element.scrollHeight - element.scrollTop - element.clientHeight)).toBeLessThan(3);
  await messages.evaluate(element => { element.scrollTop = 4321; element.dispatchEvent(new Event("scroll")); });
  await page.getByRole("button", { name: "Older messages" }).evaluate(element => element.click());
  await expect(page.locator("[data-message-id='message-1']")).toBeAttached();
  await messages.evaluate(element => { element.scrollTop = 1234; element.dispatchEvent(new Event("scroll")); });
  await page.getByRole("button", { name: "Latest messages" }).evaluate(element => element.click());
  await expect.poll(() => messages.evaluate(element => element.scrollTop)).toBe(4321);
  await page.getByRole("button", { name: "Older messages" }).evaluate(element => element.click());
  await expect.poll(() => messages.evaluate(element => element.scrollTop)).toBe(1234);
});

test("Late content growth preserves the visible anchor; copy failure is explicit and source remains selectable", async ({ page }) => {
  const messages = page.getByLabel("Session messages", { exact: true });
  await messages.evaluate(element => { element.scrollTop = 4321; element.dispatchEvent(new Event("scroll")); });
  const anchor = await messages.evaluate(element => {
    const message = [...element.querySelectorAll("[data-message-id]")].find(item => item.getBoundingClientRect().bottom > element.getBoundingClientRect().top);
    return { id: message.dataset.messageId, top: message.getBoundingClientRect().top };
  });
  await page.locator(".message-list").evaluate(element => {
    const image = new Image();
    image.alt = "Synthetic late image";
    image.style.cssText = "display:block;height:200px;width:200px";
    element.firstElementChild.after(image);
    image.dispatchEvent(new Event("load"));
  });
  await expect.poll(() => page.locator(`[data-message-id="${anchor.id}"]`).evaluate(element => element.getBoundingClientRect().top)).toBeCloseTo(anchor.top, 0);
  await page.evaluate(() => {
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: {
      writeText: async () => { throw new DOMException("Synthetic clipboard denial", "NotAllowedError"); },
    } });
  });
  const message = page.locator("[data-message-id]").first();
  await message.getByRole("button", { name: "Copy source", exact: true }).click();
  await expect(message).toContainText("Copy failed. Select and copy the source manually.");
  await message.getByRole("button", { name: "View source", exact: true }).click();
  await expect(message.locator(".message-source")).toContainText("Synthetic message");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});
