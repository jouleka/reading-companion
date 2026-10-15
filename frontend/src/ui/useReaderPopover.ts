import { useCallback, useEffect, useId, useRef, useState } from "react";

const OPEN_EVENT = "litlet:reader-popover-open";
export const POPOVER_EXIT_MS = 140;

/** Shared behavior for the reader's nonmodal toolbar panels. */
export function useReaderPopover() {
  const [open, setOpen] = useState(false);
  const [closing, setClosing] = useState(false);
  const openRef = useRef(false);
  const closingRef = useRef(false);
  const timerRef = useRef<number | null>(null);
  const id = useId();
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLElement>(null);

  const close = useCallback((restoreFocus = true, immediately = false) => {
    if (!openRef.current) return;
    if (closingRef.current && !immediately) return;
    if (timerRef.current != null) window.clearTimeout(timerRef.current);
    timerRef.current = null;
    if (restoreFocus) triggerRef.current?.focus();
    else if (panelRef.current?.contains(document.activeElement)) {
      (document.activeElement as HTMLElement | null)?.blur();
    }
    const reduceMotion =
      window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? true;
    if (immediately || reduceMotion) {
      openRef.current = false;
      closingRef.current = false;
      setOpen(false);
      setClosing(false);
      return;
    }
    closingRef.current = true;
    // Hide the departing panel from interaction immediately while the visible surface fades away.
    if (panelRef.current) panelRef.current.inert = true;
    setClosing(true);
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null;
      openRef.current = false;
      closingRef.current = false;
      setOpen(false);
      setClosing(false);
    }, POPOVER_EXIT_MS);
  }, []);

  const toggle = () => {
    if (openRef.current && !closingRef.current) {
      close();
      return;
    }
    if (timerRef.current != null) window.clearTimeout(timerRef.current);
    timerRef.current = null;
    // Close the previous panel without sending focus back to its opener.
    document.dispatchEvent(new CustomEvent(OPEN_EVENT, { detail: id }));
    openRef.current = true;
    closingRef.current = false;
    if (panelRef.current) panelRef.current.inert = false;
    setClosing(false);
    setOpen(true);
  };

  useEffect(
    () => () => {
      if (timerRef.current != null) window.clearTimeout(timerRef.current);
    },
    [],
  );

  useEffect(() => {
    if (!open) return;
    if (!closing) panelRef.current?.focus();

    const onOpen = (event: Event) => {
      if ((event as CustomEvent<string>).detail !== id) close(false, true);
    };
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (
        panelRef.current?.contains(target) ||
        triggerRef.current?.contains(target)
      )
        return;
      // Leave focus on the newly clicked control instead of pulling it back to this toolbar.
      close(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (closingRef.current) return;
      if (
        event.key.startsWith("Arrow") &&
        event.target instanceof Node &&
        panelRef.current?.contains(event.target)
      ) {
        // Native controls keep their own arrow behavior; page-turn shortcuts behind them yield.
        event.stopPropagation();
      }
      if (event.key !== "Escape" || event.defaultPrevented) return;
      // A full-screen companion dialog can temporarily make this panel inert.
      if (panelRef.current?.closest("[inert]")) return;
      event.preventDefault();
      close();
    };
    document.addEventListener(OPEN_EVENT, onOpen);
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener(OPEN_EVENT, onOpen);
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open, closing, id, close]);

  return { open, closing, id, triggerRef, panelRef, toggle, close };
}
