/** The Codex (LIT-31): the deep re-orientation surface — a two-leaf spread where the story broken
 * down (left), People & Connections (right), the foot scrubber, and the layered name card all work
 * together over ONE shared cursor T ∈ 1..bookmark. Every panel renders CLAMPED structured server data only
 * (/graph + /notes + /character — no LLM, no gate); T is clamped ≤ bookmark here AND server-side.
 * Dialog a11y is the proven hero pattern (3ba2970): synchronous trigger capture at render, focus
 * effect keyed [], Tab trap, Escape, card-owns-the-keyboard, synchronous un-inert on card close;
 * ReaderPage's onClose un-inerts the grid synchronously before unmount (WCAG 2.4.3). */
import { useEffect, useRef, useState } from "react";
import { api, type BookType, type Graph, type MemoryCorrections, type Notes } from "../api";
import { presentationFor } from "./bookProfile";
import { ChapterBreakdown } from "./ChapterBreakdown";
import { keepTabFocusInside } from "./focusTrap";
import { mostConnected } from "./graph";
import { NameCard } from "./NameCard";
import { PeopleConnections } from "./PeopleConnections";
import { roman } from "./roman";
import { Scrubber } from "./Scrubber";

export function Codex({
  bookId,
  bookmark,
  bookType = "novel",
  onClose,
}: {
  bookId: string;
  bookmark: number;
  bookType?: BookType;
  onClose: () => void;
}) {
  const [t, setT] = useState(bookmark); // the shared cursor: everything renders as-of chapter T
  const [graph, setGraph] = useState<Graph | null>(null);
  const [notes, setNotes] = useState<Notes | null>(null);
  const [corrections, setCorrections] = useState<MemoryCorrections | null>(null);
  const [err, setErr] = useState(false);
  const [focusId, setFocusId] = useState<number | null>(null);
  const [scrollTo, setScrollTo] = useState<{ chapter: number; requestId: number } | null>(null);
  const scrollRequestId = useRef(0);
  const [card, setCard] = useState<{ entityId: number; anchorRect: DOMRect | null } | null>(null);
  const cardRef = useRef(card);
  cardRef.current = card;
  const rootRef = useRef<HTMLDivElement>(null);
  const articleRef = useRef<HTMLElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  // capture the opener SYNCHRONOUSLY at render — before ReaderPage's modal-inert steals its focus —
  // so close restores focus to the opener, not <body> (the 3ba2970 pattern; WCAG 2.4.3)
  const triggerRef = useRef<HTMLElement | null>(null);
  if (triggerRef.current === null) triggerRef.current = document.activeElement as HTMLElement | null;

  // while a name card is open the codex leaves are inert — the card is the sole active surface
  // (the LIT-30 nested-modal pattern). Close-time un-inert is SYNCHRONOUS in closeCard, not here.
  useEffect(() => {
    const a = articleRef.current;
    if (a) a.inert = card != null;
  }, [card]);

  const closeCard = () => {
    if (articleRef.current) articleRef.current.inert = false;
    setCard(null);
  };

  useEffect(() => {
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (cardRef.current) return; // the card owns Escape/Tab while open (its own trap)
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
      const trig = triggerRef.current;
      if (trig && document.contains(trig) && typeof trig.focus === "function") trig.focus();
    };
  }, []); // capture once; onClose/card read via refs so the trigger is never re-captured

  // the breakdown is a STABLE table of contents of 1..bookmark (all already read, all spoiler-safe):
  // fetched once at the frontier, never truncated by scrubbing (the spec's chosen model — a
  // full-rewind ToC would be equally safe but less navigable)
  useEffect(() => {
    let dead = false;
    api
      .notes(bookId, bookmark)
      .then((n) => !dead && setNotes(n))
      .catch(() => !dead && setErr(true));
    return () => {
      dead = true;
    };
  }, [bookId, bookmark]);

  // the graph + cast re-time whenever T moves (scrubber / chapter click). Clamped both sides: T is
  // bounded 1..bookmark here, and the server clamps to the high-water regardless.
  useEffect(() => {
    let dead = false;
    setErr(false);
    api
      .graph(bookId, t)
      .then((g) => {
        if (dead) return;
        setGraph(g);
        // keep the focus if still visible at this T, else re-default to the most-connected —
        // a pure narrowing choice over already-clamped data (never widens the frontier)
        setFocusId((cur) =>
          cur != null && g.characters.some((c) => c.entity_id === cur) ? cur : mostConnected(g),
        );
      })
      .catch(() => !dead && setErr(true));
    return () => {
      dead = true;
    };
  }, [bookId, t]);

  useEffect(() => {
    let dead = false;
    setCorrections(null);
    api.memoryCorrections(bookId, t)
      .then((history) => !dead && setCorrections(history))
      .catch(() => !dead && setCorrections({ as_of_chapter: t, items: [] }));
    return () => { dead = true; };
  }, [bookId, t]);

  const jumpToChapter = (chapter: number) => {
    scrollRequestId.current += 1;
    setScrollTo({ chapter, requestId: scrollRequestId.current });
  };
  const retime = (rev: number) => {
    const next = Math.max(1, Math.min(rev, bookmark));
    setGraph(null); // never label the previous frontier as the newly requested, earlier chapter
    setErr(false);
    setT(next);
    jumpToChapter(next);
  };

  const openCard = (entityId: number, anchorRect: DOMRect | null) => {
    setCard({ entityId, anchorRect });
  };

  // Selecting a person must not silently move the reader's place in the chapter breakdown.
  const recenter = (entityId: number) => {
    setFocusId(entityId);
  };

  const correctMemory = async (entityId: number, canonicalName: string, reason: string) => {
    const result = await api.correctMemory(bookId, {
      source_entity_id: entityId,
      canonical_name: canonicalName,
      reason,
      bookmark,
    });
    const refreshed = await api.graph(bookId, bookmark);
    setGraph(refreshed);
    setCorrections({ as_of_chapter: result.as_of_chapter, items: result.items });
    if (typeof result.target_entity_id === "number") setFocusId(result.target_entity_id);
    else setFocusId(mostConnected(refreshed));
  };

  const presentation = presentationFor(bookType);
  const peopleUseful = graph != null && (
    presentation.peopleMode === "primary"
    || graph.characters.length > 0
    || graph.relationships.length > 0
  );
  const emptyNovel = bookType === "novel" && graph != null && graph.characters.length === 0;

  return (
    <div
      className="hero-scrim codex-scrim"
      role="dialog"
      aria-modal="true"
      aria-label={presentation.codexTitle}
      ref={rootRef}
    >
      <article className="hero-page codex-page" ref={articleRef}>
        <header className="hero-head">
          <span className="smallcaps hero-eyebrow">{presentation.codexTitle}</span>
          <span className="codex-asof smallcaps">as of {presentation.unit} {roman(t)}</span>
          <button className="plain smallcaps hero-close" ref={closeRef} onClick={onClose}>
            close ✕
          </button>
        </header>

        {err ? (
          <p className="quiet hero-quiet">The codex could not be gathered — try again in a moment.</p>
        ) : graph == null || notes == null ? (
          <p className="quiet hero-quiet">Gathering the codex…</p>
        ) : emptyNovel ? (
          <p className="quiet hero-quiet">No one has stepped onto the page yet.</p>
        ) : (
          <div className="codex-spread">
            <section className="codex-leaf left" aria-label="the story broken down">
              <h2 className="smallcaps codex-leaf-head">{presentation.breakdownTitle}</h2>
              <ChapterBreakdown
                chapters={notes.chapters}
                cast={notes.cast}
                currentChapter={t}
                scrollTo={scrollTo}
                onSelectChapter={retime}
                onOpenCard={openCard}
                unit={presentation.unit}
              />
            </section>
            {peopleUseful ? (
              <section className="codex-leaf right" aria-label="the cast and their ties">
                <div className="codex-right">
                  <PeopleConnections
                    graph={graph}
                    focusId={focusId}
                    onFocus={recenter}
                    onOpenCard={openCard}
                    onJumpToChapter={jumpToChapter}
                    onCorrectMemory={
                      t === bookmark
                      && graph.characters.some(
                        (character) => character.entity_id === focusId
                          && character.revealed_at < bookmark,
                      )
                        ? correctMemory
                        : undefined
                    }
                  />
                  <section className="memory-correction-history" aria-labelledby="memory-history-title">
                    <h3 className="smallcaps" id="memory-history-title">memory correction history</h3>
                    {corrections == null ? (
                      <p className="quiet">Gathering correction provenance…</p>
                    ) : corrections.items.length === 0 ? (
                      <p className="quiet">No corrections are visible at this chapter.</p>
                    ) : (
                      <ol>
                        {corrections.items.map((correction) => (
                          <li key={correction.correction_id}>
                            <p>
                              <span>{correction.source_entities.map((item) => item.name).join(", ")}</span>
                              <span aria-hidden="true"> → </span>
                              <span className="sr-only"> corrected to </span>
                              <strong>{correction.target_entities.map((item) => item.name).join(", ")}</strong>
                            </p>
                            <p>{correction.reason || "Reader correction"}</p>
                            <p className="quiet">
                              <time dateTime={correction.recorded_at}>
                                Recorded {correction.recorded_at.slice(0, 10)}
                              </time>
                            </p>
                            <button type="button" onClick={() => retime(correction.effective_at)}>
                              Effective Chapter {roman(correction.effective_at)}
                            </button>
                          </li>
                        ))}
                      </ol>
                    )}
                  </section>
                </div>
              </section>
            ) : (
              <aside className="codex-leaf right quiet" aria-label="book profile note">
                <h2 className="smallcaps codex-leaf-head">about these notes</h2>
                <p>{presentation.description}</p>
                <p>People and plot sections stay out of the way until grounded extraction finds useful material.</p>
              </aside>
            )}
          </div>
        )}

        {bookmark > 1 && (
          <div className="codex-foot">
            <Scrubber max={bookmark} value={t} onChange={retime} unit={presentation.unit} />
          </div>
        )}
      </article>
      {card && (
        <NameCard
          bookId={bookId}
          entityId={card.entityId}
          // the card reads at the FRONTIER, not T: everything <= bookmark is already read (safe), and
          // a breakdown chip from a chapter past a scrubbed-back T must not 404 in the reader's face
          bookmark={bookmark}
          anchorRect={card.anchorRect}
          onClose={closeCard}
          onNavigate={(id) => setCard((c) => ({ entityId: id, anchorRect: c?.anchorRect ?? null }))}
        />
      )}
    </div>
  );
}
