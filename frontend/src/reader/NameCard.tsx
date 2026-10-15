/** LIT-30 the live name card: click a name-chip -> a bookmark-clamped popover with who they are
 * (identity) + their ties (relationships), each tie a chip that walks the graph. Renders ONLY the
 * server-clamped /character payload (the server 404s a future/unknown entity), never client-computed
 * or future data. A labelled dialog: focus moves in, Escape / click-away close, focus restores to the
 * chip. It layers over the hero; while open it owns the keyboard (the hero yields). */
import { useEffect, useRef, useState } from "react";
import { api, type Character } from "../api";
import { Button } from "../ui/Button";
import { useDialogMotion } from "../ui/useDialogMotion";
import { keepTabFocusInside } from "./focusTrap";
import { roman } from "./roman";

export function NameCard({
  bookId,
  entityId,
  bookmark,
  anchorRect,
  onClose,
  onNavigate,
}: {
  bookId: string;
  entityId: number;
  bookmark: number;
  anchorRect?: DOMRect | null;
  onClose: () => void;
  onNavigate: (entityId: number) => void;
}) {
  const { closing, requestClose } = useDialogMotion(onClose);
  const [snapshot, setSnapshot] = useState<{
    bookId: string;
    entityId: number;
    bookmark: number;
    data: Character;
  } | null>(null);
  const card =
    snapshot?.bookId === bookId &&
    snapshot.entityId === entityId &&
    snapshot.bookmark === bookmark
      ? snapshot.data
      : null;
  const [err, setErr] = useState<unknown>(null);
  const [retry, setRetry] = useState(0);
  const rootRef = useRef<HTMLDivElement>(null);
  const onCloseRef = useRef(requestClose);
  onCloseRef.current = requestClose;
  // capture the triggering chip SYNCHRONOUSLY at first render — before the fetch effect focuses the
  // card root — so focus restores to the chip on close, not to <body> (a11y review HIGH, WCAG 2.4.3).
  // Keyed once (survives tie-navigations), so it always points at the ORIGINAL chip that opened the card.
  const triggerRef = useRef<HTMLElement | null>(null);
  if (triggerRef.current === null)
    triggerRef.current = document.activeElement as HTMLElement | null;

  // fetch (and focus the card) on open AND on navigate to a tie; renders clamped data only
  useEffect(() => {
    let dead = false;
    setSnapshot(null);
    setErr(null);
    api
      .character(bookId, entityId, bookmark)
      .then(
        (data) => !dead && setSnapshot({ bookId, entityId, bookmark, data }),
      )
      .catch((error: unknown) => !dead && setErr(error));
    rootRef.current?.focus();
    return () => {
      dead = true;
    };
  }, [bookId, entityId, bookmark, retry]);

  // dialog keyboard/focus: Escape + Tab-trap + click-away, focus restored to the trigger chip on close
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        onCloseRef.current();
        return;
      }
      keepTabFocusInside(rootRef.current, e);
    };
    const onDown = (e: MouseEvent) => {
      const root = rootRef.current;
      if (root && !root.contains(e.target as Node)) onCloseRef.current();
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onDown);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onDown);
      const t = triggerRef.current;
      if (t && document.contains(t) && typeof t.focus === "function") t.focus();
    };
  }, []);

  // anchored to the clicked chip, CLAMPED so the card stays on-screen (a11y review LOW): pin the left
  // edge inside the viewport, and open UPWARD when the chip sits low (else the card would start below
  // the fold). No rect -> centered. Content overflow is handled by max-height + scroll (CSS).
  const style: React.CSSProperties = (() => {
    if (!anchorRect)
      return {
        position: "fixed",
        top: "18vh",
        left: "50%",
        transform: "translateX(-50%)",
      };
    const vw = window.innerWidth || 1024;
    const vh = window.innerHeight || 768;
    const m = 16;
    const cardW = Math.min(350, vw - m * 2);
    const left = Math.round(
      Math.max(m, Math.min(anchorRect.left, vw - cardW - m)),
    );
    if (anchorRect.bottom > vh * 0.6) {
      return {
        position: "fixed",
        left,
        bottom: Math.round(Math.max(m, vh - anchorRect.top + 6)),
      };
    }
    return { position: "fixed", left, top: Math.round(anchorRect.bottom + 6) };
  })();

  const label = card?.name ?? "character";
  const notFound =
    err != null &&
    typeof err === "object" &&
    "status" in err &&
    err.status === 404;
  return (
    <div
      className="name-card"
      role="dialog"
      aria-modal="true"
      aria-label={label}
      tabIndex={-1}
      ref={rootRef}
      style={style}
      data-closing={closing || undefined}
    >
      <Button
        variant="ghost"
        icon="close"
        className="name-card-close"
        onClick={requestClose}
      >
        Close
      </Button>
      <div
        className="sr-only"
        role="status"
        aria-live="polite"
        aria-atomic="true"
      >
        {card ? "Character details are ready." : ""}
      </div>
      {err ? (
        <div role="alert">
          <p className="quiet">
            {notFound
              ? "This character is not available in the chapters you have read."
              : "Character details could not be loaded. Try again."}
          </p>
          <Button icon="refresh" onClick={() => setRetry((value) => value + 1)}>
            Try character again
          </Button>
        </div>
      ) : card == null ? (
        <p className="quiet" role="status">
          Loading character details…
        </p>
      ) : (
        <>
          <h3 className="name-card-name">{card.name}</h3>
          <div className="name-card-meta">
            {card.type}
            {card.aliases.length > 0 && (
              <> · also known as {card.aliases.join(", ")}</>
            )}
          </div>
          <div className="name-card-meta">
            first seen in Chapter {roman(card.first_seen)}
          </div>
          {typeof card.status === "string" && card.status && (
            <div className="name-card-meta">{card.status}</div>
          )}
          {card.ties.length > 0 && (
            <ul className="name-card-ties">
              {card.ties.map((t) => (
                <li key={`${t.entity_id}:${t.rel_type}:${t.label}`}>
                  <span className="tie-label smallcaps">{t.label}</span>{" "}
                  <button
                    className="name-chip"
                    onClick={() => onNavigate(t.entity_id)}
                  >
                    {t.name}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  );
}
