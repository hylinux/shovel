"use client";

import { useI18n } from "./locale-context";

import { useId, useState } from "react";
import { useAction } from "./api";
import { confirmedActions, confirmationCommand, serviceControlState, type Confirmation, type Permission, type PlatformCommand, type ServiceControlState } from "./platform-types";
import { Badge, Feedback } from "./ui";

export function usePlatformAction() {
  const action = useAction();
  const [pending, setPending] = useState<{ ticket: Confirmation; after?: () => void }>();
  const [serviceStatus, setServiceStatus] = useState<ServiceControlState>();
  function observe(result: unknown) {
    const status = serviceControlState(result);
    if (status) setServiceStatus(status);
  }
  async function send<T>(command: PlatformCommand, after?: () => void): Promise<T | undefined> {
    if (pending) return;
    if (confirmedActions.has(command.action)) {
      const ticket = await action.run<Confirmation>("/platform/commands", { ...command, prepare: true });
      if (ticket) setPending({ ticket, after });
      return;
    }
    const result = await action.run<T>("/platform/commands", command);
    if (result !== undefined) { observe(result); after?.(); action.setNotice("Server operation completed."); }
    return result;
  }
  async function confirm() {
    if (!pending) return;
    const result = await action.run<unknown>("/platform/commands", confirmationCommand(pending.ticket));
    if (result !== undefined) { observe(result); setPending(undefined); pending.after?.(); action.setNotice("Confirmed by the server."); }
  }
  return { ...action, send, pending, confirm, serviceStatus, dismiss: () => setPending(undefined), locked: action.busy || Boolean(pending) };
}
export type PlatformAction = ReturnType<typeof usePlatformAction>;

export function CommandFeedback({ action }: { action: PlatformAction }) {
  const { t, date } = useI18n();
  const heading = useId();
  return <>
    <Feedback error={action.error} notice={action.notice} />
    {action.pending && <section className="confirmation platform-confirmation" role="alertdialog" aria-labelledby={heading}>
      <h3 id={heading}>{t("Review ")}{action.pending.ticket.action}</h3>
      <p>{t("This confirmation is bound to the exact server state below. Authorization, policy, approval and version checks run again on submission. Expires ")}{date(action.pending.ticket.expires_at)}.</p>
      <pre tabIndex={0}>{JSON.stringify(action.pending.ticket.preview, null, 2)}</pre>
      <div className="button-row"><button type="button" className="primary" disabled={action.busy} onClick={action.confirm}>{t("Confirm ")}{action.pending.ticket.action}</button><button type="button" disabled={action.busy} onClick={action.dismiss}>{t("Cancel confirmation")}</button></div>
    </section>}
    {action.error && <p className="muted">{t("A lost response is not a failure. Inspect Activity or reload saved state before repeating an action. Expired confirmations require a new preview.")}</p>}
  </>;
}

export function JsonField({ label, value, onChange, help, rows = 5, disabled = false }: { label: string; value: string; onChange: (value: string) => void; help?: string; rows?: number; disabled?: boolean }) {
  const { t } = useI18n();
  const id = useId();
  return <div className="full"><label htmlFor={id}>{t(label)}</label><textarea id={id} aria-describedby={help ? `${id}-help` : undefined} className="json-editor" value={value} onChange={(event) => onChange(event.target.value)} rows={rows} disabled={disabled} spellCheck={false} />{help && <small id={`${id}-help`} className="muted">{t(help)}</small>}</div>;
}
export function PermissionPicker({ label, available, value, onChange, disabled }: { label: string; available: Permission[]; value: Permission[]; onChange: (value: Permission[]) => void; disabled?: boolean }) {
  const { t } = useI18n();
  const key = (permission: Permission) => JSON.stringify(permission);
  const selected = new Set(value.map(key));
  const all = [...available, ...value.filter((item) => !available.some((candidate) => key(candidate) === key(item)))];
  return <fieldset className="permission-picker"><legend>{t(label)}</legend>
    {!all.length && <p className="muted">{t("No permissions configured. This is not a grant editor.")}</p>}
    {all.map((permission) => {
      const granted = available.some((candidate) => key(candidate) === key(permission));
      return <label className="check-label" key={key(permission)}><input type="checkbox" checked={selected.has(key(permission))} disabled={disabled || (!granted && !selected.has(key(permission)))} onChange={(event) => onChange(event.target.checked ? [...value, permission] : value.filter((item) => key(item) !== key(permission)))} />{permission.action} · {permission.resource}{!granted && t(" (unavailable; remove before saving)")}</label>;
    })}
    <p className="muted">{t("Options reflect current server grants; the server always rechecks them.")}</p>
  </fieldset>;
}
export function RiskSummary({ risk, effect, approval }: { risk: string; effect: string; approval: string }) {
  const { t } = useI18n();
  return <div className="risk-summary" aria-label={t("Risk, side effect and approval")}><span>{t("Risk ")}<Badge state={risk} /></span><span>{t("Side effect ")}<Badge state={effect} /></span><span>{t("Approval ")}<Badge state={approval} /></span></div>;
}
