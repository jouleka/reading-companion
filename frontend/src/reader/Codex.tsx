/** The Codex (LIT-31): the deep re-orientation surface — a two-leaf spread where the story broken
 * down (left), People & Connections (right), the foot scrubber, and the layered name card all work
 * together over ONE shared cursor T ∈ 1..bookmark. Every panel renders CLAMPED structured server data only
 * (/graph + /notes + /character — no LLM, no gate); T is clamped ≤ bookmark here AND server-side.
 * Dialog a11y is the proven hero pattern (3ba2970): synchronous trigger capture at render, focus
 * effect keyed [], Tab trap, Escape, card-owns-the-keyboard, synchronous un-inert on card close;
 * ReaderPage's onClose un-inerts the grid synchronously before unmount (WCAG 2.4.3). */
import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import {
  api,
  type BookType,
  type Graph,
  type MemoryCorrections,
  type Notes,
} from "../api";
import { Button } from "../ui/Button";
import { useDialogMotion } from "../ui/useDialogMotion";
import { presentationFor } from "./bookProfile";
import { ChapterBreakdown } from "./ChapterBreakdown";
import { keepTabFocusInside } from "./focusTrap";
import { mostConnected } from "./graph";
import { useCompactReaderShell } from "./mobileShell";
import { NameCard } from "./NameCard";
import { PeopleConnections } from "./PeopleConnections";
import { roman } from "./roman";
import { Scrubber } from "./Scrubber";

type ChapterSnapshot<T> = { bookId: string; chapter: number; data: T };

export function Codex({
  bookId,
  bookmark,
  bookType = "novel",
  workspaceNavigation,
  onClose,
}: {
  bookId: string;
  bookmark: number;
  bookType?: BookType;
  workspaceNavigation?: ReactNode;
  onClose: () => void;
}) {
  const { closing, requestClose } = useDialogMotion(onClose);
  const compactShell = useCompactReaderShell();
  const [mobileTab, setMobileTab] = useState<"chapters" | "people">("chapters");
  const mobileTabsId = useId();
  const chapterTabRef = useRef<HTMLButtonElement>(null);
  const peopleTabRef = useRef<HTMLButtonElement>(null);
  const [t, setT] = useState(bookmark); // the shared cursor: everything renders as-of chapter T
  const [graphSnapshot, setGraphSnapshot] =
    useState<ChapterSnapshot<Graph> | null>(null);
  const [notesSnapshot, setNotesSnapshot] =
    useState<ChapterSnapshot<Notes> | null>(null);
  const [correctionsSnapshot, setCorrectionsSnapshot] =
    useState<ChapterSnapshot<MemoryCorrections> | null>(null);
  const [graphError, setGraphError] = useState(false);
  const [notesError, setNotesError] = useState(false);
  const [correctionsError, setCorrectionsError] = useState(false);
  const [graphRetry, setGraphRetry] = useState(0);
  const [notesRetry, setNotesRetry] = useState(0);
  const [correctionsRetry, setCorrectionsRetry] = useState(0);
  const [showCorrections, setShowCorrections] = useState(false);
  const historyId = useId();
  // A result belongs to its requested cursor. A late frontier response must never appear under an
  // earlier chapter label, including when a memory correction finishes after the reader scrubs back.
  const graph =
    graphSnapshot?.bookId === bookId && graphSnapshot.chapter === t
      ? graphSnapshot.data
      : null;
  const notes =
    notesSnapshot?.bookId === bookId && notesSnapshot.chapter === bookmark
      ? notesSnapshot.data
      : null;
  const corrections =
    correctionsSnapshot?.bookId === bookId && correctionsSnapshot.chapter === t
      ? correctionsSnapshot.data
      : null;
  const cursorRef = useRef({ bookId, chapter: t });
  cursorRef.current = { bookId, chapter: t };
  const [focusId, setFocusId] = useState<number | null>(null);
  const [scrollTo, setScrollTo] = useState<{
    chapter: number;
    requestId: number;
  } | null>(null);
  const scrollRequestId = useRef(0);
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
  // capture the opener SYNCHRONOUSLY at render — before ReaderPage's modal-inert steals its focus —
  // so close restores focus to the opener, not <body> (the 3ba2970 pattern; WCAG 2.4.3)
  const triggerRef = useRef<HTMLElement | null>(null);
  if (triggerRef.current === null)
    triggerRef.current = document.activeElement as HTMLElement | null;

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
      if (trig && document.contains(trig) && typeof trig.focus === "function")
        trig.focus();
    };
  }, []); // capture once; onClose/card read via refs so the trigger is never re-captured

  // the breakdown is a STABLE table of contents of 1..bookmark (all already read, all spoiler-safe):
  // fetched once at the frontier, never truncated by scrubbing (the spec's chosen model — a
  // full-rewind ToC would be equally safe but less navigable)
  useEffect(() => {
    let dead = false;
    setNotesError(false);
    api
      .notes(bookId, bookmark)
      .then(
        (data) =>
          !dead && setNotesSnapshot({ bookId, chapter: bookmark, data }),
      )
      .catch(() => !dead && setNotesError(true));
    return () => {
      dead = true;
    };
  }, [bookId, bookmark, notesRetry]);

  // the graph + cast re-time whenever T moves (scrubber / chapter click). Clamped both sides: T is
  // bounded 1..bookmark here, and the server clamps to the high-water regardless.
  useEffect(() => {
    let dead = false;
    setGraphError(false);
    api
      .graph(bookId, t)
      .then((g) => {
        if (dead) return;
        setGraphSnapshot({ bookId, chapter: t, data: g });
        // keep the focus if still visible at this T, else re-default to the most-connected —
        // a pure narrowing choice over already-clamped data (never widens the frontier)
        setFocusId((cur) =>
          cur != null && g.characters.some((c) => c.entity_id === cur)
            ? cur
            : mostConnected(g),
        );
      })
      .catch(() => !dead && setGraphError(true));
    return () => {
      dead = true;
    };
  }, [bookId, t, graphRetry]);

  useEffect(() => {
    let dead = false;
    setCorrectionsError(false);
    api
      .memoryCorrections(bookId, t)
      .then(
        (data) => !dead && setCorrectionsSnapshot({ bookId, chapter: t, data }),
      )
      .catch(() => !dead && setCorrectionsError(true));
    return () => {
      dead = true;
    };
  }, [bookId, t, correctionsRetry]);

  const jumpToChapter = (chapter: number, activatePanel = true) => {
    if (compactShell && activatePanel && mobileTab !== "chapters") {
      setMobileTab("chapters");
      // The citation's source becomes hidden. Keep keyboard focus on a visible navigation control.
      chapterTabRef.current?.focus();
    }
    scrollRequestId.current += 1;
    setScrollTo({ chapter, requestId: scrollRequestId.current });
  };
  const retime = (rev: number) => {
    const next = Math.max(1, Math.min(rev, bookmark));
    // Re-selecting the current chapter is a navigation request, not a data invalidation. Clearing
    // the graph here used to leave the panel loading forever because T (and its effect) did not change.
    if (next !== t) {
      setGraphError(false);
      setCorrectionsError(false);
      setT(next);
    }
    // Scrubbing the people panel should preserve that panel while its memory re-times.
    jumpToChapter(next, false);
  };

  const openCard = (entityId: number, anchorRect: DOMRect | null) => {
    setCard({ entityId, anchorRect });
  };

  // Selecting a person must not silently move the reader's place in the chapter breakdown.
  const recenter = (entityId: number) => {
    setFocusId(entityId);
  };

  const correctMemory = async (
    entityId: number,
    canonicalName: string,
    reason: string,
  ) => {
    const result = await api.correctMemory(bookId, {
      source_entity_id: entityId,
      canonical_name: canonicalName,
      reason,
      bookmark,
    });
    const refreshed = await api.graph(bookId, bookmark);
    if (
      cursorRef.current.bookId !== bookId ||
      cursorRef.current.chapter !== bookmark
    )
      return;
    setGraphSnapshot({ bookId, chapter: bookmark, data: refreshed });
    setGraphError(false);
    setCorrectionsSnapshot({
      bookId,
      chapter: bookmark,
      data: { as_of_chapter: result.as_of_chapter, items: result.items },
    });
    setCorrectionsError(false);
    setShowCorrections(true);
    if (typeof result.target_entity_id === "number")
      setFocusId(result.target_entity_id);
    else setFocusId(mostConnected(refreshed));
  };

  const presentation = presentationFor(bookType);
  const peopleUseful =
    graph != null &&
    (presentation.peopleMode === "primary" ||
      graph.characters.length > 0 ||
      graph.relationships.length > 0);
  const emptyNovel =
    bookType === "novel" && graph != null && graph.characters.length === 0;

  return (
    <div
      className="hero-scrim codex-scrim"
      data-closing={closing || undefined}
      role="dialog"
      aria-modal="true"
      aria-label={presentation.codexTitle}
      ref={rootRef}
    >
      <article
        className="hero-page codex-page"
        data-compact={compactShell || undefined}
        ref={articleRef}
      >
        <header className="hero-head">
          <span className="smallcaps hero-eyebrow">
            {presentation.codexTitle}
          </span>
          <span className="codex-asof smallcaps">
            as of {presentation.unit} {roman(t)}
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

        {compactShell && !(emptyNovel && notes != null) && (
          <div
            className="codex-mobile-tabs"
            role="tablist"
            aria-label="Reading memory views"
            onKeyDown={(event) => {
              let next: "chapters" | "people";
              if (event.key === "Home") next = "chapters";
              else if (event.key === "End") next = "people";
              else if (
                event.key === "ArrowLeft" ||
                event.key === "ArrowRight"
              ) {
                next = mobileTab === "chapters" ? "people" : "chapters";
              } else return;
              event.preventDefault();
              setMobileTab(next);
              (next === "chapters"
                ? chapterTabRef
                : peopleTabRef
              ).current?.focus();
            }}
          >
            <Button
              variant="ghost"
              role="tab"
              id={`${mobileTabsId}-chapters-tab`}
              aria-controls={`${mobileTabsId}-chapters`}
              aria-selected={mobileTab === "chapters"}
              tabIndex={mobileTab === "chapters" ? 0 : -1}
              ref={chapterTabRef}
              onClick={() => setMobileTab("chapters")}
            >
              {presentation.unit === "chapter" ? "Chapters" : "Sections"}
            </Button>
            <Button
              variant="ghost"
              role="tab"
              id={`${mobileTabsId}-people-tab`}
              aria-controls={`${mobileTabsId}-people`}
              aria-selected={mobileTab === "people"}
              tabIndex={mobileTab === "people" ? 0 : -1}
              ref={peopleTabRef}
              onClick={() => setMobileTab("people")}
            >
              People &amp; connections
            </Button>
          </div>
        )}

        {emptyNovel && notes != null ? (
          <p className="quiet hero-quiet">
            No one has stepped onto the page yet.
          </p>
        ) : (
          <div className="codex-spread" data-mobile-tab={mobileTab}>
            <section
              className="codex-leaf left"
              id={`${mobileTabsId}-chapters`}
              role={compactShell ? "tabpanel" : undefined}
              aria-label={compactShell ? undefined : "the story broken down"}
              aria-labelledby={
                compactShell ? `${mobileTabsId}-chapters-tab` : undefined
              }
              hidden={compactShell && mobileTab !== "chapters"}
              aria-busy={notes == null && !notesError}
            >
              <h2 className="smallcaps codex-leaf-head">
                {presentation.breakdownTitle}
              </h2>
              {notesError ? (
                <div className="codex-state" role="alert">
                  <p className="quiet">
                    Chapter notes could not be loaded. Your reading position is
                    safe.
                  </p>
                  <Button
                    icon="refresh"
                    onClick={() => {
                      setNotesError(false);
                      setNotesRetry((value) => value + 1);
                    }}
                  >
                    Retry chapter notes
                  </Button>
                </div>
              ) : notes == null ? (
                <p className="quiet hero-quiet" role="status">
                  Gathering chapter notes…
                </p>
              ) : (
                <ChapterBreakdown
                  chapters={notes.chapters}
                  cast={notes.cast}
                  currentChapter={t}
                  scrollTo={
                    compactShell && mobileTab !== "chapters" ? null : scrollTo
                  }
                  onSelectChapter={retime}
                  onOpenCard={openCard}
                  unit={presentation.unit}
                />
              )}
            </section>
            <section
              className="codex-leaf right"
              id={`${mobileTabsId}-people`}
              role={compactShell ? "tabpanel" : undefined}
              aria-label={compactShell ? undefined : "the cast and their ties"}
              aria-labelledby={
                compactShell ? `${mobileTabsId}-people-tab` : undefined
              }
              hidden={compactShell && mobileTab !== "people"}
              aria-busy={graph == null && !graphError}
            >
              {graphError ? (
                <div className="codex-state" role="alert">
                  <p className="quiet">
                    This {presentation.unit}’s memory could not be loaded.
                    Choose another {presentation.unit} or try again.
                  </p>
                  <Button
                    icon="refresh"
                    onClick={() => {
                      setGraphError(false);
                      setGraphRetry((value) => value + 1);
                    }}
                  >
                    Retry memory
                  </Button>
                </div>
              ) : graph == null ? (
                <p className="quiet hero-quiet" role="status">
                  Gathering the codex…
                </p>
              ) : peopleUseful ? (
                <div className="codex-right">
                  <PeopleConnections
                    graph={graph}
                    focusId={focusId}
                    onFocus={recenter}
                    onOpenCard={openCard}
                    onJumpToChapter={(chapter) => jumpToChapter(chapter)}
                    onCorrectMemory={
                      t === bookmark &&
                      graph.characters.some(
                        (character) =>
                          character.entity_id === focusId &&
                          character.revealed_at < bookmark,
                      )
                        ? correctMemory
                        : undefined
                    }
                  />
                  <section
                    className="memory-correction-history"
                    aria-labelledby={`${historyId}-title`}
                  >
                    <h3 className="smallcaps" id={`${historyId}-title`}>
                      <button
                        type="button"
                        className="plain"
                        aria-expanded={showCorrections}
                        aria-controls={historyId}
                        onClick={() => setShowCorrections((value) => !value)}
                      >
                        Memory correction history
                        {corrections && corrections.items.length > 0
                          ? ` · ${corrections.items.length}`
                          : ""}
                      </button>
                    </h3>
                    <div id={historyId} hidden={!showCorrections}>
                      {showCorrections &&
                        (correctionsError ? (
                          <div role="alert">
                            <p className="quiet">
                              Correction history could not be loaded.
                            </p>
                            <Button
                              icon="refresh"
                              onClick={() => {
                                setCorrectionsError(false);
                                setCorrectionsRetry((value) => value + 1);
                              }}
                            >
                              Retry correction history
                            </Button>
                          </div>
                        ) : corrections == null ? (
                          <p className="quiet" role="status">
                            Loading correction history…
                          </p>
                        ) : corrections.items.length === 0 ? (
                          <p className="quiet">
                            No corrections are visible at this chapter.
                          </p>
                        ) : (
                          <ol>
                            {corrections.items.map((correction) => (
                              <li key={correction.correction_id}>
                                <p>
                                  <span>
                                    {correction.source_entities
                                      .map((item) => item.name)
                                      .join(", ")}
                                  </span>
                                  <span aria-hidden="true"> → </span>
                                  <span className="sr-only">
                                    {" "}
                                    corrected to{" "}
                                  </span>
                                  <strong>
                                    {correction.target_entities
                                      .map((item) => item.name)
                                      .join(", ")}
                                  </strong>
                                </p>
                                <p>
                                  {correction.reason || "Reader correction"}
                                </p>
                                <p className="quiet">
                                  <time dateTime={correction.recorded_at}>
                                    Recorded{" "}
                                    {correction.recorded_at.slice(0, 10)}
                                  </time>
                                </p>
                                <button
                                  type="button"
                                  onClick={() =>
                                    retime(correction.effective_at)
                                  }
                                >
                                  Effective Chapter{" "}
                                  {roman(correction.effective_at)}
                                </button>
                              </li>
                            ))}
                          </ol>
                        ))}
                    </div>
                  </section>
                </div>
              ) : (
                <div className="quiet" aria-label="book profile note">
                  <h2 className="smallcaps codex-leaf-head">
                    about these notes
                  </h2>
                  <p>{presentation.description}</p>
                  <p>
                    People and plot sections stay out of the way until grounded
                    extraction finds useful material.
                  </p>
                </div>
              )}
            </section>
          </div>
        )}

        {bookmark > 1 && (
          <div className="codex-foot">
            <Scrubber
              max={bookmark}
              value={t}
              onChange={retime}
              unit={presentation.unit}
            />
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
