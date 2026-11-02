"use client";

import { useState } from "react";
import { useQuery } from "./api";
import { isPage, pagePath, pagination, type Page } from "./pagination";

export function usePageQuery<T>(path: string, interval = 0) {
  const [state, setState] = useState({ path, page: 1, pageSize: 10 });
  if (state.path !== path) setState({ ...state, path, page: 1 });
  const requestPath = pagePath(path, state.page, state.pageSize);
  const query = useQuery<Page<T>>(requestPath, interval);
  const current = query.dataPath === requestPath ? query.data : undefined;
  const invalid = current !== undefined && !isPage<T>(current);
  const data = invalid ? undefined : current;
  const error = invalid ? "Shovel returned invalid pagination metadata. Restart the updated backend, then retry."
    : query.errorPath === requestPath ? query.error : "";
  const range = pagination(data?.total ?? 0, data ? state.page : 1, state.pageSize);
  if (data && range.page !== state.page) setState({ ...state, page: range.page });
  return {
    ...range, page: data ? range.page : state.page, data, error, loading: !data && !error,
    refresh: query.refresh,
    setPage: (page: number) => setState((current) => ({ ...current, page })),
    setPageSize: (pageSize: number) => setState((current) => ({ ...current, pageSize, page: 1 })),
  };
}
