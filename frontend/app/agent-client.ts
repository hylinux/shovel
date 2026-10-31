import { HttpAgent } from "@ag-ui/client";

export function chatTrace(event: string, fields: Record<string, string | number | boolean | undefined>) {
  if (process.env.NODE_ENV === "development") {
    console.info(`[shovel.chat] ${JSON.stringify({ timestamp: new Date().toISOString(), event, component: "frontend", ...fields })}`);
  }
}

export type AgentMessageInput = {
  content: string;
  resource_ids: string[];
  memory_enabled: boolean;
  search_mode: string;
  interaction_mode?: "agent" | "chat";
};

export async function runAgentMessage(
  url: string,
  sessionId: string,
  input: AgentMessageInput,
  signal: AbortSignal,
  onText: (text: string) => void,
  onTool: (name: string) => void,
  runId = crypto.randomUUID(),
): Promise<void> {
  const started = performance.now();
  const context = { run_id: runId, conversation_id: sessionId };
  let chunks = 0;
  let characters = 0;
  const { content, ...forwardedProps } = input;
  const agent = new HttpAgent({
    url,
    threadId: sessionId,
    initialMessages: [{ id: crypto.randomUUID(), role: "user", content }],
    fetch: async (target, options) => {
      const sent = performance.now();
      chatTrace("http.request", { ...context, method: "POST", endpoint: "agent" });
      const response = await fetch(target, options);
      chatTrace("http.response", {
        ...context, status: response.status, request_id: response.headers.get("X-Shovel-Request-ID") || undefined,
        elapsed_ms: Math.round(performance.now() - sent),
      });
      return response;
    },
  });
  const abort = () => agent.abortRun();
  signal.addEventListener("abort", abort, { once: true });
  let finished = false;
  let failure = "";
  try {
    if (signal.aborted) throw new DOMException("Aborted", "AbortError");
    chatTrace("agent.started", { ...context, input_chars: content.length, memory_enabled: input.memory_enabled, resource_count: input.resource_ids.length });
    await agent.runAgent({ runId, forwardedProps }, {
      onRunStartedEvent: () => { chatTrace("agent.stream.started", context); },
      onTextMessageContentEvent: ({ event, textMessageBuffer }) => {
        chunks++; characters += event.delta.length;
        if (chunks === 1) chatTrace("agent.first_text", { ...context, elapsed_ms: Math.round(performance.now() - started) });
        onText(textMessageBuffer + event.delta);
      },
      onTextMessageEndEvent: () => { chatTrace("agent.text.completed", { ...context, chunks, output_chars: characters }); },
      onToolCallStartEvent: ({ event }) => {
        chatTrace("agent.tool.started", { ...context, tool_name: event.toolCallName, tool_call_id: event.toolCallId });
        onTool(event.toolCallName);
      },
      onToolCallEndEvent: ({ event }) => { chatTrace("agent.tool.arguments.completed", { ...context, tool_call_id: event.toolCallId }); },
      onToolCallResultEvent: ({ event }) => { chatTrace("agent.tool.result", { ...context, tool_call_id: event.toolCallId, output_chars: event.content.length }); },
      onCustomEvent: ({ event }) => {
        const value: unknown = event.value;
        if (event.name === "shovel.message" && value !== null && typeof value === "object") {
          chatTrace("agent.message.persisted", {
            ...context,
            message_id: "id" in value && typeof value.id === "string" ? value.id : undefined,
            citations: "citations" in value && Array.isArray(value.citations) ? value.citations.length : 0,
            warnings: "warnings" in value && Array.isArray(value.warnings) ? value.warnings.length : 0,
          });
        }
      },
      onRunErrorEvent: ({ event }) => { failure = event.message; chatTrace("agent.run_error", context); },
      onRunFinishedEvent: () => { finished = true; },
    });
    if (failure) throw new Error(failure);
    if (!finished) throw new Error("Agent connection ended before completion. Check session history before sending again.");
    chatTrace("agent.completed", { ...context, chunks, output_chars: characters, elapsed_ms: Math.round(performance.now() - started) });
  } catch (error) {
    chatTrace(signal.aborted ? "agent.cancelled" : "agent.failed", {
      ...context, error_type: error instanceof Error ? error.name : "UnknownError",
      chunks, output_chars: characters, elapsed_ms: Math.round(performance.now() - started),
    });
    throw error;
  } finally {
    signal.removeEventListener("abort", abort);
  }
}
