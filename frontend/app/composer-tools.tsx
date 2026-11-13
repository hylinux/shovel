"use client";

import { useState, type RefObject } from "react";
import { codeLanguages, formatJson, insertMarkup, type EditorInsertion, type MessageFormat, messageFormats } from "./message-format";
import { useI18n } from "./locale-context";

const tools: { label: string; symbol: string; insertion: EditorInsertion }[] = [
  { label: "Bold", symbol: "B", insertion: { before: "**", after: "**", placeholder: "text" } },
  { label: "Italic", symbol: "I", insertion: { before: "*", after: "*", placeholder: "text" } },
  { label: "Strikethrough", symbol: "S", insertion: { before: "~~", after: "~~", placeholder: "text" } },
  { label: "Heading", symbol: "H", insertion: { before: "\n## ", after: "\n", placeholder: "Heading" } },
  { label: "Bulleted list", symbol: "•", insertion: { before: "\n- ", after: "\n", placeholder: "item" } },
  { label: "Numbered list", symbol: "1.", insertion: { before: "\n1. ", after: "\n", placeholder: "item" } },
  { label: "Quote", symbol: "❞", insertion: { before: "\n> ", after: "\n", placeholder: "text" } },
  { label: "Link", symbol: "↗", insertion: { before: "[", after: "](https://example.com)", placeholder: "text" } },
  { label: "Table", symbol: "▦", insertion: { before: "\n", after: "\n", placeholder: "| Column | Column |\n| --- | --- |\n| Value | Value |" } },
  { label: "Code block", symbol: "</>", insertion: { before: "\n```\n", after: "\n```\n", placeholder: "code" } },
  { label: "Formula", symbol: "∑", insertion: { before: "\n$$\n", after: "\n$$\n", placeholder: "E = mc^2" } },
  { label: "Mermaid diagram", symbol: "◇", insertion: { before: "\n```mermaid\n", after: "\n```\n", placeholder: "flowchart LR\n  A --> B" } },
];
const formatLabels: Record<MessageFormat, string> = { markdown: "Markdown", text: "Plain text", json: "JSON", csv: "CSV", html: "HTML", latex: "LaTeX", mermaid: "Mermaid", code: "Code" };

export function ComposerTools({ value, onChange, textarea, disabled, format, onFormat, language, onLanguage, preview, onPreview }: {
  value: string; onChange: (value: string) => void; textarea: RefObject<HTMLTextAreaElement | null>; disabled: boolean;
  language: string; onLanguage: (language: string) => void;
  format: MessageFormat; onFormat: (format: MessageFormat) => void; preview: boolean; onPreview: () => void;
}) {
  const { t } = useI18n();
  const [notice, setNotice] = useState<{ source: string; text: string; error: boolean }>();
  function insert(insertion: EditorInsertion) {
    const element = textarea.current;
    if (!element) return;
    const result = insertMarkup(value, element.selectionStart, element.selectionEnd, insertion);
    if (result.value.length > 16000) { setNotice({ source: value, text: "Formatting would exceed 16,000 characters. Shorten the message first.", error: true }); return; }
    setNotice(undefined);
    onChange(result.value);
    requestAnimationFrame(() => { element.focus(); element.setSelectionRange(result.start, result.end); });
  }
  function json(pretty: boolean) {
    try {
      const result = formatJson(value);
      if (pretty && result.length > 16000) throw new Error("Formatting would exceed 16,000 characters. Shorten the message first.");
      if (pretty) onChange(result);
      setNotice({ source: pretty ? result : value, text: "JSON is valid.", error: false });
    } catch (error) {
      setNotice({ source: value, text: error instanceof Error ? error.message : "Invalid JSON. Check the source.", error: true });
    }
  }
  return <><div className="composer-tools">
    <div className="formatting-tools" role="group" aria-label={t("Message formatting")}>{tools.map(tool => <button key={tool.label} type="button" title={t(tool.label)} aria-label={t(tool.label)} disabled={disabled || preview || format !== "markdown"} onClick={() => insert(tool.label === "Code block" ? { ...tool.insertion, before: `\n\`\`\`${language}\n` } : tool.insertion)}>{tool.symbol}</button>)}</div>
    <div className="composer-modes"><select aria-label={t("Message format")} disabled={disabled} value={format} onChange={event => onFormat(event.target.value as MessageFormat)}>
      {messageFormats.map(item => <option key={item} value={item}>{t(formatLabels[item])}</option>)}
    </select>{(format === "code" || format === "markdown") && <select aria-label={t("Code language")} value={language} disabled={disabled || preview} onChange={event => onLanguage(event.target.value)}>
      {codeLanguages.map(item => <option key={item} value={item}>{item || t("Unspecified language")}</option>)}
    </select>}{format === "json" && <><button type="button" disabled={disabled || preview} onClick={() => json(false)}>{t("Validate JSON")}</button><button type="button" disabled={disabled || preview} onClick={() => json(true)}>{t("Format JSON")}</button></>}
    <button type="button" aria-pressed={preview} aria-controls="composer-preview" disabled={disabled} onClick={onPreview}>{preview ? t("Edit") : t("Preview")}</button></div>
  </div>{notice?.source === value && <small className={`${notice.error ? "warning" : "muted"} composer-format-notice`} role={notice.error ? "alert" : "status"}>{t(notice.text)}</small>}</>;
}
