import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { axeAA } from "../test-a11y";

const mocks = vi.hoisted(() => ({ selectionAction: vi.fn() }));
vi.mock("../api", () => ({ api: { selectionAction: mocks.selectionAction } }));

import { SelectionAssistant } from "./SelectionAssistant";

const cost = {
  currency: "USD" as const,
  usd: "0.0000100000",
  input_tokens: 30,
  output_tokens: 10,
  pricing_known: true,
  calls: [{ provider: "openai-compatible", model: "gpt-4o-mini", usd: "0.0000100000" }],
  payer: "your configured provider account",
};
const selection = {
  anchor: { cfi: "epubcfi(/6/4!/4/2)", atom: 2 },
  text: "The lantern guttered in the rain.",
};

beforeEach(() => mocks.selectionAction.mockReset());

describe("SelectionAssistant", () => {
  test("explains only the selection, returns to its exact anchor, exposes cost, and passes axe", async () => {
    mocks.selectionAction.mockResolvedValue({
      action: "explain",
      as_of_chapter: 1,
      insufficient_evidence: false,
      text: "The flame became weak and unsteady because of the rain.",
      citation: {
        id: 1, ordinal: 2, chapter_key: "2", href: "two.xhtml", title: "Chapter II",
        excerpt: selection.text, cfi: selection.anchor.cfi,
      },
      cost,
    });
    const onNavigate = vi.fn();
    const { container } = render(
      <StrictMode>
        <SelectionAssistant
          bookId="b"
          action="explain"
          selection={selection}
          onNavigate={onNavigate}
          onClose={() => {}}
        />
      </StrictMode>,
    );
    expect(await screen.findByText(/flame became weak/i)).toBeTruthy();
    expect(screen.getByText(/Provider cost: \$0\.000010 USD/i)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /return to chapter ii/i }));
    expect(onNavigate).toHaveBeenCalledWith(selection.anchor.cfi);
    expect(mocks.selectionAction).toHaveBeenCalledWith("b", {
      action: "explain", text: selection.text, atom: 2, cfi: selection.anchor.cfi,
    });
    expect(mocks.selectionAction).toHaveBeenCalledTimes(1);
    expect(await axeAA(container)).toHaveNoViolations();
  });

  test("dismisses with Escape and restores focus to the action button", async () => {
    mocks.selectionAction.mockResolvedValue({
      action: "define", as_of_chapter: 1, insufficient_evidence: true,
      text: null, citation: null, cost,
    });
    const opener = document.createElement("button");
    document.body.append(opener);
    opener.focus();
    const onClose = vi.fn();
    const { unmount } = render(
      <SelectionAssistant
        bookId="b" action="define" selection={selection} onNavigate={() => {}} onClose={onClose}
      />,
    );
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
    unmount();
    await waitFor(() => expect(opener).toBe(document.activeElement));
    opener.remove();
  });
});
