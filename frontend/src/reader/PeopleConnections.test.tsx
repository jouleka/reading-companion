import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import type { Graph } from "../api";
import { PeopleConnections } from "./PeopleConnections";

const graph: Graph = {
  as_of_chapter: 5,
  characters: [
    { entity_id: 1, canonical_name: "Fyodor", type: "character", revealed_at: 1 },
    { entity_id: 2, canonical_name: "Mitya", type: "character", revealed_at: 1 },
    { entity_id: 3, canonical_name: "Ivan", type: "character", revealed_at: 2 },
    { entity_id: 4, canonical_name: "Grigory", type: "character", revealed_at: 3 },
    { entity_id: 5, canonical_name: "Isolated", type: "character", revealed_at: 5 },
  ],
  relationships: [
    { edge_id: 10, src_entity: 1, dst_entity: 2, rel_type: "father", label: "father of", revealed_at: 1, invalid_at: null },
    { edge_id: 11, src_entity: 2, dst_entity: 1, rel_type: "debt", label: "owes money to", revealed_at: 4, invalid_at: null },
    { edge_id: 14, src_entity: 1, dst_entity: 2, rel_type: "family", label: "Father-Son Relationship", revealed_at: 5, invalid_at: null },
    { edge_id: 12, src_entity: 1, dst_entity: 3, rel_type: "father", label: "father of", revealed_at: 2, invalid_at: null },
    { edge_id: 13, src_entity: 3, dst_entity: 4, rel_type: "acquaintance", label: "knows", revealed_at: 3, invalid_at: null },
  ],
};

describe("PeopleConnections", () => {
  test("renders one bundled relationship row per person for the selected character", () => {
    render(
      <PeopleConnections
        graph={graph}
        focusId={1}
        onFocus={vi.fn()}
        onOpenCard={vi.fn()}
        onJumpToChapter={vi.fn()}
      />,
    );

    expect(screen.getByRole("heading", { name: "Fyodor" })).toBeTruthy();
    const ledger = screen.getByRole("list", { name: /known connections for Fyodor/i });
    const rows = within(ledger).getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    const mityaRow = within(ledger).getByRole("button", { name: "Mitya" }).closest("li")!;
    expect(within(mityaRow).getByText("father of")).toBeTruthy();
    expect(within(mityaRow).getByText("owes money to")).toBeTruthy();
    expect(within(mityaRow).queryByText("Father-Son Relationship")).toBeNull();
    expect(within(ledger).getByRole("button", { name: "Ivan" })).toBeTruthy();
    expect(within(ledger).queryByText("Grigory")).toBeNull();
  });

  test("searches the cast and focuses a chosen result", () => {
    const onFocus = vi.fn();
    render(
      <PeopleConnections
        graph={graph}
        focusId={1}
        onFocus={onFocus}
        onOpenCard={vi.fn()}
        onJumpToChapter={vi.fn()}
      />,
    );

    const cast = screen.getByRole("navigation", { name: /cast index/i });
    fireEvent.change(within(cast).getByRole("searchbox", { name: /find a person/i }), {
      target: { value: "mitya" },
    });
    expect(within(cast).getByRole("button", { name: /Mitya/i })).toBeTruthy();
    expect(within(cast).queryByRole("button", { name: /Ivan/i })).toBeNull();
    fireEvent.click(within(cast).getByRole("button", { name: /Mitya/i }));
    expect(onFocus).toHaveBeenCalledWith(2);
  });

  test("keeps a large cast compact until the reader asks to show everyone", () => {
    const largeGraph: Graph = {
      as_of_chapter: 40,
      characters: Array.from({ length: 40 }, (_, index) => ({
        entity_id: index + 1,
        canonical_name: `Person ${String(index + 1).padStart(2, "0")}`,
        type: "character",
        revealed_at: index + 1,
      })),
      relationships: [],
    };
    render(
      <PeopleConnections
        graph={largeGraph}
        focusId={1}
        onFocus={vi.fn()}
        onOpenCard={vi.fn()}
        onJumpToChapter={vi.fn()}
      />,
    );

    const cast = screen.getByRole("navigation", { name: /cast index/i });
    const people = () => within(cast).getAllByRole("button").filter((button) =>
      button.closest("li")?.classList.contains("people-index-person"),
    );
    expect(people()).toHaveLength(24);
    fireEvent.click(within(cast).getByRole("button", { name: /show all 40 people/i }));
    expect(people()).toHaveLength(40);
  });

  test("keeps LIT-28 roving keyboard navigation in the relationship-ledger cast index", () => {
    const onFocus = vi.fn();
    render(
      <PeopleConnections
        graph={graph}
        focusId={1}
        onFocus={onFocus}
        onOpenCard={vi.fn()}
        onJumpToChapter={vi.fn()}
      />,
    );

    const cast = screen.getByRole("navigation", { name: /cast index/i });
    const people = within(cast).getAllByRole("button").filter((button) =>
      button.closest("li")?.classList.contains("people-index-person"),
    );
    expect(people).toHaveLength(5);
    expect(people.map((person) => person.tabIndex)).toEqual([0, -1, -1, -1, -1]);

    people[0].focus();
    fireEvent.keyDown(people[0], { key: "ArrowDown" });
    expect(document.activeElement).toBe(people[1]);
    expect(people.map((person) => person.tabIndex)).toEqual([-1, 0, -1, -1, -1]);
    expect(onFocus).not.toHaveBeenCalled();

    fireEvent.keyDown(people[1], { key: "Enter" });
    expect(onFocus).toHaveBeenCalledWith(2);

    fireEvent.keyDown(people[1], { key: "End" });
    expect(document.activeElement).toBe(people[4]);
    fireEvent.keyDown(people[4], { key: "Home" });
    expect(document.activeElement).toBe(people[0]);
    expect(people[0].getAttribute("aria-describedby")).toBeTruthy();
  });

  test("repairs a removed roving person without stealing focus from the cast search", () => {
    const props = {
      focusId: 1,
      onFocus: vi.fn(),
      onOpenCard: vi.fn(),
      onJumpToChapter: vi.fn(),
    };
    const { rerender } = render(<PeopleConnections graph={graph} {...props} />);
    const cast = screen.getByRole("navigation", { name: /cast index/i });
    const people = within(cast).getAllByRole("button").filter((button) =>
      button.closest("li")?.classList.contains("people-index-person"),
    );
    people[4].focus();
    fireEvent.keyDown(people[4], { key: "End" });
    expect(document.activeElement).toBe(people[4]);

    const smaller = { ...graph, characters: graph.characters.slice(0, 2), relationships: graph.relationships.slice(0, 2) };
    rerender(<PeopleConnections graph={smaller} {...props} />);
    const survivors = within(cast).getAllByRole("button").filter((button) =>
      button.closest("li")?.classList.contains("people-index-person"),
    );
    expect(document.activeElement).toBe(survivors[1]);

    const search = within(cast).getByRole("searchbox", { name: /find a person/i });
    search.focus();
    rerender(<PeopleConnections graph={{ ...smaller, characters: smaller.characters.slice(0, 1), relationships: [] }} {...props} />);
    expect(document.activeElement).toBe(search);
  });

  test("searches aliases, displays them, and announces an empty result", () => {
    const graphWithAliases = {
      ...graph,
      characters: graph.characters.map((character) =>
        character.entity_id === 1 ? { ...character, aliases: ["Alyosha", "Alexey"] } : character,
      ),
    } as Graph;
    render(
      <PeopleConnections
        graph={graphWithAliases}
        focusId={1}
        onFocus={vi.fn()}
        onOpenCard={vi.fn()}
        onJumpToChapter={vi.fn()}
      />,
    );

    expect(screen.getByText(/also known as Alyosha, Alexey/i)).toBeTruthy();
    const search = screen.getByRole("searchbox", { name: /find a person/i });
    fireEvent.change(search, { target: { value: "alyosha" } });
    expect(screen.getByRole("button", { name: /Fyodor.*Alyosha/i })).toBeTruthy();

    fireEvent.change(search, { target: { value: "nobody here" } });
    expect(screen.getByRole("status").textContent).toMatch(/no people found/i);
  });

  test("keeps chapter citations and character-card opening as explicit actions", () => {
    const onOpenCard = vi.fn();
    const onJumpToChapter = vi.fn();
    render(
      <PeopleConnections
        graph={graph}
        focusId={1}
        onFocus={vi.fn()}
        onOpenCard={onOpenCard}
        onJumpToChapter={onJumpToChapter}
      />,
    );

    const ledger = screen.getByRole("list", { name: /known connections for Fyodor/i });
    const mityaRow = within(ledger).getByRole("button", { name: "Mitya" }).closest("li")!;
    fireEvent.click(within(mityaRow).getByRole("button", { name: "Chapter 4" }));
    expect(onJumpToChapter).toHaveBeenCalledWith(4);

    fireEvent.click(screen.getByRole("button", { name: /open character card/i }));
    expect(onOpenCard).toHaveBeenCalledTimes(1);
    expect(onOpenCard.mock.calls[0][0]).toBe(1);
    expect(onOpenCard.mock.calls[0][1]).toMatchObject({ left: 0, top: 0, width: 0, height: 0 });
  });

  test("makes the introduction jump explicit and exposes selection changes as a live region", () => {
    const onJumpToChapter = vi.fn();
    const { container } = render(
      <PeopleConnections
        graph={graph}
        focusId={1}
        onFocus={vi.fn()}
        onOpenCard={vi.fn()}
        onJumpToChapter={onJumpToChapter}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /introduced in chapter I/i }));
    expect(onJumpToChapter).toHaveBeenCalledWith(1);
    expect(container.querySelector(".person-entry")?.getAttribute("aria-live")).toBe("polite");
  });

  test("offers a complete register with one row per directed statement", () => {
    const onFocus = vi.fn();
    render(
      <PeopleConnections
        graph={graph}
        focusId={1}
        onFocus={onFocus}
        onOpenCard={vi.fn()}
        onJumpToChapter={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /show all relationships/i }));
    const register = screen.getByRole("list", { name: /relationship register/i });
    expect(within(register).getAllByRole("listitem")).toHaveLength(4);
    const debt = within(register).getByText("owes money to").closest("li")!;
    expect(debt.textContent).toMatch(/Mitya.*owes money to.*Fyodor/);
    expect(within(debt).getByRole("button", { name: "Chapter 4" })).toBeTruthy();
    fireEvent.click(within(register).getByRole("button", { name: "Grigory" }));
    expect(onFocus).toHaveBeenCalledWith(4);
  });

  test("filters and reorders the dense relationship register", () => {
    render(
      <PeopleConnections
        graph={graph}
        focusId={1}
        onFocus={vi.fn()}
        onOpenCard={vi.fn()}
        onJumpToChapter={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /show all relationships/i }));
    const register = screen.getByRole("list", { name: /relationship register/i });

    fireEvent.change(screen.getByRole("searchbox", { name: /filter relationships/i }), {
      target: { value: "grigory" },
    });
    expect(within(register).getAllByRole("listitem")).toHaveLength(1);
    expect(register.textContent).toMatch(/Ivan.*knows.*Grigory/);

    fireEvent.change(screen.getByRole("searchbox", { name: /filter relationships/i }), {
      target: { value: "" },
    });
    fireEvent.change(screen.getByRole("combobox", { name: /relationship kind/i }), {
      target: { value: "debt" },
    });
    expect(within(register).getAllByRole("listitem")).toHaveLength(1);
    expect(register.textContent).toContain("owes money to");

    fireEvent.change(screen.getByRole("combobox", { name: /relationship kind/i }), {
      target: { value: "all" },
    });
    fireEvent.change(screen.getByRole("combobox", { name: /relationship order/i }), {
      target: { value: "recent" },
    });
    expect(within(register).getAllByRole("listitem")[0].textContent).toContain("owes money to");
  });

  test("keeps directed statements and their own chapter in the focused ledger", () => {
    render(
      <PeopleConnections
        graph={graph}
        focusId={1}
        onFocus={vi.fn()}
        onOpenCard={vi.fn()}
        onJumpToChapter={vi.fn()}
      />,
    );

    const ledger = screen.getByRole("list", { name: /known connections for Fyodor/i });
    const mityaRow = within(ledger).getByRole("button", { name: "Mitya" }).closest("li")!;
    const statements = Array.from(mityaRow.querySelectorAll(".connection-statement"));
    expect(statements.map((statement) => statement.textContent)).toEqual([
      expect.stringMatching(/Fyodor.*father of.*Mitya.*Chapter I/),
      expect.stringMatching(/Mitya.*owes money to.*Fyodor.*Chapter IV/),
    ]);
  });

  test("traces one known connection as an ordered textual trail", () => {
    render(
      <PeopleConnections
        graph={graph}
        focusId={1}
        onFocus={vi.fn()}
        onOpenCard={vi.fn()}
        onJumpToChapter={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /trace a thread/i }));
    const directTrail = screen.getByRole("list", { name: /one known connection from Fyodor to Mitya/i });
    expect(within(directTrail).queryByText(/Father-Son Relationship/)).toBeNull();
    expect(directTrail.textContent).toMatch(/Fyodor.*father of.*Mitya/);
    expect(directTrail.textContent).toMatch(/Mitya.*owes money to.*Fyodor/);
    expect(within(directTrail).getByRole("button", { name: "Chapter 1" })).toBeTruthy();
    expect(within(directTrail).getByRole("button", { name: "Chapter 4" })).toBeTruthy();
    fireEvent.change(screen.getByRole("combobox", { name: /connect Fyodor to/i }), {
      target: { value: "4" },
    });
    const trail = screen.getByRole("list", { name: /one known connection from Fyodor to Grigory/i });
    const steps = within(trail).getAllByRole("listitem");
    expect(steps).toHaveLength(2);
    expect(steps[0].textContent).toContain("Fyodor");
    expect(steps[0].textContent).toContain("father of");
    expect(steps[0].textContent).toContain("Ivan");
    expect(steps[1].textContent).toContain("knows");
    expect(steps[1].textContent).toContain("Grigory");

    fireEvent.change(screen.getByRole("combobox", { name: /connect Fyodor to/i }), {
      target: { value: "5" },
    });
    expect(screen.getByText(/no known thread through chapter 5/i)).toBeTruthy();
  });

  test("does not offer thread tracing when fewer than two people are visible", () => {
    render(
      <PeopleConnections
        graph={{ ...graph, characters: graph.characters.slice(0, 1), relationships: [] }}
        focusId={1}
        onFocus={vi.fn()}
        onOpenCard={vi.fn()}
        onJumpToChapter={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button", { name: /trace a thread/i })).toBeNull();
  });
});
