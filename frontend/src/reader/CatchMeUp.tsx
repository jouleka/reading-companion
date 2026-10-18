/** The catch-me-up HERO (LIT-14): the companion's memory opened to a full "the story so far" page —
 * a grand printed leaf with a drop-cap recap, the cast and open threads in the margin, over the
 * clamped /catch-me-up route. It is a dialog (focus moves in, Escape closes, focus restores — the
 * LIT-16 pattern) and renders ONLY the server-clamped data: the recap the runtime gate + LLM-judge
 * cleared, never anything client-computed or past the frontier. Fetches by (bookId, bookmark) so the
 * LIT-15 scrubber can walk it back through time; the server still clamps to the high-water. */
import { Fragment, useEffect, useRef, useState, type ReactNode } from "react";
import { api, type BookType, type CatchMeUp as CMU } from "../api";
import { Button } from "../ui/Button";
import { Icon } from "../ui/Icon";
import { useDialogMotion } from "../ui/useDialogMotion";
import { presentationFor } from "./bookProfile";
import { keepTabFocusInside } from "./focusTrap";
import { NameCard } from "./NameCard";
import { wrapNames } from "./names";
import { roman } from "./roman";
import { recapFailureMessage } from "./recapFailure";

// Keep the reviewed words intact. Long paragraphs become manageable cards at sentence boundaries.
function recapMoments(text: string): string[] {
  const paragraphs = text.split(/\n+/).map(value => value.trim()).filter(Boolean);
  if (typeof Intl.Segmenter !== "function") return paragraphs;
  const segmenter = new Intl.Segmenter(undefined, { granularity: "sentence" });
  return paragraphs.flatMap(paragraph => {
    const moments: string[] = [];
    let current = "";
    for (const { segment } of segmenter.segment(paragraph)) {
      if (current && `${current}${segment}`.trim().split(/\s+/).length > 55) {
        moments.push(current.trim());
        current = "";
      }
      current += segment;
    }
    if (current.trim()) moments.push(current.trim());
    return moments;
  });
}

export function CatchMeUp({
  bookId,
  bookmark,
  totalAtoms,
  bookType = "novel",
  welcomeBackChapter,
  workspaceNavigation,
  onClose,
}: {
  bookId: string;
  bookmark: number;
  totalAtoms: number;
  bookType?: BookType;
  // LIT-29 "since you were last here": when the hero auto-opens after a return gap, the chapter the
  // reader left off in — drives the welcome-back framing. null/omitted on a deliberate open.
  welcomeBackChapter?: number | null;
  workspaceNavigation?: ReactNode;
  onClose: () => void;
}) {
  const { closing, requestClose } = useDialogMotion(onClose);
  const [snapshot, setSnapshot] = useState<{ bookId: string; bookmark: number; data: CMU } | null>(null);
  const cmu = snapshot?.bookId === bookId && snapshot.bookmark === bookmark ? snapshot.data : null;
  const [err, setErr] = useState<unknown>(null);
  const [retry, setRetry] = useState(0);
  const [slow, setSlow] = useState(false);
  const [beat, setBeat] = useState(0);
  const [readAll, setReadAll] = useState(false);
  // LIT-30: the open name card (a clicked chip), anchored to it. While a card is open it owns the
  // keyboard; the hero yields (below) so its own Escape/Tab-trap take over.
  const [card, setCard] = useState<{
    entityId: number;
    anchorRect: DOMRect | null;
  } | null>(null);
  const cardRef = useRef(card);
  cardRef.current = card;
  const rootRef = useRef<HTMLDivElement>(null);
  const articleRef = useRef<HTMLElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const onCloseRef = useRef(requestClose);
  onCloseRef.current = requestClose;
  // capture the opener SYNCHRONOUSLY at render — before ReaderPage's modal-inert can steal its focus
  // (the opener sits in the .companion that inerts on open) — so the hero's close restores focus to the
  // opener, not <body> (a11y pass-2, WCAG 2.4.3; the same fix as the NameCard trigger capture).
  const heroTriggerRef = useRef<HTMLElement | null>(null);
  if (heroTriggerRef.current === null)
    heroTriggerRef.current = document.activeElement as HTMLElement | null;

  // while a name card is open, mark the hero content inert so the two modals don't coexist — the card
  // is the sole active surface (a11y review: tighten the nested aria-modal). Mirrors the LIT-16 pattern
  // ReaderPage uses to inert the reader behind the hero. The CLOSE-time un-inert timing is NOT left to
  // this reactive effect (it re-runs too late for focus-restore) — closeCard does it synchronously.
  useEffect(() => {
    const a = articleRef.current;
    if (a) a.inert = card != null;
  }, [card]);

  // close the card, un-inerting the hero SYNCHRONOUSLY first so the unmounting card's focus-restore
  // lands on the (now focusable) chip, not <body>: `.focus()` inside an inert subtree is a no-op, and a
  // reactive effect re-runs too late to clear it in time (a11y pass-2 — proven live). Execution order,
  // not effect order, is the guarantee.
  const closeCard = () => {
    if (articleRef.current) articleRef.current.inert = false;
    setCard(null);
  };

  useEffect(() => {
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (cardRef.current) return; // a name card is open — it owns Escape/Tab (its own trap)
      if (e.key === "Escape") {
        e.preventDefault();
        onCloseRef.current();
        return;
      }
      keepTabFocusInside(rootRef.current, e);
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      const t = heroTriggerRef.current;
      if (t && document.contains(t) && typeof t.focus === "function") t.focus();
    };
  }, []); // capture once; onClose/card read via refs so the trigger is never re-captured on re-render

  useEffect(() => {
    let dead = false;
    setSnapshot(null);
    setErr(null);
    setSlow(false);
    setBeat(0);
    const slowTimer = window.setTimeout(() => { if (!dead) setSlow(true); }, 8_000);
    api
      .catchMeUp(bookId, bookmark)
      .then((data) => {
        if (dead) return;
        if (data.as_of_chapter > bookmark) throw new Error("This recap could not be verified against your reading position. Please try again.");
        setSnapshot({ bookId, bookmark, data });
      })
      .catch((error: unknown) => !dead && setErr(error))
      .finally(() => window.clearTimeout(slowTimer));
    return () => {
      dead = true;
      window.clearTimeout(slowTimer);
    };
  }, [bookId, bookmark, retry]);

  const paras = recapMoments(cmu?.recap ?? "");
  const cast = cmu?.cast ?? []; // bookmark-bounded; wrapNames wraps ONLY these, never a future name
  const asOf = cmu?.as_of_chapter ?? bookmark;
  // a11y: when the hero AUTO-opens after a return gap, fold the welcome-back framing into the dialog's
  // accessible NAME. A freshly-opened dialog announces its name when focus moves in, so this is
  // reliable — unlike a sibling role=status region mounted already-populated, which screen readers drop
  // (a11y review HIGH). The visible .welcome-back banner below is sighted-reader reinforcement only.
  const empty = cmu != null && (cmu.as_of_chapter === 0 || paras.length === 0);
  const presentation = presentationFor(bookType);
  const dialogLabel =
    welcomeBackChapter != null
      ? `Welcome back — you left off in ${presentation.unit} ${roman(welcomeBackChapter)}. ${presentation.recapLink}.`
      : presentation.recapLink;
  const showPeopleStats =
    cmu != null &&
    (presentation.peopleMode === "primary" ||
      cmu.cast_size > 0 ||
      cmu.open_threads > 0);

  return (
    <div
      className="hero-scrim"
      data-closing={closing || undefined}
      role="dialog"
      aria-modal="true"
      aria-label={dialogLabel}
      ref={rootRef}
    >
      <article className="hero-page recap-workspace" ref={articleRef}>
        <div className="hero-ribbon" aria-hidden="true" />
        <header className="hero-head">
          <span className="smallcaps hero-eyebrow">
            {presentation.recapLink}
          </span>
          <Button
            variant="ghost"
            icon="close"
            className="hero-close"
            ref={closeRef}
            onClick={requestClose}
          >
            Close
          </Button>
        </header>
        {workspaceNavigation}
        {welcomeBackChapter != null && (
          // visual reinforcement only — the SR announcement rides the dialog's accessible name (above)
          <div className="welcome-back">
            <span className="smallcaps">welcome back</span> — you left off in{" "}
            {presentation.unit} {roman(welcomeBackChapter)}. Here is{" "}
            {presentation.recapLink}.
          </div>
        )}
        <div className="recap-intro">
          <div>
            <p className="world-kicker">A little rewind. A way back in.</p>
            <h1>{bookType === "novel" ? <>Back to<br /><em>the story.</em></> : <>Your reading,<br /><em>remembered.</em></>}</h1>
          </div>
          <div className="story-orb" aria-hidden="true"><i /><i /><i /><Icon name="sparkle" size={44} /></div>
        </div>
        <div className="hero-asof recap-boundary">
          <Icon name="shield" size={14} />
          {empty
            ? "nothing read yet"
            : `as of ${presentation.unit} ${roman(asOf)} · ${asOf} of ${totalAtoms}`}
        </div>

        <div
          className="sr-only"
          role="status"
          aria-live="polite"
          aria-atomic="true"
        >
          {cmu
            ? empty
              ? "No reading memory is available yet."
              : "Your recap is ready."
            : ""}
        </div>
        {err ? (
          <div className="codex-state" role="alert">
            <p className="quiet hero-quiet">
              {recapFailureMessage(err, "hero")}
            </p>
            <Button
              icon="refresh"
              onClick={() => setRetry((value) => value + 1)}
            >
              Try recap again
            </Button>
          </div>
        ) : cmu == null ? (
          <section className="recap-loading" role="status">
            <div className="recap-loading-art" aria-hidden="true"><span /><span /><span /></div>
            <strong>{slow ? "This is taking longer than usual." : "Preparing your recap"}</strong>
            <p>Gathering the moments from your completed chapters, and checking the details.</p>
            {slow && <p>You can keep reading while this finishes, and check back when you’re ready.</p>}
            <Button variant="ghost" onClick={requestClose}>Keep reading</Button>
          </section>
        ) : empty ? (
          <p className="quiet hero-quiet">
            Finish a {presentation.unit} and {presentation.recapLink} will
            gather here.
          </p>
        ) : (
          <div className="hero-body recap-stage" data-read-all={readAll || undefined}>
            <div className="recap-story-column">
              <div className="recap-story-tools">
                <span>{readAll ? "The complete recap" : `${String(beat + 1).padStart(2, "0")} / ${String(paras.length).padStart(2, "0")} moments`}</span>
                <button type="button" className="plain" aria-pressed={readAll} onClick={() => setReadAll(value => !value)}>{readAll ? "One moment at a time" : "Read all"}<Icon name={readAll ? "book" : "list"} size={16} /></button>
              </div>
              {!readAll && <nav className="recap-segments" aria-label="Recap moments">{paras.map((_, i) => <button key={i} type="button" aria-label={`Go to recap moment ${i + 1}`} aria-current={i === beat ? "step" : undefined} onClick={() => setBeat(i)}><span /></button>)}</nav>}
              <div className="hero-recap" aria-live="polite" aria-atomic="true">
              {paras.map((p, i) => (
                <section key={i} className="recap-beat" hidden={!readAll && i !== beat} aria-label={`Moment ${i + 1}`}>
                  <span className="recap-beat-number" aria-hidden="true">{String(i + 1).padStart(2, "0")}</span>
                  <p>
                  {wrapNames(p, cast).map((seg, j) =>
                    // LIT-30: a character name -> a live button that opens its card, anchored to the chip
                    seg.entityId != null ? (
                      <button
                        key={j}
                        type="button"
                        className="name-chip"
                        data-entity-id={seg.entityId}
                        onClick={(e) =>
                          setCard({
                            entityId: seg.entityId!,
                            anchorRect: e.currentTarget.getBoundingClientRect(),
                          })
                        }
                      >
                        {seg.text}
                      </button>
                    ) : (
                      <Fragment key={j}>{seg.text}</Fragment>
                    ),
                  )}
                  </p>
                </section>
              ))}
              </div>
              <div className="recap-controls">
                {!readAll && <Button icon="arrowLeft" disabled={beat === 0} onClick={() => setBeat(value => value - 1)}>Previous moment</Button>}
                {!readAll && beat < paras.length - 1 ? <Button variant="primary" icon="arrowRight" onClick={() => setBeat(value => value + 1)}>Next moment</Button> : <Button variant="primary" icon="book" onClick={requestClose}>Back to reading</Button>}
              </div>
            </div>
            {showPeopleStats && (
              <aside
                className="hero-facts"
                aria-label={
                  bookType === "novel"
                    ? "the cast and threads so far"
                    : "people and connections so far"
                }
              >
                <span className="world-kicker">Within these pages</span>
                {cmu.now && <div className="recap-now"><Icon name="bookmark" size={19} /><h2>Where we left off</h2><p>{cmu.now}</p></div>}
                <div className="stat-line">
                  <span className="smallcaps">{presentation.peopleLabel}</span>
                  <span className="n">{cmu.cast_size}</span>
                </div>
                <div className="stat-line">
                  <span className="smallcaps">
                    {presentation.connectionsLabel}
                  </span>
                  <span className="n">{cmu.open_threads}</span>
                </div>
                <p className="recap-side-note">Your memory stops where your reading does.</p>
              </aside>
            )}
          </div>
        )}
      </article>
      {card && (
        <NameCard
          bookId={bookId}
          entityId={card.entityId}
          bookmark={bookmark}
          anchorRect={card.anchorRect}
          onClose={closeCard}
          onNavigate={(id) =>
            setCard((c) => ({
              entityId: id,
              anchorRect: c?.anchorRect ?? null,
            }))
          }
        />
      )}
    </div>
  );
}
