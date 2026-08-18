/** The companion leaf: the spoiler-safe memory, rendered from the CLAMPED server routes only. Shows
 * the catch-me-up recap (drop cap, printed-page voice), the cast/threads counts, ingest state, and
 * the ribbon bookmark whose length is reading progress. */
import { useEffect, useId, useRef, useState } from "react";
import { api, type BookType, type CatchMeUp, type IngestStatus } from "../api";
import { presentationFor } from "./bookProfile";
import { roman } from "./roman";
import { recapFailureMessage } from "./recapFailure";

export function Companion({
  bookId,
  bookmark,
  totalAtoms,
  bookType = "novel",
  onOpenHero,
  onOpenCodex,
  onOpenAsk,
  onOpenCloseout,
  compact = false,
}: {
  bookId: string;
  bookmark: number | null;
  totalAtoms: number;
  bookType?: BookType;
  onOpenHero?: () => void;
  onOpenCodex?: () => void;
  onOpenAsk?: () => void;
  onOpenCloseout?: () => void;
  compact?: boolean;
}) {
  const [cmu, setCmu] = useState<CatchMeUp | null>(null);
  const [ingest, setIngest] = useState<IngestStatus | null>(null);
  const [recapErr, setRecapErr] = useState<unknown>(null);
  const [expanded, setExpanded] = useState(false);
  const bodyId = useId();
  const toggleRef = useRef<HTMLButtonElement>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    if (compact && expanded) headingRef.current?.focus();
  }, [compact, expanded]);

  const closeSheet = () => {
    setExpanded(false);
    toggleRef.current?.focus();
  };

  // After the bookmark advances the worker ingests; poll /ingest a few times, then ask for the recap
  // (the server serves min(bookmark, ingest_progress) — always safe, sometimes behind). The whole
  // chain is owned by ITS effect run: `cancelled` is checked after every await so a tick resolving
  // after unmount / a bookmark change can neither set stale state nor spawn a rival timer chain.
  useEffect(() => {
    if (bookmark == null) return;
    let cancelled = false;
    let timer: number | undefined;
    let attempts = 0;
    const tick = async () => {
      try {
        const st = await api.ingest(bookId);
        if (cancelled) return;
        setIngest(st);
        if (st.ingest_progress >= bookmark || st.status === "blocked" || st.status === "error" || attempts >= 8) {
          try {
            const c = await api.catchMeUp(bookId);
            if (cancelled) return;
            setCmu(c);
            setRecapErr(null);
          } catch (error: unknown) {
            if (!cancelled) setRecapErr(error);
          }
          return;
        }
        attempts += 1;
        timer = window.setTimeout(tick, 1200);
      } catch {
        // genuinely transient only if we RETRY: dropping the timer here parked the companion on
        // "Reading your chapters…" until the next bookmark change (pass-2 F3). Bounded by the same
        // attempts cap so a persistently-down server does not poll forever.
        if (!cancelled && attempts < 8) {
          attempts += 1;
          timer = window.setTimeout(tick, 1200);
        }
      }
    };
    tick();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [bookId, bookmark]);

  const progress = totalAtoms > 0 && bookmark != null ? Math.min(bookmark / totalAtoms, 1) : 0;
  // the one-liner covers min(bookmark, ingest_progress): when it lags, attribute it to ITS chapter —
  // presenting a chapter-2 situation under "as of chapter IX" would be the companion lying
  const behind = cmu != null && bookmark != null && cmu.as_of_chapter < bookmark;
  const presentation = presentationFor(bookType);
  const showPeopleStats = cmu != null && (
    presentation.peopleMode === "primary" || cmu.cast_size > 0 || cmu.open_threads > 0
  );
  const closeoutReady = bookmark != null && bookmark > 0
    && ingest != null && ingest.ingest_progress >= bookmark;

  return (
    <aside
      className="companion"
      aria-label="the reading companion"
      data-compact={compact || undefined}
      data-expanded={compact ? expanded : true}
      onKeyDown={(event) => {
        if (compact && expanded && event.key === "Escape") closeSheet();
      }}
    >
      {compact && (
        <button
          ref={toggleRef}
          type="button"
          className="mobile-companion-toggle"
          aria-controls={bodyId}
          aria-expanded={expanded}
          aria-label={`${expanded ? "Close" : "Open"} reading companion`}
          onClick={() => expanded ? closeSheet() : setExpanded(true)}
        >
          <span className="smallcaps">the companion</span>
          <span aria-hidden="true">{expanded ? "close ↓" : "open ↑"}</span>
        </button>
      )}
      <div id={bodyId} className="companion-body" hidden={compact && !expanded}>
        <div
          className="ribbon"
          style={{ height: `${Math.max(progress * 100, 3)}%` }}
          aria-hidden="true"
        />
        <div className="smallcaps" style={{ color: "var(--ink-soft)", fontSize: 13 }}>
          the companion
        </div>
        <h2 ref={headingRef} tabIndex={compact ? -1 : undefined}>Where you stand</h2>
        <div className="asof">
          {bookmark == null || bookmark === 0
            ? "nothing read yet — the memory begins with your first finished chapter"
            : `as of ${presentation.unit} ${roman(bookmark)} · ${bookmark} of ${totalAtoms}`}
        </div>

        {showPeopleStats && cmu && (
          <>
            <div className="stat-line">
              <span className="smallcaps">{presentation.peopleLabel}</span>
              <span className="n">{cmu.cast_size}</span>
            </div>
            <div className="stat-line">
              <span className="smallcaps">{presentation.connectionsLabel}</span>
              <span className="n">{cmu.open_threads}</span>
            </div>
            <hr className="companion-rule" />
          </>
        )}

        {/* a persistent polite live region: the 'right now' one-liner (or a placeholder / rejection
          note) is announced when it lands, not silently swapped in. The full flowing recap lives in
          the hero — the sidebar is a tight orientation, never a duplicate of it (LIT-29). */}
        <div className="recap-region" aria-live="polite">
          {cmu?.now ? (
            <p className="now-line">
              {behind && (
                <span className="smallcaps recap-behind">through {presentation.unit} {roman(cmu.as_of_chapter)} · </span>
              )}
              {cmu.now}
            </p>
          ) : cmu && cmu.as_of_chapter === 0 ? (
            <p className="quiet">Finish a chapter and your bearings will gather here.</p>
          ) : cmu && cmu.recap ? (
            <p className="quiet">Open the story so far for the full recap.</p>
          ) : recapErr ? (
            <p className="quiet">{recapFailureMessage(recapErr, "sidebar")}</p>
          ) : bookmark ? (
            <p className="quiet">Reading your chapters…</p>
          ) : null}
        </div>

        {((cmu?.recap && onOpenHero) || (bookmark != null && bookmark > 0
          && (onOpenCodex || onOpenAsk || (onOpenCloseout && closeoutReady)))) && (
          <nav className="companion-links" aria-label="the full companion">
            {cmu?.recap && onOpenHero && (
              <button className="plain smallcaps hero-open" onClick={onOpenHero}>
                {presentation.recapLink} ⟶
              </button>
            )}
            {bookmark != null && bookmark > 0 && onOpenCodex && (
              <button className="plain smallcaps hero-open" onClick={onOpenCodex}>
                {presentation.codexLink} ⟶
              </button>
            )}
            {bookmark != null && bookmark > 0 && onOpenAsk && (
              <button className="plain smallcaps hero-open" onClick={onOpenAsk}>
                ask the book ⟶
              </button>
            )}
            {closeoutReady && onOpenCloseout && (
              <button className="plain smallcaps hero-open" onClick={onOpenCloseout}>
                close out chapter {roman(bookmark)} ⟶
              </button>
            )}
          </nav>
        )}

        <div className="ingest-note" role="status">
          {ingest?.status === "blocked" && <span>⚠ ingestion blocked: {ingest.flags[0] ?? "gate"}</span>}
          {ingest?.status === "error" && <span>⚠ ingestion error — will resume on your next page</span>}
          {ingest && bookmark != null && ingest.ingest_progress < bookmark && ingest.status !== "blocked" && (
            <span className="working">reading along… chapter {ingest.ingest_progress} of {bookmark} remembered</span>
          )}
        </div>
      </div>
    </aside>
  );
}
