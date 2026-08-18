/** Codex (LIT-31): the dialog shell where everything works together — chapter breakdown (left leaf),
 * People & Connections (right leaf), foot scrubber, layered name card — all over ONE shared cursor
 * T. These tests own the wiring (fetch-at-T, click-to-retime, scrub-back-shows-less, Escape, axe). */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, test, vi } from "vitest";
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

const graphAt = (t: number) => ({
  as_of_chapter: t,
  characters: [
    { entity_id: 1, canonical_name: "Fyodor", type: "character", revealed_at: 1 },
    ...(t >= 2 ? [{ entity_id: 2, canonical_name: "Mitya", type: "character", revealed_at: 2 }] : []),
  ],
  relationships:
    t >= 2
      ? [{ edge_id: 1, src_entity: 1, dst_entity: 2, rel_type: "father", label: "father of", revealed_at: 2, invalid_at: null }]
      : [],
});
const notesAt = (t: number) => ({
  as_of_chapter: t,
  cast: graphAt(t).characters.map((c) => ({ name: c.canonical_name, entity_id: c.entity_id })),
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
  mocks.graph.mockReset().mockImplementation((_id: string, t: number = 2) => Promise.resolve(graphAt(t)));
  mocks.notes.mockReset().mockImplementation((_id: string, t: number = 2) => Promise.resolve(notesAt(t)));
  mocks.character.mockReset().mockResolvedValue({
    as_of_chapter: 2, entity_id: 1, name: "Fyodor", type: "character",
    aliases: [], first_seen: 1, status: null, ties: [],
  });
  mocks.memoryCorrections.mockReset().mockImplementation((_id: string, t: number = 2) =>
    Promise.resolve({ as_of_chapter: t, items: [] }));
  mocks.correctMemory.mockReset().mockResolvedValue({
    as_of_chapter: 2,
    correction_id: 1,
    target_entity_id: 3,
    items: [{
      correction_id: 1,
      kind: "replace",
      effective_at: 2,
      source_entities: [{ entity_id: 1, name: "Fyodor" }],
      target_entities: [{ entity_id: 3, name: "Fyodor Pavlovich" }],
      reason: "The extracted name was incomplete.",
      recorded_at: "2026-07-21T12:00:00Z",
    }],
  });
});

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
    await user.click(await screen.findByRole("button", { name: "Correct this name" }));
    await user.type(screen.getByLabelText("Correct name"), "Fyodor Pavlovich");
    await user.type(
      screen.getByLabelText("Why this is a correction"),
      "The extracted name was incomplete.",
    );
    await user.click(screen.getByRole("button", { name: "Save correction" }));
    await waitFor(() => expect(mocks.correctMemory).toHaveBeenCalledWith("b", {
      source_entity_id: 1,
      canonical_name: "Fyodor Pavlovich",
      reason: "The extracted name was incomplete.",
      bookmark: 2,
    }));
    expect(await screen.findByText("The extracted name was incomplete.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Effective Chapter II" })).toBeTruthy();
  });

  test("clicking a chapter re-times graph+cast to that T; the chapter index stays a full ToC", async () => {
    render(<Codex bookId="b" bookmark={2} onClose={() => {}} />);
    await waitFor(() =>
      expect(within(screen.getByRole("navigation", { name: /cast index/i })).getByRole("button", { name: /Mitya/ })).toBeTruthy(),
    );
    fireEvent.click(screen.getByRole("button", { name: /The Leaf 1/ }));
    await waitFor(() => expect(mocks.graph).toHaveBeenCalledWith("b", 1));
    await waitFor(() =>
      expect(within(screen.getByRole("navigation", { name: /cast index/i })).queryByRole("button", { name: /Mitya/ })).toBeNull(),
    ); // as-of 1: Mitya (revealed_at 2) is gone from the cast — scrubbing back never shows more
    // …but the breakdown does NOT truncate: it is a stable ToC of 1..bookmark (all already read),
    // fetched once at the frontier — never re-fetched at T (the spec's chosen model)
    expect(screen.getByRole("button", { name: /The Leaf 2/ })).toBeTruthy();
    expect(mocks.notes).toHaveBeenCalledTimes(1);
    expect(mocks.notes).toHaveBeenCalledWith("b", 2);
  });

  test("hides the later graph immediately while a scrub-back request is pending", async () => {
    let resolveEarlier!: (value: ReturnType<typeof graphAt>) => void;
    mocks.graph.mockImplementation((_id: string, t: number) =>
      t === 1 ? new Promise((resolve) => { resolveEarlier = resolve; }) : Promise.resolve(graphAt(t)),
    );
    render(<Codex bookId="b" bookmark={2} onClose={() => {}} />);
    const cast = await screen.findByRole("navigation", { name: /cast index/i });
    expect(within(cast).getByRole("button", { name: /Mitya/ })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /The Leaf 1/ }));
    expect(screen.getByText(/Gathering the codex/i)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Mitya/ })).toBeNull();

    resolveEarlier(graphAt(1));
    await screen.findByRole("navigation", { name: /cast index/i });
  });

  test("a cast click opens the chosen character's relationship entry", async () => {
    render(<Codex bookId="b" bookmark={2} onClose={() => {}} />);
    const cast = await screen.findByRole("navigation", { name: /cast index/i });
    fireEvent.click(within(cast).getByRole("button", { name: /Mitya/ }));
    expect(screen.getByRole("heading", { name: "Mitya" })).toBeTruthy();
    expect(screen.getByRole("list", { name: /known connections for Mitya/i })).toBeTruthy();
  });

  test("Tab traverses interior controls instead of jumping from close to the scrubber", async () => {
    const user = userEvent.setup();
    render(<Codex bookId="b" bookmark={2} onClose={() => {}} />);
    await screen.findByText("The Leaf 2");
    const close = screen.getByRole("button", { name: /close/i });
    expect(document.activeElement).toBe(close);

    await user.tab();
    expect(document.activeElement).toBe(screen.getByRole("searchbox", { name: /find a chapter/i }));

    await user.tab();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: /The Leaf 1/ }));

    const search = screen.getByRole("searchbox", { name: /find a person/i });
    search.focus();
    await user.tab();
    expect(document.activeElement).toBe(within(screen.getByRole("navigation", { name: /cast index/i }))
      .getByRole("button", { name: /Fyodor/ }));
  });

  test("Escape closes; no axe violations", async () => {
    const onClose = vi.fn();
    const { container } = render(<Codex bookId="b" bookmark={2} onClose={onClose} />);
    await waitFor(() => expect(screen.getByText("The Leaf 2")).toBeTruthy());
    expect(await axeAA(container)).toHaveNoViolations();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalled();
  });

  test("a reference book with no people keeps section notes and explains the omitted people panel", async () => {
    mocks.graph.mockResolvedValue({ as_of_chapter: 2, characters: [], relationships: [] });
    render(<Codex bookId="b" bookmark={2} bookType="reference" onClose={() => {}} />);
    expect(await screen.findByText("The Leaf 2")).toBeTruthy();
    expect(screen.getByRole("heading", { name: /section by section/i })).toBeTruthy();
    expect(screen.queryByRole("navigation", { name: /cast index/i })).toBeNull();
    expect(screen.getByText(/people and plot sections stay out of the way/i)).toBeTruthy();
  });

  test("the established empty-novel state remains unchanged", async () => {
    mocks.graph.mockResolvedValue({ as_of_chapter: 2, characters: [], relationships: [] });
    render(<Codex bookId="b" bookmark={2} onClose={() => {}} />);
    expect(await screen.findByText("No one has stepped onto the page yet.")).toBeTruthy();
    expect(screen.queryByText("The Leaf 2")).toBeNull();
  });
});
