import { describe, expect, test } from "vitest";
import { mapReaderToc, safeTocLabel, tocContainsAtom } from "./readerNavigation";

const atoms = [
  { ordinal: 1, href: "part/ch1.xhtml", title: "The Arrival", part_label: "Part I", char_len: 10 },
  { ordinal: 2, href: "part/ch2.xhtml", title: "", part_label: "", char_len: 10 },
];

describe("reader navigation model", () => {
  test("preserves nested EPUB hierarchy and maps percent/query variants to spine atoms", () => {
    const toc = mapReaderToc([
      { label: "raw part title", subitems: [
        { label: "raw chapter", href: "part/ch1.xhtml?edition=1" },
        { label: "future spoiler", href: "part/ch2.xhtml" },
      ] },
    ], ["part/ch1.xhtml", "part/ch2.xhtml"], [0, 1], atoms);
    expect(toc[0].children.map((item) => item.atom)).toEqual([0, 1]);
    expect(tocContainsAtom(toc[0], 1)).toBe(true);
  });

  test("never exposes raw future labels and uses released manifest labels", () => {
    const toc = mapReaderToc([
      { label: "raw part title", subitems: [
        { label: "raw chapter", href: "part/ch1.xhtml" },
        { label: "the murderer revealed", href: "part/ch2.xhtml" },
      ] },
    ], ["part/ch1.xhtml", "part/ch2.xhtml"], [0, 1], atoms);
    expect(safeTocLabel(toc[0], atoms)).toBe("Part I");
    expect(safeTocLabel(toc[0].children[0], atoms)).toBe("The Arrival");
    expect(safeTocLabel(toc[0].children[1], atoms)).toBe("Chapter 2");
    expect(JSON.stringify(toc)).not.toContain("murderer");
  });

  test("maps multiple anchor TOC leaves in one spine file to distinct atoms", () => {
    const anchoredAtoms = [
      { ...atoms[0], href: "whole.xhtml" },
      { ...atoms[1], href: "whole.xhtml" },
    ];
    const toc = mapReaderToc([
      { href: "whole.xhtml#one" },
      { href: "whole.xhtml#two" },
    ], ["whole.xhtml"], [0], anchoredAtoms);
    expect(toc.map((item) => item.atom)).toEqual([0, 1]);
  });
});
