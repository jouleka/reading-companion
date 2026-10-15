/** Codex (LIT-31): the dialog shell where everything works together — chapter breakdown (left leaf),
 * People & Connections (right leaf), foot scrubber, layered name card — all over ONE shared cursor
 * T. These tests own the wiring (fetch-at-T, click-to-retime, scrub-back-shows-less, Escape, axe). */
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { axeAA } from "../test-a11y";

const mocks = vi.hoisted(() => ({
  graph: vi.fn(),
  notes: vi.fn(),
  character: vi.fn(),
  memoryCorrections: vi.fn(),
  correctMemory: vi.fn(),
}));
vi.mock("../api", () => ({ api: mocks }));

import { Codex } from "./Codex";
import { MOBILE_READER_QUERY } from "./mobileShell";

const graphAt = (t: number) => ({
  as_of_chapter: t,
  characters: [
    {
      entity_id: 1,
      canonical_name: "Fyodor",
      type: "character",
      revealed_at: 1,
    },
    ...(t >= 2
      ? [
          {
            entity_id: 2,
            canonical_name: "Mitya",
            type: "character",
            revealed_at: 2,
          },
        ]
      : []),
  ],
  relationships:
    t >= 2
      ? [
          {
            edge_id: 1,
            src_entity: 1,
            dst_entity: 2,
            rel_type: "father",
            label: "father of",
            revealed_at: 2,
            invalid_at: null,
          },
        ]
      : [],
});
const notesAt = (t: number) => ({
  as_of_chapter: t,
  cast: graphAt(t).characters.map((c) => ({
    name: c.canonical_name,
    entity_id: c.entity_id,
  })),
  chapters: Array.from({ length: t }, (_, i) => ({
    chapter_key: `c${i + 1}`,
    revealed_at: i + 1,
    title: `The Leaf ${i + 1}`,
    summary: `Summary ${i + 1}.`,
    new_characters: [],
    events: [],
  })),
});

beforeEach(() => {
  mocks.graph
    .mockReset()
    .mockImplementation((_id: string, t: number = 2) =>
      Promise.resolve(graphAt(t)),
    );
  mocks.notes
    .mockReset()
    .mockImplementation((_id: string, t: number = 2) =>
      Promise.resolve(notesAt(t)),
    );
  mocks.character.mockReset().mockResolvedValue({
    as_of_chapter: 2,
    entity_id: 1,
    name: "Fyodor",
    type: "character",
    aliases: [],
    first_seen: 1,
    status: null,
    ties: [],
  });
  mocks.memoryCorrections
    .mockReset()
    .mockImplementation((_id: string, t: number = 2) =>
      Promise.resolve({ as_of_chapter: t, items: [] }),
    );
  mocks.correctMemory.mockReset().mockResolvedValue({
    as_of_chapter: 2,
    correction_id: 1,
    target_entity_id: 3,
    items: [
      {
        correction_id: 1,
        kind: "replace",
        effective_at: 2,
        source_entities: [{ entity_id: 1, name: "Fyodor" }],
        target_entities: [{ entity_id: 3, name: "Fyodor Pavlovich" }],
        reason: "The extracted name was incomplete.",
        recorded_at: "2026-07-21T12:00:00Z",
      },
    ],
  });
});

afterEach(() => vi.unstubAllGlobals());

describe("Codex", () => {
  test("opens as-of the bookmark: fetches graph+notes at T=bookmark, shows both leaves", async () => {
    render(<Codex bookId="b" bookmark={2} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText("The Leaf 2")).toBeTruthy()); // left leaf entry
    expect(mocks.graph).toHaveBeenCalledWith("b", 2);
    expect(mocks.notes).toHaveBeenCalledWith("b", 2);
    const cast = screen.getByRole("navigation", { name: /cast index/i });
    expect(within(cast).getByRole("button", { name: /Fyodor/ })).toBeTruthy();
    // the scrubber walks 1..bookmark
    expect((screen.getByRole("slider") as HTMLInputElement).max).toBe("2");
  });

  test("publishes a named correction at the exact frontier and shows immutable provenance", async () => {
    const user = userEvent.setup();
    render(<Codex bookId="b" bookmark={2} onClose={() => {}} />);
    await user.click(
      await screen.findByRole("button", { name: "Correct this name" }),
    );
    await user.type(screen.getByLabelText("Correct name"), "Fyodor Pavlovich");
    await user.type(
      screen.getByLabelText("Why this is a correction"),
      "The extracted name was incomplete.",
    );
    await user.click(screen.getByRole("button", { name: "Save correction" }));
    await waitFor(() =>
      expect(mocks.correctMemory).toHaveBeenCalledWith("b", {
        source_entity_id: 1,
        canonical_name: "Fyodor Pavlovich",
        reason: "The extracted name was incomplete.",
        bookmark: 2,
      }),
    );
    expect(
      await screen.findByText("The extracted name was incomplete."),
    ).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Effective Chapter II" }),
    ).toBeTruthy();
  });

  test("clicking a chapter re-times graph+cast to that T; the chapter index stays a full ToC", async () => {
    render(<Codex bookId="b" bookmark={2} onClose={() => {}} />);
    await waitFor(() =>
      expect(
        within(
          screen.getByRole("navigation", { name: /cast index/i }),
        ).getByRole("button", { name: /Mitya/ }),
      ).toBeTruthy(),
    );
    fireEvent.click(screen.getByRole("button", { name: /The Leaf 1/ }));
    await waitFor(() => expect(mocks.graph).toHaveBeenCalledWith("b", 1));
    await waitFor(() =>
      expect(
        within(
          screen.getByRole("navigation", { name: /cast index/i }),
        ).queryByRole("button", { name: /Mitya/ }),
      ).toBeNull(),
    ); // as-of 1: Mitya (revealed_at 2) is gone from the cast — scrubbing back never shows more
    // …but the breakdown does NOT truncate: it is a stable ToC of 1..bookmark (all already read),
    // fetched once at the frontier — never re-fetched at T (the spec's chosen model)
    expect(screen.getByRole("button", { name: /The Leaf 2/ })).toBeTruthy();
    expect(mocks.notes).toHaveBeenCalledTimes(1);
    expect(mocks.notes).toHaveBeenCalledWith("b", 2);
  });

  test("selecting the current chapter again preserves its loaded memory", async () => {
    render(<Codex bookId="b" bookmark={2} onClose={() => {}} />);
    const cast = await screen.findByRole("navigation", { name: /cast index/i });
    fireEvent.click(screen.getByRole("button", { name: /The Leaf 2/ }));
    expect(screen.getByRole("navigation", { name: /cast index/i })).toBe(cast);
    expect(within(cast).getByRole("button", { name: /Mitya/ })).toBeTruthy();
    expect(screen.queryByText(/Gathering the codex/i)).toBeNull();
    expect(mocks.graph).toHaveBeenCalledTimes(1);
  });

  test("hides the later graph immediately while a scrub-back request is pending", async () => {
    let resolveEarlier!: (value: ReturnType<typeof graphAt>) => void;
    mocks.graph.mockImplementation((_id: string, t: number) =>
      t === 1
        ? new Promise((resolve) => {
            resolveEarlier = resolve;
          })
        : Promise.resolve(graphAt(t)),
    );
    render(<Codex bookId="b" bookmark={2} onClose={() => {}} />);
    const cast = await screen.findByRole("navigation", { name: /cast index/i });
    expect(within(cast).getByRole("button", { name: /Mitya/ })).toBeTruthy();

    const chapter = screen.getByRole("button", { name: /The Leaf 1/ });
    fireEvent.click(chapter);
    expect(screen.getByText(/Gathering the codex/i)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Mitya/ })).toBeNull();
    expect(screen.getByRole("button", { name: /The Leaf 1/ })).toBe(chapter);

    resolveEarlier(graphAt(1));
    await screen.findByRole("navigation", { name: /cast index/i });
  });

  test("an out-of-order chapter response never replaces the current memory", async () => {
    let resolveEarlier!: (value: ReturnType<typeof graphAt>) => void;
    mocks.graph.mockImplementation((_id: string, t: number) =>
      t === 1
        ? new Promise((resolve) => {
            resolveEarlier = resolve;
          })
        : Promise.resolve(graphAt(t)),
    );
    render(<Codex bookId="b" bookmark={2} onClose={() => {}} />);
    await screen.findByRole("navigation", { name: /cast index/i });
    fireEvent.click(screen.getByRole("button", { name: /The Leaf 1/ }));
    fireEvent.click(screen.getByRole("button", { name: /The Leaf 2/ }));
    await screen.findByRole("navigation", { name: /cast index/i });
    await act(async () => resolveEarlier(graphAt(1)));
    expect(
      within(screen.getByRole("navigation", { name: /cast index/i })).getByRole(
        "button",
        { name: /Mitya/ },
      ),
    ).toBeTruthy();
    expect((screen.getByRole("slider") as HTMLInputElement).value).toBe("2");
  });

  test("a memory failure preserves chapter navigation and can be retried", async () => {
    mocks.graph.mockRejectedValueOnce(new Error("offline"));
    render(<Codex bookId="b" bookmark={2} onClose={() => {}} />);
    const retry = await screen.findByRole("button", { name: "Retry memory" });
    expect(screen.getByRole("button", { name: /The Leaf 1/ })).toBeTruthy();
    fireEvent.click(retry);
    expect(
      await screen.findByRole("navigation", { name: /cast index/i }),
    ).toBeTruthy();
    expect(mocks.notes).toHaveBeenCalledTimes(1);
  });

  test("a chapter-note failure does not hide memory or disappear when the cursor changes", async () => {
    mocks.notes.mockRejectedValueOnce(new Error("offline"));
    render(<Codex bookId="b" bookmark={2} onClose={() => {}} />);
    await screen.findByRole("button", { name: "Retry chapter notes" });
    expect(
      await screen.findByRole("navigation", { name: /cast index/i }),
    ).toBeTruthy();
    fireEvent.change(screen.getByRole("slider"), { target: { value: "1" } });
    await waitFor(() => expect(mocks.graph).toHaveBeenCalledWith("b", 1));
    expect(
      screen.getByRole("button", { name: "Retry chapter notes" }),
    ).toBeTruthy();
    fireEvent.click(
      screen.getByRole("button", { name: "Retry chapter notes" }),
    );
    expect(
      await screen.findByRole("button", { name: /The Leaf 1/ }),
    ).toBeTruthy();
  });

  test("an unavailable correction history is distinct from an empty history and can be retried", async () => {
    mocks.memoryCorrections.mockRejectedValueOnce(new Error("offline"));
    render(<Codex bookId="b" bookmark={2} onClose={() => {}} />);
    fireEvent.click(
      await screen.findByRole("button", { name: /Memory correction history/ }),
    );
    const retry = await screen.findByRole("button", {
      name: "Retry correction history",
    });
    expect(
      screen.queryByText("No corrections are visible at this chapter."),
    ).toBeNull();
    fireEvent.click(retry);
    expect(
      await screen.findByText("No corrections are visible at this chapter."),
    ).toBeTruthy();
    expect(mocks.graph).toHaveBeenCalledTimes(1);
  });

  test("a correction finishing after a rewind cannot insert frontier memory into the earlier chapter", async () => {
    let resolveCorrection!: (value: {
      as_of_chapter: number;
      target_entity_id: number;
      items: [];
    }) => void;
    mocks.correctMemory.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveCorrection = resolve;
        }),
    );
    const user = userEvent.setup();
    render(<Codex bookId="b" bookmark={2} onClose={() => {}} />);
    await user.click(
      await screen.findByRole("button", { name: "Correct this name" }),
    );
    await user.type(screen.getByLabelText("Correct name"), "Fyodor Pavlovich");
    await user.type(
      screen.getByLabelText("Why this is a correction"),
      "The name was incomplete.",
    );
    await user.click(screen.getByRole("button", { name: "Save correction" }));
    fireEvent.click(screen.getByRole("button", { name: /The Leaf 1/ }));
    await screen.findByRole("navigation", { name: /cast index/i });
    await act(async () =>
      resolveCorrection({ as_of_chapter: 2, target_entity_id: 3, items: [] }),
    );
    expect(
      within(
        screen.getByRole("navigation", { name: /cast index/i }),
      ).queryByRole("button", { name: /Mitya/ }),
    ).toBeNull();
    expect(screen.getByRole("heading", { name: "Fyodor" })).toBeTruthy();
    expect((screen.getByRole("slider") as HTMLInputElement).value).toBe("1");
  });

  test("a cast click opens the chosen character's relationship entry", async () => {
    render(<Codex bookId="b" bookmark={2} onClose={() => {}} />);
    const cast = await screen.findByRole("navigation", { name: /cast index/i });
    fireEvent.click(within(cast).getByRole("button", { name: /Mitya/ }));
    expect(screen.getByRole("heading", { name: "Mitya" })).toBeTruthy();
    expect(
      screen.getByRole("list", { name: /known connections for Mitya/i }),
    ).toBeTruthy();
  });

  test("Tab traverses interior controls instead of jumping from close to the scrubber", async () => {
    const user = userEvent.setup();
    render(<Codex bookId="b" bookmark={2} onClose={() => {}} />);
    await screen.findByText("The Leaf 2");
    const close = screen.getByRole("button", { name: /close/i });
    expect(document.activeElement).toBe(close);

    await user.tab();
    expect(document.activeElement).toBe(
      screen.getByRole("searchbox", { name: /find a chapter/i }),
    );

    await user.tab();
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: /The Leaf 1/ }),
    );

    const search = screen.getByRole("searchbox", { name: /find a person/i });
    search.focus();
    await user.tab();
    expect(document.activeElement).toBe(
      within(screen.getByRole("navigation", { name: /cast index/i })).getByRole(
        "button",
        { name: /Fyodor/ },
      ),
    );
  });

  test("Escape closes; no axe violations", async () => {
    const onClose = vi.fn();
    const { container } = render(
      <Codex bookId="b" bookmark={2} onClose={onClose} />,
    );
    await waitFor(() => expect(screen.getByText("The Leaf 2")).toBeTruthy());
    expect(await axeAA(container)).toHaveNoViolations();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalled();
  });

  test("a reference book with no people keeps section notes and explains the omitted people panel", async () => {
    mocks.graph.mockResolvedValue({
      as_of_chapter: 2,
      characters: [],
      relationships: [],
    });
    render(
      <Codex bookId="b" bookmark={2} bookType="reference" onClose={() => {}} />,
    );
    expect(await screen.findByText("The Leaf 2")).toBeTruthy();
    expect(
      screen.getByRole("heading", { name: /section by section/i }),
    ).toBeTruthy();
    expect(
      screen.queryByRole("navigation", { name: /cast index/i }),
    ).toBeNull();
    expect(
      screen.getByText(/people and plot sections stay out of the way/i),
    ).toBeTruthy();
  });

  test("the established empty-novel state remains unchanged", async () => {
    mocks.graph.mockResolvedValue({
      as_of_chapter: 2,
      characters: [],
      relationships: [],
    });
    render(<Codex bookId="b" bookmark={2} onClose={() => {}} />);
    expect(
      await screen.findByText("No one has stepped onto the page yet."),
    ).toBeTruthy();
    expect(screen.queryByText("The Leaf 2")).toBeNull();
  });

  test("compact views retain search state when switching tabs and support tab keyboard navigation", async () => {
    vi.stubGlobal(
      "matchMedia",
      vi.fn((media: string) => ({
        media,
        matches: media === MOBILE_READER_QUERY,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      })),
    );
    const user = userEvent.setup();
    const { container } = render(
      <Codex bookId="b" bookmark={2} onClose={() => {}} />,
    );
    const chapters = screen.getByRole("tab", { name: "Chapters" });
    const people = screen.getByRole("tab", { name: "People & connections" });
    expect(chapters.getAttribute("aria-selected")).toBe("true");
    await user.type(
      await screen.findByRole("searchbox", { name: "Find a chapter" }),
      "Leaf 1",
    );
    expect(screen.queryByRole("button", { name: /The Leaf 2/ })).toBeNull();

    await user.click(people);
    expect(
      screen.queryByRole("searchbox", { name: "Find a chapter" }),
    ).toBeNull();
    await user.type(
      screen.getByRole("searchbox", { name: "Find a person" }),
      "Mitya",
    );
    chapters.focus();
    await user.keyboard("{Home}");
    expect(document.activeElement).toBe(chapters);
    expect(
      (
        screen.getByRole("searchbox", {
          name: "Find a chapter",
        }) as HTMLInputElement
      ).value,
    ).toBe("Leaf 1");
    await user.keyboard("{ArrowRight}");
    expect(document.activeElement).toBe(people);
    expect(people.getAttribute("aria-selected")).toBe("true");
    expect(
      (
        screen.getByRole("searchbox", {
          name: "Find a person",
        }) as HTMLInputElement
      ).value,
    ).toBe("Mitya");
    expect(mocks.graph).toHaveBeenCalledTimes(1);
    expect(mocks.notes).toHaveBeenCalledTimes(1);
    expect(await axeAA(container)).toHaveNoViolations();
  });

  test("a compact relationship citation reveals the chapter panel, clears its filter, and preserves visible focus", async () => {
    vi.stubGlobal(
      "matchMedia",
      vi.fn((media: string) => ({
        media,
        matches: media === MOBILE_READER_QUERY,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      })),
    );
    const user = userEvent.setup();
    render(<Codex bookId="b" bookmark={2} onClose={() => {}} />);
    await user.type(
      await screen.findByRole("searchbox", { name: "Find a chapter" }),
      "no matching chapter",
    );
    await user.click(screen.getByRole("tab", { name: "People & connections" }));
    await user.click(await screen.findByRole("button", { name: "Chapter 2" }));
    expect(
      screen
        .getByRole("tab", { name: "Chapters" })
        .getAttribute("aria-selected"),
    ).toBe("true");
    expect(document.activeElement).toBe(
      screen.getByRole("tab", { name: "Chapters" }),
    );
    expect(
      await screen.findByRole("button", { name: /The Leaf 2/ }),
    ).toBeTruthy();
    expect(
      (
        screen.getByRole("searchbox", {
          name: "Find a chapter",
        }) as HTMLInputElement
      ).value,
    ).toBe("");
  });

  test("scrubbing compact people memory keeps the selected view while loading an earlier chapter", async () => {
    vi.stubGlobal(
      "matchMedia",
      vi.fn((media: string) => ({
        media,
        matches: media === MOBILE_READER_QUERY,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      })),
    );
    const user = userEvent.setup();
    render(<Codex bookId="b" bookmark={2} onClose={() => {}} />);
    await user.click(screen.getByRole("tab", { name: "People & connections" }));
    await screen.findByRole("navigation", { name: /cast index/i });
    fireEvent.change(screen.getByRole("slider"), { target: { value: "1" } });
    await waitFor(() => expect(mocks.graph).toHaveBeenLastCalledWith("b", 1));
    expect(
      screen
        .getByRole("tab", { name: "People & connections" })
        .getAttribute("aria-selected"),
    ).toBe("true");
    expect(
      screen.queryByRole("searchbox", { name: "Find a chapter" }),
    ).toBeNull();
  });
});
