import { act, fireEvent, render, screen } from "@testing-library/react";
import { StrictMode } from "react";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { axeAA } from "../test-a11y";

const mocks = vi.hoisted(() => ({ chapterCloseout: vi.fn() }));
vi.mock("../api", () => ({ api: { chapterCloseout: mocks.chapterCloseout } }));

import { ChapterCloseout } from "./ChapterCloseout";

const citation = {
  id: 1,
  ordinal: 2,
  chapter_key: "2",
  href: "two.xhtml",
  title: "Chapter II",
  excerpt: "Berenice met Aldric at the forge.",
};
const answer = {
  chapter: 2,
  as_of_chapter: 2,
  insufficient_evidence: false,
  claims: [{ text: "Berenice met Aldric at the forge.", citation_ids: [1] }],
  citations: [citation],
  cost: {
    currency: "USD" as const,
    usd: "0.0000120000",
    input_tokens: 50,
    output_tokens: 15,
    pricing_known: true,
    calls: [
      { provider: "anthropic", model: "claude-haiku", usd: "0.0000120000" },
    ],
    payer: "your configured provider account",
  },
};

beforeEach(() => {
  mocks.chapterCloseout.mockReset();
});

describe("ChapterCloseout", () => {
  test("renders cited takeaways, navigates to evidence, exposes cost, and passes axe", async () => {
    mocks.chapterCloseout.mockResolvedValue(answer);
    const onNavigate = vi.fn();
    const { container } = render(
      <StrictMode>
        <ChapterCloseout
          bookId="b"
          chapter={2}
          onNavigate={onNavigate}
          onClose={() => {}}
        />
      </StrictMode>,
    );
    expect(
      await screen.findByText(/^Berenice met Aldric at the forge\.$/),
    ).toBeTruthy();
    fireEvent.click(
      screen.getByRole("button", { name: /open citation 1 in chapter ii/i }),
    );
    expect(onNavigate).toHaveBeenCalledWith(citation);
    expect(screen.getByText(/50 input \/ 15 output tokens/i)).toBeTruthy();
    expect(mocks.chapterCloseout).toHaveBeenCalledTimes(1);
    expect(await axeAA(container)).toHaveNoViolations();
  });

  test("prefers an explicit insufficient closeout to invented takeaways", async () => {
    mocks.chapterCloseout.mockResolvedValue({
      ...answer,
      insufficient_evidence: true,
      claims: [],
      citations: [],
    });
    render(
      <ChapterCloseout
        bookId="b"
        chapter={2}
        onNavigate={() => {}}
        onClose={() => {}}
      />,
    );
    expect(
      await screen.findByText(/does not provide enough evidence/i),
    ).toBeTruthy();
    expect(screen.queryByText(/Berenice met/)).toBeNull();
  });

  test("lets readers step through takeaways, revisit one, or read them together", async () => {
    mocks.chapterCloseout.mockResolvedValue({ ...answer, claims: [
      ...answer.claims,
      { text: "The letter changes what Berenice knows.", citation_ids: [1] },
      { text: "Aldric agrees to help.", citation_ids: [1] },
    ] });
    const onClose = vi.fn();
    render(<ChapterCloseout bookId="journey" chapter={2} onNavigate={vi.fn()} onClose={onClose} />);
    await screen.findByText(/^Berenice met Aldric at the forge\.$/);
    expect(screen.queryByText("The letter changes what Berenice knows.")).toBeNull();
    expect((screen.getByRole("button", { name: "Previous" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Next takeaway" }));
    expect(screen.getByText("The letter changes what Berenice knows.")).toBeTruthy();
    expect(screen.queryByText(/^Berenice met Aldric at the forge\.$/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Go to takeaway 3" }));
    expect(screen.getByText("Aldric agrees to help.")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Next takeaway" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "All takeaways" }));
    expect(screen.getAllByRole("article", { name: /takeaway \d of 3/i })).toHaveLength(3);
    fireEvent.click(screen.getByRole("button", { name: "One at a time" }));
    expect(screen.getByText("Aldric agrees to help.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Back to reading" }));
    expect(onClose).toHaveBeenCalledOnce();
  });

  test("discards late reflections after the chapter changes", async () => {
    let finish!: (value: typeof answer) => void;
    mocks.chapterCloseout.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }))
      .mockResolvedValueOnce({ ...answer, chapter: 1, as_of_chapter: 1, claims: [{ text: "The journey begins.", citation_ids: [] }], citations: [] });
    const props = { bookId: "late", chapter: 2, onNavigate: vi.fn(), onClose: vi.fn() };
    const { rerender } = render(<ChapterCloseout {...props} />);
    rerender(<ChapterCloseout {...props} chapter={1} />);
    await screen.findByText("The journey begins.");
    await act(async () => { finish(answer); });
    expect(screen.queryByText(/^Berenice met Aldric at the forge\.$/)).toBeNull();
    expect(screen.getByText("The journey begins.")).toBeTruthy();
  });

  test("does not show a reflection returned beyond the completed chapter", async () => {
    mocks.chapterCloseout.mockResolvedValue({ ...answer, as_of_chapter: 3 });
    render(<ChapterCloseout bookId="future" chapter={2} onNavigate={vi.fn()} onClose={vi.fn()} />);
    await screen.findByText(/could not be verified against your completed chapter/i);
    expect(screen.queryByText(/^Berenice met Aldric at the forge\.$/)).toBeNull();
  });

  test("retries a failed closeout and announces the completed result", async () => {
    mocks.chapterCloseout
      .mockRejectedValueOnce(new Error("Provider is offline."))
      .mockResolvedValue(answer);
    render(
      <ChapterCloseout
        bookId="b"
        chapter={2}
        onNavigate={() => {}}
        onClose={() => {}}
      />,
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "Try closeout again" }),
    );
    expect(
      await screen.findByText(/^Berenice met Aldric at the forge\.$/),
    ).toBeTruthy();
    expect(screen.getByRole("status").textContent).toBe(
      "Your chapter closeout is ready.",
    );
    expect(mocks.chapterCloseout).toHaveBeenCalledTimes(2);
  });

  test("explains a failed evidence review without exposing the internal error", async () => {
    mocks.chapterCloseout.mockRejectedValue(new Error("answer evidence review could not be cleared"));
    render(<ChapterCloseout bookId="review-failure" chapter={2} onNavigate={vi.fn()} onClose={vi.fn()} />);
    await screen.findByText(/We couldn’t verify this reflection against the chapter/);
    expect(screen.queryByText("answer evidence review could not be cleared")).toBeNull();
    expect(screen.getByRole("button", { name: "Try closeout again" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Back to reading" })).toBeTruthy();
  });
});
