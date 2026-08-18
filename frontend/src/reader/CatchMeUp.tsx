/** The catch-me-up HERO (LIT-14): the companion's memory opened to a full "the story so far" page —
 * a grand printed leaf with a drop-cap recap, the cast and open threads in the margin, over the
 * clamped /catch-me-up route. It is a dialog (focus moves in, Escape closes, focus restores — the
 * LIT-16 pattern) and renders ONLY the server-clamped data: the recap the runtime gate + LLM-judge
 * cleared, never anything client-computed or past the frontier. Fetches by (bookId, bookmark) so the
 * LIT-15 scrubber can walk it back through time; the server still clamps to the high-water. */
import { Fragment, useEffect, useRef, useState } from "react";
import { api, type BookType, type CatchMeUp as CMU } from "../api";
import { presentationFor } from "./bookProfile";
import { keepTabFocusInside } from "./focusTrap";
import { NameCard } from "./NameCard";
import { wrapNames } from "./names";
import { roman } from "./roman";
import { recapFailureMessage } from "./recapFailure";

export function CatchMeUp({
  bookId,
  bookmark,
  totalAtoms,
  bookType = "novel",
  welcomeBackChapter,
  onClose,
}: {
  bookId: string;
  bookmark: number;
  totalAtoms: number;
  bookType?: BookType;
  // LIT-29 "since you were last here": when the hero auto-opens after a return gap, the chapter the
  // reader left off in — drives the welcome-back framing. null/omitted on a deliberate open.
  welcomeBackChapter?: number | null;
  onClose: () => void;
}) {
  const [cmu, setCmu] = useState<CMU | null>(null);
  const [err, setErr] = useState<unknown>(null);
  // LIT-30: the open name card (a clicked chip), anchored to it. While a card is open it owns the
  // keyboard; the hero yields (below) so its own Escape/Tab-trap take over.
  const [card, setCard] = useState<{ entityId: number; anchorRect: DOMRect | null } | null>(null);
  const cardRef = useRef(card);
  cardRef.current = card;
  const rootRef = useRef<HTMLDivElement>(null);
  const articleRef = useRef<HTMLElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  // capture the opener SYNCHRONOUSLY at render — before ReaderPage's modal-inert can steal its focus
  // (the opener sits in the .companion that inerts on open) — so the hero's close restores focus to the
  // opener, not <body> (a11y pass-2, WCAG 2.4.3; the same fix as the NameCard trigger capture).
  const heroTriggerRef = useRef<HTMLElement | null>(null);
  if (heroTriggerRef.current === null) heroTriggerRef.current = document.activeElement as HTMLElement | null;

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
    setCmu(null);
    setErr(null);
    api
      .catchMeUp(bookId, bookmark)
      .then((d) => !dead && setCmu(d))
      .catch((error: unknown) => !dead && setErr(error));
    return () => {
      dead = true;
    };
  }, [bookId, bookmark]);

  const paras = (cmu?.recap ?? "").split(/\n+/).filter(Boolean);
  const cast = cmu?.cast ?? []; // bookmark-bounded; wrapNames wraps ONLY these, never a future name
  const asOf = cmu?.as_of_chapter ?? bookmark;
  // a11y: when the hero AUTO-opens after a return gap, fold the welcome-back framing into the dialog's
  // accessible NAME. A freshly-opened dialog announces its name when focus moves in, so this is
  // reliable — unlike a sibling role=status region mounted already-populated, which screen readers drop
  // (a11y review HIGH). The visible .welcome-back banner below is sighted-reader reinforcement only.
  const empty = cmu != null && (cmu.as_of_chapter === 0 || !cmu.recap);
  const presentation = presentationFor(bookType);
  const dialogLabel =
    welcomeBackChapter != null
      ? `Welcome back — you left off in ${presentation.unit} ${roman(welcomeBackChapter)}. ${presentation.recapLink}.`
      : presentation.recapLink;
  const showPeopleStats = cmu != null && (
    presentation.peopleMode === "primary" || cmu.cast_size > 0 || cmu.open_threads > 0
  );

  return (
    <div className="hero-scrim" role="dialog" aria-modal="true" aria-label={dialogLabel} ref={rootRef}>
      <article className="hero-page" ref={articleRef}>
        <div className="hero-ribbon" aria-hidden="true" />
        <header className="hero-head">
          <span className="smallcaps hero-eyebrow">{presentation.recapLink}</span>
          <button className="plain smallcaps hero-close" ref={closeRef} onClick={onClose}>
            close the book ✕
          </button>
        </header>
        {welcomeBackChapter != null && (
          // visual reinforcement only — the SR announcement rides the dialog's accessible name (above)
          <div className="welcome-back">
            <span className="smallcaps">welcome back</span> — you left off in {presentation.unit}{" "}
            {roman(welcomeBackChapter)}. Here is {presentation.recapLink}.
          </div>
        )}
        <div className="hero-asof">
          {empty
            ? "nothing read yet"
            : `as of ${presentation.unit} ${roman(asOf)} · ${asOf} of ${totalAtoms}`}
        </div>

        {err ? (
          <p className="quiet hero-quiet">{recapFailureMessage(err, "hero")}</p>
        ) : cmu == null ? (
          <p className="quiet hero-quiet">Gathering the memory…</p>
        ) : empty ? (
          <p className="quiet hero-quiet">
            Finish a {presentation.unit} and {presentation.recapLink} will gather here.
          </p>
        ) : (
          <div className="hero-body">
            <div className="hero-recap">
              {paras.map((p, i) => (
                <p key={i}>
                  {wrapNames(p, cast).map((seg, j) =>
                    // LIT-30: a character name -> a live button that opens its card, anchored to the chip
                    seg.entityId != null ? (
                      <button
                        key={j}
                        type="button"
                        className="name-chip"
                        data-entity-id={seg.entityId}
                        onClick={(e) =>
                          setCard({ entityId: seg.entityId!, anchorRect: e.currentTarget.getBoundingClientRect() })
                        }
                      >
                        {seg.text}
                      </button>
                    ) : (
                      <Fragment key={j}>{seg.text}</Fragment>
                    ),
                  )}
                </p>
              ))}
            </div>
            {showPeopleStats && <aside className="hero-facts" aria-label={
              bookType === "novel" ? "the cast and threads so far" : "people and connections so far"
            }>
              <div className="stat-line">
                <span className="smallcaps">{presentation.peopleLabel}</span>
                <span className="n">{cmu.cast_size}</span>
              </div>
              <div className="stat-line">
                <span className="smallcaps">{presentation.connectionsLabel}</span>
                <span className="n">{cmu.open_threads}</span>
              </div>
            </aside>}
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
          onNavigate={(id) => setCard((c) => ({ entityId: id, anchorRect: c?.anchorRect ?? null }))}
        />
      )}
    </div>
  );
}
