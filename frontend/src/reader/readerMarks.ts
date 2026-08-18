import type {
  AnnotationMark,
  BookmarkMark,
  HighlightMark,
  ReaderMark,
  ReaderMarkAnchor,
} from "../api";

const key = (bookId: string) => `rc:reader-marks:${bookId}`;

export function visibleReaderMarks(marks: ReaderMark[], bookmark: number): ReaderMark[] {
  return marks.filter((mark) => mark.anchor.atom <= bookmark + 1);
}

export function readLocalReaderMarks(
  bookId: string,
  bookmark = Number.MAX_SAFE_INTEGER,
): ReaderMark[] {
  try {
    const value = JSON.parse(localStorage.getItem(key(bookId)) ?? "[]") as unknown;
    return Array.isArray(value) ? visibleReaderMarks(value as ReaderMark[], bookmark) : [];
  } catch {
    return [];
  }
}

export function writeLocalReaderMarks(bookId: string, marks: ReaderMark[]): void {
  localStorage.setItem(key(bookId), JSON.stringify(marks));
}

function base(anchor: ReaderMarkAnchor) {
  const timestamp = new Date().toISOString();
  return {
    id: crypto.randomUUID(),
    anchor,
    version: 1,
    created_at: timestamp,
    updated_at: timestamp,
  };
}

export function localHighlight(
  anchor: ReaderMarkAnchor,
  selectedText: string,
  color: HighlightMark["color"] = "yellow",
): HighlightMark {
  return {
    ...base(anchor), kind: "highlight", color, selected_text: selectedText,
    body: null, label: null,
  };
}

export function localAnnotation(anchor: ReaderMarkAnchor, body: string): AnnotationMark {
  return {
    ...base(anchor), kind: "annotation", body, color: null, selected_text: null, label: null,
  };
}

export function localBookmark(anchor: ReaderMarkAnchor, label?: string): BookmarkMark {
  return {
    ...base(anchor), kind: "bookmark", label: label || null,
    color: null, selected_text: null, body: null,
  };
}

export const HIGHLIGHT_COLORS: Record<HighlightMark["color"], string> = {
  yellow: "#e6c95c",
  green: "#83b88a",
  blue: "#7ea7d8",
  pink: "#d995ad",
};
