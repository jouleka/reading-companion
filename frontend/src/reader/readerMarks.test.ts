import { describe, expect, test } from "vitest";
import type { ReaderMark } from "../api";
import { readLocalReaderMarks, visibleReaderMarks, writeLocalReaderMarks } from "./readerMarks";

const mark = (atom: number): ReaderMark => ({
  id: String(atom), kind: "bookmark", anchor: { cfi: `epubcfi(/6/${atom * 2})`, atom },
  label: `chapter ${atom}`, color: null, selected_text: null, body: null,
  version: 1, created_at: "now", updated_at: "now",
});

describe("reader mark frontier", () => {
  test("a new reading pass hides marks beyond its current chapter", () => {
    expect(visibleReaderMarks([mark(1), mark(2), mark(8)], 0).map((item) => item.anchor.atom))
      .toEqual([1]);
    expect(visibleReaderMarks([mark(1), mark(2), mark(8)], 1).map((item) => item.anchor.atom))
      .toEqual([1, 2]);
  });

  test("the local-mode fallback keeps the same portable payload and boundary", () => {
    localStorage.clear();
    writeLocalReaderMarks("book", [mark(1), mark(4)]);
    expect(readLocalReaderMarks("book", 0)).toEqual([mark(1)]);
    expect(readLocalReaderMarks("book").map((item) => item.anchor.atom)).toEqual([1, 4]);
  });
});
