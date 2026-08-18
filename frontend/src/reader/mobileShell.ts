import { useSyncExternalStore } from "react";

export const MOBILE_READER_QUERY = "(max-width: 900px)";

function query(): MediaQueryList | null {
  return typeof window !== "undefined" && typeof window.matchMedia === "function"
    ? window.matchMedia(MOBILE_READER_QUERY)
    : null;
}

export function useCompactReaderShell(): boolean {
  return useSyncExternalStore(
    (notify) => {
      const media = query();
      if (!media) return () => {};
      media.addEventListener("change", notify);
      return () => media.removeEventListener("change", notify);
    },
    () => query()?.matches ?? false,
    () => false,
  );
}
