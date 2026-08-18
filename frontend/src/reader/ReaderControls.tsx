import { useId, useRef, useState } from "react";
import type { PreferenceSaveStatus, ReaderPreferences } from "./readerPreferences";

type Choice = { value: string; label: string };
type PreferenceKey = Exclude<keyof ReaderPreferences, "preference_version">;
const GROUPS: Array<{ key: PreferenceKey; label: string; choices: Choice[] }> = [
  { key: "font_size", label: "Text size", choices: [
    { value: "small", label: "Small" }, { value: "book", label: "Book" },
    { value: "large", label: "Large" }, { value: "x-large", label: "Extra large" },
  ] },
  { key: "line_height", label: "Line spacing", choices: [
    { value: "compact", label: "Compact" }, { value: "comfortable", label: "Comfortable" },
    { value: "relaxed", label: "Relaxed" },
  ] },
  { key: "measure", label: "Line length", choices: [
    { value: "narrow", label: "Narrow" }, { value: "balanced", label: "Balanced" },
    { value: "wide", label: "Wide" },
  ] },
  { key: "theme", label: "Theme", choices: [
    { value: "paper", label: "Paper" }, { value: "sepia", label: "Sepia" },
    { value: "night", label: "Night" }, { value: "system", label: "System" },
  ] },
  { key: "margins", label: "Margins", choices: [
    { value: "compact", label: "Compact" }, { value: "balanced", label: "Balanced" },
    { value: "generous", label: "Generous" },
  ] },
  { key: "typeface", label: "Typeface", choices: [
    { value: "publisher", label: "Publisher" }, { value: "serif", label: "Serif" },
    { value: "sans", label: "Sans serif" },
  ] },
];

export function ReaderControls({
  preferences,
  onChange,
  status,
}: {
  preferences: ReaderPreferences;
  onChange: (preferences: ReaderPreferences) => void;
  status: PreferenceSaveStatus;
}) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const opener = useRef<HTMLButtonElement>(null);
  const statusLabel = status === "error" ? "appearance not synced" : status === "saved" ? "appearance saved" : "saving appearance";

  return (
    <div className="reader-controls">
      <button
        ref={opener}
        className="plain smallcaps appearance-trigger"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen((value) => !value)}
      >
        reading appearance
      </button>
      {open && (
        <section
          id={id}
          className="appearance-panel"
          role="region"
          aria-label="Reading appearance"
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              setOpen(false);
              opener.current?.focus();
            }
          }}
        >
          <div className="appearance-heading">
            <h2>Reading appearance</h2>
            <button className="plain" onClick={() => { setOpen(false); opener.current?.focus(); }}>
              close
            </button>
          </div>
          <div className="appearance-groups">
            {GROUPS.map((group) => (
              <fieldset key={group.key}>
                <legend>{group.label}</legend>
                <div className="preset-row">
                  {group.choices.map((choice) => (
                    <label key={choice.value}>
                      <input
                        type="radio"
                        name={`${id}-${group.key}`}
                        value={choice.value}
                        checked={preferences[group.key] === choice.value}
                        onChange={() => onChange({ ...preferences, [group.key]: choice.value })}
                      />
                      <span>{choice.label}</span>
                    </label>
                  ))}
                </div>
              </fieldset>
            ))}
          </div>
          <p className={`preference-status ${status}`} role="status" aria-live="polite">
            {statusLabel}
          </p>
        </section>
      )}
    </div>
  );
}
