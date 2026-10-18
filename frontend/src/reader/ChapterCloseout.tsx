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
import { activeOfflineOwner } from "../pwa/offline";
import { Icon } from "../ui/Icon";
import "./ask-reflect-world.css";

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
  const owner = activeOfflineOwner();
  const [loaded, setLoaded] = useState<{ bookId: string; chapter: number; owner: string; answer: ChapterCloseoutAnswer } | null>(null);
  const answer = loaded?.bookId === bookId && loaded.chapter === chapter && loaded.owner === owner ? loaded.answer : null;
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const [step, setStep] = useState(0);
  const [view, setView] = useState<"journey" | "all">("journey");
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
    setLoaded(null);
    setError("");
    setStep(0);
    setView("journey");
    void assistRequest(`closeout:${owner}:${bookId}:${chapter}`, () =>
      api.chapterCloseout(bookId, chapter),
    )
      .then((value) => {
        if (!cancelled && owner === activeOfflineOwner()) {
          if (value.as_of_chapter > chapter || value.chapter !== chapter)
            throw new Error("This reflection could not be verified against your completed chapter. Please try again.");
          setLoaded({ bookId, chapter, owner, answer: value });
        }
      })
      .catch((reason: unknown) => {
        if (!cancelled && owner === activeOfflineOwner())
          setError(
            reason instanceof Error
              ? ["answer evidence review could not be cleared", "chapter closeout could not be cleared against the completed chapter"].includes(reason.message)
                ? "We couldn’t verify this reflection against the chapter. Try again, or keep reading for now."
                : reason.message
              : "Chapter closeout is unavailable.",
          );
      });
    return () => {
      cancelled = true;
    };
  }, [bookId, chapter, owner, retry]);

  const citationById = new Map(
    answer?.citations.map((citation) => [citation.id, citation]),
  );
  const takeaways = answer?.claims ?? [];
  const currentStep = Math.min(step, Math.max(0, takeaways.length - 1));
  const visibleTakeaways = view === "all" ? takeaways : takeaways.slice(currentStep, currentStep + 1);
  return (
    <div
      className="hero-scrim assist-scrim"
      data-closing={closing || undefined}
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      ref={rootRef}
    >
      <article className="hero-page assist-page closeout-page reflect-world">
        <header className="hero-head">
          <span className="smallcaps hero-eyebrow"><Icon name="check" size={15} /> CHAPTER COMPLETE</span>
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
        <header className="reflect-heading">
          <div><p className="world-kicker">CHAPTER {roman(chapter)} · A MOMENT TO REFLECT</p><h2 id={titleId}>Let it <em>sink in.</em></h2><p>A few things to carry into the next chapter.</p></div>
          <div className="reflect-mark" aria-hidden="true"><Icon name="sparkle" size={40} /></div>
        </header>
        <div
          className="ask-status"
          role="status"
          aria-live="polite"
          aria-atomic="true"
        >
          {error ||
            (!answer
              ? "Reviewing the completed chapter…"
              : answer.insufficient_evidence || !takeaways.length
                ? "There is not enough evidence for a useful closeout yet."
                : "Your chapter closeout is ready.")}
        </div>
        {error && (
          <Button icon="refresh" onClick={() => setRetry((value) => value + 1)}>
            Try closeout again
          </Button>
        )}
        {!answer && !error && <div className="reflection-loading">
          <div className="reflection-loading-cards" aria-hidden="true"><i /><i /><i /></div>
          <h3>Finding the moments worth keeping.</h3>
          <p>Checking the details in your completed chapter. You can keep reading and return when you’re ready.</p>
        </div>}
        {answer && (answer.insufficient_evidence || !takeaways.length) && (
          <section className="reflection-empty" aria-label="Chapter closeout">
            <Icon name="book" size={30} />
            <h3>Some chapters need a little more room.</h3>
            <p>
              This chapter does not provide enough evidence for a useful
              closeout.
            </p>
          </section>
        )}
        {answer && !answer.insufficient_evidence && takeaways.length > 0 && (
          <section className="reflection-journey" aria-label="Chapter closeout">
            <div className="reflection-toolbar">
              <span>{takeaways.length} {takeaways.length === 1 ? "takeaway" : "takeaways"} worth keeping</span>
              <div className="reflection-view" aria-label="Reflection view">
                <button type="button" aria-pressed={view === "journey"} onClick={() => setView("journey")}><Icon name="grid" size={14} /> One at a time</button>
                <button type="button" aria-pressed={view === "all"} onClick={() => setView("all")}><Icon name="list" size={14} /> All takeaways</button>
              </div>
            </div>
            {view === "journey" && (
              <nav className="reflection-progress" aria-label="Takeaways">
                {takeaways.map((_claim, index) => (
                  <button key={index} type="button" aria-label={`Go to takeaway ${index + 1}`} aria-current={currentStep === index ? "step" : undefined} onClick={() => setStep(index)}><span>{String(index + 1).padStart(2, "0")}</span></button>
                ))}
              </nav>
            )}
            <div className="reflection-cards" data-view={view}>
              {visibleTakeaways.map((claim, visibleIndex) => {
                const index = view === "all" ? visibleIndex : currentStep;
                return (
                  <article className="reflection-card" key={`${index}-${claim.text}`} data-tone={index % 3} aria-label={`Takeaway ${index + 1} of ${takeaways.length}`}>
                    <div className="reflection-card-top"><span>TAKEAWAY {String(index + 1).padStart(2, "0")}</span><Icon name="sparkle" size={22} /></div>
                    <p className="reflection-thought">{claim.text}</p>
                    <div className="reflection-evidence" aria-label={`Citations for takeaway ${index + 1}`}>
                      <span><Icon name="book" size={15} /> Return to the moment</span>
                      {claim.citation_ids.map((citationId) => {
                        const citation = citationById.get(citationId);
                        return (
                          <button key={citationId} type="button" aria-label={`Open citation ${citationId} in ${citation?.title ?? "the chapter"}`} onClick={() => { if (citation && owner === activeOfflineOwner()) onNavigate(citation); }}>
                            <span>{citationId}</span>{citation?.title || `Chapter ${roman(chapter)}`}<Icon name="arrowRight" size={15} />
                          </button>
                        );
                      })}
                    </div>
                  </article>
                );
              })}
            </div>
            {view === "journey" && (
              <div className="reflection-controls">
                <Button variant="ghost" icon="arrowLeft" disabled={currentStep === 0} onClick={() => setStep((value) => Math.max(0, value - 1))}>Previous</Button>
                <span aria-live="polite" aria-atomic="true">{currentStep + 1} of {takeaways.length}</span>
                <Button variant="secondary" icon="arrowRight" disabled={currentStep === takeaways.length - 1} onClick={() => setStep((value) => Math.min(takeaways.length - 1, value + 1))}>Next takeaway</Button>
              </div>
            )}
          </section>
        )}
        <footer className="reflection-return"><p>Keep the thought.<br /><strong>See where the story goes.</strong></p><Button variant="primary" icon="arrowRight" onClick={requestClose}>Back to reading</Button></footer>
        {answer && <ProviderCost cost={answer.cost} />}
      </article>
    </div>
  );
}
