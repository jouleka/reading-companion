/** The companion leaf: the spoiler-safe memory, rendered from the CLAMPED server routes only. Shows
 * the catch-me-up recap (drop cap, printed-page voice), the cast/threads counts, ingest state, and
 * the ribbon bookmark whose length is reading progress. */
import { useEffect, useId, useRef, useState } from "react";
import { api, type BookType, type CatchMeUp, type IngestStatus } from "../api";
import { presentationFor } from "./bookProfile";
import { roman } from "./roman";
import { recapFailureMessage } from "./recapFailure";
import { Icon, type IconName } from "../ui/Icon";
import { Button } from "../ui/Button";
import { AiStatus } from "../ui/AiStatus";
import { keepTabFocusInside } from "./focusTrap";

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
  desktopHidden = false,
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
  desktopHidden?: boolean;
}) {
  const [recapSnapshot, setRecapSnapshot] = useState<{
    bookId: string;
    data: CatchMeUp;
  } | null>(null);
  const cmu =
    recapSnapshot?.bookId === bookId &&
    bookmark != null &&
    recapSnapshot.data.as_of_chapter <= bookmark
      ? recapSnapshot.data
      : null;
  const [ingest, setIngest] = useState<IngestStatus | null>(null);
  const [recapErr, setRecapErr] = useState<unknown>(null);
  const [refreshState, setRefreshState] = useState<
    "polling" | "ready" | "updating" | "unavailable"
  >("polling");
  const [retry, setRetry] = useState(0);
  const [expanded, setExpanded] = useState(false);
  const [sheetVisible, setSheetVisible] = useState(false);
  const closeTimer = useRef<ReturnType<typeof setTimeout>>();
  const bodyId = useId();
  const toggleRef = useRef<HTMLButtonElement>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    if (compact && expanded) headingRef.current?.focus();
  }, [compact, expanded]);

  useEffect(() => () => clearTimeout(closeTimer.current), []);
  useEffect(() => {
    if (!compact || !expanded) return;
    const main = document.getElementById("main");
    if (!main) return;
    const wasInert = main.inert;
    main.inert = true;
    return () => {
      main.inert = wasInert;
    };
  }, [compact, expanded]);

  const closeSheet = () => {
    setExpanded(false);
    toggleRef.current?.focus();
    clearTimeout(closeTimer.current);
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches)
      setSheetVisible(false);
    else closeTimer.current = setTimeout(() => setSheetVisible(false), 180);
  };
  const openSheet = () => {
    clearTimeout(closeTimer.current);
    setSheetVisible(true);
    setExpanded(true);
  };
  const launch = (action: () => void) => {
    if (compact) closeSheet();
    action();
  };

  // After the bookmark advances the worker ingests; poll /ingest a few times, then ask for the recap
  // (the server serves min(bookmark, ingest_progress) — always safe, sometimes behind). The whole
  // chain is owned by ITS effect run: `cancelled` is checked after every await so a tick resolving
  // after unmount / a bookmark change can neither set stale state nor spawn a rival timer chain.
  useEffect(() => {
    if (bookmark == null) return;
    setRefreshState("polling");
    setRecapErr(null);
    let cancelled = false;
    let timer: number | undefined;
    let attempts = 0;
    const tick = async () => {
      try {
        const st = await api.ingest(bookId);
        if (cancelled) return;
        setIngest(st);
        if (
          st.ingest_progress >= bookmark ||
          st.status === "blocked" ||
          st.status === "error" ||
          attempts >= 8
        ) {
          try {
            const c = await api.catchMeUp(bookId);
            if (cancelled) return;
            setRecapSnapshot({ bookId, data: c });
            setRecapErr(null);
          } catch (error: unknown) {
            if (!cancelled) setRecapErr(error);
          }
          if (!cancelled)
            setRefreshState(
              st.status === "blocked" || st.status === "error"
                ? "unavailable"
                : st.ingest_progress < bookmark
                  ? "updating"
                  : "ready",
            );
          return;
        }
        attempts += 1;
        timer = window.setTimeout(tick, 1200);
      } catch {
        // Retry transient failures, then offer an explicit recovery action at the same bookmark.
        if (!cancelled && attempts < 8) {
          attempts += 1;
          timer = window.setTimeout(tick, 1200);
        } else if (!cancelled) {
          setRefreshState("unavailable");
        }
      }
    };
    tick();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [bookId, bookmark, retry]);

  const progress =
    totalAtoms > 0 && bookmark != null ? Math.min(bookmark / totalAtoms, 1) : 0;
  // the one-liner covers min(bookmark, ingest_progress): when it lags, attribute it to ITS chapter —
  // presenting a chapter-2 situation under "as of chapter IX" would be the companion lying
  const behind =
    cmu != null && bookmark != null && cmu.as_of_chapter < bookmark;
  const presentation = presentationFor(bookType);
  const showPeopleStats =
    cmu != null &&
    (presentation.peopleMode === "primary" ||
      cmu.cast_size > 0 ||
      cmu.open_threads > 0);
  const closeoutReady =
    bookmark != null &&
    bookmark > 0 &&
    ingest != null &&
    ingest.ingest_progress >= bookmark;

  return (
    <aside
      className="companion"
      hidden={!compact && desktopHidden}
      aria-label="the reading companion"
      data-compact={compact || undefined}
      data-expanded={compact ? expanded : true}
      onKeyDown={(event) => {
        if (compact && expanded && event.key === "Escape") closeSheet();
        if (compact && expanded)
          keepTabFocusInside(event.currentTarget, event.nativeEvent);
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
          onClick={() => (expanded ? closeSheet() : openSheet())}
        >
          <span>
            <Icon name="sparkle" size={17} />
            The companion
          </span>
          <span aria-hidden="true">
            {expanded ? "Back to reading" : "Your story, so far"}
            <Icon name={expanded ? "chevronDown" : "chevronUp"} size={15} />
          </span>
        </button>
      )}
      {compact && (
        <div
          className="companion-sheet-scrim"
          aria-hidden="true"
          onClick={closeSheet}
        />
      )}
      <div
        id={bodyId}
        className="companion-body"
        hidden={compact && !sheetVisible}
      >
        <div className="companion-label">
          <Icon name="sparkle" size={17} />
          YOUR COMPANION
        </div>
        <h2 ref={headingRef} tabIndex={compact ? -1 : undefined}>
          Where you stand
        </h2>
        <div className="asof">
          {bookmark == null || bookmark === 0
            ? "nothing read yet — the memory begins with your first finished chapter"
            : `as of ${presentation.unit} ${roman(bookmark)} · ${bookmark} of ${totalAtoms}`}
        </div>

        <div className="companion-progress" aria-hidden="true">
          <span style={{ width: `${progress * 100}%` }} />
        </div>
        <div className="companion-progress-label">
          <span>
            {presentation.unit === "chapter" ? "Chapters" : "Sections"}{" "}
            remembered
          </span>
          <span>
            {bookmark ?? 0} / {totalAtoms}
          </span>
        </div>
        <div className="companion-safety">
          <Icon name="shield" size={13} />
          Only what you’ve read
        </div>
        <AiStatus />

        {showPeopleStats && cmu && (
          <div className="companion-stats">
            <div className="stat-line">
              <span className="smallcaps">{presentation.peopleLabel}</span>
              <span className="n">{cmu.cast_size}</span>
            </div>
            <div className="stat-line">
              <span className="smallcaps">{presentation.connectionsLabel}</span>
              <span className="n">{cmu.open_threads}</span>
            </div>
          </div>
        )}

        {/* a persistent polite live region: the 'right now' one-liner (or a placeholder / rejection
          note) is announced when it lands, not silently swapped in. The full flowing recap lives in
          the hero — the sidebar is a tight orientation, never a duplicate of it (LIT-29). */}
        <hr className="companion-rule" />
        <div className="recap-region" aria-live="polite">
          {cmu?.now ? (
            <p className="now-line">
              {behind && (
                <span className="smallcaps recap-behind">
                  through {presentation.unit} {roman(cmu.as_of_chapter)} ·{" "}
                </span>
              )}
              {cmu.now}
            </p>
          ) : cmu && cmu.as_of_chapter === 0 ? (
            <p className="quiet">
              Finish a chapter and your bearings will gather here.
            </p>
          ) : cmu && cmu.recap ? (
            <p className="quiet">Open the story so far for the full recap.</p>
          ) : recapErr != null ? null : refreshState === "unavailable" ? (
            <p className="quiet">
              Your reading memory is temporarily unavailable.
            </p>
          ) : refreshState === "updating" ? (
            <p className="quiet">Your reading memory is still being updated.</p>
          ) : bookmark ? (
            <p className="quiet">Reading your chapters…</p>
          ) : (
            <div className="companion-empty">
              <Icon name="book" size={24} />
              <p>
                Settle into your book. As you finish chapters, the people,
                connections, and story so far will find a home here.
              </p>
            </div>
          )}
          {recapErr != null && (
            <p className="quiet">{recapFailureMessage(recapErr, "hero")}</p>
          )}
        </div>

        {((cmu?.recap && onOpenHero) ||
          (bookmark != null &&
            bookmark > 0 &&
            (onOpenCodex ||
              onOpenAsk ||
              (onOpenCloseout && closeoutReady)))) && (
          <nav className="companion-links" aria-label="the full companion">
            {cmu?.recap && onOpenHero && (
              <CompanionAction
                icon="book"
                title={presentation.recapLink}
                detail="Find your place in the story"
                onClick={() => launch(onOpenHero)}
              />
            )}
            {bookmark != null && bookmark > 0 && onOpenCodex && (
              <CompanionAction
                icon="library"
                title={presentation.codexLink}
                detail="Chapters, people & connections"
                onClick={() => launch(onOpenCodex)}
              />
            )}
            {bookmark != null && bookmark > 0 && onOpenAsk && (
              <CompanionAction
                icon="sparkle"
                title="Ask the book"
                detail="Answers from your completed pages"
                onClick={() => launch(onOpenAsk)}
              />
            )}
            {closeoutReady && onOpenCloseout && (
              <CompanionAction
                icon="check"
                title={`Close out chapter ${roman(bookmark)}`}
                detail="The moments worth remembering"
                onClick={() => launch(onOpenCloseout)}
              />
            )}
          </nav>
        )}

        <div className="ingest-note" role="status">
          {refreshState === "unavailable" ? (
            <span>
              Companion updates are temporarily unavailable. You can keep
              reading or retry.
            </span>
          ) : refreshState === "updating" ? (
            <span>
              The companion is still updating. You can keep reading or check
              again.
            </span>
          ) : refreshState === "polling" &&
            ingest &&
            bookmark != null &&
            ingest.ingest_progress < bookmark ? (
            <span className="working">
              reading along… chapter {ingest.ingest_progress} of {bookmark}{" "}
              remembered
            </span>
          ) : null}
        </div>
        {(retry > 0 ||
          refreshState === "unavailable" ||
          refreshState === "updating" ||
          recapErr != null) && (
          <Button
            variant="ghost"
            icon="refresh"
            className="companion-retry"
            aria-disabled={refreshState === "polling"}
            onClick={() => {
              if (refreshState !== "polling") setRetry((value) => value + 1);
            }}
          >
            {refreshState === "polling"
              ? "Updating companion…"
              : refreshState === "ready" && recapErr == null
                ? "Refresh companion"
                : "Retry companion"}
          </Button>
        )}
      </div>
    </aside>
  );
}

function CompanionAction({
  icon,
  title,
  detail,
  onClick,
}: {
  icon: IconName;
  title: string;
  detail: string;
  onClick: () => void;
}) {
  return (
    <button className="companion-action" onClick={onClick}>
      <Icon name={icon} size={18} />
      <span>
        <strong>{title}</strong>
        <small>{detail}</small>
      </span>
      <Icon name="arrowRight" size={15} />
    </button>
  );
}
