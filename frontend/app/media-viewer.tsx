"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useI18n } from "./locale-context";

export function MediaViewer({ diagram = false, children }: { diagram?: boolean; children: (zoom: number) => ReactNode }) {
  const { t } = useI18n();
  const dialog = useRef<HTMLDialogElement>(null);
  const [open, setOpen] = useState(false);
  const [zoom, setZoom] = useState(100);
  useEffect(() => { if (open && dialog.current && !dialog.current.open) dialog.current.showModal(); }, [open]);
  return <><button type="button" onClick={() => setOpen(true)}>{t(diagram ? "Enlarge diagram" : "Enlarge image")}</button>
    {open && createPortal(<dialog ref={dialog} className="media-viewer" aria-label={t(diagram ? "Enlarged diagram" : "Enlarged image")} onClose={() => { setOpen(false); setZoom(100); }}>
      <header><strong>{t(diagram ? "Enlarged diagram" : "Enlarged image")}</strong><button type="button" onClick={() => dialog.current?.close()}>{t("Close viewer")}</button></header>
      <div className="media-viewer-controls"><label>{t("Zoom")} <input type="range" min={50} max={300} step={25} value={zoom} onChange={event => setZoom(Number(event.target.value))} /><output>{zoom}{"%"}</output></label>
        <button type="button" onClick={() => setZoom(100)}>{t("Reset zoom")}</button></div>
      <div className="media-viewer-canvas" tabIndex={0} role="region" aria-label={t("Enlarged content")}>{children(zoom)}</div>
    </dialog>, document.body)}</>;
}
