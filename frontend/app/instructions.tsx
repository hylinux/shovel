"use client";

import { useState, type FormEvent } from "react";
import { useAction, useQuery, type AgentInstructions, type MCPConfiguration } from "./api";
import { Feedback } from "./ui";

export function AgentInstructionsManagement({ running }: { running: boolean }) {
  const query = useQuery<AgentInstructions>("/agent/instructions");
  return <section className="settings-card instructions-section" aria-labelledby="instructions-heading">
    <header className="settings-card-header"><div><p className="settings-kicker">Role & behavior</p><h2 id="instructions-heading">Agent instructions</h2><p className="muted">Customize how the main Agent responds. Saving automatically restarts the Agent.</p></div></header>
    <Feedback error={query.error} retry={query.refresh} />
    {query.loading && <p role="status">Loading Agent instructions…</p>}
    {query.data && <InstructionsEditor initial={query.data} running={running} />}
  </section>;
}

function InstructionsEditor({ initial, running }: { initial: AgentInstructions; running: boolean }) {
  const [config, setConfig] = useState(initial);
  const [content, setContent] = useState(initial.content);
  const [confirmReload, setConfirmReload] = useState(false);
  const action = useAction();
  const dirty = content !== config.content;

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const saved = await action.run<AgentInstructions>("/agent/instructions", { content, revision: config.revision }, "PUT");
    if (saved) {
      setConfig(saved); setContent(saved.content); setConfirmReload(false);
      action.setNotice("Instructions saved and Agent restarted. The next message uses the new instructions. Sessions and Memory are kept.");
    }
  }
  async function reload() {
    const latest = await action.run<AgentInstructions>("/agent/instructions", undefined, "GET");
    if (latest) {
      setConfig(latest); setContent(latest.content); setConfirmReload(false);
      action.setNotice("Saved instructions reloaded.");
    }
  }
  async function restart() {
    const restarted = await action.run<MCPConfiguration>("/agent/restart");
    if (restarted) {
      const latest = await action.run<AgentInstructions>("/agent/instructions", undefined, "GET");
      if (latest) {
        setConfig(latest); setContent(latest.content); setConfirmReload(false);
        action.setNotice("Agent restarted with the saved instructions.");
      }
    }
  }

  return <>
    <p className="mcp-path"><code>{config.path}</code></p>
    <p className="muted">Instructions are saved in this workspace and passed to Microsoft Agent Framework. Shovel always retains its built-in citation, Memory and tool-use rules. Do not put credentials here.</p>
    <p role="status">{config.agent_state === "restart_failed" ? "Agent restart failed. Reload the saved file and retry Restart Agent." : config.restart_required ? "The saved file differs from the active Agent. Restart Agent to apply it." : config.applied ? "Saved instructions are applied. Model and extension clients initialize on demand." : "The next Agent run will load the saved instructions."}</p>
    <Feedback error={config.validation_error || ""} />
    <form onSubmit={save} className="instructions-editor">
      <div className="mcp-editor-heading"><label htmlFor="agent-instructions">Additional Agent instructions</label>{dirty && <span className="badge">Unsaved changes</span>}</div>
      <p id="instructions-help" className="muted">Set the Agent&apos;s role, tone, response format and working preferences. Leave empty to use only Shovel&apos;s built-in instructions. This does not change Memory extraction or summary instructions.</p>
      <textarea id="agent-instructions" aria-describedby="instructions-help instructions-budget" rows={16} maxLength={config.max_length} value={content} disabled={action.busy} spellCheck={false} placeholder="Describe the Agent's role and how you want it to work…" onChange={(event) => { setContent(event.target.value); setConfirmReload(false); }} />
      <p id="instructions-budget" className="muted">{content.length} / {config.max_length} characters · Included in model.max_input_chars</p>
      <div className="button-row">
        <button type="submit" className="primary" disabled={action.busy || running || (!dirty && config.exists && !config.restart_required)}>{action.busy ? "Applying…" : "Save & restart Agent"}</button>
        <button type="button" disabled={action.busy || running || dirty || Boolean(config.validation_error)} onClick={restart}>Restart Agent</button>
        <button type="button" disabled={action.busy} onClick={() => dirty ? setConfirmReload(true) : void reload()}>Reload saved file</button>
      </div>
      {running && <p className="warning" role="status">Wait for active conversations to finish before saving. Your edits are kept.</p>}
      {confirmReload && <div className="confirmation" role="alert"><p>Discard unsaved instructions and reload the saved file?</p><div className="button-row"><button type="button" disabled={action.busy} onClick={reload}>Discard edits and reload</button><button type="button" onClick={() => setConfirmReload(false)}>Keep editing</button></div></div>}
      <Feedback error={action.error} notice={action.notice} />
      {action.error && <p className="muted">If the response was lost or instructions were saved before a restart failure, reload the saved file before retrying.</p>}
    </form>
    <details className="settings-details"><summary>Built-in instructions (always retained)</summary><pre className="instructions-policy">{config.builtin_instructions}</pre></details>
  </>;
}
