import type { Session } from "./api";

export function mergeSessionHistory(current: Session[], incoming: Session[]): Session[] {
  const sessions = new Map(current.map((session) => [session.id, session]));
  for (const session of incoming) {
    const previous = sessions.get(session.id);
    if (!previous || session.updated_at >= previous.updated_at) sessions.set(session.id, session);
  }
  return [...sessions.values()].sort((left, right) =>
    right.updated_at - left.updated_at || left.id.localeCompare(right.id));
}
