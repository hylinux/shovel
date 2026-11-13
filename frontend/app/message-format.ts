import Papa from "papaparse";

export type MessageFormat = "markdown" | "text" | "json" | "csv" | "html" | "latex" | "mermaid" | "code";
export const messageFormats: MessageFormat[] = ["markdown", "text", "json", "csv", "html", "latex", "mermaid", "code"];

export function formattedMessage(source: string, format: MessageFormat): string {
  if (format === "markdown") return source;
  const fence = "`".repeat(Math.max(3, ...[...source.matchAll(/`+/g)].map(match => match[0].length + 1)));
  return `${fence}${format === "code" ? "" : format === "text" ? "text" : format}\n${source}\n${fence}`;
}

export function safeHttpUrl(value: string | undefined): string | undefined {
  if (!value || /[\u0000-\u0020\u007f\\]/.test(value) || !/^https?:\/\//i.test(value)) return;
  try {
    const url = new URL(value);
    if (url.username || url.password || !url.hostname) return;
    return url.href;
  } catch { return; }
}

export function attachmentPath(sessionId: string, id: string): string {
  return `/api/v1/conversations/${encodeURIComponent(sessionId)}/attachments/${encodeURIComponent(id)}`;
}

export function safeImageUrl(value: string | undefined, sessionId?: string): string | undefined {
  if (!value || !sessionId) return;
  const prefix = `/api/v1/conversations/${encodeURIComponent(sessionId)}/attachments/`;
  if (value.startsWith(prefix) && /^[a-f0-9-]{36}$/.test(value.slice(prefix.length))) return value;
}

export function csvTable(source: string): string[][] {
  if (source.length > 100_000) throw new Error("CSV preview is limited to 100 KB.");
  const result = Papa.parse<string[]>(source, { skipEmptyLines: true, preview: 201 });
  if (result.errors.some(error => error.code !== "UndetectableDelimiter")) throw new Error("Invalid CSV. Check the source.");
  if (result.data.length > 200 || result.data.some(row => row.length > 40)) throw new Error("CSV preview is limited to 200 rows and 40 columns.");
  return result.data;
}

export const previewPolicy = "default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; img-src 'none'; font-src 'none'; connect-src 'none'; frame-src 'none'; media-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'";

export function isolatedDocument(body: string): string {
  return `<!doctype html><html><head><meta http-equiv="Content-Security-Policy" content="${previewPolicy}"><meta name="referrer" content="no-referrer"><style>body{margin:12px;font:14px system-ui;overflow-wrap:anywhere;color:#203846}svg{max-width:100%;height:auto}table{border-collapse:collapse}td,th{border:1px solid #cad4dd;padding:6px}</style></head><body>${body}</body></html>`;
}

export function validateDiagram(source: string): void {
  if (source.length > 12_000 || source.split("\n").length > 200) throw new Error("Diagram preview is limited to 12,000 characters and 200 lines.");
  if (/%%\s*\{|^\s*---|^\s*(?:click|link|links)\s/im.test(source)) throw new Error("Diagram configuration and interactive links are disabled.");
  // Mermaid measures shapes in the host DOM before producing SVG: reject resource
  // syntax before rendering, not only after sanitizing its output.
  if (/<\s*\/?[a-z!]|(?:https?:|data:|blob:|file:|javascript:|\/\/)|\b(?:img|image|icon)\s*:|url\s*\(|@(?:import|font-face)/i.test(source)) {
    throw new Error("Diagram HTML, images and external resources are disabled.");
  }
}

export type EditorInsertion = { before: string; after?: string; placeholder: string };
export function pastedText(value: string, start: number, end: number, text: string, limit = 16000) {
  const available = Math.max(0, limit - (value.length - (end - start)));
  const inserted = text.slice(0, available);
  return { value: value.slice(0, start) + inserted + value.slice(end), cursor: start + inserted.length,
    truncated: inserted.length !== text.length };
}

export function insertMarkup(value: string, start: number, end: number, insertion: EditorInsertion) {
  const selection = value.slice(start, end) || insertion.placeholder;
  const replacement = insertion.before + selection + (insertion.after ?? "");
  return {
    value: value.slice(0, start) + replacement + value.slice(end),
    start: start + insertion.before.length,
    end: start + insertion.before.length + selection.length,
  };
}
