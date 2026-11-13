// Test-only static export and synthetic API. No Host, user stores or upstream models.
import { createServer } from "node:http";
import { readFile, stat } from "node:fs/promises";
import { resolve, extname, sep } from "node:path";
import { randomUUID } from "node:crypto";
import { setTimeout as delay } from "node:timers/promises";

const root = resolve("out");
await stat(resolve(root, "index.html"));
const clients = new Map();
const limits = { extensions: [".txt", ".png"], max_files: 5, max_file_bytes: 10485760,
  max_text_chars: 8000, max_image_pixels: 16000000, max_image_edge: 2048,
  max_input_images: 2, max_input_image_bytes: 10485760 };
function state(req, res) {
  let token = /(?:^|; )fixture=([a-f0-9-]+)/.exec(req.headers.cookie || "")?.[1];
  if (!token || !clients.has(token)) {
    token = randomUUID();
    res.setHeader("Set-Cookie", `fixture=${token}; HttpOnly; SameSite=Strict; Path=/`);
    const now = Math.floor(Date.now() / 1000);
    clients.set(token, { sessions: [{ id: "fixture-session", title: "Fixture Session", created_at: now, updated_at: now }],
      messages: Array.from({ length: 124 }, (_, index) => ({
        id: `message-${index + 1}`, conversation_id: "fixture-session", sequence: index + 1,
        role: index % 2 ? "assistant" : "user", state: "completed", created_at: now,
        content: `Synthetic message ${index + 1}\n\n${"Fixture paragraph for scroll verification. ".repeat(20)}`,
        attachments: [], citations: [], warnings: [],
      })), attachments: new Map(), uploads: [], previews: [], failSend: false, failBudget: false });
  }
  return clients.get(token);
}
async function body(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  return Buffer.concat(chunks);
}
function json(res, value, status = 200) {
  res.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store" });
  res.end(JSON.stringify(value));
}
async function api(req, res, url, data) {
  const path = url.pathname;
  if (path === "/__fixture/control") { Object.assign(data, JSON.parse(await body(req))); return json(res, {}); }
  if (path === "/__fixture/state") return json(res, { uploads: data.uploads, previews: data.previews, messages: data.messages.length });
  if (path.endsWith("/status")) return json(res, {
    service: "fixture", workspace_id: "browser-fixture", model_configured: true, memory_configured: true,
    model_name: "synthetic-only", embedding_configured: false,
  });
  if (path.endsWith("/user/profile")) return json(res, { revision: 0, nickname: "", gender: "", occupation: "", bio: "", avatar_url: "", updated_at: null });
  if (path.endsWith("/working-memory")) return json(res, { items: [] });
  if (path.endsWith("/conversations")) {
    if (req.method === "POST") {
      const session = { ...data.sessions[0], id: randomUUID(), title: "New fixture Session" };
      data.sessions.unshift(session);
      return json(res, session, 201);
    }
    return json(res, { items: data.sessions });
  }
  const identifier = /\/conversations\/([^/]+)/.exec(path)?.[1];
  if (path.endsWith("/messages")) {
    const before = Number(url.searchParams.get("before") || Infinity);
    const limit = Number(url.searchParams.get("limit") || 100);
    const items = data.messages.filter(item => item.conversation_id === identifier && item.sequence < before).slice(-limit);
    return json(res, { items, next_before: items.length === limit ? items[0].sequence : null });
  }
  if (path.endsWith("/budget-preview")) {
    const input = JSON.parse(await body(req));
    data.previews.push(input);
    if (data.failBudget) return json(res, { error: { message: "Synthetic budget failure" } }, 503);
    const attachments = (input.attachment_ids || []).map(id => data.attachments.get(id)).filter(Boolean);
    const images = attachments.filter(item => item.model_context === "image");
    const chars = 1612 + Array.from(input.content).length
      + attachments.filter(item => item.model_context === "text").reduce((sum, item) => sum + item.reading_scope.excerpt_chars + 150, 0);
    const imageBytes = images.reduce((sum, item) => sum + item.size, 0);
    return json(res, { estimated_chars: chars, max_chars: 5000, remaining_chars: Math.max(0, 5000 - chars),
      reserved_chars: 1012, images: images.length, max_images: 2, image_bytes: imageBytes, max_image_bytes: 10485760,
      history: { messages: data.messages.filter(item => item.conversation_id === identifier).length, chars: 10000, images: 0, image_bytes: 0 },
      exceeded: [...(chars > 5000 ? ["text"] : []), ...(images.length > 2 ? ["images"] : [])], estimate_only: true });
  }
  if (path.endsWith("/attachments")) {
    const items = [...data.attachments.values()].filter(item => item.session === identifier);
    return json(res, { items, limits, remaining: { pending_files: 20 - items.filter(item => !item.bound).length,
      session_files: 100 - items.length, session_bytes: 104857600, store_bytes: 1073741824 } });
  }
  const id = /\/attachments\/([^/]+)/.exec(path)?.[1];
  if (id) {
    if (req.method === "PUT") {
      const bytes = await body(req);
      data.uploads.push(id);
      const name = url.searchParams.get("filename");
      const image = name.endsWith(".png");
      await delay(700);
      if (!data.attachments.has(id)) data.attachments.set(id, {
        id, session: identifier, name, size: bytes.length, bytes, available: true, bound: false,
        media_type: image ? "image/png" : "text/plain", model_context: image ? "image" : "text",
        url: path, expires_at: Math.floor(Date.now() / 1000) + 86400,
        reading_scope: { original_bytes: bytes.length, stored_bytes: bytes.length, ...(image
          ? { width: 1, height: 1, original_width: 1, original_height: 1 }
          : { excerpt_chars: Math.min(8000, bytes.toString().length), truncated: bytes.toString().length > 8000 }) },
      });
      return json(res, data.attachments.get(id));
    }
    const attachment = data.attachments.get(id);
    if (!attachment || attachment.session !== identifier) return json(res, { error: { message: "Not found" } }, 404);
    if (req.method === "DELETE") {
      if (attachment.bound) return json(res, { error: { code: "AttachmentBound", message: "Attached file" } }, 409);
      data.attachments.delete(id);
      return json(res, { deleted: true });
    }
    res.writeHead(200, { "Content-Type": attachment.media_type });
    return res.end(attachment.bytes);
  }
  if (path === "/api/v1/agent") {
    const input = JSON.parse(await body(req));
    res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache, no-transform" });
    const emit = event => res.write(`data: ${JSON.stringify(event)}\n\n`);
    const { threadId, runId } = input;
    emit({ type: "RUN_STARTED", threadId, runId });
    if (data.failSend) { emit({ type: "RUN_ERROR", message: "Synthetic send failure" }); return res.end(); }
    const messageId = randomUUID();
    emit({ type: "TEXT_MESSAGE_START", messageId, role: "assistant" });
    let answer = "";
    for (let index = 0; index < 24; index++) {
      await delay(70);
      const delta = `Synthetic reply chunk ${index + 1}.\n\n`;
      answer += delta;
      emit({ type: "TEXT_MESSAGE_CONTENT", messageId, delta });
    }
    const attachments = (input.forwardedProps.attachment_ids || []).map(id => {
      const item = data.attachments.get(id);
      item.bound = true; item.expires_at = Math.floor(Date.now() / 1000) + 604800;
      return item;
    });
    const next = data.messages.length + 1;
    for (const [offset, role, content] of [[0, "user", input.messages.at(-1).content], [1, "assistant", answer]]) {
      data.messages.push({ id: randomUUID(), conversation_id: threadId, sequence: next + offset, role, content,
        state: "completed", created_at: Math.floor(Date.now() / 1000), citations: [], warnings: [],
        attachments: role === "user" ? attachments : [] });
    }
    emit({ type: "TEXT_MESSAGE_END", messageId });
    emit({ type: "RUN_FINISHED", threadId, runId });
    return res.end();
  }
  return json(res, { error: { message: `Unimplemented synthetic API: ${path}` } }, 404);
}
const types = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".json": "application/json",
  ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon", ".woff2": "font/woff2", ".txt": "text/plain" };
createServer(async (req, res) => {
  const url = new URL(req.url, "http://127.0.0.1:59992");
  if (url.pathname === "/__fixture/health") return json(res, { fixture: true });
  try {
    if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/__fixture/")) return await api(req, res, url, state(req, res));
    if (url.pathname === "/") state(req, res);
    const path = resolve(root, "." + decodeURIComponent(url.pathname === "/" ? "/index.html" : url.pathname));
    if (!path.startsWith(root + sep)) { res.writeHead(403); return res.end(); }
    const bytes = await readFile(path);
    res.writeHead(200, { "Content-Type": types[extname(path)] || "application/octet-stream" });
    res.end(bytes);
  } catch (error) {
    if (error.code === "ENOENT") { res.writeHead(404); res.end(); }
    else { console.error(error); if (!res.headersSent) res.writeHead(500); res.end(); }
  }
}).listen(59992, "127.0.0.1");
