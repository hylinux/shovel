"use client";

import { useI18n } from "./locale-context";

import { useEffect, useState, type FormEvent } from "react";
import { segment, useAction, useQuery, type MCPAuthorization, type MCPAuthorizationStart, type MCPConfiguration, type MCPDisconnect, type MCPServer } from "./api";
import { Empty, Feedback } from "./ui";

export function MCPManagement({ active = true }: { active?: boolean }) {
  const { t } = useI18n();
  const query = useQuery<MCPConfiguration>(active ? "/agent/mcp" : null, 3000);
  return <section className="settings-card mcp-section" id="settings-mcp" aria-labelledby="mcp-management-heading" tabIndex={-1}>
    <header className="settings-card-header"><div><p className="settings-kicker">{t("Tools & extensions")}</p><h2 id="mcp-management-heading">{t("MCP Server Management")}</h2><p className="muted">{t("Edit workspace servers, save the file, then restart Agent to apply changes.")}</p></div></header>
    <Feedback error={query.error} retry={query.refresh} />
    {query.loading && <p role="status">{t("Loading MCP configuration...")}</p>}
    {query.data && <MCPEditor initial={query.data} active={active} refresh={query.refresh} />}
  </section>;
}

function MCPEditor({ initial, active, refresh }: { initial: MCPConfiguration; active: boolean; refresh: () => void }) {
  const { t } = useI18n();
  const [config, setConfig] = useState(initial);
  const [content, setContent] = useState(initial.content);
  const [confirmReload, setConfirmReload] = useState(false);
  const action = useAction();
  const dirty = content !== config.content;
  const live = initial.revision === config.revision ? initial : config;

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const saved = await action.run<MCPConfiguration>("/agent/mcp", { content, revision: config.revision }, "PUT");
    if (saved) {
      setConfig(saved); setConfirmReload(false);
      refresh();
      action.setNotice("Configuration saved. You can restart Agent now or keep its current connections until later.");
    }
  }
  async function reload() {
    const latest = await action.run<MCPConfiguration>("/agent/mcp", undefined, "GET");
    if (latest) {
      setConfig(latest); setContent(latest.content); setConfirmReload(false);
      refresh();
      action.setNotice("Configuration reloaded from disk.");
    }
  }
  async function restart() {
    const restarted = await action.run<MCPConfiguration>("/agent/restart");
    if (restarted) {
      // Reloading the Agent must not replace an editor's revision or local changes.
      setConfig((current) => ({ ...current, agent_loaded: restarted.agent_loaded, agent_state: restarted.agent_state, restart_required: restarted.restart_required || restarted.revision !== current.revision }));
      refresh();
      action.setNotice("Agent restarted. MCP connections and extensions will load on the next chat. Sessions and Memory are kept.");
    }
  }
  return <>
    <section className="configuration">
      <h3>{t("Workspace MCP configuration")}</h3>
      <p className="mcp-path"><code>{config.path}</code></p>
      <p className="muted">{t("Only configure trusted servers. Enabled servers can run commands with your account permissions or receive tool arguments.")}</p>
      <p role="status">{live.agent_state === "restart_failed" ? t("Agent restart failed. Retry Restart Agent before chatting.") : live.agent_state === "restarting" ? t("Agent is restarting.") : live.restart_required ? t("Configuration or authentication changed. Restart Agent to replace cached connections.") : live.agent_loaded ? t("Agent has loaded this configuration.") : t("MCP connections have not loaded. The next chat will load the saved file.")}</p>
      <details className="settings-details"><summary>{t("Connection lifecycle & credentials")}</summary><p>{t("Saving updates this file only. Restart Agent to close cached MCP connections and reload extensions on the next chat. Active sessions must finish first; sessions and Memory are kept.")}</p><p>{t("Use env: or keyring: references in auth and env_credentials. Keyring inputs below only write new values; existing secrets are never loaded into them. Legacy headers/env and this JSON editor still display the original file, including inline secrets.")}</p><p>{t("Environment credentials are read from the Host. Restart the Host after changing its environment.")}</p></details>
      {config.warnings?.map((warning) => <p className="warning" key={warning}>{t(warning)}</p>)}
    </section>
    <section aria-labelledby="mcp-servers-heading">
      <h3 id="mcp-servers-heading">{t("Saved servers ")}<span className="count">{config.items.length}</span></h3>
      <Feedback error={config.validation_error || ""} />
      {config.items.length ? <div className="mcp-servers"><table>
        <caption className="sr-only">{t("Servers in the saved workspace configuration")}</caption>
        <thead><tr><th scope="col">{t("Server")}</th><th scope="col">{t("Transport")}</th><th scope="col">{t("Configuration")}</th><th scope="col">{t("Authentication")}</th><th scope="col">{t("Actual connection")}</th></tr></thead>
        <tbody>{live.items.map((server) => <tr key={server.name}><th scope="row">{server.name}</th><td>{server.transport === "http" ? t("Streamable HTTP") : t("stdio")}</td><td><span className={`badge ${server.enabled ? "good" : ""}`}>{server.enabled ? t("Enabled") : t("Disabled")}</span></td><td>{t(authenticationLabels[server.auth_type])}</td><td>{server.enabled ? t(connectionLabels[server.connection_state]) : t("Disabled")}</td></tr>)}</tbody>
      </table></div> : !config.validation_error && <Empty title={t("No MCP servers configured")}>{t("Add servers in the configuration editor below.")}</Empty>}
      <p className="muted">{t("Saved configuration, local tokens and live connections are separate. Tools discovered does not mean a business tool call was verified. No connection or tool call is started from this page.")}</p>
      {dirty && <p className="warning">{t("Save or discard editor changes before managing authentication.")}</p>}
      <div className="mcp-authentication">{config.items.filter((server) => server.auth_type.startsWith("oauth_") || server.credential_refs.length > 0).map((server) => <section className="mcp-auth-card" key={`${server.name}:${config.revision}`} aria-label={t("Authentication for {p0}", { p0: server.name })}>
        <h4>{server.name} · {t(authenticationLabels[server.auth_type])}</h4>
        {server.auth_type.startsWith("oauth_") && <MCPAuthorizationControls server={server} revision={config.revision} blocked={dirty || action.busy} active={active} refresh={refresh} />}
        {server.credential_refs.map((reference) => <MCPCredentialEditor key={reference} server={server.name} reference={reference} revision={config.revision} blocked={dirty || action.busy} refresh={refresh} />)}
      </section>)}</div>
    </section>
    <form onSubmit={save} className="mcp-editor">
      <div className="mcp-editor-heading"><label htmlFor="mcp-configuration">{t("mcp.json configuration")}</label>{dirty && <span className="badge">{t("Unsaved changes")}</span>}</div>
      <p id="mcp-editor-help" className="muted">{t("Edit the mcpServers object to add, change or remove servers. Set disabled to true to disable a server. Each entry needs either command or url, not both. The list above shows the saved file, not unsaved edits.")}</p>
      <textarea id="mcp-configuration" aria-describedby="mcp-editor-help" spellCheck={false} autoCapitalize="off" autoCorrect="off" rows={18} maxLength={1048576} value={content} disabled={action.busy} onChange={(event) => { setContent(event.target.value); setConfirmReload(false); }} />
      <div className="button-row">
        <button type="submit" className="primary" disabled={action.busy || (!dirty && config.exists)}>{t("Save configuration")}</button>
        <button type="button" disabled={action.busy || dirty || Boolean(config.validation_error)} onClick={restart}>{t("Restart Agent")}</button>
        <button type="button" disabled={action.busy} onClick={() => dirty ? setConfirmReload(true) : void reload()}>{t("Reload file")}</button>
        {dirty && <span className="muted">{t("Save before restarting Agent.")}</span>}
      </div>
      {confirmReload && <div className="confirmation" role="alert"><p>{t("Discard unsaved edits and reload mcp.json from disk?")}</p><div className="button-row"><button type="button" disabled={action.busy} onClick={reload}>{t("Discard edits and reload")}</button><button type="button" onClick={() => setConfirmReload(false)}>{t("Keep editing")}</button></div></div>}
      <Feedback error={action.error} notice={action.notice} />
    </form>
    <details className="configuration"><summary>{t("Authentication configuration examples")}</summary><p>{t("Copy only the required example into the editor and use a different credential_id UUID for each service. Examples are disabled and contain references, not secrets.")}</p><pre className="mcp-example">{JSON.stringify(authenticationExamples, null, 2)}</pre></details>
  </>;
}

const authenticationLabels = {
  none: "No explicit authentication", legacy: "Legacy headers / environment", api_key: "API Key", bearer: "Static Bearer / PAT",
  stdio_credentials: "stdio credential references", oauth_user: "OAuth user authorization", oauth_client_credentials: "OAuth application identity",
};
const connectionLabels = {
  not_connected: "Not connected / not verified", connecting: "Connecting", connected: "Connected · tools discovered",
  authentication_failed: "Authentication failed (401)", permission_denied: "Permission denied (403)",
  authorization_required: "User authorization required", configuration_changed: "Configuration changed · reload file",
  configuration_error: "Invalid authentication configuration", unavailable: "Connection or credentials unavailable",
  restart_required: "Authentication changed · restart required",
};
const authorizationLabels = {
  not_authorized: "No local authorization", authorizing: "Waiting for user authorization",
  authorized: "Local tokens available · connection not verified", reauthorization_required: "Authorization expired or rejected · authorize again",
};

function MCPAuthorizationControls({ server, revision, blocked, active, refresh }: {
  server: MCPServer; revision: string; blocked: boolean; active: boolean; refresh: () => void;
}) {
  const { t, date } = useI18n();
  const action = useAction();
  const [observation, setObservation] = useState(0);
  const [start, setStart] = useState<MCPAuthorizationStart>();
  const [error, setError] = useState("");
  const [confirmDisconnect, setConfirmDisconnect] = useState(false);
  const base = `/agent/mcp/${segment(server.name)}/auth`;
  const path = `${base}?revision=${segment(revision)}&observation=${observation}`;
  const query = useQuery<MCPAuthorization>(active && server.enabled ? path : null, 3000);
  const refreshAuthorization = query.refresh;
  const status = query.dataPath === path && query.data?.revision === revision ? query.data : undefined;
  const pending = status?.state === "authorizing";
  const link = pending && start?.authorization_id === status.authorization_id ? start : undefined;
  useEffect(() => {
    if (!start) return;
    const timer = setTimeout(() => { setStart(undefined); refreshAuthorization(); }, Math.max(0, start.expires_at * 1000 - Date.now()));
    return () => clearTimeout(timer);
  }, [start, refreshAuthorization]);
  const locked = blocked || action.busy || !server.enabled || !status || Boolean(query.error);
  function changed() { setObservation((value) => value + 1); refresh(); }
  async function begin() {
    setStart(undefined); setError(""); setConfirmDisconnect(false);
    const result = await action.run<MCPAuthorizationStart>(`${base}/start`, { revision });
    if (result) {
      try {
        const url = new URL(result.authorization_url);
        if (url.username || url.password || (url.protocol !== "https:" && !(url.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)))) {
          throw new Error("Shovel returned an invalid authorization URL.");
        }
        setStart(result);
      } catch { setError("Shovel returned an invalid authorization URL."); }
    }
    changed();
  }
  async function cancel() {
    setStart(undefined); setError("");
    await action.run(`${base}/cancel`, { revision, authorization_id: status?.authorization_id });
    changed();
  }
  async function disconnect() {
    setStart(undefined); setError(""); setConfirmDisconnect(false);
    const result = await action.run<MCPDisconnect>(`${base}/disconnect`, { revision });
    if (result) action.setNotice(result.provider_revocation === "completed"
      ? "Local authorization cleared and provider revocation completed. Restart Agent."
      : "Local authorization cleared. Provider revocation is unsupported; tokens may remain valid at the provider. Restart Agent.");
    changed();
  }
  return <div className="mcp-oauth">
    {!server.enabled ? <p>{t("Enable and save this server before authorizing. No login is started for disabled servers.")}</p> : <p role="status">{status ? t(authorizationLabels[status.state]) : t("Checking local authorization...")}</p>}
    {status?.expires_at != null && <p className="muted">{t("Token expiry: {p0}", { p0: date(status.expires_at) })}</p>}
    <p className="muted">{server.auth_type === "oauth_user" ? t("Authorization is explicit. Start, then open the authorization link in a new tab. Return here after the backend callback; status updates automatically. Restart Agent before using new authorization.") : t("Application tokens are acquired lazily when the Agent connects. Saving a client secret does not request tokens or validate a connection.")}</p>
    <div className="button-row">
      {server.auth_type === "oauth_user" && <button type="button" disabled={locked || pending} onClick={begin}>{t(status?.state === "authorized" || status?.state === "reauthorization_required" ? "Authorize again" : "Start authorization")}</button>}
      {pending && server.auth_type === "oauth_user" && <button type="button" disabled={locked} onClick={cancel}>{t("Cancel authorization")}</button>}
      <button type="button" disabled={blocked || action.busy || !server.enabled} onClick={() => { query.refresh(); refresh(); }}>{t("Refresh authorization status")}</button>
      <button type="button" disabled={locked} onClick={() => setConfirmDisconnect(true)}>{t("Disconnect authorization")}</button>
    </div>
    {link && <div className="confirmation">
      <a className="mcp-authorize-link" href={link.authorization_url} target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer">{t("Open authorization page")}</a>
      <p>{t("Authorization link expires: {p0}", { p0: date(link.expires_at) })}</p><p>{t("Issuer")}: <code>{link.issuer}</code></p><p>{t("Scopes")}: <code>{link.scopes.join(" ") || "—"}</code></p>
    </div>}
    {pending && !link && <p className="muted">{t("A pending authorization exists, but its link is not available in this page. Complete the original browser flow or cancel it before starting again.")}</p>}
    {confirmDisconnect && <div className="confirmation" role="alert"><p>{t("Clear local tokens and invalidate authenticated connections? Provider revocation is attempted only when supported and may fail. Restart Agent afterwards.")}</p><div className="button-row"><button type="button" disabled={locked} onClick={disconnect}>{t("Confirm disconnect")}</button><button type="button" onClick={() => setConfirmDisconnect(false)}>{t("Keep authorization")}</button></div></div>}
    <Feedback error={error || action.error || query.error} notice={action.notice} />
  </div>;
}

function MCPCredentialEditor({ server, reference, revision, blocked, refresh }: {
  server: string; reference: string; revision: string; blocked: boolean; refresh: () => void;
}) {
  const { t } = useI18n();
  const action = useAction();
  const [value, setValue] = useState("");
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const saved = await action.run<{ saved: boolean }>(`/agent/mcp/${segment(server)}/credentials`, { revision, reference, value }, "PUT");
    if (saved) {
      setValue("");
      action.setNotice("Credential saved without readback. Restart Agent to replace cached credentials; no connection was tested.");
    }
    refresh();
  }
  return <div className="mcp-credential">
    <code>{reference}</code>
    {reference.startsWith("env:") ? <p className="muted">{t("Set this variable in the Host environment and restart the Host. Environment references are read-only here.")}</p> : <form onSubmit={save}>
      <label>{t("New keyring credential (write only)")}<input type="password" autoComplete="new-password" spellCheck={false} autoCapitalize="off" autoCorrect="off" maxLength={65536} value={value} onChange={(event) => setValue(event.target.value)} disabled={blocked || action.busy} /></label>
      <button type="submit" disabled={blocked || action.busy || !value}>{t("Save credential")}</button>
      <Feedback error={action.error} notice={action.notice} />
    </form>}
  </div>;
}

const authenticationExamples = { mcpServers: {
  api_key: { url: "https://mcp.example.com/mcp", credential_id: "12b9ae42-c658-4177-a3bb-f8b9b616edaf", auth: { type: "api_key", header: "X-API-Key", credential_ref: "keyring:api-key" }, disabled: true },
  bearer: { url: "https://mcp.example.com/mcp", credential_id: "23b9ae42-c658-4177-a3bb-f8b9b616edaf", auth: { type: "bearer", credential_ref: "env:MCP_TOKEN" }, disabled: true },
  user: { url: "https://mcp.example.com/mcp", credential_id: "34b9ae42-c658-4177-a3bb-f8b9b616edaf", auth: { type: "oauth_user", client_id: "YOUR-PUBLIC-CLIENT-ID", issuer: "https://identity.example.com", scopes: ["tools.read"] }, disabled: true },
  application: { url: "https://mcp.example.com/mcp", credential_id: "45b9ae42-c658-4177-a3bb-f8b9b616edaf", auth: { type: "oauth_client_credentials", client_id: "YOUR-APPLICATION-ID", issuer: "https://identity.example.com", client_secret_ref: "keyring:application-secret", scopes: ["tools.read"], token_endpoint_auth_method: "client_secret_basic" }, disabled: true },
  local: { command: "YOUR-MCP-EXECUTABLE", args: [], credential_id: "56b9ae42-c658-4177-a3bb-f8b9b616edaf", env_credentials: { SERVICE_TOKEN: "keyring:service-token" }, disabled: true },
} };
