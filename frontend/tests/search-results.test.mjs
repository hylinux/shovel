import assert from "node:assert/strict";
import test from "node:test";
import { orderSearchResults } from "../app/search-results.ts";

test("Top N results support both relevance directions without mutating retrieval order", () => {
  const items = [{ title: "Beta", score: 0.1 }, { title: "Alpha", score: 0.3 }];
  assert.deepEqual(orderSearchResults(items, "score_desc").map((item) => item.title), ["Alpha", "Beta"]);
  assert.deepEqual(orderSearchResults(items, "score_asc").map((item) => item.title), ["Beta", "Alpha"]);
  assert.equal(items[0].title, "Beta");
});

test("Title sorting supports both directions and uses the document URI when title is absent", () => {
  const items = [{ title: "", doc_uri: "Alpha", score: 0.1 }, { title: "Beta", score: 0.3 }];
  assert.deepEqual(orderSearchResults(items, "title_asc").map((item) => item.title), ["", "Beta"]);
  assert.deepEqual(orderSearchResults(items, "title_desc").map((item) => item.title), ["Beta", ""]);
});
