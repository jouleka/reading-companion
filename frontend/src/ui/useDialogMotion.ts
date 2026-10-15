import { useCallback, useEffect, useRef, useState } from "react";

export const DIALOG_EXIT_MS = 160;

/** Keep the modal mounted, and its focus trap active, until its short exit transition finishes. */
export function useDialogMotion(onClose: () => void) {
  const [closing, setClosing] = useState(false);
  const closingRef = useRef(false);
  const timerRef = useRef<number | null>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  const requestClose = useCallback(() => {
    if (closingRef.current) return;
    closingRef.current = true;
    // If motion preferences cannot be queried, choose the immediate accessible fallback.
    const reduceMotion =
      window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? true;
    if (reduceMotion) {
      onCloseRef.current();
      return;
    }
    setClosing(true);
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null;
      onCloseRef.current();
    }, DIALOG_EXIT_MS);
  }, []);

  useEffect(
    () => () => {
      if (timerRef.current != null) window.clearTimeout(timerRef.current);
    },
    [],
  );

  return { closing, requestClose };
}
