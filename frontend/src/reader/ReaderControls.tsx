import { useState } from "react";
import { Icon } from "../ui/Icon";
import { Button } from "../ui/Button";
import { useReaderPopover } from "../ui/useReaderPopover";
import "./reading-tools-redesign.css";
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
  const [customizing, setCustomizing] = useState(false);
  const statusLabel =
    status === "error"
      ? "appearance not synced"
      : status === "saved"
        ? "appearance saved"
        : "saving appearance";
  const renderGroup = (group: (typeof GROUPS)[number]) => (
    <fieldset
      key={group.key}
      className={`appearance-group appearance-group--${group.key}`}
    >
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
                onChange({ ...preferences, [group.key]: choice.value })
              }
            />
            <span data-preset={choice.value}>
              {group.key === "theme" && (
                <span className="appearance-theme-sample" aria-hidden="true">
                  Aa
                </span>
              )}
              {choice.label}
            </span>
          </label>
        ))}
      </div>
    </fieldset>
  );

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
          <div className="appearance-preview-frame">
            <div
              className="appearance-preview"
              aria-label="Reading preview"
              data-preview-theme={preferences.theme}
              data-preview-size={preferences.font_size}
              data-preview-spacing={preferences.line_height}
              data-preview-measure={preferences.measure}
              data-preview-margins={preferences.margins}
              data-preview-typeface={preferences.typeface}
            >
              <span className="appearance-preview-kicker">A moment to read</span>
              <p>
                The evening settled softly around the room. For a little while,
                there was only the quiet pleasure of another page.
              </p>
            </div>
            <span className="appearance-preview-caption">Make the page feel like yours.</span>
          </div>
          <fieldset className="reading-moods">
            <legend>Set the mood</legend>
            <div>
              {([
                { name: "Daylight", detail: "Clear & spacious", theme: "paper", typeface: "serif", line_height: "comfortable" },
                { name: "Candlelight", detail: "Warm & unhurried", theme: "sepia", typeface: "serif", line_height: "relaxed" },
                { name: "After hours", detail: "A softer night", theme: "night", typeface: "sans", line_height: "relaxed" },
              ] as const).map(mood => <button key={mood.name} type="button" data-mood={mood.theme} aria-pressed={preferences.theme === mood.theme && preferences.typeface === mood.typeface && preferences.line_height === mood.line_height} onClick={() => onChange({ ...preferences, theme: mood.theme, typeface: mood.typeface, line_height: mood.line_height })}><span aria-hidden="true">Aa</span><strong>{mood.name}</strong><small>{mood.detail}</small></button>)}
            </div>
          </fieldset>
          <div className="appearance-groups appearance-essentials">
            {renderGroup(GROUPS.find((group) => group.key === "theme")!)}
            {renderGroup(GROUPS.find((group) => group.key === "font_size")!)}
          </div>
          <div className="appearance-customize">
            <button
              type="button"
              className="appearance-customize-toggle"
              aria-expanded={customizing}
              aria-controls={`${id}-customize`}
              onClick={() => setCustomizing((value) => !value)}
            >
              <span><strong>Customize</strong><small>Font, spacing &amp; layout</small></span>
              <Icon name="chevronDown" size={16} />
            </button>
            <div id={`${id}-customize`} className="appearance-groups appearance-advanced" hidden={!customizing}>
              {GROUPS.filter((group) => group.key !== "theme" && group.key !== "font_size").map(renderGroup)}
            </div>
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
