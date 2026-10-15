import { Icon } from "../ui/Icon";
import { Button } from "../ui/Button";
import { useReaderPopover } from "../ui/useReaderPopover";
import type {
  PreferenceSaveStatus,
  ReaderPreferences,
} from "./readerPreferences";

type Choice = { value: string; label: string };
type PreferenceKey = Exclude<keyof ReaderPreferences, "preference_version">;
const GROUPS: Array<{ key: PreferenceKey; label: string; choices: Choice[] }> =
  [
    {
      key: "font_size",
      label: "Text size",
      choices: [
        { value: "small", label: "Small" },
        { value: "book", label: "Book" },
        { value: "large", label: "Large" },
        { value: "x-large", label: "Extra large" },
      ],
    },
    {
      key: "line_height",
      label: "Line spacing",
      choices: [
        { value: "compact", label: "Compact" },
        { value: "comfortable", label: "Comfortable" },
        { value: "relaxed", label: "Relaxed" },
      ],
    },
    {
      key: "measure",
      label: "Line length",
      choices: [
        { value: "narrow", label: "Narrow" },
        { value: "balanced", label: "Balanced" },
        { value: "wide", label: "Wide" },
      ],
    },
    {
      key: "theme",
      label: "Theme",
      choices: [
        { value: "paper", label: "Paper" },
        { value: "sepia", label: "Sepia" },
        { value: "night", label: "Night" },
        { value: "system", label: "System" },
      ],
    },
    {
      key: "margins",
      label: "Margins",
      choices: [
        { value: "compact", label: "Compact" },
        { value: "balanced", label: "Balanced" },
        { value: "generous", label: "Generous" },
      ],
    },
    {
      key: "typeface",
      label: "Typeface",
      choices: [
        { value: "publisher", label: "Publisher" },
        { value: "serif", label: "Serif" },
        { value: "sans", label: "Sans serif" },
      ],
    },
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
  const { open, closing, id, triggerRef, panelRef, toggle, close } =
    useReaderPopover();
  const statusLabel =
    status === "error"
      ? "appearance not synced"
      : status === "saved"
        ? "appearance saved"
        : "saving appearance";

  return (
    <div className="reader-controls">
      <button
        ref={triggerRef}
        type="button"
        className="plain reader-tool appearance-trigger"
        aria-label="reading appearance"
        aria-expanded={open && !closing}
        aria-controls={id}
        onClick={toggle}
      >
        <Icon name="type" />
        <span>Appearance</span>
      </button>
      {open && (
        <section
          id={id}
          className="reader-popover appearance-panel"
          data-closing={closing || undefined}
          aria-hidden={closing || undefined}
          role="region"
          aria-label="Reading appearance"
          tabIndex={-1}
          ref={panelRef}
        >
          <div className="reader-panel-heading appearance-heading">
            <h2>Reading appearance</h2>
            <Button
              variant="ghost"
              className="icon-button"
              aria-label="Close reading appearance"
              onClick={() => close()}
            >
              <Icon name="close" />
            </Button>
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
                        onChange={() =>
                          onChange({
                            ...preferences,
                            [group.key]: choice.value,
                          })
                        }
                      />
                      <span>{choice.label}</span>
                    </label>
                  ))}
                </div>
              </fieldset>
            ))}
          </div>
          <p
            className={`preference-status ${status}`}
            role="status"
            aria-live="polite"
          >
            {statusLabel}
          </p>
        </section>
      )}
    </div>
  );
}
