import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";
import * as i18n from "../app/i18n.ts";
import { chinese, defaultLocale, isLocale, localizedDate, localeStorageKey, readLocale, translate, writeLocale } from "../app/i18n.ts";

test("The existing interface has Chinese translations for all static localized messages", () => {
  const missing = [];
  const directory = new URL("../app/", import.meta.url);
  for (const file of readdirSync(directory).filter(file => file.endsWith(".tsx"))) {
    const source = ts.createSourceFile(file, readFileSync(new URL(file, directory), "utf8"), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
    function visit(node) {
      if (ts.isCallExpression(node) && node.expression.getText(source) === "t" && node.arguments[0] && ts.isStringLiteral(node.arguments[0])) {
        const message = node.arguments[0].text.replace(/\s+/g, " ").trim();
        if (!Object.hasOwn(chinese, message)) missing.push(`${file}: ${message}`);
      }
      ts.forEachChild(node, visit);
    }
    visit(source);
  }
  assert.deepEqual(missing, []);
});

test("Visible static labels cannot bypass localization, except language names and code examples", () => {
  const missing = [];
  const directory = new URL("../app/", import.meta.url);
  for (const file of readdirSync(directory).filter(file => file.endsWith(".tsx"))) {
    const source = ts.createSourceFile(file, readFileSync(new URL(file, directory), "utf8"), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
    function visit(node) {
      if (ts.isJsxText(node) && /[A-Za-z]/.test(node.text)) {
        let protectedText = false;
        for (let parent = node.parent; parent; parent = parent.parent) {
          if (ts.isJsxElement(parent) && ["code", "pre"].includes(parent.openingElement.tagName.getText(source))) protectedText = true;
        }
        if (!protectedText && !(file === "locale-context.tsx" && ["Language / 语言", "English"].includes(node.text.trim()))) missing.push(`${file}: ${node.text.trim()}`);
      }
      if (ts.isJsxAttribute(node) && ["aria-label", "placeholder", "title", "label", "help", "alt"].includes(node.name.getText(source)) && node.initializer && ts.isStringLiteral(node.initializer) && /[A-Za-z]/.test(node.initializer.text) && !/^(?:https?:\/\/|[A-Za-z]:\\)/.test(node.initializer.text)) {
        missing.push(`${file}: ${node.name.getText(source)}`);
      }
      ts.forEachChild(node, visit);
    }
    visit(source);
  }
  assert.deepEqual(missing, []);
});

test("Translations preserve placeholders and interpolate dynamic notices without changing IDs", () => {
  for (const [message, translated] of Object.entries(chinese)) {
    assert.deepEqual((translated.match(/\{p\d+\}/g) || []).sort(), (message.match(/\{p\d+\}/g) || []).sort(), message);
  }
  assert.equal(translate("Scan progress for {p0}", "zh-CN", { p0: "Research notes" }), "Research notes 的扫描进度");
  assert.equal(translate("Index rebuild queued: op_123. Track it in Background tasks.", "zh-CN"), "索引重建已排队：op_123。请在后台任务中跟踪。");
  assert.equal(translate("Recovered op_123: queued.", "zh-CN"), "已恢复 op_123：排队中。");
  assert.equal(translate("resource.create queued. Follow it in Background tasks.", "zh-CN"), "创建资源已排队。请在后台任务中跟踪。");
  assert.equal(translate("local_write", "zh-CN"), "本地写入");
  assert.equal(translate("First page of {p0}", "en", { p0: "scan jobs" }), "First page of scan jobs");
  assert.equal(translate("Unknown backend detail: custom_payload", "zh-CN"), "Unknown backend detail: custom_payload");
  for (const message of ["constructor", "toString", "__proto__"]) assert.equal(translate(message, "zh-CN"), message);
});

test("Language preferences persist and invalid or absent saved choices keep the English default", () => {
  const values = new Map();
  const storage = { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) };
  assert.equal(readLocale(storage), defaultLocale);
  writeLocale(storage, "zh-CN");
  assert.equal(values.get(localeStorageKey), "zh-CN");
  assert.equal(readLocale(storage), "zh-CN");
  writeLocale(storage, "en");
  assert.equal(readLocale(storage), "en");
  for (const value of ["zh", "fr", "", null]) {
    values.set(localeStorageKey, value);
    assert.equal(isLocale(value), false);
    assert.equal(readLocale(storage), "en");
  }
});

test("Storage failures are not silently treated as successful preference persistence", () => {
  const unavailable = {
    getItem() { throw new Error("Storage unavailable"); },
    setItem() { throw new Error("Storage unavailable"); },
  };
  assert.throws(() => readLocale(unavailable), /Storage unavailable/);
  assert.throws(() => writeLocale(unavailable, "zh-CN"), /Storage unavailable/);
});

test("Translated select options retain explicit wire values and form identities are not translated", () => {
  const failures = [];
  const directory = new URL("../app/", import.meta.url);
  for (const file of readdirSync(directory).filter(file => file.endsWith(".tsx"))) {
    const source = ts.createSourceFile(file, readFileSync(new URL(file, directory), "utf8"), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
    function visit(node) {
      if (ts.isJsxOpeningElement(node) && node.tagName.getText(source) === "option") {
        if (!node.attributes.properties.some(attribute => ts.isJsxAttribute(attribute) && attribute.name.getText(source) === "value")) failures.push(`${file}: implicit option value`);
      }
      if (ts.isJsxAttribute(node) && ["name", "id", "key", "href", "value", "defaultValue"].includes(node.name.getText(source)) && node.initializer) {
        const attributes = node.parent;
        const displayOnly = attributes.properties.some(attribute => ts.isJsxAttribute(attribute) && attribute.name.getText(source) === "readOnly");
        function checkTranslation(child) {
          if (ts.isCallExpression(child) && child.expression.getText(source) === "t" && !displayOnly) failures.push(`${file}: translated ${node.name.getText(source)}`);
          ts.forEachChild(child, checkTranslation);
        }
        checkTranslation(node.initializer);
      }
      ts.forEachChild(node, visit);
    }
    visit(source);
  }
  assert.deepEqual(failures, []);
});

test("Localized timestamps remain Unix seconds and preserve invalid-date handling", () => {
  const timestamp = 1700000000;
  const instant = new Date(timestamp * 1000);
  assert.equal(localizedDate(timestamp, "zh-CN"), instant.toLocaleString("zh-CN"));
  assert.equal(localizedDate(timestamp, "en"), instant.toLocaleString("en-US"));
  for (const value of [undefined, null, NaN, Infinity, 1e20]) assert.equal(localizedDate(value, "zh-CN"), "—");
});

test("Day precision formats a UTC calendar date without a clock and default timestamps retain their format", () => {
  const timestamp = Date.UTC(2026, 10, 8, 23, 30) / 1000;
  const instant = new Date(timestamp * 1000);
  for (const [language, tag] of [["en", "en-US"], ["zh-CN", "zh-CN"]]) {
    assert.equal(localizedDate(timestamp, language, "day"), instant.toLocaleDateString(tag, { timeZone: "UTC" }));
    assert.equal(localizedDate(timestamp, language), instant.toLocaleString(tag));
    assert.equal(localizedDate(timestamp, language, "instant"), localizedDate(timestamp, language));
    assert.ok(!localizedDate(timestamp, language, "day").includes(":"));
    for (const value of [null, undefined, NaN, Infinity, 1e20]) assert.equal(localizedDate(value, language, "day"), "—");
  }
});

test("The locale provider keeps server rendering in English without accessing browser storage", () => {
  const source = readFileSync(new URL("../app/locale-context.tsx", import.meta.url), "utf8");
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 },
  });
  const exports = {};
  const require = createRequire(import.meta.url);
  new Function("require", "exports", outputText)(id => id === "./i18n" ? i18n : require(id), exports);
  const { LocaleProvider, LanguageSelector } = exports;
  const html = renderToStaticMarkup(createElement(LocaleProvider, null,
    createElement(LanguageSelector),
    createElement(LanguageSelector, { id: "settings-interface-language" }),
  ));
  assert.match(html, /aria-label="Language"/);
  assert.match(html, /value="en"[^>]*selected=""/);
  assert.match(html, /value="zh-CN"/);
  for (const id of ["interface-language", "settings-interface-language"]) {
    assert.equal(html.match(new RegExp(`id="${id}"`, "g"))?.length, 1);
    assert.match(html, new RegExp(`<label for="${id}">Language / 语言</label>`));
  }
  assert.throws(() => renderToStaticMarkup(createElement(LanguageSelector)), /requires LocaleProvider/);
});
