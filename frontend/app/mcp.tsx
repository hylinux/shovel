"use client";

import { useI18n } from "./locale-context";

import { useState, type FormEvent } from "react";
import { useAction, useQuery, type MCPConfiguration } from "./api";
import { Empty, Feedback } from "./ui";

export function MCPManagement() {
  const { t } = useI18n();
  const query = useQuery<MCPConfiguration>("/agent/mcp");
  return <section className="settings-card mcp-section" id="settings-mcp" aria-labelledby="mcp-management-heading" tabIndex={-1}>
    <header className="settings-card-header"><div><p className="settings-kicker">{t("Tools & extensions")}</p><h2 id="mcp-management-heading">{t("MCP Server Management")}</h2><p className="muted">{t("Edit workspace servers, save the file, then restart Agent to apply changes.")}</p></div></header>
    <Feedback error={query.error} retry={query.refresh} />
    {query.loading && <p role="status">{t("Loading MCP configuration...")}</p>}
    {query.data && <MCPEditor initial={query.data} />}
  </section>;
}

function MCPEditor({ initial }: { initial: MCPConfiguration }) {
  const { t } = useI18n();
  const [config, setConfig] = useState(initial);
  const [content, setContent] = useState(initial.content);
  const [confirmReload, setConfirmReload] = useState(false);
  const action = useAction();
  const dirty = content !== config.content;

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const saved = await action.run<MCPConfiguration>("/agent/mcp", { content, revision: config.revision }, "PUT");
    if (saved) {
      setConfig(saved); setConfirmReload(false);
      action.setNotice("Configuration saved. You can restart Agent now or keep its current connections until later.");
    }
  }
  async function reload() {
    const latest = await action.run<MCPConfiguration>("/agent/mcp", undefined, "GET");
    if (latest) {
      setConfig(latest); setContent(latest.content); setConfirmReload(false);
      action.setNotice("Configuration reloaded from disk.");
    }
  }
  async function restart() {
    const restarted = await action.run<MCPConfiguration>("/agent/restart");
    if (restarted) {
      // Reloading the Agent must not replace an editor's revision or local changes.
      setConfig((current) => ({ ...current, agent_loaded: restarted.agent_loaded, agent_state: restarted.agent_state, restart_required: restarted.restart_required || restarted.revision !== current.revision }));
      action.setNotice("Agent restarted. MCP connections and extensions will load on the next chat. Sessions and Memory are kept.");
    }
  }
  return <>
    <section className="configuration">
      <h3>{t("Workspace MCP configuration")}</h3>
      <p className="mcp-path"><code>{config.path}</code></p>
      <p className="muted">{t("Only configure trusted servers. Enabled servers can run commands with your account permissions or receive tool arguments.")}</p>
      <p role="status">{config.agent_state === "restart_failed" ? t("Agent restart failed. Retry Restart Agent before chatting.") : config.agent_state === "restarting" ? t("Agent is restarting.") : config.restart_required ? t("Saved configuration differs from the Agent cache. Restart required.") : config.agent_loaded ? t("Agent has loaded this configuration.") : t("MCP connections have not loaded. The next chat will load the saved file.")}</p>
      <details className="settings-details"><summary>{t("Connection lifecycle & credentials")}</summary><p>{t("Saving updates this file only. Restart Agent to close cached MCP connections and reload extensions on the next chat. Active sessions must finish first; sessions and Memory are kept.")}</p><p>{t("Prefer ")}<code>{"${ENV_VAR}"}</code>{t(" references for credentials. The editor displays the actual file, including inline secrets. Environment references are resolved when the Agent loads connections; restart the Host after changing its environment.")}</p></details>
    </section>
    <section aria-labelledby="mcp-servers-heading">
      <h3 id="mcp-servers-heading">{t("Saved servers ")}<span className="count">{config.items.length}</span></h3>
      <Feedback error={config.validation_error || ""} />
      {config.items.length ? <div className="mcp-servers"><table>
        <caption className="sr-only">{t("Servers in the saved workspace configuration")}</caption>
        <thead><tr><th scope="col">{t("Server")}</th><th scope="col">{t("Transport")}</th><th scope="col">{t("Configuration")}</th></tr></thead>
        <tbody>{config.items.map((server) => <tr key={server.name}><th scope="row">{server.name}</th><td>{server.transport === "http" ? t("Streamable HTTP") : t("stdio")}</td><td><span className={`badge ${server.enabled ? "good" : ""}`}>{server.enabled ? t("Enabled") : t("Disabled")}</span></td></tr>)}</tbody>
      </table></div> : !config.validation_error && <Empty title={t("No MCP servers configured")}>{t("Add servers in the configuration editor below.")}</Empty>}
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
    <details className="configuration"><summary>{t("Configuration example")}</summary><pre className="mcp-example">{JSON.stringify({ mcpServers: { example: { type: "http", url: "https://mcp.example.com/mcp", headers: { Authorization: "Bearer ${MCP_TOKEN}" }, allowed_tools: ["search"], disabled: true } } }, null, 2)}</pre></details>
  </>;
}
