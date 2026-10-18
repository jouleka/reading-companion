import {
  type FormEvent,
  type MutableRefObject,
  type ReactNode,
  useEffect,
  useId,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import { api, type AskAnswer, type AskCitation } from "../api";
import { Button } from "../ui/Button";
import { AiStatus } from "../ui/AiStatus";
import { useDialogMotion } from "../ui/useDialogMotion";
import { keepTabFocusInside } from "./focusTrap";
import { ProviderCost } from "./ProviderCost";
import { roman } from "./roman";
import { Icon } from "../ui/Icon";
import { activeOfflineOwner } from "../pwa/offline";
import "./ask-reflect-world.css";

type AskExchange = { question: string; answer: AskAnswer };

type AskSnapshot = {
  question: string;
  answer: AskAnswer | null;
  exchanges: AskExchange[];
  pendingQuestion: string | null;
  busy: boolean;
  error: string;
};

/** Owned by the mounted reader, never persisted. A pending request survives workspace tab changes. */
export type AskSession = {
  readonly bookId: string;
  readonly bookmark: number;
  readonly owner: string;
  getSnapshot: () => AskSnapshot;
  subscribe: (listener: () => void) => () => void;
  update: (changes: Partial<AskSnapshot>) => void;
};

function createAskSession(bookId: string, bookmark: number, owner: string): AskSession {
  let snapshot: AskSnapshot = { question: "", answer: null, exchanges: [], pendingQuestion: null, busy: false, error: "" };
  const listeners = new Set<() => void>();
  return {
    bookId,
    bookmark,
    owner,
    getSnapshot: () => snapshot,
    subscribe: (listener) => {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
    update: (changes) => {
      snapshot = { ...snapshot, ...changes };
      listeners.forEach((listener) => listener());
    },
  };
}

export function AskTheBook({
  bookId,
  bookmark,
  onNavigate,
  onClose,
  workspaceNavigation,
  sessionRef,
}: {
  bookId: string;
  bookmark: number;
  onNavigate: (citation: AskCitation) => void;
  onClose: () => void;
  workspaceNavigation?: ReactNode;
  sessionRef?: MutableRefObject<AskSession | null>;
}) {
  const { closing, requestClose } = useDialogMotion(onClose);
  const localSessionRef = useRef<AskSession | null>(null);
  const retainedSession = sessionRef ?? localSessionRef;
  const owner = activeOfflineOwner();
  if (
    !retainedSession.current ||
    retainedSession.current.bookId !== bookId ||
    retainedSession.current.bookmark !== bookmark ||
    retainedSession.current.owner !== owner
  ) {
    retainedSession.current = createAskSession(bookId, bookmark, owner);
  }
  const session = retainedSession.current;
  const { question, answer, exchanges, pendingQuestion, busy, error } = useSyncExternalStore(
    session.subscribe,
    session.getSnapshot,
    session.getSnapshot,
  );
  const setQuestion = (value: string) => session.update({ question: value });
  const [prepared, setPrepared] = useState<{ bookId: string; bookmark: number; owner: string; questions: string[] } | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const onCloseRef = useRef(requestClose);
  onCloseRef.current = requestClose;
  const triggerRef = useRef<HTMLElement | null>(null);
  if (triggerRef.current === null)
    triggerRef.current = document.activeElement as HTMLElement | null;
  const titleId = useId();
  const helpId = useId();
  const conversationRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const conversation = conversationRef.current;
    if (!conversation) return;
    if (busy) {
      conversation.scrollTop = conversation.scrollHeight;
    } else {
      // Only move the conversation viewport. A long answer's opening must remain
      // visible, including when returning to a retained reading session.
      const latest = conversation.querySelector<HTMLElement>("[data-latest-exchange]");
      conversation.scrollTop = latest?.offsetTop ?? 0;
    }
  }, [exchanges.length, busy]);

  useEffect(() => {
    inputRef.current?.focus();
    if (inputRef.current?.disabled)
      rootRef.current?.querySelector<HTMLButtonElement>("button")?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      keepTabFocusInside(rootRef.current, event);
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      const trigger = triggerRef.current;
      if (trigger && document.contains(trigger)) trigger.focus();
    };
  }, []);

  useEffect(() => {
    let active = true;
    void api.preparedQuestions(bookId, bookmark).then((result) => {
      if (active && owner === activeOfflineOwner() && result.as_of_chapter <= bookmark) {
        setPrepared({ bookId, bookmark, owner, questions: result.questions });
      }
    }).catch(() => { /* Suggestions remain available when preparation is unavailable. */ });
    return () => { active = false; };
  }, [bookId, bookmark, owner]);

  const suggestions = prepared?.bookId === bookId && prepared.bookmark === bookmark && prepared.owner === owner && prepared.questions.length
    ? prepared.questions
    : ["What should I remember so far?", "What are the key connections?"];

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (retainedSession.current !== session || owner !== activeOfflineOwner()) {
      session.update({ question: "", answer: null, exchanges: [], pendingQuestion: null, busy: false, error: "" });
      return;
    }
    const snapshot = session.getSnapshot();
    const value = snapshot.question.trim();
    if (value.length < 2 || snapshot.busy) return;
    session.update({ busy: true, error: "", pendingQuestion: value });
    const isCurrentSession = () =>
      retainedSession.current === session && owner === activeOfflineOwner();
    try {
      const result = await api.askBook(bookId, value, bookmark);
      if (isCurrentSession()) {
        if (result.as_of_chapter > bookmark)
          throw new Error("The answer could not be verified against your completed chapters. Please try again.");
        session.update({
          answer: result,
          exchanges: [...session.getSnapshot().exchanges, { question: value, answer: result }],
          question: "",
          pendingQuestion: null,
        });
      }
    } catch (reason) {
      if (isCurrentSession()) {
        session.update({
          error: reason instanceof Error
            ? reason.message
            : "The book could not answer right now.",
        });
      }
    } finally {
      if (isCurrentSession()) session.update({ busy: false, pendingQuestion: null });
    }
  };

  const openCitation = (citation: AskCitation | undefined) => {
    if (citation && retainedSession.current === session && owner === activeOfflineOwner()) onNavigate(citation);
  };

  return (
    <div
      className="hero-scrim ask-scrim"
      data-closing={closing || undefined}
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      ref={rootRef}
    >
      <article className="hero-page ask-page ask-world">
        <header className="hero-head">
          <span className="smallcaps hero-eyebrow"><Icon name="sparkle" size={15} /> ROOM FOR CURIOSITY</span>
          <Button
            variant="ghost"
            icon="close"
            className="hero-close"
            onClick={requestClose}
          >
            Close
          </Button>
        </header>
        {workspaceNavigation}
        <header className="ask-world-heading">
          <div>
            <h2 id={titleId}>Ask the Book</h2>
            <p>A little curiosity goes a long way.</p>
          </div>
          <span className="world-boundary"><Icon name="shield" size={14} /> Through chapter {roman(bookmark)}</span>
        </header>
        <p id={helpId} className="sr-only">
          Ask about your completed chapters. Answers include passages from your book.
          Questions are answered individually and kept together for this reading session.
        </p>

        <div className="ask-conversation" ref={conversationRef} role="region" aria-label="Book conversation">
          {exchanges.length === 0 && !busy && (
            <section className="ask-invitation" aria-label="Explore your book">
              <div className="ask-orbit" aria-hidden="true"><span /><Icon name="sparkle" size={35} /><span /></div>
              <p className="world-kicker">THERE'S MORE BETWEEN THE LINES</p>
              <h3>What has you<br /><em>wondering?</em></h3>
              <p>Untangle a connection. Revisit a moment.<br />Find an answer in the pages you've read.</p>
              <div className="ask-suggestions" aria-label="Suggested questions">
                {suggestions.map((prompt) => (
                  <Button key={prompt} variant="ghost" onClick={() => { setQuestion(prompt); inputRef.current?.focus(); }}>
                    <span>{prompt}</span><Icon name="arrowRight" size={15} />
                  </Button>
                ))}
              </div>
            </section>
          )}
          {exchanges.map((exchange, exchangeIndex) => {
            const citationById = new Map(exchange.answer.citations.map((citation) => [citation.id, citation]));
            return (
              <section className="ask-exchange" key={exchangeIndex} data-latest-exchange={exchangeIndex === exchanges.length - 1 || undefined} aria-label={`Question ${exchangeIndex + 1}`}>
                <div className="ask-question-bubble"><span>You wondered</span><p>{exchange.question}</p></div>
                <section className="ask-answer world-answer" aria-label="Answer">
                  <h3><Icon name={exchange.answer.insufficient_evidence ? "book" : "sparkle"} size={16} /> {exchange.answer.insufficient_evidence ? "The story hasn't told us yet" : "From the pages you've read"}</h3>
                  {exchange.answer.insufficient_evidence ? (
                    <p>The pages you have completed do not establish an answer yet.</p>
                  ) : (
                    <>
                      {exchange.answer.claims.map((claim, index) => (
                        <p key={`${index}-${claim.text}`}>
                          {claim.text}{" "}
                          <span className="ask-citation-links" aria-label={`Citations for claim ${index + 1}`}>
                            {claim.citation_ids.map((citationId) => {
                              const citation = citationById.get(citationId);
                              return (
                                <button key={citationId} type="button" className="citation-link" aria-label={`Open citation ${citationId} in ${citation?.title ?? "the book"}`} onClick={() => openCitation(citation)}>
                                  {citationId}
                                </button>
                              );
                            })}
                          </span>
                        </p>
                      ))}
                      <details className="ask-evidence">
                        <summary><Icon name="book" size={15} /> {exchange.answer.citations.length} cited {exchange.answer.citations.length === 1 ? "passage" : "passages"}<span>Explore the evidence</span></summary>
                        <ol className="ask-sources" aria-label="Cited passages">
                          {exchange.answer.citations.map((citation) => (
                            <li key={citation.id}>
                              <button type="button" className="plain" onClick={() => openCitation(citation)}>
                                <span className="ask-source-location"><span className="ask-source-number" aria-hidden="true">{citation.id}</span><span>{citation.title || `Chapter ${roman(citation.ordinal)}`}</span><Icon name="arrowRight" size={14} /></span>
                                <q>{citation.excerpt}</q>
                              </button>
                            </li>
                          ))}
                        </ol>
                      </details>
                    </>
                  )}
                  <ProviderCost cost={exchange.answer.cost} />
                </section>
              </section>
            );
          })}
          {busy && pendingQuestion && (
            <div className="ask-exchange ask-pending">
              <div className="ask-question-bubble"><span>You wondered</span><p>{pendingQuestion}</p></div>
              <div className="ask-thinking" aria-hidden="true"><span /><span /><span /><p>Finding the thread in your book</p></div>
            </div>
          )}
        </div>

        <div className="ask-status" role="status" aria-live="polite" aria-atomic="true" data-busy={busy || undefined} data-error={error ? true : undefined}>
          {busy ? "Searching only the chapters you have completed…" : error || (answer ? answer.insufficient_evidence ? "There is not enough evidence in your completed chapters yet." : "Your cited answer is ready." : "")}
        </div>
        <form className="ask-form world-composer" onSubmit={(event) => void submit(event)}>
          <div className="ask-composer-head"><label htmlFor={`${titleId}-question`}>Your question</label><span>{question.length} / 500</span></div>
          <textarea
            ref={inputRef}
            id={`${titleId}-question`}
            aria-describedby={helpId}
            rows={2}
            placeholder={exchanges.length ? "What else are you curious about?" : "Ask about a person, a moment, or an idea…"}
            minLength={2}
            maxLength={500}
            value={question}
            disabled={busy}
            onChange={(event) => setQuestion(event.currentTarget.value)}
          />
          <div className="ask-composer-footer">
            <span><Icon name="shield" size={14} /> Only what you've read</span>
            <Button
              type="submit"
              variant="primary"
              icon="sparkle"
              disabled={busy || question.trim().length < 2}
              aria-busy={busy}
            >
              {busy
                ? "Checking the pages…"
                : error
                  ? "Try again"
                  : "Ask with citations"}
            </Button>
          </div>
        </form>
        <div className="ask-world-footnote"><AiStatus compact /><span>Questions stay here during this reading session.</span></div>
      </article>
    </div>
  );
}
