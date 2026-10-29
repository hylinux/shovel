type PendingRead = {
  controller: AbortController;
  promise: Promise<Response>;
  users: number;
  settled: boolean;
  cleanup?: ReturnType<typeof setTimeout>;
};

const pending = new Map<string, PendingRead>();

export function invalidateReads() { pending.clear(); }

export function sharedRead(path: string, options: RequestInit = {}): Promise<Response> {
  const signal = options.signal;
  if (signal?.aborted) return Promise.reject(new DOMException("Request aborted", "AbortError"));
  const key = JSON.stringify([path, { ...options, method: "GET", signal: undefined, headers: Array.from(new Headers(options.headers).entries()).sort() }]);
  let entry = pending.get(key);
  if (!entry) {
    const controller = new AbortController();
    const promise = fetch(path, { ...options, signal: controller.signal }).then(async (response) => {
      // These reads are JSON, not Agent streams; retain cancellation until the body finishes.
      await response.clone().arrayBuffer();
      return response;
    });
    const created: PendingRead = { controller, users: 0, settled: false, promise };
    entry = created;
    pending.set(key, created);
    const clear = () => {
      created.settled = true;
      if (pending.get(key) === created) pending.delete(key);
      clearTimeout(created.cleanup);
    };
    void created.promise.then(clear, clear);
  }
  const read = entry;
  read.users++;
  clearTimeout(read.cleanup);
  return new Promise<Response>((resolve, reject) => {
    let finished = false;
    function finish() {
      finished = true;
      signal?.removeEventListener("abort", abort);
      read.users--;
      if (read.users === 0 && !read.settled) {
        // A Strict Mode cleanup/re-subscribe can reuse the request within this turn.
        read.cleanup = setTimeout(() => {
          if (pending.get(key) === read) pending.delete(key);
          read.controller.abort();
        }, 0);
      }
    }
    function abort() {
      if (finished) return;
      finish();
      reject(new DOMException("Request aborted", "AbortError"));
    }
    signal?.addEventListener("abort", abort, { once: true });
    void read.promise.then((response) => {
      if (finished) return;
      finish();
      resolve(response.clone());
    }, (error: unknown) => {
      if (finished) return;
      finish();
      reject(error);
    });
  });
}
