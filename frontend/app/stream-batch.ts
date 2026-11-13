export function batchStreamText(
  deliver: (text: string) => void,
  schedule: (callback: () => void) => ReturnType<typeof setTimeout> = callback => setTimeout(callback, 50),
  cancel: (timer: ReturnType<typeof setTimeout>) => void = clearTimeout,
) {
  let latest: string | undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const flush = () => {
    if (timer !== undefined) cancel(timer);
    timer = undefined;
    if (latest !== undefined) { const text = latest; latest = undefined; deliver(text); }
  };
  return {
    push(text: string) { latest = text; timer ??= schedule(flush); },
    flush,
    cancel() { if (timer !== undefined) cancel(timer); timer = undefined; latest = undefined; },
  };
}
