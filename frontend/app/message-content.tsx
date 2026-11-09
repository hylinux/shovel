"use client";

import { Children, isValidElement, memo, useEffect, useId, useState, type ReactNode } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeRaw from "rehype-raw";
import rehypeSanitize, { defaultSchema } from "rehype-sanitize";
import rehypeKatex from "rehype-katex";
import rehypeHighlight from "rehype-highlight";
import { useI18n } from "./locale-context";
import { csvTable, isolatedDocument, safeHttpUrl, safeImageUrl, validateDiagram } from "./message-format";

const schema = {
  ...defaultSchema,
  attributes: {
    ...defaultSchema.attributes,
    code: [["className", /^language-[\w-]+$/, "math-inline", "math-display"]],
  },
};

function nodeText(children: ReactNode): string {
  return Children.toArray(children).map(child => typeof child === "string" || typeof child === "number"
    ? String(child) : isValidElement<{ children?: ReactNode }>(child) ? nodeText(child.props.children) : "").join("");
}

export function CopyText({ text }: { text: string }) {
  const { t } = useI18n();
  const [notice, setNotice] = useState("");
  return <span className="copy-control"><button type="button" onClick={async () => {
    try { await navigator.clipboard.writeText(text); setNotice(t("Copied")); }
    catch { setNotice(t("Copy failed. Select and copy the source manually.")); }
  }}>{t("Copy source")}</button>{notice && <small role="status">{notice}</small>}</span>;
}

function ExternalLink({ href, children }: { href?: string; children: ReactNode }) {
  const { t } = useI18n();
  const url = safeHttpUrl(href);
  return url ? <a href={url} target="_blank" rel="noopener noreferrer nofollow" referrerPolicy="no-referrer" title={t("Opens external website: {p0}", { p0: url })}>{children}<span className="external-link-mark" aria-hidden="true">{" ↗"}</span></a> : <span>{children}</span>;
}

function ImageContent({ src, alt, sessionId }: { src?: string; alt?: string; sessionId?: string }) {
  const { t } = useI18n();
  const safe = safeImageUrl(src, sessionId);
  if (!safe) return <span className="blocked-image"><ExternalLink href={src}>{alt || t("External image (not loaded)")}</ExternalLink><small> {t("Remote images are not loaded automatically.")}</small></span>;
  return <a className="message-image-link" href={safe} target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer">
    {/* Uploaded raster images use the local API, not the Next.js image optimizer. */}
    {/* eslint-disable-next-line @next/next/no-img-element */}
    <img className="message-image" src={safe} alt={alt || t("Uploaded image")} loading="lazy" />
  </a>;
}

function IsolatedPreview({ source, kind }: { source: string; kind: "html" | "mermaid" }) {
  const { t } = useI18n();
  const id = useId().replace(/[^a-zA-Z0-9]/g, "");
  const [result, setResult] = useState<{ source: string; document?: string; error?: string }>();
  useEffect(() => {
    let active = true;
    async function render() {
      try {
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
          if (!active) return;
          mermaid.initialize({
            startOnLoad: false, securityLevel: "strict", theme: "neutral",
            maxTextSize: 12_000, maxEdges: 100, suppressErrorRendering: true,
            flowchart: { htmlLabels: false }, htmlLabels: false,
            secure: ["securityLevel", "startOnLoad", "maxTextSize", "maxEdges", "suppressErrorRendering", "htmlLabels"],
          });
          const { svg } = await mermaid.render(`diagram${id}`, source);
          body = purifier.sanitize(svg, {
            USE_PROFILES: { svg: true, svgFilters: true },
            FORBID_TAGS: ["foreignObject", "a", "image", "script", "animate", "set"],
            FORBID_ATTR: ["href", "xlink:href"],
          });
        }
        if (active) setResult({ source, document: isolatedDocument(body) });
      } catch (error) {
        if (active) setResult({ source, error: error instanceof Error ? error.message : "Preview failed. View the source." });
      }
    }
    void render();
    return () => { active = false; };
  }, [source, kind, id]);
  if (result?.source !== source) return <p role="status">{t("Preparing preview…")}</p>;
  if (result.error) return <p className="warning" role="status">{t("Preview unavailable. View the source.")} <span className="preserve">{t(result.error)}</span></p>;
  return <div className="isolated-preview">
    <iframe title={kind === "html" ? t("Isolated HTML preview") : t("Mermaid diagram")} sandbox="" referrerPolicy="no-referrer" srcDoc={result.document} />
    <small className="muted">{t("Isolated preview: scripts, navigation and remote resources are disabled.")}</small>
  </div>;
}

function CsvPreview({ source }: { source: string }) {
  const { t } = useI18n();
  let rows: string[][];
  try {
    rows = csvTable(source);
  } catch (error) {
    return <p className="warning">{t(error instanceof Error ? error.message : "Preview failed. View the source.")}</p>;
  }
  return <div className="message-table-scroll"><table><tbody>{rows.map((row, i) => <tr key={i}>{row.map((cell, j) => i === 0 ? <th scope="col" key={j}>{cell}</th> : <td key={j}>{cell}</td>)}</tr>)}</tbody></table></div>;
}

function CodeBlock({ children, className, streaming = false }: { children: ReactNode; className?: string; streaming?: boolean }) {
  const { t } = useI18n();
  const source = nodeText(children).replace(/\n$/, "");
  const language = /(?:^|\s)language-([\w-]+)/.exec(className || "")?.[1]?.toLowerCase() || "";
  const previewable = ["mermaid", "html", "csv", "latex", "tex"].includes(language);
  const [raw, setRaw] = useState(false);
  return <div className="message-code">
    <div className="message-code-toolbar"><span>{language || t("Code")}</span><div>{previewable && !streaming && <button type="button" aria-pressed={raw} onClick={() => setRaw(!raw)}>{raw ? t("Preview") : t("View source")}</button>}<CopyText text={source} /></div></div>
    {!raw && !streaming && language === "mermaid" ? <IsolatedPreview source={source} kind="mermaid" />
      : !raw && !streaming && language === "html" ? <IsolatedPreview source={source} kind="html" />
        : !raw && !streaming && language === "csv" ? <CsvPreview source={source} />
          : !raw && !streaming && ["latex", "tex"].includes(language) ? <Markdown remarkPlugins={[remarkMath]} rehypePlugins={[[rehypeKatex, { trust: false, strict: "error", maxExpand: 100, maxSize: 20 }]]}>{`$$\n${source}\n$$`}</Markdown>
            : <pre tabIndex={0}><code className={className}>{children}</code></pre>}
  </div>;
}

export const RichText = memo(function RichText({ content, sessionId, streaming = false }: { content: string; sessionId?: string; streaming?: boolean }) {
  const { t } = useI18n();
  // Long/untrusted responses remain accessible without unbounded parser work.
  if (content.length > 100_000) return <><p className="muted">{t("Large message: showing source only.")}</p><pre className="message-source" tabIndex={0}>{content}</pre></>;
  return <div className="rich-text"><Markdown
    remarkPlugins={[remarkGfm, remarkMath]}
    rehypePlugins={[rehypeRaw, [rehypeSanitize, schema], [rehypeHighlight, { detect: false, ignoreMissing: true }], [rehypeKatex, { trust: false, strict: "error", maxExpand: 100, maxSize: 20 }]]}
    urlTransform={(url, key) => key === "src" ? safeImageUrl(url, sessionId) || safeHttpUrl(url) || "" : safeHttpUrl(url) || ""}
    components={{
      a: ({ href, children }) => <ExternalLink href={href}>{children}</ExternalLink>,
      img: ({ src, alt }) => <ImageContent src={typeof src === "string" ? src : undefined} alt={alt} sessionId={sessionId} />,
      pre: ({ children }) => {
        const code = Children.toArray(children)[0];
        return isValidElement<{ children?: ReactNode; className?: string }>(code)
          ? <CodeBlock className={code.props.className} streaming={streaming}>{code.props.children}</CodeBlock> : <pre>{children}</pre>;
      },
      table: ({ children }) => <div className="message-table-scroll"><table>{children}</table></div>,
    }}
  >{content}</Markdown></div>;
});

export function MessageContent({ content, sessionId, streaming = false }: { content: string; sessionId?: string; streaming?: boolean }) {
  const { t } = useI18n();
  const [source, setSource] = useState(false);
  return <div className="message-content"><div className="message-display-toolbar">
    <button type="button" aria-pressed={source} onClick={() => setSource(!source)}>{source ? t("Preview") : t("View source")}</button>
    <CopyText text={content} />
  </div>{source ? <pre className="message-source" tabIndex={0}>{content}</pre> : <RichText content={content} sessionId={sessionId} streaming={streaming} />}</div>;
}
