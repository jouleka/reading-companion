import { describe, expect, test } from "vitest";
import { createReaderStylesheets } from "../vendor/foliate-js/paginator.js";
import { fromRange, parse, toRange } from "../vendor/foliate-js/epubcfi.js";

const namespace = "http://www.w3.org/1999/xhtml";
const parseChapter = (content: string) =>
  new DOMParser().parseFromString(
    `<html xmlns="${namespace}">${content}</html>`,
    "application/xhtml+xml",
  );

describe("reader styles in XHTML chapters", () => {
  test("creates active XHTML styles when a chapter omits its head", () => {
    const doc = parseChapter("<body><p>A chapter</p></body>");
    const originalRange = doc.createRange();
    originalRange.setStart(doc.querySelector("p")!.firstChild!, 2);
    originalRange.setEnd(doc.querySelector("p")!.firstChild!, 9);
    const savedBookmark = fromRange(originalRange);
    const [before, after] = createReaderStylesheets(doc);
    after.textContent = "p { line-height: 1.6; }";

    expect(doc.head).not.toBeNull();
    expect(doc.documentElement.firstElementChild).toBe(doc.body);
    expect(before.namespaceURI).toBe(namespace);
    expect(after.namespaceURI).toBe(namespace);
    expect(after).toBeInstanceOf(HTMLStyleElement);
    expect(doc.head?.querySelectorAll("style")).toHaveLength(2);
    expect(doc.body.textContent).toBe("A chapter");
    expect(toRange(doc, parse(savedBookmark)).toString()).toBe(
      originalRange.toString(),
    );
  });

  test("keeps publisher styles between the reader's two override layers", () => {
    const doc = parseChapter(
      '<head><style id="publisher">p { color: black; }</style></head><body><p>A chapter</p></body>',
    );
    const publisher = doc.getElementById("publisher");
    const [before, after] = createReaderStylesheets(doc);

    expect(Array.from(doc.head!.children)).toEqual([before, publisher, after]);
    expect(doc.querySelectorAll("head")).toHaveLength(1);
    expect(publisher?.textContent).toContain("color: black");
  });
});
