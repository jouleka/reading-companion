import { useEffect, useRef } from "react";

/** Destructive-reading-state confirmation. Memory is retained; only the visible frontier rewinds. */
export function StartOverNote({
  busy,
  error,
  onConfirm,
  onCancel,
}: {
  busy: boolean;
  error: string | null;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const rootRef = useRef<HTMLDivElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const trigger = document.activeElement as HTMLElement | null;
    if (busy) rootRef.current?.focus();
    else cancelRef.current?.focus();

    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) {
        event.preventDefault();
        onCancel();
        return;
      }
      if (event.key !== "Tab") return;
      const buttons = Array.from(
        rootRef.current?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)") ?? [],
      );
      if (buttons.length === 0) {
        event.preventDefault();
        rootRef.current?.focus();
        return;
      }
      event.preventDefault();
      const active = document.activeElement;
      const first = buttons[0];
      const last = buttons[buttons.length - 1];
      if (event.shiftKey) (active === first ? last : first).focus();
      else (active === last ? first : last).focus();
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      if (trigger && document.contains(trigger)) trigger.focus();
      else document.getElementById("main")?.focus();
    };
  }, [busy, onCancel]);

  return (
    <div
      className="start-over-note"
      role="alertdialog"
      aria-labelledby="start-over-title"
      aria-describedby="start-over-description"
      aria-busy={busy}
      tabIndex={-1}
      ref={rootRef}
    >
      <strong id="start-over-title">Start a new reading pass?</strong>
      <span id="start-over-description">
        The companion will return to nothing read and reveal your existing memory again as you
        progress. The book, extracted notes, receipts, and costs stay intact.
      </span>
      {error && <span role="alert">{error}</span>}
      <div className="start-over-actions">
        <button className="plain smallcaps" disabled={busy} onClick={onConfirm}>
          {busy ? "starting…" : "start again"}
        </button>
        <button className="plain smallcaps" ref={cancelRef} disabled={busy} onClick={onCancel}>
          keep my place
        </button>
      </div>
    </div>
  );
}
