/** ChapterBreakdown (LIT-31): the codex's left leaf — the story broken down, chapter by chapter
 * (summary + highlights: who first appears + that chapter's events). Character names are chips that
 * open cards; clicking a chapter re-times the whole spread to that point; the current chapter is
 * marked. Names get wrapped OUT of the summary/event text, so getByText matches the plain remainder. */
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import type { CastMember, ChapterNote } from "../api";
import { axeAA } from "../test-a11y";
import { ChapterBreakdown } from "./ChapterBreakdown";

const cast: CastMember[] = [
  { name: "Fyodor", entity_id: 1 },
  { name: "Mitya", entity_id: 2 },
];
const chapters: ChapterNote[] = [
  { chapter_key: "c1", revealed_at: 1, title: "The Elder", summary: "Fyodor arrives.",
    new_characters: [{ entity_id: 1, name: "Fyodor" }], events: ["Fyodor buys the estate."] },
  { chapter_key: "c2", revealed_at: 2, title: "The Sons", summary: "Mitya returns to Fyodor.",
    new_characters: [{ entity_id: 2, name: "Mitya" }], events: ["Mitya quarrels with Fyodor."] },
];

describe("ChapterBreakdown", () => {
  afterEach(() => vi.restoreAllMocks());

  test("opens only the current chapter by default; clicking another re-times and moves disclosure", () => {
    const onSelect = vi.fn();
    render(<ChapterBreakdown chapters={chapters} cast={cast} currentChapter={2}
                             scrollTo={null} onSelectChapter={onSelect} onOpenCard={() => {}} />);
    expect(screen.getByText(/quarrels with/)).toBeTruthy();   // ch2 event
    expect(screen.queryByText(/buys the estate/)).toBeNull(); // ch1 is collapsed
    fireEvent.click(screen.getByRole("button", { name: /The Elder/ }));
    expect(onSelect).toHaveBeenCalledWith(1);
    expect(screen.getByText(/buys the estate/)).toBeTruthy();
    expect(screen.queryByText(/quarrels with/)).toBeNull();
    expect(screen.getByRole("button", { name: /The Elder/ }).getAttribute("aria-expanded")).toBe("true");
  });

  test("searches already-read chapter titles and summaries without expanding every result", () => {
    render(<ChapterBreakdown chapters={chapters} cast={cast} currentChapter={2}
                             scrollTo={null} onSelectChapter={() => {}} onOpenCard={() => {}} />);
    fireEvent.change(screen.getByRole("searchbox", { name: /find a chapter/i }), {
      target: { value: "Elder" },
    });
    expect(screen.getByRole("button", { name: /The Elder/ })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /The Sons/ })).toBeNull();
    expect(screen.queryByText(/buys the estate/)).toBeNull();
  });

  test("the current chapter is marked; names are chips that open cards", () => {
    const onOpenCard = vi.fn();
    render(<ChapterBreakdown chapters={chapters} cast={cast} currentChapter={2}
                             scrollTo={null} onSelectChapter={() => {}} onOpenCard={onOpenCard} />);
    expect(screen.getByRole("button", { name: /The Sons/ }).getAttribute("aria-current")).toBe("true");
    // a name-chip (in the summary / event / "new here" line) opens the card (LIT-30 mechanism, reused)
    fireEvent.click(screen.getAllByRole("button", { name: "Fyodor" })[0]);
    expect(onOpenCard).toHaveBeenCalledWith(1, expect.anything());
  });

  test("replays a jump to the same chapter when it is explicitly requested again", () => {
    const scrollIntoView = vi.fn();
    Object.defineProperty(HTMLElement.prototype, "scrollIntoView", { configurable: true, value: scrollIntoView });
    const { rerender } = render(
      <ChapterBreakdown chapters={chapters} cast={cast} currentChapter={2}
                        scrollTo={{ chapter: 1, requestId: 1 } as never}
                        onSelectChapter={() => {}} onOpenCard={() => {}} />,
    );
    expect(scrollIntoView).toHaveBeenCalledTimes(1);

    rerender(
      <ChapterBreakdown chapters={chapters} cast={cast} currentChapter={2}
                        scrollTo={{ chapter: 1, requestId: 2 } as never}
                        onSelectChapter={() => {}} onOpenCard={() => {}} />,
    );
    expect(scrollIntoView).toHaveBeenCalledTimes(2);
  });

  test("no axe violations", async () => {
    const { container } = render(<ChapterBreakdown chapters={chapters} cast={cast} currentChapter={1}
                             scrollTo={null} onSelectChapter={() => {}} onOpenCard={() => {}} />);
    expect(await axeAA(container)).toHaveNoViolations();
  });
});
