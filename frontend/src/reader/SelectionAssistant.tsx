import { useEffect, useId, useRef, useState } from "react";
import { api, type SelectionAction, type SelectionAssistAnswer } from "../api";
import { keepTabFocusInside } from "./focusTrap";
import { ProviderCost } from "./ProviderCost";
import type { ReaderSelection } from "./ReaderMarksPanel";
import { assistRequest } from "./assistRequests";

const LABELS: Record<SelectionAction, string> = {
  explain: "Explain this passage",
  define: "Define this selection",
  translate: "Translate to English",
};

export function SelectionAssistant({
  bookId,
  action,
  selection,
  onNavigate,
  onClose,
}: {
  bookId: string;
  action: SelectionAction;
  selection: ReaderSelection;
  onNavigate: (cfi: string) => void;
  onClose: () => void;
}) {
  const [answer, setAnswer] = useState<SelectionAssistAnswer | null>(null);
  const [error, setError] = useState("");
  const rootRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const triggerRef = useRef<HTMLElement | null>(document.activeElement as HTMLElement | null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
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
    const request = {
      action, text: selection.text, atom: selection.anchor.atom, cfi: selection.anchor.cfi,
    };
    const key = `selection:${bookId}:${JSON.stringify(request)}`;
    void assistRequest(key, () => api.selectionAction(bookId, request)).then((value) => {
      if (!cancelled) setAnswer(value);
    }).catch((reason: unknown) => {
      if (!cancelled) setError(reason instanceof Error ? reason.message : "Selection help is unavailable.");
    });
    return () => { cancelled = true; };
  }, [action, bookId, selection.anchor.atom, selection.anchor.cfi, selection.text]);

  return (
    <div
      className="hero-scrim assist-scrim"
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      ref={rootRef}
    >
      <article className="hero-page assist-page">
        <header className="hero-head">
          <span className="smallcaps hero-eyebrow">selected passage</span>
          <button ref={closeRef} type="button" className="plain smallcaps hero-close" onClick={onClose}>
            close ✕
          </button>
        </header>
        <h2 id={titleId}>{LABELS[action]}</h2>
        <blockquote>{selection.text}</blockquote>
        <div className="ask-status" role="status" aria-live="polite">
          {!answer && !error ? "Working only from the selected passage…" : error}
        </div>
        {answer?.insufficient_evidence && (
          <section className="ask-answer" aria-label="Selection result">
            <p>The selected passage does not establish a safe answer to that action.</p>
          </section>
        )}
        {answer && !answer.insufficient_evidence && answer.text && (
          <section className="ask-answer" aria-label="Selection result">
            <p>{answer.text}</p>
            {answer.citation && (
              <button
                type="button"
                className="plain assist-source"
                onClick={() => onNavigate(answer.citation!.cfi)}
              >
                Return to {answer.citation.title || `chapter ${answer.citation.ordinal}`} [1]
              </button>
            )}
          </section>
        )}
        {answer && <ProviderCost cost={answer.cost} />}
      </article>
    </div>
  );
}
