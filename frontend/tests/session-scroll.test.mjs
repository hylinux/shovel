import assert from "node:assert/strict";
import test from "node:test";
import { SessionScroll } from "../app/session-scroll.ts";
import { pastedText } from "../app/message-format.ts";

function viewport() {
  let top = 0;
  const element = {
    clientHeight: 200, scrollHeight: 1000, anchorY: 220,
    get scrollTop() { return top; },
    set scrollTop(value) { top = Math.max(0, Math.min(value, this.scrollHeight - this.clientHeight)); },
    getBoundingClientRect: () => ({ top: 0 }),
    querySelectorAll: () => [{ dataset: { messageId: "anchor" },
      getBoundingClientRect: () => ({ top: element.anchorY - top, bottom: element.anchorY + 200 - top }) }],
  };
  const notices = [];
  return { element, notices, scroll: new SessionScroll(element, (paused, unread) => notices.push({ paused, unread })) };
}

test("Streaming follows only near bottom, pauses on reading, signals updates and resumes explicitly", () => {
  const { element, scroll, notices } = viewport();
  scroll.update("latest", true, "first");
  assert.equal(element.scrollTop, 800);
  element.scrollHeight = 1200;
  scroll.update("latest", true, "second");
  assert.equal(element.scrollTop, 1000);
  element.scrollTop = 200;
  scroll.onScroll();
  element.scrollHeight = 1400;
  scroll.update("latest", true, "third");
  assert.equal(element.scrollTop, 200);
  assert.deepEqual(notices.at(-1), { paused: true, unread: true });
  element.anchorY += 80;
  element.scrollHeight += 80;
  scroll.resize();
  assert.equal(element.scrollTop, 280, "Image growth above the viewport retains its visible anchor");
  scroll.bottom();
  assert.equal(element.scrollTop, 1280);
  assert.deepEqual(notices.at(-1), { paused: false, unread: false });
  element.scrollTop -= 20;
  scroll.onScroll();
  element.scrollHeight += 50;
  scroll.resize();
  assert.equal(element.scrollTop, 1330);
});

test("History pages have independent reading positions and loading never applies a stale page's layout", () => {
  const { element, scroll } = viewport();
  scroll.update("latest", true, "first");
  element.scrollTop = 200;
  scroll.onScroll();
  scroll.navigate("100");
  scroll.update("100", false, "loading");
  scroll.resize();
  assert.equal(element.scrollTop, 200);
  scroll.update("100", true, "older");
  assert.equal(element.scrollTop, 0);
  element.scrollTop = 100;
  scroll.onScroll();
  scroll.navigate("latest");
  scroll.update("latest", true, "latest");
  assert.equal(element.scrollTop, 200);
  scroll.navigate("100");
  scroll.update("100", true, "older");
  assert.equal(element.scrollTop, 100);
});

test("Mixed clipboard text replaces the selected range, preserves surrounding text and reports truncation", () => {
  assert.deepEqual(pastedText("before AFTER", 7, 12, "pasted"), { value: "before pasted", cursor: 13, truncated: false });
  assert.deepEqual(pastedText("12345", 2, 4, "abcdef", 6), { value: "12abc5", cursor: 5, truncated: true });
  assert.deepEqual(pastedText("text", 2, 2, ""), { value: "text", cursor: 2, truncated: false });
});
