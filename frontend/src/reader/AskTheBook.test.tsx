import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { axeAA } from "../test-a11y";

const mocks = vi.hoisted(() => ({ askBook: vi.fn(), preparedQuestions: vi.fn() }));
vi.mock("../api", () => ({ api: mocks }));

import { AskTheBook } from "./AskTheBook";

const answer = {
  as_of_chapter: 2,
  insufficient_evidence: false,
  claims: [{ text: "Berenice met Aldric at the forge.", citation_ids: [1] }],
  citations: [
    {
      id: 1,
      ordinal: 2,
      chapter_key: "two",
      href: "c2.xhtml",
      title: "Chapter II",
      excerpt: "Berenice met Aldric at the forge and they spoke.",
    },
  ],
  cost: {
    currency: "USD" as const,
    usd: "0.0000120000",
    input_tokens: 80,
    output_tokens: 20,
    pricing_known: true,
    calls: [
      {
        provider: "openai-compatible",
        model: "gpt-4o-mini",
        usd: "0.0000120000",
      },
    ],
    payer: "your configured provider account",
  },
};

beforeEach(() => {
  mocks.askBook.mockReset();
  mocks.preparedQuestions.mockReset().mockResolvedValue({ as_of_chapter: 2, questions: [] });
});

describe("AskTheBook", () => {
  test("offers current reviewed questions and submits the exact question", async () => {
    const question = "Where did Berenice meet Aldric?";
    mocks.preparedQuestions.mockResolvedValue({ as_of_chapter: 2, questions: [question] });
    mocks.askBook.mockResolvedValue(answer);
    render(<AskTheBook bookId="b" bookmark={2} onNavigate={() => {}} onClose={() => {}} />);
    fireEvent.click(await screen.findByRole("button", { name: question }));
    fireEvent.click(screen.getByRole("button", { name: /ask with citations/i }));
    await screen.findByText(/^Berenice met Aldric at the forge\.$/);
    expect(mocks.askBook).toHaveBeenCalledWith("b", question, 2);
  });

  test("discards late prepared questions after rewinding or switching books", async () => {
    let finish: (value: { as_of_chapter: number; questions: string[] }) => void = () => {};
    mocks.preparedQuestions.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    const { rerender } = render(<AskTheBook bookId="b" bookmark={2} onNavigate={() => {}} onClose={() => {}} />);
    rerender(<AskTheBook bookId="other" bookmark={1} onNavigate={() => {}} onClose={() => {}} />);
    await act(async () => { finish({ as_of_chapter: 2, questions: ["Future identity?"] }); });
    expect(screen.queryByText("Future identity?")).toBeNull();
    expect(screen.getByRole("button", { name: "What should I remember so far?" })).toBeTruthy();
  });

  test("renders cited claims, measured provider cost, and navigates a citation", async () => {
    mocks.askBook.mockResolvedValue(answer);
    const onNavigate = vi.fn();
    const { container } = render(
      <AskTheBook
        bookId="b"
        bookmark={2}
        onNavigate={onNavigate}
        onClose={() => {}}
      />,
    );
    fireEvent.change(screen.getByLabelText(/your question/i), {
      target: { value: "Where did Berenice meet Aldric?" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: /ask with citations/i }),
    );
    expect(
      await screen.findByText(/^Berenice met Aldric at the forge\.$/),
    ).toBeTruthy();
    expect(screen.getByText(/Provider cost: \$0\.000012 USD/i)).toBeTruthy();
    expect(screen.getByText(/gpt-4o-mini/)).toBeTruthy();
    fireEvent.click(
      screen.getByRole("button", { name: /open citation 1 in chapter ii/i }),
    );
    expect(onNavigate).toHaveBeenCalledWith(answer.citations[0]);
    expect(await axeAA(container)).toHaveNoViolations();
  });

  test("states insufficient evidence without inventing an answer", async () => {
    mocks.askBook.mockResolvedValue({
      ...answer,
      insufficient_evidence: true,
      claims: [],
      citations: [],
    });
    render(
      <AskTheBook
        bookId="b"
        bookmark={2}
        onNavigate={() => {}}
        onClose={() => {}}
      />,
    );
    fireEvent.change(screen.getByLabelText(/your question/i), {
      target: { value: "Who wins?" },
    });
    fireEvent.submit(screen.getByLabelText(/your question/i).closest("form")!);
    expect(
      await screen.findByText(/do not establish an answer yet/i),
    ).toBeTruthy();
    expect(screen.queryByText(/Berenice met/)).toBeNull();
  });

  test("preserves the question for a retry and announces the cited answer", async () => {
    mocks.askBook
      .mockRejectedValueOnce(new Error("Provider is offline."))
      .mockResolvedValue(answer);
    render(
      <AskTheBook
        bookId="b"
        bookmark={2}
        onNavigate={() => {}}
        onClose={() => {}}
      />,
    );
    fireEvent.change(screen.getByLabelText(/your question/i), {
      target: { value: "Where did they meet?" },
    });
    fireEvent.submit(screen.getByLabelText(/your question/i).closest("form")!);
    fireEvent.click(await screen.findByRole("button", { name: "Try again" }));
    expect(
      await screen.findByText(/^Berenice met Aldric at the forge\.$/),
    ).toBeTruthy();
    expect(screen.getByRole("status").textContent).toBe(
      "Your cited answer is ready.",
    );
    expect(mocks.askBook).toHaveBeenLastCalledWith(
      "b",
      "Where did they meet?",
      2,
    );
  });

  test("discards a response when the reading frontier changes during the request", async () => {
    let resolveAnswer!: (value: typeof answer) => void;
    mocks.askBook.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveAnswer = resolve;
        }),
    );
    const { rerender } = render(
      <AskTheBook
        bookId="b"
        bookmark={2}
        onNavigate={() => {}}
        onClose={() => {}}
      />,
    );
    fireEvent.change(screen.getByLabelText(/your question/i), {
      target: { value: "Where did they meet?" },
    });
    fireEvent.submit(screen.getByLabelText(/your question/i).closest("form")!);
    rerender(
      <AskTheBook
        bookId="b"
        bookmark={1}
        onNavigate={() => {}}
        onClose={() => {}}
      />,
    );
    await act(async () => resolveAnswer(answer));
    expect(
      screen.queryByText(/^Berenice met Aldric at the forge\.$/),
    ).toBeNull();
    expect(screen.getByRole("status").textContent).toBe("");
  });

  test("does not present an unpriced provider model as free", async () => {
    mocks.askBook.mockResolvedValue({
      ...answer,
      cost: { ...answer.cost, usd: "0.0000000000", pricing_known: false },
    });
    render(
      <AskTheBook
        bookId="b"
        bookmark={2}
        onNavigate={() => {}}
        onClose={() => {}}
      />,
    );
    fireEvent.change(screen.getByLabelText(/your question/i), {
      target: { value: "Where?" },
    });
    fireEvent.submit(screen.getByLabelText(/your question/i).closest("form")!);
    expect(await screen.findByText(/Provider price unavailable/i)).toBeTruthy();
    expect(screen.queryByText(/Provider cost: \$0/)).toBeNull();
  });

  test("traps dismissal on Escape and restores focus to the opener", async () => {
    const opener = document.createElement("button");
    document.body.append(opener);
    opener.focus();
    const onClose = vi.fn();
    const { unmount } = render(
      <AskTheBook
        bookId="b"
        bookmark={2}
        onNavigate={() => {}}
        onClose={onClose}
      />,
    );
    expect(screen.getByLabelText(/your question/i)).toBe(
      document.activeElement,
    );
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
    unmount();
    await waitFor(() => expect(opener).toBe(document.activeElement));
    opener.remove();
  });
});
