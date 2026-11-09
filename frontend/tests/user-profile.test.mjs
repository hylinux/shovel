import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";
import { translate } from "../app/i18n.ts";

const require = createRequire(import.meta.url);
function load(file, dependencies) {
  const { outputText } = ts.transpileModule(readFileSync(new URL(`../app/${file}`, import.meta.url), "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 },
  });
  const exports = {};
  new Function("require", "exports", outputText)(id => dependencies[id] ?? (id.startsWith("./") ? {} : require(id)), exports);
  return exports;
}
function elements(tree) {
  if (Array.isArray(tree)) return tree.flatMap(elements);
  if (!tree || typeof tree !== "object") return [];
  return [tree, ...elements(tree.props?.children)];
}
const original = { revision: 1, nickname: "River", gender: "", occupation: "Engineer", bio: "", avatar_url: "", updated_at: 1700000000 };

test("User message headings use the current literal nickname and avatar; assistant and empty profiles retain defaults", () => {
  for (const language of ["en", "zh-CN"]) {
    let profile = { ...original, nickname: "<Name & 昵称>", avatar_url: "/api/v1/user/profile/avatar/hash" };
    const { MessageHeading } = load("knowledge.tsx", {
      "./locale-context": { useI18n: () => ({ t: key => translate(key, language) }) },
      "./user-profile": {
        useUserProfile: () => ({ profile }),
        UserAvatar: ({ src }) => src ? createElement("img", { src, alt: "" }) : createElement("svg"),
      },
      "next/image": { default: props => createElement("img", { src: props.src, alt: props.alt, width: props.width, height: props.height }) },
      "./icon.svg": { default: "/icon.svg" },
    });
    const heading = () => renderToStaticMarkup(createElement(MessageHeading, { user: true }));
    assert.match(heading(), /&lt;Name &amp; 昵称&gt;/);
    assert.match(heading(), /src="\/api\/v1\/user\/profile\/avatar\/hash"/);
    profile = { ...original, nickname: "New nickname" };
    assert.match(heading(), /New nickname/);
    assert.doesNotMatch(heading(), /&lt;Name/);
    profile = undefined;
    assert.ok(heading().includes(translate("You", language)));
    assert.match(heading(), /<svg/);
    const assistant = renderToStaticMarkup(createElement(MessageHeading, { user: false }));
    assert.match(assistant, /src="\/icon.svg"/);
    assert.ok(assistant.includes(translate("Shovel", language)));
  }
});

function harness() {
  const states = new Map();
  let state, index;
  const context = { workspace: "workspace", profile: original, error: "", refresh() {}, accept(value) { context.profile = value; } };
  const requests = [];
  const query = { data: original, dataPath: "/user/profile?workspace=workspace", error: "", refresh() {} };
  let language = "en";
  const action = { busy: false, error: "", notice: "", setNotice(value) { action.notice = value; },
    async run(path, body, method) {
      requests.push({ path, body, method });
      return method === "GET" ? context.profile : { ...context.profile, ...body, revision: context.profile.revision + 1 };
    } };
  const react = {
    ...require("react"),
    useContext: () => context,
    useState(initial) {
      const slot = index++;
      const current = state;
      if (!(slot in current)) current[slot] = typeof initial === "function" ? initial() : initial;
      return [current[slot], value => { current[slot] = typeof value === "function" ? value(current[slot]) : value; }];
    },
    useRef(initial) {
      const slot = index++;
      if (!(slot in state)) state[slot] = { current: initial };
      return state[slot];
    },
  };
  const components = load("user-profile.tsx", {
    react,
    "./api": { segment: encodeURIComponent, useQuery: () => query, useAction: () => action },
    "./locale-context": { useI18n: () => ({ t: key => translate(key, language) }) },
  });
  function render(component, props = {}) {
    if (!states.has(component)) states.set(component, []);
    state = states.get(component); index = 0;
    return component(props);
  }
  const editor = elements(render(components.UserProfileSettings)).find(node => typeof node.type === "function" && node.type.name === "ProfileEditor");
  return { ...components, context, requests, query, action, render,
    language: value => { language = value; },
    editor: () => elements(render(editor.type, editor.props)),
  };
}

test("Profile editing preserves drafts across language/Tab renders, saves exact fields and explicitly clears avatars", async () => {
  const ui = harness();
  const field = id => ui.editor().find(node => node.props.id === id);
  field("profile-nickname").props.onChange({ target: { value: "用户昵称" } });
  field("profile-bio").props.onChange({ target: { value: "My own content, not a translation key." } });
  ui.language("zh-CN");
  ui.render(ui.UserProfileSettings);
  assert.equal(field("profile-nickname").props.value, "用户昵称");
  assert.equal(field("profile-bio").props.value, "My own content, not a translation key.");
  assert.ok(ui.editor().some(node => node.props.children === "保存用户资料"));
  await ui.editor().find(node => node.type === "form").props.onSubmit({ preventDefault() {} });
  assert.deepEqual(ui.requests[0], {
    path: "/user/profile", method: "PUT",
    body: { revision: 1, nickname: "用户昵称", gender: "", occupation: "Engineer", bio: "My own content, not a translation key." },
  });
  assert.equal(ui.context.profile.nickname, "用户昵称");
  assert.equal(ui.context.profile.revision, 2);
  const remove = ui.editor().find(node => node.props.children === translate("Remove avatar", "zh-CN"));
  remove.props.onClick();
  await ui.editor().find(node => node.type === "form").props.onSubmit({ preventDefault() {} });
  assert.equal(ui.requests[1].body.avatar_data, null);
});

test("Polling never replaces a newer acknowledged profile or leaks one workspace into another", () => {
  const ui = harness();
  const render = workspace => ui.render(ui.UserProfileProvider, { workspace, children: null }).props.value;
  assert.equal(render("workspace").profile.nickname, "River");
  render("workspace").accept({ ...original, revision: 3, nickname: "Latest" });
  assert.equal(render("workspace").profile.nickname, "Latest");
  ui.query.data = { ...original, revision: 2, nickname: "Older poll" };
  assert.equal(render("workspace").profile.nickname, "Latest");
  ui.query.data = { ...original, revision: 4, nickname: "External update" };
  assert.equal(render("workspace").profile.nickname, "External update");
  assert.equal(render("another-workspace").profile, undefined);
});

test("External changes disable stale saves without replacing drafts and reload requires explicit discard", async () => {
  const ui = harness();
  const input = ui.editor().find(node => node.props.id === "profile-nickname");
  input.props.onChange({ target: { value: "Unsaved" } });
  ui.context.profile = { ...original, revision: 2, nickname: "External" };
  assert.equal(ui.editor().find(node => node.props.id === "profile-nickname").props.value, "Unsaved");
  assert.equal(ui.editor().find(node => node.type === "button" && node.props.type === "submit").props.disabled, true);
  ui.editor().find(node => node.props.children === "Reload saved profile").props.onClick();
  assert.equal(ui.requests.length, 0);
  await ui.editor().find(node => node.props.children === "Discard edits and reload").props.onClick();
  assert.equal(ui.editor().find(node => node.props.id === "profile-nickname").props.value, "External");
});

test("Settings-managed Memory exposes its origin without offering conflicting edit, reject or forget controls", () => {
  for (const language of ["en", "zh-CN"]) {
    const managed = { id: "profile", memory: "user.name: River", kind: "semantic", scope: "user", state: "active",
      provenance: [{ source_kind: "user_settings", field: "nickname", profile_revision: 2 }] };
    const { MemoryManagement } = load("memories.tsx", {
      react: { useState: initial => [initial, () => {}] },
      "./locale-context": { useI18n: () => ({ t: key => translate(key, language) }) },
      "./api": { segment: encodeURIComponent, useAction: () => ({ busy: false }),
        useQuery: path => ({ dataPath: path, data: { items: [managed] }, refresh() {} }) },
    });
    const panel = elements(MemoryManagement({})).find(node => typeof node.type === "function" && node.type.name === "Memories");
    const nodes = elements(panel.type({ enabled: true, indexEnabled: true, sessionId: "" }));
    const row = elements(nodes.find(node => node.type === "article"));
    assert.equal(row.filter(node => node.type === "button").length, 0);
    assert.ok(row.some(node => node.props?.children === translate("Managed in General settings. Edit or clear the profile field there.", language)));
  }
});
