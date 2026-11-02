export type Page<T> = { items: T[]; total: number };
export type Pagination = {
  page: number; pageSize: number; pageCount: number; total: number;
  offset: number; start: number; end: number;
};

export function pagination(total: number, requestedPage: number, pageSize: number): Pagination {
  if (!Number.isSafeInteger(total) || total < 0 || !Number.isSafeInteger(requestedPage) || requestedPage < 1 || !Number.isSafeInteger(pageSize) || pageSize < 1) {
    throw new Error("Invalid pagination values.");
  }
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const page = Math.min(requestedPage, pageCount);
  const offset = (page - 1) * pageSize;
  return { page, pageSize, pageCount, total, offset, start: total ? offset + 1 : 0, end: Math.min(offset + pageSize, total) };
}

export function pagePath(path: string, page: number, pageSize: number): string {
  return `${path}${path.includes("?") ? "&" : "?"}limit=${pageSize}&offset=${(page - 1) * pageSize}`;
}

export function isPage<T>(value: unknown): value is Page<T> {
  return typeof value === "object" && value !== null && "items" in value && Array.isArray(value.items)
    && "total" in value && typeof value.total === "number" && Number.isSafeInteger(value.total) && value.total >= 0;
}
