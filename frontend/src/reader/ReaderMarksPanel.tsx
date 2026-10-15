import { type FormEvent, useEffect, useState } from "react";
import type {
  HighlightMark,
  ReaderMark,
  ReaderMarkAnchor,
  SelectionAction,
} from "../api";
import { Icon } from "../ui/Icon";
import { Button } from "../ui/Button";
import { useReaderPopover } from "../ui/useReaderPopover";

export type ReaderSelection = { anchor: ReaderMarkAnchor; text: string };

export function ReaderMarks({
  marks,
  selection,
  currentAnchor,
  exportUrl,
  status,
  onHighlight,
  onAssist,
  onAnnotate,
  onBookmark,
  onNavigate,
  onDelete,
  onClearSelection,
}: {
  marks: ReaderMark[];
  selection: ReaderSelection | null;
  currentAnchor: ReaderMarkAnchor | null;
  exportUrl: string;
  status: string;
  onHighlight: (color: HighlightMark["color"]) => void;
  onAssist: (action: SelectionAction) => void;
  onAnnotate: (body: string) => void;
  onBookmark: (label: string) => void;
  onNavigate: (mark: ReaderMark) => void;
  onDelete: (mark: ReaderMark) => void;
  onClearSelection: () => void;
}) {
  const { open, closing, id, triggerRef, panelRef, toggle, close } =
    useReaderPopover();
  const [note, setNote] = useState("");
  const [label, setLabel] = useState("");
  const busy = /^(saving|deleting)\b/.test(status);

  // A failed request leaves the selection and draft intact. The reader clears the selection
  // only after a note is saved, so dismissing it is also the natural end of this draft.
  useEffect(() => {
    if (!selection) setNote("");
  }, [selection]);
  useEffect(() => {
    if (status === "bookmark saved" || status === "bookmark queued for sync")
      setLabel("");
  }, [status]);
  const saveNote = (event: FormEvent) => {
    event.preventDefault();
    if (!note.trim()) return;
    onAnnotate(note.trim());
  };
  const saveBookmark = (event: FormEvent) => {
    event.preventDefault();
    if (!currentAnchor) return;
    onBookmark(label.trim());
  };

  return (
    <div className="reader-marks">
      <button
        ref={triggerRef}
        type="button"
        className="plain reader-tool marks-trigger"
        aria-label="notes"
        aria-expanded={open && !closing}
        aria-controls={id}
        onClick={toggle}
      >
        <Icon name="bookmark" />
        <span>Notes</span>
      </button>
      {selection && (
        <section className="selection-actions" aria-label="Selection actions">
          <q>{selection.text}</q>
          <div
            className="selection-ai-actions"
            aria-label="Understand this passage"
          >
            <Button
              icon="sparkle"
              disabled={busy}
              onClick={() => onAssist("explain")}
            >
              explain
            </Button>
            <Button disabled={busy} onClick={() => onAssist("define")}>
              define
            </Button>
            <Button disabled={busy} onClick={() => onAssist("translate")}>
              translate to English
            </Button>
          </div>
          <div
            className="selection-highlight-actions"
            aria-label="Highlight color"
          >
            {(["yellow", "green", "blue", "pink"] as const).map((color) => (
              <button
                key={color}
                type="button"
                disabled={busy}
                onClick={() => onHighlight(color)}
              >
                <span
                  className={`highlight-swatch ${color}`}
                  aria-hidden="true"
                />
                <span className="sr-only">{color} highlight</span>
              </button>
            ))}
          </div>
          <form onSubmit={saveNote}>
            <label htmlFor={`${id}-note`}>Add a note</label>
            <textarea
              id={`${id}-note`}
              value={note}
              maxLength={10000}
              onChange={(event) => setNote(event.currentTarget.value)}
            />
            <Button
              type="submit"
              variant="primary"
              disabled={!note.trim() || busy}
            >
              save note
            </Button>
          </form>
          {!open && (
            <p className="marks-status" role="status" aria-live="polite">
              {status}
            </p>
          )}
          <Button
            variant="ghost"
            className="selection-dismiss"
            disabled={busy}
            onClick={onClearSelection}
          >
            <Icon name="close" size={16} /> dismiss
          </Button>
        </section>
      )}
      {open && (
        <section
          id={id}
          className="reader-popover marks-panel"
          data-closing={closing || undefined}
          aria-hidden={closing || undefined}
          aria-label="Highlights, notes, and bookmarks"
          tabIndex={-1}
          ref={panelRef}
        >
          <header className="reader-panel-heading">
            <h2>Your marks</h2>
            <Button
              variant="ghost"
              className="icon-button"
              aria-label="Close notes"
              onClick={() => close()}
            >
              <Icon name="close" />
            </Button>
          </header>
          <form className="bookmark-form" onSubmit={saveBookmark}>
            <label htmlFor={`${id}-label`}>Bookmark this page</label>
            <div>
              <input
                id={`${id}-label`}
                value={label}
                maxLength={500}
                placeholder="Optional label"
                onChange={(event) => setLabel(event.currentTarget.value)}
              />
              <Button
                type="submit"
                variant="primary"
                disabled={!currentAnchor || busy}
              >
                bookmark
              </Button>
            </div>
          </form>
          <p className="marks-status" role="status" aria-live="polite">
            {status}
          </p>
          {marks.length ? (
            <ol className="marks-list">
              {marks.map((mark) => (
                <li key={`${mark.kind}-${mark.id}`}>
                  <button
                    type="button"
                    className="plain mark-location"
                    onClick={() => {
                      onNavigate(mark);
                      close();
                    }}
                  >
                    <span className="smallcaps">
                      {mark.kind} · chapter {mark.anchor.atom}
                    </span>
                    {mark.pending && (
                      <span className="smallcaps">queued for sync</span>
                    )}
                    <span>
                      {mark.kind === "highlight"
                        ? mark.selected_text
                        : mark.kind === "annotation"
                          ? mark.body
                          : mark.label || "Bookmarked page"}
                    </span>
                  </button>
                  <Button
                    variant="ghost"
                    className="mark-delete"
                    aria-label={`Delete ${mark.kind}`}
                    disabled={busy}
                    onClick={() => onDelete(mark)}
                  >
                    delete
                  </Button>
                </li>
              ))}
            </ol>
          ) : (
            <p className="quiet">
              No marks in the pages available to this reading pass.
            </p>
          )}
          <a
            className="plain marks-export"
            href={exportUrl}
            download="litlet-reader-marks.json"
          >
            <Icon name="download" size={16} /> export marks
          </a>
        </section>
      )}
      {!open && !selection && status && (
        <p className="reader-feedback" role="status" aria-live="polite">
          {status}
        </p>
      )}
    </div>
  );
}
