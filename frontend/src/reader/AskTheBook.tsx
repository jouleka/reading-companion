import { type FormEvent, useEffect, useId, useRef, useState } from "react";
import { api, type AskAnswer, type AskCitation } from "../api";
import { keepTabFocusInside } from "./focusTrap";
import { ProviderCost } from "./ProviderCost";
import { roman } from "./roman";

export function AskTheBook({
  bookId,
  bookmark,
  onNavigate,
  onClose,
}: {
  bookId: string;
  bookmark: number;
  onNavigate: (citation: AskCitation) => void;
  onClose: () => void;
}) {
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState<AskAnswer | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const rootRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const triggerRef = useRef<HTMLElement | null>(null);
  if (triggerRef.current === null) triggerRef.current = document.activeElement as HTMLElement | null;
  const titleId = useId();
  const helpId = useId();

  useEffect(() => {
    inputRef.current?.focus();
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

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const value = question.trim();
    if (value.length < 2 || busy) return;
    setBusy(true);
    setError("");
    setAnswer(null);
    try {
      setAnswer(await api.askBook(bookId, value, bookmark));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The book could not answer right now.");
    } finally {
      setBusy(false);
    }
  };

  const citationById = new Map(answer?.citations.map((citation) => [citation.id, citation]));
  const openCitation = (citation: AskCitation | undefined) => {
    if (citation) onNavigate(citation);
  };

  return (
    <div
      className="hero-scrim ask-scrim"
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      ref={rootRef}
    >
      <article className="hero-page ask-page">
        <header className="hero-head">
          <span className="smallcaps hero-eyebrow">cited companion</span>
          <button type="button" className="plain smallcaps hero-close" onClick={onClose}>
            close ✕
          </button>
        </header>
        <h2 id={titleId}>Ask the Book</h2>
        <p id={helpId} className="quiet ask-intro">
          Ask about pages you have completed through chapter {roman(bookmark)}. Answers use only the
          cited passages; if those pages do not establish an answer, the companion will say so.
        </p>
        <form className="ask-form" onSubmit={(event) => void submit(event)}>
          <label htmlFor={`${titleId}-question`}>Your question</label>
          <textarea
            ref={inputRef}
            id={`${titleId}-question`}
            aria-describedby={helpId}
            rows={3}
            minLength={2}
            maxLength={500}
            value={question}
            disabled={busy}
            onChange={(event) => setQuestion(event.currentTarget.value)}
          />
          <button type="submit" disabled={busy || question.trim().length < 2}>
            {busy ? "checking the pages…" : "ask with citations"}
          </button>
        </form>

        <div className="ask-status" role="status" aria-live="polite">
          {busy ? "Searching only the chapters you have completed…" : error}
        </div>

        {answer?.insufficient_evidence && (
          <section className="ask-answer" aria-label="Answer">
            <p>The pages you have completed do not establish an answer yet.</p>
          </section>
        )}
        {answer && !answer.insufficient_evidence && (
          <section className="ask-answer" aria-label="Answer">
            <h3>From what you have read</h3>
            {answer.claims.map((claim, index) => (
              <p key={`${index}-${claim.text}`}>
                {claim.text}{" "}
                <span className="ask-citation-links" aria-label={`Citations for claim ${index + 1}`}>
                  {claim.citation_ids.map((citationId) => {
                    const citation = citationById.get(citationId);
                    return (
                      <button
                        key={citationId}
                        type="button"
                        className="citation-link"
                        aria-label={`Open citation ${citationId} in ${citation?.title ?? "the book"}`}
                        onClick={() => openCitation(citation)}
                      >
                        [{citationId}]
                      </button>
                    );
                  })}
                </span>
              </p>
            ))}
            <ol className="ask-sources" aria-label="Cited passages">
              {answer.citations.map((citation) => (
                <li key={citation.id}>
                  <button type="button" className="plain" onClick={() => openCitation(citation)}>
                    <span>{citation.title || `Chapter ${roman(citation.ordinal)}`}</span>
                    <q>{citation.excerpt}</q>
                  </button>
                </li>
              ))}
            </ol>
          </section>
        )}
        {answer && <ProviderCost cost={answer.cost} />}
      </article>
    </div>
  );
}
