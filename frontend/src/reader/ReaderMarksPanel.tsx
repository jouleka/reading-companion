import { type FormEvent, useId, useRef, useState } from "react";
import type { HighlightMark, ReaderMark, ReaderMarkAnchor, SelectionAction } from "../api";

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
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState("");
  const [label, setLabel] = useState("");
  const id = useId();
  const opener = useRef<HTMLButtonElement>(null);
  const close = () => {
    setOpen(false);
    opener.current?.focus();
  };
  const saveNote = (event: FormEvent) => {
    event.preventDefault();
    if (!note.trim()) return;
    onAnnotate(note.trim());
    setNote("");
  };
  const saveBookmark = (event: FormEvent) => {
    event.preventDefault();
    if (!currentAnchor) return;
    onBookmark(label.trim());
    setLabel("");
  };

  return (
    <div className="reader-marks">
      <button
        ref={opener}
        type="button"
        className="plain smallcaps marks-trigger"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen((value) => !value)}
      >
        notes
      </button>
      {selection && (
        <section className="selection-actions" aria-label="Selection actions">
          <q>{selection.text}</q>
          <div className="selection-ai-actions" aria-label="Understand this passage">
            <button type="button" onClick={() => onAssist("explain")}>explain</button>
            <button type="button" onClick={() => onAssist("define")}>define</button>
            <button type="button" onClick={() => onAssist("translate")}>translate to English</button>
          </div>
          <div className="selection-highlight-actions" aria-label="Highlight color">
            {(["yellow", "green", "blue", "pink"] as const).map((color) => (
              <button key={color} type="button" onClick={() => onHighlight(color)}>
                <span className={`highlight-swatch ${color}`} aria-hidden="true" />
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
            <button type="submit" disabled={!note.trim()}>save note</button>
          </form>
          <button type="button" className="plain" onClick={onClearSelection}>dismiss</button>
        </section>
      )}
      {open && (
        <section
          id={id}
          className="marks-panel"
          aria-label="Highlights, notes, and bookmarks"
          onKeyDown={(event) => { if (event.key === "Escape") close(); }}
        >
          <header>
            <h2>Your marks</h2>
            <button type="button" className="plain" onClick={close}>close</button>
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
              <button type="submit" disabled={!currentAnchor}>bookmark</button>
            </div>
          </form>
          <p className="marks-status" role="status" aria-live="polite">{status}</p>
          {marks.length ? (
            <ol className="marks-list">
              {marks.map((mark) => (
                <li key={`${mark.kind}-${mark.id}`}>
                  <button type="button" className="plain mark-location" onClick={() => onNavigate(mark)}>
                    <span className="smallcaps">{mark.kind} · chapter {mark.anchor.atom}</span>
                    {mark.pending && <span className="smallcaps">queued for sync</span>}
                    <span>{mark.kind === "highlight"
                      ? mark.selected_text
                      : mark.kind === "annotation" ? mark.body : mark.label || "Bookmarked page"}</span>
                  </button>
                  <button
                    type="button"
                    className="plain mark-delete"
                    aria-label={`Delete ${mark.kind}`}
                    onClick={() => onDelete(mark)}
                  >
                    delete
                  </button>
                </li>
              ))}
            </ol>
          ) : <p className="quiet">No marks in the pages available to this reading pass.</p>}
          <a className="plain marks-export" href={exportUrl} download="litlet-reader-marks.json">
            export marks
          </a>
        </section>
      )}
    </div>
  );
}
