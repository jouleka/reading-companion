import { fireEvent, render, screen } from "@testing-library/react";
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
    calls: [{ provider: "anthropic", model: "claude-haiku", usd: "0.0000120000" }],
    payer: "your configured provider account",
  },
};

beforeEach(() => mocks.chapterCloseout.mockReset());

describe("ChapterCloseout", () => {
  test("renders cited takeaways, navigates to evidence, exposes cost, and passes axe", async () => {
    mocks.chapterCloseout.mockResolvedValue(answer);
    const onNavigate = vi.fn();
    const { container } = render(
      <StrictMode>
        <ChapterCloseout bookId="b" chapter={2} onNavigate={onNavigate} onClose={() => {}} />
      </StrictMode>,
    );
    expect(await screen.findByText(/^Berenice met Aldric at the forge\.$/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /open citation 1 in chapter ii/i }));
    expect(onNavigate).toHaveBeenCalledWith(citation);
    expect(screen.getByText(/50 input \/ 15 output tokens/i)).toBeTruthy();
    expect(mocks.chapterCloseout).toHaveBeenCalledTimes(1);
    expect(await axeAA(container)).toHaveNoViolations();
  });

  test("prefers an explicit insufficient closeout to invented takeaways", async () => {
    mocks.chapterCloseout.mockResolvedValue({
      ...answer, insufficient_evidence: true, claims: [], citations: [],
    });
    render(<ChapterCloseout bookId="b" chapter={2} onNavigate={() => {}} onClose={() => {}} />);
    expect(await screen.findByText(/does not provide enough evidence/i)).toBeTruthy();
    expect(screen.queryByText(/Berenice met/)).toBeNull();
  });
});
