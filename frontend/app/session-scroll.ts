import { useEffect, useLayoutEffect, useRef, useState, type RefObject } from "react";

type Position = { top: number; follow: boolean; anchor?: string; offset?: number };

export class SessionScroll {
  private page = "latest";
  private ready = false;
  private follow = true;
  private position: Position = { top: 0, follow: true };
  private pages = new Map<string, Position>();
  private changed = "";
  private unread = false;
  private element: HTMLElement;
  private notify: (paused: boolean, unread: boolean) => void;
  constructor(element: HTMLElement, notify: (paused: boolean, unread: boolean) => void) {
    this.element = element;
    this.notify = notify;
  }

  private capture(): Position {
    const top = this.element.getBoundingClientRect().top;
    const anchor = [...this.element.querySelectorAll<HTMLElement>("[data-message-id]")]
      .find(node => node.getBoundingClientRect().bottom > top);
    return { top: this.element.scrollTop, follow: this.follow,
      anchor: anchor?.dataset.messageId, offset: anchor ? anchor.getBoundingClientRect().top - top : undefined };
  }
  private restore() {
    const anchor = [...this.element.querySelectorAll<HTMLElement>("[data-message-id]")]
      .find(node => node.dataset.messageId === this.position.anchor);
    if (anchor && this.position.offset !== undefined)
      this.element.scrollTop += anchor.getBoundingClientRect().top - this.element.getBoundingClientRect().top - this.position.offset;
    else this.element.scrollTop = this.position.top;
  }
  onScroll = () => {
    if (!this.ready) return;
    this.follow = this.page === "latest" && this.element.scrollHeight - this.element.clientHeight - this.element.scrollTop <= 64;
    if (this.follow) this.unread = false;
    this.position = this.capture();
    this.notify(!this.follow, this.unread);
  };
  navigate(page: string) {
    if (this.ready) this.pages.set(this.page, this.capture());
    this.page = page;
    this.ready = false;
    this.position = this.pages.get(page) ?? { top: 0, follow: page === "latest" };
    this.follow = this.position.follow;
  }
  update(page: string, ready: boolean, changed: string) {
    if (page !== this.page) this.navigate(page);
    if (!ready) return;
    const first = !this.ready;
    this.ready = true;
    if (!first && changed !== this.changed && !this.follow && page === "latest") this.unread = true;
    this.changed = changed;
    this.resize();
  }
  resize = () => {
    if (!this.ready) return;
    if (this.follow) this.element.scrollTop = this.element.scrollHeight;
    else this.restore();
    this.position = this.capture();
    this.notify(!this.follow, this.unread);
  };
  bottom = () => {
    this.follow = true;
    this.unread = false;
    this.resize();
  };
}

export function useSessionScroll(messages: RefObject<HTMLDivElement | null>, content: RefObject<HTMLDivElement | null>, page: string, ready: boolean, changed: string) {
  const controller = useRef<SessionScroll | null>(null);
  const [state, setState] = useState({ paused: false, unread: false });
  useLayoutEffect(() => {
    if (!messages.current || !content.current) return;
    const scroll = new SessionScroll(messages.current, (paused, unread) =>
      setState(current => current.paused === paused && current.unread === unread ? current : { paused, unread }));
    controller.current = scroll;
    messages.current.addEventListener("scroll", scroll.onScroll, { passive: true });
    const element = messages.current;
    const observer = new ResizeObserver(scroll.resize);
    observer.observe(content.current);
    observer.observe(element);
    return () => { observer.disconnect(); element.removeEventListener("scroll", scroll.onScroll); controller.current = null; };
  }, [messages, content]);
  useLayoutEffect(() => { controller.current?.update(page, ready, changed); }, [page, ready, changed]);
  useEffect(() => {
    const element = messages.current;
    const resize = () => controller.current?.resize();
    element?.addEventListener("load", resize, true);
    return () => element?.removeEventListener("load", resize, true);
  }, [messages]);
  return { ...state, navigate: (next: string) => controller.current?.navigate(next),
    bottom: () => controller.current?.bottom() };
}
