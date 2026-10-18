/** A stable rail of completed chapters and one selected chapter spotlight. Summary, events and
 * names are rendered verbatim from clamped notes. Explicit citation requests reveal their chapter
 * after clearing search, while the caller controls the historical memory cursor. */
import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import type { CastMember, ChapterNote } from "../api";
import { wrapNames } from "./names";
import { roman } from "./roman";
import { Icon } from "../ui/Icon";

export function ChapterBreakdown({
  chapters,
  cast,
  currentChapter,
  scrollTo,
  onSelectChapter,
  onOpenCard,
  unit = "chapter",
}: {
  chapters: ChapterNote[];
  cast: CastMember[];
  currentChapter: number;
  scrollTo: { chapter: number; requestId: number } | null;
  onSelectChapter: (revealedAt: number) => void;
  onOpenCard: (entityId: number, anchorRect: DOMRect) => void;
  unit?: "chapter" | "scene" | "section";
}) {
  const entryRefs = useRef<Record<number, HTMLElement | null>>({});
  const handledScrollRequest = useRef<number | null>(null);
  const [jumpedChapter, setJumpedChapter] = useState<number | null>(null);
  const [expandedChapter, setExpandedChapter] = useState<number | null>(
    currentChapter,
  );
  const [query, setQuery] = useState("");

  useEffect(() => setExpandedChapter(currentChapter), [currentChapter]);

  const visibleChapters = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    if (!needle) return chapters;
    return chapters.filter((chapter) =>
      [
        chapter.title,
        chapter.summary,
        ...chapter.new_characters.map((character) => character.name),
        ...chapter.events,
      ].some((value) => value.toLocaleLowerCase().includes(needle)),
    );
  }, [chapters, query]);

  useEffect(() => {
    if (scrollTo == null) return;
    setQuery("");
    setExpandedChapter(scrollTo.chapter);
  }, [scrollTo]);

  useEffect(() => {
    if (
      scrollTo == null ||
      query !== "" ||
      expandedChapter !== scrollTo.chapter ||
      handledScrollRequest.current === scrollTo.requestId
    )
      return;
    const entry = entryRefs.current[scrollTo.chapter];
    if (!entry) return;
    // Wait for a cleared search and expanded entry to render before measuring the destination.
    // Otherwise a citation hidden by the current filter never scrolls into view.
    handledScrollRequest.current = scrollTo.requestId;
    entry.scrollIntoView?.({
      block: "center",
      behavior: window.matchMedia?.("(prefers-reduced-motion: reduce)").matches
        ? "auto"
        : "smooth",
    });
    setJumpedChapter(scrollTo.chapter);
  }, [scrollTo, query, expandedChapter, chapters]);

  useEffect(() => {
    if (jumpedChapter == null) return;
    const timer = window.setTimeout(() => setJumpedChapter(null), 1400);
    return () => window.clearTimeout(timer);
  }, [jumpedChapter, scrollTo?.requestId]);

  // render free text with character names as card-opening chips (the LIT-30 affordance, reused)
  const renderText = (text: string) =>
    wrapNames(text, cast).map((seg, j) =>
      seg.entityId != null ? (
        <button
          key={j}
          type="button"
          className="name-chip"
          data-entity-id={seg.entityId}
          onClick={(e) =>
            onOpenCard(seg.entityId!, e.currentTarget.getBoundingClientRect())
          }
        >
          {seg.text}
        </button>
      ) : (
        <Fragment key={j}>{seg.text}</Fragment>
      ),
    );

  if (chapters.length === 0)
    return (
      <p className="quiet chapter-breakdown-empty">
        The story has not begun yet.
      </p>
    );

  const selected = visibleChapters.find((chapter) => chapter.revealed_at === expandedChapter);
  const selectedIndex = chapters.findIndex((chapter) => chapter.revealed_at === expandedChapter);
  const select = (chapter: ChapterNote) => {
    setExpandedChapter(chapter.revealed_at);
    onSelectChapter(chapter.revealed_at);
  };

  return (
    <div className="journey-workspace">
      <div className="journey-toolbar">
        <div><span className="memory-eyebrow">THE PATH YOU’VE TAKEN</span><p>{chapters.length} completed {unit}{chapters.length === 1 ? "" : "s"}. Every detail is yours to revisit.</p></div>
        <label className="chapter-search">
          <Icon name="search" size={17} />
          <span className="sr-only">Find a chapter</span>
          <input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder={`Find a ${unit}…`} />
        </label>
      </div>
      {visibleChapters.length === 0 ? (
        <p className="quiet chapter-search-empty" role="status">No {unit}s match “{query}”.</p>
      ) : (
        <>
          <ol className="chapter-breakdown journey-rail" aria-label="Completed chapters">
            {visibleChapters.map((chapter) => (
              <li key={chapter.chapter_key} className={`chapter-entry${chapter.revealed_at === expandedChapter ? " is-current" : ""}`}>
                <button
                  type="button"
                  className="chapter-head plain"
                  aria-label={`${unit[0].toUpperCase() + unit.slice(1)} ${roman(chapter.revealed_at)}${chapter.title ? ` — ${chapter.title}` : ""}`}
                  aria-current={chapter.revealed_at === currentChapter ? "true" : undefined}
                  aria-expanded={expandedChapter === chapter.revealed_at}
                  onClick={() => select(chapter)}
                >
                  <span className="journey-stop" aria-hidden="true">{roman(chapter.revealed_at)}</span>
                  <span className="journey-stop-label">{unit} {chapter.revealed_at}</span>
                  <span className="journey-stop-detail">{chapter.events.length} {chapter.events.length === 1 ? "moment" : "moments"}</span>
                </button>
              </li>
            ))}
          </ol>
          {selected ? (
            <article
              className={`journey-spotlight${selected.revealed_at === jumpedChapter ? " is-jump-target" : ""}`}
              key={selected.chapter_key}
              ref={(element) => { entryRefs.current[selected.revealed_at] = element; }}
              aria-label={`${unit} ${selected.revealed_at} details`}
            >
              <header className="journey-chapter-heading">
                <div><span className="memory-eyebrow">{unit.toUpperCase()} {roman(selected.revealed_at)}</span><h3>{selected.title || `${unit[0].toUpperCase() + unit.slice(1)} ${roman(selected.revealed_at)}`}</h3></div>
                <span className="journey-chapter-number" aria-hidden="true">{String(selected.revealed_at).padStart(2, "0")}</span>
              </header>
              {selected.summary && <p className="chapter-summary">{renderText(selected.summary)}</p>}
              <div className="journey-details">
                {selected.events.length > 0 && (
                  <section className="journey-moments">
                    <h4><Icon name="sparkle" size={16} /> Moments to remember</h4>
                    <ol className="chapter-events">
                      {selected.events.map((event, index) => <li key={index}><span className="journey-event-number" aria-hidden="true">{String(index + 1).padStart(2, "0")}</span><p>{renderText(event)}</p></li>)}
                    </ol>
                  </section>
                )}
                {selected.new_characters.length > 0 && (
                  <section className="journey-arrivals">
                    <h4>New to the story</h4>
                    <p>The people you met in this {unit}.</p>
                    <div className="chapter-new">
                      {selected.new_characters.map((person) => (
                        <button type="button" className="journey-person name-chip" key={person.entity_id} onClick={(event) => onOpenCard(person.entity_id, event.currentTarget.getBoundingClientRect())}>
                          <span aria-hidden="true">{person.name.split(/\s+/).slice(0, 2).map((word) => word[0]).join("")}</span>{person.name}<Icon name="arrowRight" size={14} />
                        </button>
                      ))}
                    </div>
                  </section>
                )}
              </div>
              <footer className="journey-step-controls">
                <button type="button" disabled={selectedIndex <= 0} onClick={() => select(chapters[selectedIndex - 1])}><Icon name="arrowLeft" size={16} /> Previous {unit}</button>
                <span>{selectedIndex + 1} of {chapters.length} remembered</span>
                <button type="button" disabled={selectedIndex >= chapters.length - 1} onClick={() => select(chapters[selectedIndex + 1])}>Next {unit}<Icon name="arrowRight" size={16} /></button>
              </footer>
            </article>
          ) : <p className="journey-choose">Choose a {unit} above to step back into the story.</p>}
        </>
      )}
    </div>
  );
}
