type SessionStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;

export function sessionCacheKey(workspace: string): string {
  return `shovel.current-session:${workspace}`;
}

export async function resolveInitialSession(
  workspace: string,
  storage: SessionStorage,
  lookup: (identifier: string) => Promise<void>,
  create: () => Promise<string>,
): Promise<string> {
  const key = sessionCacheKey(workspace);
  const identifier = storage.getItem(key);
  if (identifier) {
    try {
      await lookup(identifier);
      return identifier;
    } catch (error) {
      if (!(error instanceof Error) || !("status" in error) || error.status !== 404) throw error;
      storage.removeItem(key);
    }
  }
  const created = await create();
  storage.setItem(key, created);
  return created;
}
