export function startQueryPolling(load: (signal: AbortSignal) => Promise<void>, interval: number): () => void {
  const controller = new AbortController();
  const page = document;
  let timer: ReturnType<typeof setTimeout>;
  let pending = false;

  async function run() {
    if (controller.signal.aborted || pending || (interval > 0 && page.hidden)) return;
    pending = true;
    try {
      await load(controller.signal);
    } finally {
      pending = false;
      if (!controller.signal.aborted && interval > 0 && !page.hidden) timer = setTimeout(run, interval);
    }
  }

  function visibilityChanged() {
    clearTimeout(timer);
    if (!page.hidden) void run();
  }

  if (interval > 0) page.addEventListener("visibilitychange", visibilityChanged);
  void run();
  return () => {
    controller.abort();
    clearTimeout(timer);
    if (interval > 0) page.removeEventListener("visibilitychange", visibilityChanged);
  };
}
