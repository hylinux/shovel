import { isolatedDocument, validateDiagram } from "./message-format";

type Kind = "html" | "mermaid";
export class PreviewQueue {
  private tail: Promise<void> = Promise.resolve();
  private waiting = 0;
  private cache = new Map<string, string>();
  private chars = 0;
  private render: (source: string, kind: Kind) => Promise<string>;
  constructor(render: (source: string, kind: Kind) => Promise<string>) { this.render = render; }
  run(source: string, kind: Kind, signal: AbortSignal): Promise<string> {
    if (signal.aborted) return Promise.reject(new DOMException("Aborted", "AbortError"));
    if (this.waiting >= 32) return Promise.reject(new Error("Preview queue is full. View the source or try again."));
    const key = JSON.stringify([kind, source]);
    this.waiting++;
    const task = this.tail.then(async () => {
      if (signal.aborted) throw new DOMException("Aborted", "AbortError");
      const cached = this.cache.get(key);
      if (cached !== undefined) {
        this.cache.delete(key); this.cache.set(key, cached);
        return cached;
      }
      const document = await this.render(source, kind);
      if (signal.aborted) throw new DOMException("Aborted", "AbortError");
      const size = key.length + document.length;
      if (size <= 2_000_000) {
        while (this.cache.size && (this.cache.size >= 16 || this.chars + size > 2_000_000)) {
          const oldest = this.cache.entries().next().value!;
          this.cache.delete(oldest[0]); this.chars -= oldest[0].length + oldest[1].length;
        }
        this.cache.set(key, document); this.chars += size;
      }
      return document;
    }).finally(() => { this.waiting--; });
    // The returned task surfaces failures; only the scheduling tail recovers so later work can run.
    this.tail = task.then(() => {}, () => {});
    return task;
  }
}

let diagramId = 0;
async function renderDocument(source: string, kind: Kind): Promise<string> {
  const { default: purifier } = await import("dompurify");
  let body: string;
  if (kind === "html") {
    if (source.length > 100_000) throw new Error("HTML preview is limited to 100 KB.");
    body = purifier.sanitize(source, {
      USE_PROFILES: { html: true },
      FORBID_TAGS: ["form", "input", "button", "textarea", "select", "iframe", "object", "embed", "base", "meta", "link", "video", "audio"],
      FORBID_ATTR: ["href", "src", "srcset", "action", "formaction", "ping", "background"],
    });
  } else {
    validateDiagram(source);
    const { default: mermaid } = await import("mermaid");
    mermaid.initialize({
      startOnLoad: false, securityLevel: "strict", theme: "neutral",
      maxTextSize: 12_000, maxEdges: 100, suppressErrorRendering: true,
      flowchart: { htmlLabels: false }, htmlLabels: false,
      secure: ["securityLevel", "startOnLoad", "maxTextSize", "maxEdges", "suppressErrorRendering", "htmlLabels"],
    });
    const { svg } = await mermaid.render(`diagram${++diagramId}`, source);
    body = purifier.sanitize(svg, {
      USE_PROFILES: { svg: true, svgFilters: true },
      FORBID_TAGS: ["foreignObject", "a", "image", "script", "animate", "set"],
      FORBID_ATTR: ["href", "xlink:href"],
    });
  }
  return isolatedDocument(body);
}
export const previewQueue = new PreviewQueue(renderDocument);
