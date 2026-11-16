const listeners = new Set<() => void>();
let timer: ReturnType<typeof setInterval> | undefined;
let now = 0;
export const attachmentNow = () => now;
export const attachmentServerNow = () => 0;
export function subscribeAttachmentClock(listener: () => void) {
  listeners.add(listener);
  if (timer === undefined) {
    now = Date.now() / 1000;
    timer = setInterval(() => { now = Date.now() / 1000; listeners.forEach(notify => notify()); }, 30_000);
  }
  return () => {
    listeners.delete(listener);
    if (!listeners.size) { clearInterval(timer); timer = undefined; }
  };
}
