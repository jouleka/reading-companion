/** The jump-confirmation strip (LIT-13 spoiler guard, LIT-16 a11y): an alert dialog laid over the
 * page foot when an in-book link would leap past the next chapter. It interrupts with a
 * spoiler-relevant choice, so it behaves like a real dialog: focus moves onto it (the safe choice),
 * Tab is trapped within its controls (the reader behind it must not be operable mid-decision), Escape
 * dismisses to the safe default, and focus returns to the trigger when it closes (WCAG 2.4.3). */
import { useEffect, useRef } from "react";

export function JumpNote({
  chapter,
  onFollow,
  onStay,
}: {
  chapter: number;
  onFollow: () => void;
  onStay: () => void;
}) {
  const rootRef = useRef<HTMLDivElement>(null);
  const stayRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const trigger = document.activeElement as HTMLElement | null; // whatever opened the dialog
    stayRef.current?.focus(); // land the keyboard on the safe choice, not the destructive one

    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onStay();
        return;
      }
      if (e.key === "Tab") {
        // trap: cycle Tab / Shift+Tab between this dialog's own controls, never out to the reader
        const buttons = Array.from(rootRef.current?.querySelectorAll("button") ?? []);
        if (buttons.length === 0) return;
        const first = buttons[0] as HTMLElement;
        const last = buttons[buttons.length - 1] as HTMLElement;
        const active = document.activeElement;
        e.preventDefault();
        if (e.shiftKey) (active === first ? last : first).focus();
        else (active === last ? first : last).focus();
      }
    };
    document.addEventListener("keydown", onKey);

    return () => {
      document.removeEventListener("keydown", onKey);
      // return focus where it came from, so the reader does not lose its place (2.4.3); fall back to
      // the book if the trigger is gone
      if (trigger && document.contains(trigger) && typeof trigger.focus === "function") trigger.focus();
      else document.getElementById("main")?.focus();
    };
  }, [onStay]);

  return (
    <div className="jump-note" role="alertdialog" aria-labelledby="jump-note-msg" ref={rootRef}>
      <span id="jump-note-msg">
        That link leaps ahead to chapter {chapter} — following it marks everything before it as read,
        and the companion does not forget.
      </span>
      <button className="plain smallcaps" onClick={onFollow}>
        follow
      </button>
      <button className="plain smallcaps" ref={stayRef} onClick={onStay}>
        stay
      </button>
    </div>
  );
}
