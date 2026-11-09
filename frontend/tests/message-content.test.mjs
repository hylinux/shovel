import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";
import * as format from "../app/message-format.ts";

const require = createRequire(import.meta.url);
const source = readFileSync(new URL("../app/message-content.tsx", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
});
const components = {};
new Function("require", "exports", outputText)(id => {
  if (id === "./message-format") return format;
  if (id === "./locale-context") return { useI18n: () => ({ t: (key, values) => key.replace(/\{p0\}/g, values?.p0 ?? "") }) };
  return require(id);
}, components);
const render = (content, sessionId = "session") => renderToStaticMarkup(createElement(components.RichText, { content, sessionId }));

test("Messages render Markdown, GFM tables/lists, safe inline HTML and highlighted code", () => {
  const html = render('# Heading\n\n**bold** and *italic* and ~~removed~~\n\n- item\n\n| A | B |\n| - | - |\n| x | y |\n\n<strong>HTML</strong>\n\n```json\n{"name":"value","count":2}\n```');
  for (const fragment of ["<h1>Heading</h1>", "<strong>bold</strong>", "<em>italic</em>", "<del>removed</del>", "<li>item</li>", "<th>A</th>", "<td>y</td>", "<strong>HTML</strong>", "hljs-attr", "Copy source"]) assert.ok(html.includes(fragment), fragment);
});

test("Message rendering strips executable HTML and rejects unsafe link protocols and remote image loading", () => {
  const html = render('<script>alert(1)</script><iframe src="https://evil.example"></iframe><img src=x onerror=alert(1)><a href="javascript:alert(1)">bad</a>\n\n[x](data:text/html,test)\n\n![tracker](https://evil.example/tracker.png)\n\n[ok](https://example.com/a)');
  for (const forbidden of ["<script", "<iframe", "onerror=", "javascript:", "data:text/html", '<img ', 'src="https://evil']) assert.ok(!html.includes(forbidden), forbidden);
  assert.ok(html.includes('href="https://example.com/a"'));
  assert.ok(html.includes('rel="noopener noreferrer nofollow"'));
  assert.ok(html.includes('referrerPolicy="no-referrer"') || html.includes('referrerpolicy="no-referrer"'));
  assert.ok(html.includes("Remote images are not loaded automatically."));
});

test("Only current-Session attachment URLs render images; SVG/data/file/relative URLs cannot be embedded", () => {
  const id = "550e8400-e29b-41d4-a716-446655440000";
  const url = format.attachmentPath("session", id);
  assert.ok(render(`![upload](${url})`).includes(`<img class="message-image" src="${url}"`));
  assert.ok(!render(`![upload](${url})`, "other-session").includes("<img "));
  for (const value of ["data:image/svg+xml,test", "file:///C:/test.png", "/api/v1/status", "//evil.example/x", "javascript:alert(1)"]) {
    assert.equal(format.safeImageUrl(value, "session"), undefined);
  }
});

test("LaTeX renders with bounded expansion and without trusted URL/HTML commands", () => {
  const html = render("Inline $x^2$ and display:\n\n$$\nE=mc^2\n$$");
  assert.ok(html.includes('class="katex"'));
  assert.ok(html.includes("katex-display"));
  const malicious = render("$\\href{javascript:alert(1)}{click}$\n\n$\\includegraphics{https://evil.example/x}$");
  assert.ok(!malicious.includes("<a "));
  assert.ok(!malicious.includes("<img "));
});

test("CSV previews handle quoted multiline values as text, never as HTML or spreadsheet formulas", () => {
  assert.deepEqual(format.csvTable('Name,Note\n"User","two\nlines"\n'), [["Name", "Note"], ["User", "two\nlines"]]);
  const html = render('```csv\nName,Value\n"<img src=x onerror=alert(1)>","=IMPORTXML(""https://evil.example"")"\n```');
  assert.ok(html.includes("&lt;img"));
  assert.ok(!html.includes("<img "));
  assert.ok(html.includes("=IMPORTXML"));
  assert.throws(() => format.csvTable('a,b\n"unfinished'), /Invalid CSV/);
  assert.throws(() => format.csvTable("a\n".repeat(202)), /200 rows/);
});

test("Code/source formatting cannot break out of a fence and toolbar insertion preserves selections", () => {
  const original = '```\n<script>alert(1)</script>\n```';
  const fenced = format.formattedMessage(original, "html");
  assert.ok(fenced.startsWith("````html\n"));
  assert.ok(fenced.endsWith("\n````"));
  const insertion = format.insertMarkup("hello world", 6, 11, { before: "**", after: "**", placeholder: "text" });
  assert.deepEqual(insertion, { value: "hello **world**", start: 8, end: 13 });
  assert.equal(format.formattedMessage(" \nraw\n ", "markdown"), " \nraw\n ");
});

test("External links, diagrams, isolated frames and parser work have explicit safety boundaries", () => {
  for (const url of ["javascript:alert(1)", "data:text/html,x", "file:///C:/x", "//evil.example", "https://user:pass@evil.example", "https://example.com\\@evil.example", "https://example.com\n"]) assert.equal(format.safeHttpUrl(url), undefined);
  assert.equal(format.safeHttpUrl("https://example.com/a?q=b"), "https://example.com/a?q=b");
  for (const diagram of ['%%{init: {"securityLevel":"loose"}}%%\nflowchart LR\nA-->B', "---\nconfig:\n securityLevel: loose\n---\nflowchart LR", "flowchart LR\nclick A \"https://evil.example\"", "x".repeat(12001)]) assert.throws(() => format.validateDiagram(diagram));
  for (const diagram of ['flowchart LR\n A@{ img: "/api/v1/status" }', 'flowchart LR\n A["<img src=x>"]', 'flowchart LR\n classDef a fill:url(/private)', 'flowchart LR\n A@{ icon: "fa:house" }']) assert.throws(() => format.validateDiagram(diagram), /external resources/);
  format.validateDiagram("flowchart LR\n A --> B");
  assert.ok(format.isolatedDocument("<p>Test</p>").includes("default-src 'none'; script-src 'none'"));
  assert.ok(render("x".repeat(100001)).includes("Large message: showing source only."));
});
