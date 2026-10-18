import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import {
  api,
  type AskCitation,
  type ChapterCloseout as ChapterCloseoutAnswer,
} from "../api";
import { Button } from "../ui/Button";
import { useDialogMotion } from "../ui/useDialogMotion";
import { keepTabFocusInside } from "./focusTrap";
import { ProviderCost } from "./ProviderCost";
import { roman } from "./roman";
import { assistRequest } from "./assistRequests";

export function ChapterCloseout({
  bookId,
  chapter,
  workspaceNavigation,
  onNavigate,
  onClose,
}: {
  bookId: string;
  chapter: number;
  workspaceNavigation?: ReactNode;
  onNavigate: (citation: AskCitation) => void;
  onClose: () => void;
}) {
  const { closing, requestClose } = useDialogMotion(onClose);
  const [answer, setAnswer] = useState<ChapterCloseoutAnswer | null>(null);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const rootRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const triggerRef = useRef<HTMLElement | null>(
    document.activeElement as HTMLElement | null,
  );
  const onCloseRef = useRef(requestClose);
  onCloseRef.current = requestClose;
  const titleId = useId();

  useEffect(() => {
    closeRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onCloseRef.current();
      } else {
        keepTabFocusInside(rootRef.current, event);
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      const trigger = triggerRef.current;
      if (trigger && document.contains(trigger)) trigger.focus();
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    setAnswer(null);
    setError("");
    void assistRequest(`closeout:${bookId}:${chapter}`, () =>
      api.chapterCloseout(bookId, chapter),
    )
      .then((value) => {
        if (!cancelled) setAnswer(value);
      })
      .catch((reason: unknown) => {
        if (!cancelled)
          setError(
            reason instanceof Error
              ? reason.message
              : "Chapter closeout is unavailable.",
          );
      });
    return () => {
      cancelled = true;
    };
  }, [bookId, chapter, retry]);

  const citationById = new Map(
    answer?.citations.map((citation) => [citation.id, citation]),
  );
  return (
    <div
      className="hero-scrim assist-scrim"
      data-closing={closing || undefined}
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      ref={rootRef}
    >
      <article className="hero-page assist-page closeout-page">
        <header className="hero-head">
          <span className="smallcaps hero-eyebrow">chapter complete</span>
          <Button
            ref={closeRef}
            variant="ghost"
            icon="close"
            className="hero-close"
            onClick={requestClose}
          >
            Close
          </Button>
        </header>
        {workspaceNavigation}
        <h2 id={titleId}>Chapter {roman(chapter)} closeout</h2>
        <p className="quiet">
          A few things worth carrying forward, using only this completed
          chapter.
        </p>
        <div
          className="ask-status"
          role="status"
          aria-live="polite"
          aria-atomic="true"
        >
          {error ||
            (!answer
              ? "Reviewing the completed chapter…"
              : answer.insufficient_evidence
                ? "There is not enough evidence for a useful closeout yet."
                : "Your chapter closeout is ready.")}
        </div>
        {error && (
          <Button icon="refresh" onClick={() => setRetry((value) => value + 1)}>
            Try closeout again
          </Button>
        )}
        {answer?.insufficient_evidence && (
          <section className="ask-answer" aria-label="Chapter closeout">
            <p>
              This chapter does not provide enough evidence for a useful
              closeout.
            </p>
          </section>
        )}
        {answer && !answer.insufficient_evidence && (
          <section className="ask-answer" aria-label="Chapter closeout">
            <ul className="closeout-list">
              {answer.claims.map((claim, index) => (
                <li key={`${index}-${claim.text}`}>
                  {claim.text}{" "}
                  <span
                    className="ask-citation-links"
                    aria-label={`Citations for takeaway ${index + 1}`}
                  >
                    {claim.citation_ids.map((citationId) => {
                      const citation = citationById.get(citationId);
                      return (
                        <button
                          key={citationId}
                          type="button"
                          className="citation-link"
                          aria-label={`Open citation ${citationId} in ${citation?.title ?? "the chapter"}`}
                          onClick={() => {
                            if (citation) onNavigate(citation);
                          }}
                        >
                          [{citationId}]
                        </button>
                      );
                    })}
                  </span>
                </li>
              ))}
            </ul>
          </section>
        )}
        {answer && <ProviderCost cost={answer.cost} />}
      </article>
    </div>
  );
}
