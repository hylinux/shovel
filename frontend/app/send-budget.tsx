"use client";

import { useEffect, useReducer, useState } from "react";
import { api, errorText, segment } from "./api";
import type { AgentMessageInput } from "./agent-client";
import { useI18n } from "./locale-context";

export type SendBudget = {
  estimated_chars: number; max_chars: number; remaining_chars: number; reserved_chars: number;
  images: number; max_images: number; image_bytes: number; max_image_bytes: number;
  history: { messages: number; chars: number; images: number; image_bytes: number };
  exceeded: ("text" | "images" | "image_bytes")[]; estimate_only: true;
};

export function useSendBudget(sessionId: string, input: AgentMessageInput | null, historyRevision: string) {
  const [revision, retry] = useReducer((value: number) => value + 1, 0);
  const body = input ? JSON.stringify(input) : "";
  const key = JSON.stringify([sessionId, body, historyRevision, revision]);
  const [result, setResult] = useState<{ key: string; data?: SendBudget; error?: string }>();
  useEffect(() => {
    if (!body) return;
    const request = new AbortController();
    const timer = setTimeout(() => {
      void api<SendBudget>(`/conversations/${segment(sessionId)}/budget-preview`, {
        method: "POST", body, signal: request.signal,
      }).then(data => {
        if (!request.signal.aborted) setResult({ key, data });
      }, error => {
        if (!request.signal.aborted) setResult({ key, error: errorText(error) });
      });
    }, 350);
    return () => { clearTimeout(timer); request.abort(); };
  }, [sessionId, body, key]);
  const current = result?.key === key ? result : undefined;
  return { data: current?.data, error: current?.error, pending: Boolean(body && !current),
    blocked: Boolean(body && (!current?.data || current.data.exceeded.length)), retry };
}

export function SendBudgetNotice({ budget }: { budget: ReturnType<typeof useSendBudget> }) {
  const { t } = useI18n();
  const data = budget.data;
  return <section className="attachment-budget" aria-label={t("Send budget estimate")} aria-busy={budget.pending}>
    {budget.pending && <small role="status">{t("Estimating send budget…")}</small>}
    {budget.error && <div role="alert"><small>{budget.error}</small>{" "}<button type="button" onClick={budget.retry}>{t("Retry budget estimate")}</button></div>}
    {data && <><small>{t("Estimated base text: {p0} / {p1} characters; {p2} reserved for framing and context.", {
      p0: data.estimated_chars, p1: data.max_chars, p2: data.reserved_chars,
    })}</small>
      <small>{t("Current images: {p0} / {p1}; {p2} / {p3} MiB.", {
        p0: data.images, p1: data.max_images, p2: (data.image_bytes / 1048576).toFixed(2), p3: (data.max_image_bytes / 1048576).toFixed(2),
      })}</small>
      <small>{t("Candidate history: {p0} messages, {p1} characters, {p2} images. Not all history will be included.", {
        p0: data.history.messages, p1: data.history.chars, p2: data.history.images,
      })}</small>
      {data.exceeded.length > 0 && <p className="warning" role="alert">{t("This input exceeds the budget. Shorten the message or remove attachments; no content is changed automatically.")}{" "}
        {data.exceeded.map(reason => t(reason === "text" ? "Text character limit exceeded." : reason === "images" ? "Image count limit exceeded." : "Image byte limit exceeded.")).join(" ")}</p>}
      <small>{t("Local estimate only: includes instructions, current profile, selected working context and prepared attachments. Recalled Memory, retrieval, tool schemas/results and final history selection can change the total. The server rechecks before every model call.")}</small>
    </>}
  </section>;
}
