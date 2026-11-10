"use client";

import { LanguageSelector, useI18n } from "./locale-context";

import { useRef, useState, type KeyboardEvent } from "react";
import { useQuery, type Status } from "./api";
import { MCPManagement } from "./mcp";
import { AgentInstructionsManagement } from "./instructions";
import { MemoryManagement } from "./memories";
import { UserProfileSettings } from "./user-profile";
import { RuntimeConfigurationEditor } from "./runtime-configuration";
import { Feedback } from "./ui";

const warmupNames: Record<string, string> = {
  index: "Knowledge index",
  memory_embedding: "Memory embedding",
  rag_embedding: "RAG embedding",
  chat_model: "Chat model",
};

const settingsTabs = [
  { id: "runtime", label: "Runtime configuration" },
  { id: "memory", label: "Manage long-term Memory" },
  { id: "mcp", label: "MCP Server Management" },
  { id: "instructions", label: "Agent instructions" },
  { id: "language", label: "General settings" },
] as const;
type SettingsTab = typeof settingsTabs[number]["id"];

export function Settings({ status, sessionId = "" }: { status?: Status; sessionId?: string }) {
  const { t } = useI18n();
  const [active, setActive] = useState<SettingsTab>("runtime");
  const [visited, setVisited] = useState<SettingsTab[]>(["runtime"]);
  const buttons = useRef<(HTMLButtonElement | null)[]>([]);
  function select(id: SettingsTab) {
    setActive(id);
    setVisited((current) => current.includes(id) ? current : [...current, id]);
  }
  function navigate(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    let target: number;
    if (event.key === "ArrowRight") target = (index + 1) % settingsTabs.length;
    else if (event.key === "ArrowLeft") target = (index + settingsTabs.length - 1) % settingsTabs.length;
    else if (event.key === "Home") target = 0;
    else if (event.key === "End") target = settingsTabs.length - 1;
    else return;
    event.preventDefault();
    select(settingsTabs[target].id);
    buttons.current[target]?.focus();
  }
  return <div className="settings-content">
    <div className="settings-index" role="tablist" aria-label={t("Settings")}>
      {settingsTabs.map((tab, index) => <button key={tab.id} type="button" role="tab" id={`settings-tab-${tab.id}`} aria-controls={`settings-panel-${tab.id}`} aria-selected={active === tab.id} tabIndex={active === tab.id ? 0 : -1} ref={(element) => { buttons.current[index] = element; }} onClick={() => select(tab.id)} onKeyDown={(event) => navigate(event, index)}>{t(tab.label)}</button>)}
    </div>
    {settingsTabs.map((tab) => <div key={tab.id} className="settings-panel" id={`settings-panel-${tab.id}`} role="tabpanel" aria-labelledby={`settings-tab-${tab.id}`} hidden={active !== tab.id} tabIndex={0}>
      {visited.includes(tab.id) && (tab.id === "runtime" ? <RuntimeConfiguration status={status} />
        : tab.id === "memory" ? <MemoryManagement status={status} sessionId={sessionId} />
          : tab.id === "mcp" ? <MCPManagement />
            : tab.id === "instructions" ? <AgentInstructionsManagement running={Boolean(status?.chat_capacity && (status.chat_capacity.running || status.chat_capacity.waiting))} />
              : <><LanguageSettings /><UserProfileSettings /></>)}
    </div>)}
  </div>;
}

function LanguageSettings() {
  const { t } = useI18n();
  return <section className="settings-card" aria-labelledby="settings-language-heading">
    <header className="settings-card-header"><div><h2 id="settings-language-heading">{t("Language")}</h2><p className="muted">{t("Choose the interface language. Changes apply immediately and are saved in this browser.")}</p></div></header>
    <LanguageSelector id="settings-interface-language" />
  </section>;
}

function RuntimeConfiguration({ status: initialStatus }: { status?: Status }) {
  const { t } = useI18n();
  const monitor = useQuery<Status>("/status", 5000);
  const status = monitor.data || initialStatus;
  const chatReady = status?.model_configured && status.memory_configured;
  const providers = [
    { name: "Chat model", section: "model", ready: status?.model_configured, value: status?.model_configured ? status.model_name : "Required for chat", description: "Generates replies through the main Agent.", optional: false },
    { name: "Knowledge embedding", section: "embedding", ready: status?.embedding_configured, value: status?.embedding_configured ? "Vector search available" : "Keyword search available", description: "Adds semantic search to your local documents.", optional: true },
    { name: "Native Memory", section: "memory", ready: status?.memory_configured, value: status?.memory_configured ? "SQLite · local source of truth" : "Unavailable", description: "Long-term facts with a separate, rebuildable Zvec semantic index.", optional: false },
  ];
  return <>
    <section className="settings-card" id="settings-providers" aria-labelledby="settings-providers-heading" tabIndex={-1}>
      <header className="settings-card-header">
        <div><p className="settings-kicker">{t("Models & connections")}</p><h2 id="settings-providers-heading">{t("Runtime configuration")}</h2><p className="muted">{t("Your configured providers and session readiness.")}</p></div>
        <button type="button" onClick={monitor.refresh}>{t("Refresh runtime status")}</button>
        <span className={`badge ${status ? chatReady ? "good" : "bad" : ""}`}>{status ? chatReady ? t("Configured for chat") : t("Chat setup incomplete") : t("Checking runtime")}</span>
      </header>
      <Feedback error={monitor.error} retry={monitor.refresh} />
      {status ? <div className="provider-grid">{providers.map((provider) => <article className="provider-card" key={provider.section}>
        <div className="provider-card-heading"><h3>{t(provider.name)}</h3><span className={`badge ${provider.ready ? "good" : provider.optional ? "" : "bad"}`}>{provider.ready ? t("Configured") : provider.optional ? t("Optional") : t("Not configured")}</span></div>
        <code>[{provider.section}]</code><p className="provider-value">{provider.section === "model" && provider.ready ? provider.value : t(provider.value || "")}</p><p className="muted">{t(provider.description)}</p>
      </article>)}</div> : <p className="muted" role="status">{t("Waiting for runtime configuration…")}</p>}
      <p className="settings-note">{t("These monitors describe the active Host, not unsaved or pending file settings.")}</p>
    </section>
    <section className="settings-card" id="settings-performance" aria-labelledby="settings-performance-heading" tabIndex={-1}>
      <header className="settings-card-header"><div><p className="settings-kicker">{t("Response health")}</p><h2 id="settings-performance-heading">{t("Performance & warmup")}</h2><p className="muted">{t("See what is initialized before your next session.")}</p></div></header>
      {status?.chat_capacity && <div className="chat-capacity"><p><strong>{status.chat_capacity.running}</strong> / {status.chat_capacity.limit}{t(" sessions running")}</p><span className="badge">{status.chat_capacity.waiting}{t(" waiting")}</span><p className="muted">{t("Background Memory starts after foreground sessions are idle.")}</p></div>}
      {status?.warmup && <><h3>{t("Startup warmup")}</h3><ul className="warmup-list">{Object.entries(status.warmup).map(([name, item]) => <li key={name}>
        <div><span className="warmup-name">{t(warmupNames[name] || name.replaceAll("_", " "))}</span>{item.elapsed_ms !== undefined && <span className="muted">{Math.round(item.elapsed_ms)}{t(" ms")}</span>}</div>
        <span className={`badge ${item.state === "ready" ? "good" : item.state === "failed" ? "bad" : ""}`}>{t(item.state)}</span>
        {item.error_type && <p className="warning">{item.error_type}{t(" — check backend logs.")}</p>}
      </li>)}</ul><p className="muted">{t("Local model warmup uses a synthetic prompt, not session data. Disabled components initialize on demand; ready models may later be unloaded.")}</p></>}
      {!status?.warmup && !status?.chat_capacity && <p className="muted">{status ? t("Performance metrics are not available from this Host.") : t("Waiting for runtime status…")}</p>}
    </section>
    <RuntimeConfigurationEditor />
  </>;
}
