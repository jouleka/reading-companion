import { type FormEvent, useEffect, useRef, useState } from "react";
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
  const [selectionTool, setSelectionTool] = useState<"highlight" | "note" | "understand" | null>(null);
  const [filter, setFilter] = useState<"all" | ReaderMark["kind"]>("all");
  const [query, setQuery] = useState("");
  const noteRef = useRef<HTMLTextAreaElement>(null);
  const busy = /^(saving|deleting)\b/.test(status);
  const visibleMarks = marks.filter((mark) =>
    (filter === "all" || mark.kind === filter) &&
    `${mark.selected_text ?? ""} ${mark.body ?? ""} ${mark.label ?? ""}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()),
  ).sort((a, b) => a.anchor.atom - b.anchor.atom);

  // A failed save keeps its passage and draft. Only a new passage or a cleared
  // selection starts a new draft; opening another tool does not discard it.
  useEffect(() => {
    setSelectionTool(null);
    setNote("");
  }, [selection?.anchor.cfi, selection?.text]);
  useEffect(() => {
    if (selectionTool === "note") noteRef.current?.focus();
  }, [selectionTool]);
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
        aria-label="Notebook"
        aria-expanded={open && !closing}
        aria-controls={id}
        onClick={toggle}
      >
        <Icon name="bookmark" />
        <span>Notebook</span>
      </button>
      {selection && (
        <section className="selection-actions" aria-label="Selection actions">
          <div className="selection-context">
            <q>{selection.text}</q>
            <Button
              variant="ghost"
              className="selection-dismiss icon-button"
              aria-label="Dismiss selected passage"
              disabled={busy}
              onClick={onClearSelection}
            ><Icon name="close" size={16} /></Button>
          </div>
          <div className="selection-tool-picker" role="group" aria-label="Work with selected text">
            <Button
              variant="ghost"
              aria-expanded={selectionTool === "highlight"}
              aria-controls={`${id}-highlight`}
              disabled={busy}
              onClick={() => setSelectionTool(selectionTool === "highlight" ? null : "highlight")}
            ><span className="selection-highlight-icon" aria-hidden="true">H</span>Highlight</Button>
            <Button
              variant="ghost"
              icon="bookmark"
              aria-expanded={selectionTool === "note"}
              aria-controls={`${id}-note-editor`}
              disabled={busy}
              onClick={() => setSelectionTool(selectionTool === "note" ? null : "note")}
            >Note</Button>
            <Button
              variant="ghost"
              icon="sparkle"
              aria-expanded={selectionTool === "understand"}
              aria-controls={`${id}-understand`}
              disabled={busy}
              onClick={() => setSelectionTool(selectionTool === "understand" ? null : "understand")}
            >Understand</Button>
          </div>
          {selectionTool === "understand" && <div
            id={`${id}-understand`}
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
          </div>}
          {selectionTool === "highlight" && <div
            id={`${id}-highlight`}
            className="selection-highlight-actions"
            aria-label="Highlight color"
          >
            <span className="selection-tool-hint">Save in</span>
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
          </div>}
          {selectionTool === "note" && <form id={`${id}-note-editor`} className="selection-note-editor" onSubmit={saveNote}>
            <label htmlFor={`${id}-note`}>Add a note</label>
            <textarea
              ref={noteRef}
              id={`${id}-note`}
              value={note}
              maxLength={10000}
              rows={3}
              placeholder="What would you like to remember?"
              onChange={(event) => setNote(event.currentTarget.value)}
            />
            <Button
              type="submit"
              variant="primary"
              disabled={!note.trim() || busy}
            >
              save note
            </Button>
          </form>}
          {!open && (
            <p className="marks-status" role="status" aria-live="polite">
              {status}
            </p>
          )}
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
            <div><span className="notebook-eyebrow">THE WORDS YOU KEPT</span><h2>Your notebook</h2></div>
            <Button
              variant="ghost"
              className="icon-button"
              aria-label="Close notebook"
              onClick={() => close()}
            >
              <Icon name="close" />
            </Button>
          </header>
          <label className="notebook-search">
            <Icon name="search" size={17} />
            <input type="search" aria-label="Search your notebook" placeholder="Find a thought or a passage…" value={query} onChange={(event) => setQuery(event.target.value)} />
          </label>
          <div className="notebook-filters" role="group" aria-label="Filter notebook">
            {([
              ["all", "All"], ["highlight", "Highlights"],
              ["annotation", "Notes"], ["bookmark", "Bookmarks"],
            ] as const).map(([value, title]) => (
              <button
                key={value}
                type="button"
                aria-pressed={filter === value}
                onClick={() => setFilter(value)}
              >{title}<span>{value === "all" ? marks.length : marks.filter((mark) => mark.kind === value).length}</span></button>
            ))}
          </div>
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
          {visibleMarks.length ? (
            <ol className="marks-list">
              {visibleMarks.map((mark, index) => (
                <li key={`${mark.kind}-${mark.id}`} data-mark-kind={mark.kind} data-mark-color={mark.kind === "highlight" ? mark.color : undefined}>
                  {(index === 0 || visibleMarks[index - 1].anchor.atom !== mark.anchor.atom) && <h3 className="notebook-chapter">Chapter {mark.anchor.atom}<span>{visibleMarks.filter(item => item.anchor.atom === mark.anchor.atom).length} saved</span></h3>}
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
              {query.trim() ? "No saved passages match your search." : marks.length ? "Nothing saved in this view yet." : "Your favorite words belong here. Highlight a passage, add a thought, or bookmark a page as you read."}
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
