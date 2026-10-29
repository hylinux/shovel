import type { Citation } from "./api";

export type SearchOrder = "score_desc" | "score_asc" | "title_asc" | "title_desc";

export function orderSearchResults(items: Citation[], order: SearchOrder): Citation[] {
  return [...items].sort((left, right) => {
    if (order === "score_desc") return right.score - left.score;
    if (order === "score_asc") return left.score - right.score;
    const compared = (left.title || left.doc_uri).localeCompare(right.title || right.doc_uri);
    return order === "title_asc" ? compared : -compared;
  });
}
