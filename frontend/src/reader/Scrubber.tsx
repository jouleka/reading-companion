/** The revealed_at scrubber (LIT-15, D7): slide the memory back through time. A native range input,
 * so it is a real role=slider — keyboard-operable and self-describing (aria-valuemin/max/now) — with
 * the ribbon-bookmark motif. The server clamps every re-query to the high-water, so this can only ever
 * look BACK, never widen the frontier. */
import { useEffect, useId, useRef, useState } from "react";
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
  const [preview, setPreview] = useState(value);
  const dragging = useRef(false);
  useEffect(() => {
    if (!dragging.current) setPreview(value);
  }, [value]);

  const commit = (next: number) => {
    dragging.current = false;
    setPreview(next);
    if (next !== value) onChange(next);
  };
  return (
    <div className="scrubber-wrap">
      <label className="smallcaps scrubber-label" htmlFor={id}>
        as of {unit} {roman(preview)}
      </label>
      <input
        id={id}
        className="scrubber"
        type="range"
        min={1}
        max={Math.max(max, 1)}
        value={preview}
        aria-label={
          unit === "chapter"
            ? "view the story as of an earlier chapter"
            : `view the reading memory as of an earlier ${unit}`
        }
        aria-valuetext={`${unit} ${preview}`}
        onPointerDown={(event) => {
          dragging.current = true;
          event.currentTarget.setPointerCapture?.(event.pointerId);
        }}
        onPointerUp={(event) => commit(Number(event.currentTarget.value))}
        onPointerCancel={() => {
          dragging.current = false;
          setPreview(value);
        }}
        onLostPointerCapture={(event) => {
          if (dragging.current) commit(Number(event.currentTarget.value));
        }}
        onChange={(event) => {
          const next = Number(event.currentTarget.value);
          setPreview(next);
          // Pointer drags can cross dozens of chapters. Preview each position locally and re-query
          // only on release; keyboard and assistive changes still commit through the native input.
          if (!dragging.current) commit(next);
        }}
      />
    </div>
  );
}
