"use client";

import type { RefObject } from "react";
import { insertMarkup, type EditorInsertion, type MessageFormat, messageFormats } from "./message-format";
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

export function ComposerTools({ value, onChange, textarea, disabled, format, onFormat, preview, onPreview }: {
  value: string; onChange: (value: string) => void; textarea: RefObject<HTMLTextAreaElement | null>; disabled: boolean;
  format: MessageFormat; onFormat: (format: MessageFormat) => void; preview: boolean; onPreview: () => void;
}) {
  const { t } = useI18n();
  function insert(insertion: EditorInsertion) {
    const element = textarea.current;
    if (!element) return;
    const result = insertMarkup(value, element.selectionStart, element.selectionEnd, insertion);
    if (result.value.length > 16000) return;
    onChange(result.value);
    requestAnimationFrame(() => { element.focus(); element.setSelectionRange(result.start, result.end); });
  }
  return <div className="composer-tools">
    <div className="formatting-tools" role="group" aria-label={t("Message formatting")}>{tools.map(tool => <button key={tool.label} type="button" title={t(tool.label)} aria-label={t(tool.label)} disabled={disabled || preview || format !== "markdown"} onClick={() => insert(tool.insertion)}>{tool.symbol}</button>)}</div>
    <div className="composer-modes"><select aria-label={t("Message format")} disabled={disabled} value={format} onChange={event => onFormat(event.target.value as MessageFormat)}>
      {messageFormats.map(item => <option key={item} value={item}>{t(formatLabels[item])}</option>)}
    </select><button type="button" aria-pressed={preview} disabled={disabled} onClick={onPreview}>{preview ? t("Edit") : t("Preview")}</button></div>
  </div>;
}
