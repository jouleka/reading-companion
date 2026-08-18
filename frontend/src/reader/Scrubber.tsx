/** The revealed_at scrubber (LIT-15, D7): slide the memory back through time. A native range input,
 * so it is a real role=slider — keyboard-operable and self-describing (aria-valuemin/max/now) — with
 * the ribbon-bookmark motif. The server clamps every re-query to the high-water, so this can only ever
 * look BACK, never widen the frontier. */
import { useId } from "react";
import { roman } from "./roman";

export function Scrubber({
  max,
  value,
  onChange,
  unit = "chapter",
}: {
  max: number;
  value: number;
  onChange: (v: number) => void;
  unit?: "chapter" | "scene" | "section";
}) {
  const id = useId(); // unique per instance — no hardcoded-id collision if two scrubbers ever coexist
  return (
    <div className="scrubber-wrap">
      <label className="smallcaps scrubber-label" htmlFor={id}>
        as of {unit} {roman(value)}
      </label>
      <input
        id={id}
        className="scrubber"
        type="range"
        min={1}
        max={Math.max(max, 1)}
        value={value}
        aria-label={unit === "chapter" ? "view the story as of an earlier chapter" : `view the reading memory as of an earlier ${unit}`}
        aria-valuetext={`${unit} ${value}`}
        onChange={(e) => onChange(Number(e.target.value))}
      />
    </div>
  );
}
