"use client";

import { useState, type FormEvent } from "react";
import { errorText, useAction, useQuery, type RuntimeConfiguration, type RuntimeConfigField } from "./api";
import { useI18n } from "./locale-context";
import { configurationChanges, configurationDirty, configurationDraft, type SecretAction, type SecretActions } from "./runtime-configuration-model";
import { Feedback } from "./ui";

const groups = [
  ["", "Service & logging"], ["model", "Main Agent model"], ["agent", "Agent behavior"],
  ["performance", "Performance"], ["database", "Database"], ["zvec", "Knowledge index"],
  ["embedding", "Knowledge embedding"], ["memory", "Native Memory"],
  ["memory.llm", "Memory LLM"], ["memory.embedder", "Memory embedding"],
] as const;

export function RuntimeConfigurationEditor() {
  const { t } = useI18n();
  const query = useQuery<RuntimeConfiguration>("/runtime/configuration");
  return <section className="settings-card" aria-labelledby="runtime-editor-heading">
    <header className="settings-card-header"><div><h2 id="runtime-editor-heading">{t("Edit runtime configuration")}</h2><p className="muted">{t("File settings, active process settings and runtime health are separate. Saving does not restart running services.")}</p></div></header>
    <Feedback error={query.error} retry={query.refresh} />
    {query.loading && <p role="status">{t("Loading configuration…")}</p>}
    {query.data && <ConfigurationForm initial={query.data} />}
  </section>;
}

export function ConfigurationForm({ initial }: { initial: RuntimeConfiguration }) {
  const { t } = useI18n();
  const [config, setConfig] = useState(initial);
  const [draft, setDraft] = useState(() => configurationDraft(initial.items));
  const [secrets, setSecrets] = useState<SecretActions>({});
  const [confirmReload, setConfirmReload] = useState(false);
  const [inputError, setInputError] = useState("");
  const action = useAction();
  const dirty = configurationDirty(config.items, draft, secrets);
  function accept(result: RuntimeConfiguration) {
    setConfig(result); setDraft(configurationDraft(result.items)); setSecrets({});
    setConfirmReload(false); setInputError("");
  }
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setInputError("");
    let changes;
    try { changes = configurationChanges(config.items, draft, secrets); }
    catch (error) { setInputError(errorText(error)); return; }
    const result = await action.run<RuntimeConfiguration>("/runtime/configuration", { revision: config.revision, changes }, "PUT");
    if (result) {
      accept(result);
      action.setNotice("Configuration saved. Check pending settings and environment overrides before restarting.");
    }
  }
  async function reload() {
    const result = await action.run<RuntimeConfiguration>("/runtime/configuration", undefined, "GET");
    if (result) accept(result);
  }
  async function check() {
    const result = await action.run<RuntimeConfiguration>("/runtime/configuration/check", { revision: config.revision });
    if (result) {
      setConfig(result);
      action.setNotice(result.applied ? "Application verified: the active process matches the effective configuration." : "Not applied: a full Host restart is still required. Restart Agent is not sufficient.");
    }
  }
  function edit(key: string, value: string) {
    setDraft((current) => ({ ...current, [key]: value })); setConfirmReload(false);
  }
  function display(field: RuntimeConfigField, active = false) {
    if (field.secret) return t((active ? field.active_configured : field.effective_configured) ? "Key configured" : "No key");
    const value = active ? field.active_value : field.effective_value;
    return value === null || value === "" ? t("Not set") : String(value);
  }
  return <>
    <p className="mcp-path"><code>{config.path}</code></p>
    <p className="settings-note">{t("Only the current profile settings.toml can be edited. Storage and workspace paths are read-only; hidden settings are preserved. Saving rewrites TOML formatting and comments.")}</p>
    {!config.editable && <p className="warning" role="alert">{t("This Host uses different profile stores. Configuration editing is disabled; use its local configuration.")}</p>}
    <p role="status"><span className={`badge ${config.applied ? "good" : ""}`}>{t(config.applied ? "Active configuration verified" : "Saved settings pending restart")}</span> {dirty && <span className="badge">{t("Unsaved changes")}</span>}</p>
    <p className="muted">{t("All changes require a full Host restart; none are hot-applied. Stop Shovel, then rerun shovel run dev or shovel run prd with the same profile. Development frontend changes require restarting both processes. Check application after reconnecting.")}</p>
    <p className="muted">{t("Listener changes take effect after restart. In production, reopen the saved http_host/http_port; in development, reopen dev_frontend_port and restart the backend proxy with dev_http_port. Only loopback addresses are allowed. agent_port is legacy metadata, not a separate Agent listener.")}</p>
    <p className="settings-note">{t("Chat tested scope: Azure OpenAI, DeepSeek, QianWen and Ollama. Embedding adapters: Azure OpenAI, QianWen (explicit embedding endpoint) and Ollama; chat testing does not verify embedding compatibility. Existing other providers are preserved, not certified.")}</p>
    <p className="settings-note">{t("Empty Azure keys select non-interactive Azure CLI authentication. Memory LLM follows the Main Agent when its provider and model are empty; Memory embedding is independent. Keeping a key never sends its saved value to this browser.")}</p>
    <p className="warning">{t("Changing embedding identity, dimensions or Memory collection requires a controlled vector rebuild. This editor blocks those changes while an index contains files; it never deletes or migrates data.")}</p>
    <form onSubmit={save} className="runtime-config-form">
      <fieldset disabled={action.busy || !config.editable}>
        {groups.map(([section, title]) => <details className="settings-details" key={section} open={section === ""}>
          <summary>{t(title)}</summary>
          <div className="runtime-config-grid">{config.items.filter((field) => field.section === section).map((field) => {
            const id = `runtime-${field.key}`;
            const fieldError = action.error.startsWith(`${field.key}:`) ? action.error.slice(field.key.length + 1).trim() : "";
            return <div className="runtime-config-field" key={field.key}>
              <label htmlFor={id}>{t(field.key.split(".").at(-1) || field.key)} <code>{field.key}</code></label>
              {field.readonly ? <input id={id} value={String(field.value ?? "")} readOnly />
                : field.secret ? <div className="runtime-secret">
                  <select id={id} value={secrets[field.key] || "keep"} onChange={(event) => {
                    setSecrets((current) => ({ ...current, [field.key]: event.target.value as SecretAction }));
                    edit(field.key, "");
                  }}>
                    <option value="keep">{t("Keep saved key")}</option><option value="replace">{t("Replace key")}</option><option value="clear">{t("Clear saved key")}</option>
                  </select>
                  <span className="muted">{t(field.configured ? "Key configured" : "No key")}</span>
                  {secrets[field.key] === "replace" && <><label htmlFor={`${id}-replacement`}>{t("New key")}</label><input id={`${id}-replacement`} type="password" autoComplete="new-password" value={draft[field.key] || ""} required onChange={(event) => edit(field.key, event.target.value)} /></>}
                </div>
                  : field.type === "boolean" ? <select id={id} value={draft[field.key]} onChange={(event) => edit(field.key, event.target.value)}><option value="true">{t("Enabled")}</option><option value="false">{t("Disabled")}</option></select>
                    : field.choices ? <select id={id} value={draft[field.key]} onChange={(event) => edit(field.key, event.target.value)}>{field.choices.map((choice) => <option key={String(choice)} value={String(choice)}>{choice === "" ? t("Not set") : String(choice)}</option>)}</select>
                      : <input id={id} type={field.type === "number" ? "number" : "text"} value={draft[field.key] ?? ""} min={field.minimum ?? undefined} max={field.maximum ?? undefined} step={field.integer ? 1 : "any"} required={field.type === "number" && !field.nullable} onChange={(event) => edit(field.key, event.target.value)} />}
              <small className="muted">{t("Active process")}: <code>{display(field, true)}</code></small>
              {field.environment.length > 0 && <small className="warning">{t("Environment override")}: <code>{field.environment.join(", ")}</code> · {t("Effective after restart")}: <code>{display(field)}</code></small>}
              {field.pending && <small className="warning">{t("Pending full Host restart")}</small>}
              {fieldError && <small className="warning" role="alert">{t(fieldError)}</small>}
              {field.key === "dev_http_host" && <small className="muted">{t("The development proxy supports 127.0.0.1 or localhost.")}</small>}
              {field.key === "agent.enable_python_extensions" && <small className="warning">{t("Disabled by default. Enabling Python extensions runs trusted local code with this user's permissions. Do not enable untrusted workspace code.")}</small>}
              {field.nullable && <small className="muted">{t("Leave empty to omit dimensions; Ollama does not support this parameter.")}</small>}
            </div>;
          })}</div>
        </details>)}
      </fieldset>
      <div className="button-row">
        <button type="submit" className="primary" disabled={action.busy || !config.editable || !dirty}>{t("Save configuration")}</button>
        <button type="button" disabled={action.busy || dirty} onClick={check}>{t("Check application")}</button>
        <button type="button" disabled={action.busy} onClick={() => dirty ? setConfirmReload(true) : void reload()}>{t("Reload saved file")}</button>
      </div>
      {confirmReload && <div className="confirmation" role="alert"><p>{t("Discard unsaved configuration changes and reload?")}</p><div className="button-row"><button type="button" onClick={reload} disabled={action.busy}>{t("Discard edits and reload")}</button><button type="button" onClick={() => setConfirmReload(false)}>{t("Keep editing")}</button></div></div>}
      <Feedback error={inputError || action.error} notice={action.notice} />
      {action.error && <p className="muted">{t("Save or application check failed. Edits are kept. If the response was lost, reload the saved file before retrying; do not assume the configuration was applied.")}</p>}
    </form>
  </>;
}
