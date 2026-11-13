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

test("Composer preview restores focus and selection; code disables spelling and oversized insertions report an error", async ({ page, isMobile }) => {
    await input(page).fill("Before selected after");
    await input(page).evaluate(element => { element.focus(); element.setSelectionRange(7, 15, "backward"); });
    await page.locator(".composer-modes").getByRole("button", { name: "Preview", exact: true }).click();
    await expect(page.getByLabel("Message preview", { exact: true })).toBeFocused();
    await page.getByLabel("Message preview", { exact: true }).press("Escape");
    await expect(input(page)).toBeFocused();
    expect(await input(page).evaluate(element => [element.selectionStart, element.selectionEnd, element.selectionDirection])).toEqual([7, 15, "backward"]);
    await page.getByLabel("Message format", { exact: true }).selectOption("json");
    await expect(input(page)).toHaveAttribute("spellcheck", "false");
    await expect(input(page)).toHaveAttribute("autocapitalize", "off");
    await page.getByLabel("Message format", { exact: true }).selectOption("markdown");
    await input(page).fill("x".repeat(16000));
    await page.getByRole("button", { name: "Bold", exact: true }).click();
    await expect(page.getByRole("alert").filter({ hasText: "Formatting would exceed" })).toBeVisible();
    await expect(input(page)).toHaveValue("x".repeat(16000));
    if (isMobile) {
      const bounds = await page.getByRole("button", { name: "Bold", exact: true }).boundingBox();
      expect(bounds.height).toBeGreaterThanOrEqual(44);
      expect(bounds.width).toBeGreaterThanOrEqual(44);
    }
  });

test("Offscreen rich previews defer work, duplicate diagrams reuse output and height controls retain iframe isolation", async ({ page }) => {
    await control(page, { richHistory: true });
    await page.reload();
    await expect(page.locator("[data-message-id]")).toHaveCount(100);
    await expect(page.locator(".isolated-preview iframe")).toHaveCount(0);
    const first = page.locator('[data-message-id="message-25"]');
    await first.scrollIntoViewIfNeeded();
    await expect(first.locator("iframe")).toHaveAttribute("srcdoc", /<svg/);
    const document = await first.locator("iframe").getAttribute("srcdoc");
    const second = page.locator('[data-message-id="message-26"]');
    await second.scrollIntoViewIfNeeded();
    await expect(second.locator("iframe")).toHaveAttribute("srcdoc", document);
    await first.getByLabel("Preview height").press("End");
    await expect(first.getByLabel("Preview height")).toHaveValue("800");
    await expect(first.locator("iframe")).toHaveAttribute("sandbox", "");
    const html = page.locator('[data-message-id="message-27"]');
    await html.scrollIntoViewIfNeeded();
    await expect(html.locator("iframe")).toHaveAttribute("srcdoc", /Safe fixture/);
    const sanitized = await html.locator("iframe").getAttribute("srcdoc");
    expect(sanitized).not.toContain("<script>");
    expect(sanitized).not.toContain("https://invalid.example");
    expect(await page.evaluate(() => window.bad)).toBeUndefined();
    await expect(html.locator("iframe")).toHaveAttribute("sandbox", "");
  });

test("JSON tools preserve exact numbers and duplicate keys; invalid input is not silently rewritten", async ({ page }) => {
  const source = '{"n":900719925474099312345,"n":1e999,"a":[]}';
  await page.getByLabel("Message format", { exact: true }).selectOption("json");
  await input(page).fill("{bad}");
  await page.getByRole("button", { name: "Format JSON", exact: true }).click();
  await expect(page.locator(".composer").getByRole("alert")).toContainText("Invalid JSON");
  await expect(input(page)).toHaveValue("{bad}");
  await input(page).fill(source);
  await expect(page.locator(".composer-format-notice")).toHaveCount(0);
  await page.getByRole("button", { name: "Validate JSON", exact: true }).click();
  await expect(input(page)).toHaveValue(source);
  await expect(page.locator(".composer-format-notice")).toHaveText("JSON is valid.");
  await page.getByRole("button", { name: "Format JSON", exact: true }).click();
  const formatted = await input(page).inputValue();
  expect(formatted).toContain('"n": 900719925474099312345');
  expect(formatted).toContain('"n": 1e999');
  expect(formatted).toContain("\n");
  await input(page).fill("");
  await control(page, { richHistory: true });
  await page.reload();
  const message = page.locator('[data-message-id="message-28"]');
  await message.scrollIntoViewIfNeeded();
  await message.getByRole("button", { name: "Format JSON", exact: true }).click();
  await expect(message.locator(".message-code pre")).toContainText('"n": 900719925474099312345');
  await page.evaluate(() => Object.defineProperty(navigator, "clipboard", { configurable: true, value: {
    writeText: async value => { window.fixtureCopied = value; },
  } }));
  await message.locator(".message-code").getByRole("button", { name: "Copy source", exact: true }).click();
  expect(await page.evaluate(() => window.fixtureCopied)).toBe('{"n":900719925474099312345,"n":1e999,"safe":"<script>"}');
  await message.getByRole("button", { name: "Original JSON", exact: true }).click();
  await expect(message.locator(".message-code pre")).toContainText('"n":900719925474099312345');
});

test("Code language survives Session navigation and reaches preview, send budget and saved source", async ({ page }) => {
  await page.getByLabel("Message format", { exact: true }).selectOption("code");
  await page.getByLabel("Code language", { exact: true }).selectOption("python");
  await input(page).fill("print(1)");
  await page.getByRole("button", { name: "New Session", exact: true }).click();
  await expect(input(page)).toHaveValue("");
  await page.getByRole("button", { name: /Fixture Session/ }).click();
  await expect(page.getByLabel("Code language", { exact: true })).toHaveValue("python");
  await expect(input(page)).toHaveValue("print(1)");
  await expect(send(page)).toBeEnabled();
  expect((await fixture(page)).previews.at(-1).content).toBe("```python\nprint(1)\n```");
  await send(page).click();
  await expect(input(page)).toHaveValue("");
  const message = page.locator("[data-message-id].user").last();
  await message.getByRole("button", { name: "View source", exact: true }).click();
  await expect(message.locator(".message-source")).toHaveText("```python\nprint(1)\n```");
});

test("Image and diagram viewers support keyboard zoom, Escape and focus return without relaxing isolation", async ({ page }) => {
  await page.locator("input[type=file]").setInputFiles({ name: "fixture.png", mimeType: "image/png", buffer: Buffer.from(png, "base64") });
  await expect(page.locator(".upload-list")).toContainText("Ready");
  await expect(send(page)).toBeEnabled();
  await send(page).click();
  await expect(input(page)).toHaveValue("");
  const imageButton = page.getByRole("button", { name: "Enlarge image", exact: true });
  await imageButton.click();
  const image = page.getByRole("dialog", { name: "Enlarged image", exact: true });
  await expect(image).toBeVisible();
  await image.getByLabel("Zoom").press("End");
  await expect(image.getByLabel("Zoom")).toHaveValue("300");
  await expect(image.locator("img")).toHaveAttribute("style", /300%/);
  const canvas = image.getByRole("region", { name: "Enlarged content" });
  await canvas.focus();
  await canvas.press("ArrowRight");
  await expect.poll(() => canvas.evaluate(element => element.scrollLeft)).toBeGreaterThan(0);
  await image.getByRole("button", { name: "Reset zoom" }).click();
  await expect(image.getByLabel("Zoom")).toHaveValue("100");
  await image.press("Escape");
  await expect(image).toHaveCount(0);
  await expect(imageButton).toBeFocused();
  await control(page, { richHistory: true });
  await page.reload();
  const diagram = page.locator('[data-message-id="message-25"]');
  await diagram.scrollIntoViewIfNeeded();
  const diagramButton = diagram.getByRole("button", { name: "Enlarge diagram" });
  await diagramButton.click();
  const viewer = page.getByRole("dialog", { name: "Enlarged diagram", exact: true });
  await expect(viewer.locator("iframe")).toHaveAttribute("sandbox", "");
  await expect(viewer.locator("iframe")).toHaveAttribute("srcdoc", /script-src 'none'/);
  await viewer.getByLabel("Zoom").press("End");
  await expect(viewer.locator("iframe")).toHaveAttribute("srcdoc", /zoom:3/);
  await viewer.press("Escape");
  await expect(diagramButton).toBeFocused();
});

test("PDF upload exposes page scope and unsupported-document failures without treating files as images", async ({ page }) => {
  await page.locator("input[type=file]").setInputFiles({ name: "reference.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-contract-fixture") });
  await expect(page.locator(".upload-list")).toContainText("2 of 2 pages processed");
  await expect(page.locator(".upload-list img")).toHaveCount(0);
  await expect(send(page)).toBeEnabled();
  await send(page).click();
  await expect(input(page)).toHaveValue("");
  const attachment = page.locator(".attachment-card").last();
  await expect(attachment).toContainText("PDF text excerpt");
  await expect(attachment.getByRole("link", { name: "reference.pdf" })).toHaveAttribute("download", "reference.pdf");
  await control(page, { failPdf: true });
  await page.locator("input[type=file]").setInputFiles({ name: "encrypted.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-contract-fixture") });
  await expect(page.locator(".upload-list").getByRole("alert")).toContainText("Encrypted PDFs are not supported.");
  await expect(send(page)).toBeDisabled();
});
