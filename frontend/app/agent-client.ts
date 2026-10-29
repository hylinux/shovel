import { HttpAgent } from "@ag-ui/client";

export type AgentMessageInput = {
  content: string;
  resource_ids: string[];
  memory_enabled: boolean;
  search_mode: string;
};

export async function runAgentMessage(
  url: string,
  sessionId: string,
  input: AgentMessageInput,
  signal: AbortSignal,
  onText: (text: string) => void,
  onTool: (name: string) => void,
): Promise<void> {
  const { content, ...forwardedProps } = input;
  const agent = new HttpAgent({
    url,
    threadId: sessionId,
    initialMessages: [{ id: crypto.randomUUID(), role: "user", content }],
  });
  const abort = () => agent.abortRun();
  signal.addEventListener("abort", abort, { once: true });
  let finished = false;
  let failure = "";
  try {
    if (signal.aborted) throw new DOMException("Aborted", "AbortError");
    await agent.runAgent({ forwardedProps }, {
      onTextMessageContentEvent: ({ event, textMessageBuffer }) => { onText(textMessageBuffer + event.delta); },
      onToolCallStartEvent: ({ event }) => { onTool(event.toolCallName); },
      onRunErrorEvent: ({ event }) => { failure = event.message; },
      onRunFinishedEvent: () => { finished = true; },
    });
    if (failure) throw new Error(failure);
    if (!finished) throw new Error("Agent connection ended before completion. Check session history before sending again.");
  } finally {
    signal.removeEventListener("abort", abort);
  }
}
