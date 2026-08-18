/** ChapterBreakdown (LIT-31): the codex's left leaf — the story broken down, chapter by chapter. Each
 * entry is its summary + highlights (who first appears + that chapter's events); character names are
 * chips that open the LIT-30 card (reusing wrapNames). The chapter head is a button that re-times the
 * whole spread (the shared T cursor); the current chapter carries aria-current. `scrollTo` is an
 * explicit request so choosing the same citation twice still scrolls that entry into view.
 * The full index (1..bookmark) is always shown — it is a stable table of contents, all already-read. */
import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import type { CastMember, ChapterNote } from "../api";
import { wrapNames } from "./names";
import { roman } from "./roman";

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
  const entryRefs = useRef<Record<number, HTMLLIElement | null>>({});
  const [jumpedChapter, setJumpedChapter] = useState<number | null>(null);
  const [expandedChapter, setExpandedChapter] = useState<number | null>(currentChapter);
  const [query, setQuery] = useState("");

  useEffect(() => setExpandedChapter(currentChapter), [currentChapter]);

  const visibleChapters = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    if (!needle) return chapters;
    return chapters.filter((chapter) => [
      chapter.title,
      chapter.summary,
      ...chapter.new_characters.map((character) => character.name),
      ...chapter.events,
    ].some((value) => value.toLocaleLowerCase().includes(needle)));
  }, [chapters, query]);

  useEffect(() => {
    if (scrollTo == null) return;
    setQuery("");
    setExpandedChapter(scrollTo.chapter);
    entryRefs.current[scrollTo.chapter]?.scrollIntoView?.({ block: "center", behavior: "smooth" });
    setJumpedChapter(scrollTo.chapter);
    const timer = window.setTimeout(() => setJumpedChapter(null), 1400);
    return () => window.clearTimeout(timer);
  }, [scrollTo]);

  // render free text with character names as card-opening chips (the LIT-30 affordance, reused)
  const renderText = (text: string) =>
    wrapNames(text, cast).map((seg, j) =>
      seg.entityId != null ? (
        <button
          key={j}
          type="button"
          className="name-chip"
          data-entity-id={seg.entityId}
          onClick={(e) => onOpenCard(seg.entityId!, e.currentTarget.getBoundingClientRect())}
        >
          {seg.text}
        </button>
      ) : (
        <Fragment key={j}>{seg.text}</Fragment>
      ),
    );

  if (chapters.length === 0)
    return <p className="quiet chapter-breakdown-empty">The story has not begun yet.</p>;

  return (
    <>
      <label className="chapter-search">
        <span className="sr-only">Find a chapter</span>
        <input
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder={`Find a ${unit}…`}
        />
      </label>
      {visibleChapters.length === 0 ? (
        <p className="quiet chapter-search-empty" role="status">No {unit}s match “{query}”.</p>
      ) : <ol className="chapter-breakdown">
      {visibleChapters.map((ch) => {
        const expanded = expandedChapter === ch.revealed_at;
        return (
        <li
          key={ch.chapter_key}
          className={`chapter-entry${ch.revealed_at === currentChapter ? " is-current" : ""}${ch.revealed_at === jumpedChapter ? " is-jump-target" : ""}`}
          ref={(el) => {
            entryRefs.current[ch.revealed_at] = el;
          }}
        >
          <button
            className="chapter-head plain"
            aria-current={ch.revealed_at === currentChapter ? "true" : undefined}
            aria-expanded={expanded}
            onClick={() => {
              setExpandedChapter(ch.revealed_at);
              onSelectChapter(ch.revealed_at);
            }}
          >
            <span className="chapter-num smallcaps">{unit[0].toUpperCase() + unit.slice(1)} {roman(ch.revealed_at)}</span>
            {ch.title && <span className="chapter-title">{ch.title}</span>}
          </button>
          {expanded && ch.summary && <p className="chapter-summary">{renderText(ch.summary)}</p>}
          {expanded && ch.new_characters.length > 0 && (
            <p className="chapter-new">
              <span className="smallcaps">new here:</span>{" "}
              {ch.new_characters.map((nc, k) => (
                <Fragment key={nc.entity_id}>
                  {k > 0 ? ", " : ""}
                  <button
                    type="button"
                    className="name-chip"
                    onClick={(e) => onOpenCard(nc.entity_id, e.currentTarget.getBoundingClientRect())}
                  >
                    {nc.name}
                  </button>
                </Fragment>
              ))}
            </p>
          )}
          {expanded && ch.events.length > 0 && (
            <ul className="chapter-events">
              {ch.events.map((e, k) => (
                <li key={k}>{renderText(e)}</li>
              ))}
            </ul>
          )}
        </li>
        );
      })}
      </ol>}
    </>
  );
}
