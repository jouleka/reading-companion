-- LIT-55: portable, bounded anchors and optimistic versions for reader-created marks.
ALTER TABLE highlights
  ADD COLUMN version BIGINT NOT NULL DEFAULT 1 CHECK (version > 0),
  ADD CONSTRAINT highlights_color_closed CHECK (
    color IS NULL OR color IN ('yellow', 'green', 'blue', 'pink')
  ),
  ADD CONSTRAINT highlights_text_bounded CHECK (
    selected_text IS NULL OR length(selected_text) BETWEEN 1 AND 2000
  ),
  ADD CONSTRAINT highlights_anchor_portable CHECK (
    anchor ? 'atom' AND anchor ? 'cfi'
    AND jsonb_typeof(anchor->'atom') = 'number'
    AND (anchor->>'atom')::numeric = trunc((anchor->>'atom')::numeric)
    AND (anchor->>'atom')::numeric BETWEEN 1 AND 2147483647
    AND length(anchor->>'cfi') BETWEEN 1 AND 4096
    AND anchor->>'cfi' LIKE 'epubcfi(%)'
  );

ALTER TABLE annotations
  ADD COLUMN version BIGINT NOT NULL DEFAULT 1 CHECK (version > 0),
  ADD CONSTRAINT annotations_body_bounded CHECK (length(body) BETWEEN 1 AND 10000),
  ADD CONSTRAINT annotations_anchor_portable CHECK (
    anchor ? 'atom' AND anchor ? 'cfi'
    AND jsonb_typeof(anchor->'atom') = 'number'
    AND (anchor->>'atom')::numeric = trunc((anchor->>'atom')::numeric)
    AND (anchor->>'atom')::numeric BETWEEN 1 AND 2147483647
    AND length(anchor->>'cfi') BETWEEN 1 AND 4096
    AND anchor->>'cfi' LIKE 'epubcfi(%)'
  );

ALTER TABLE bookmarks
  ADD COLUMN version BIGINT NOT NULL DEFAULT 1 CHECK (version > 0),
  ADD CONSTRAINT bookmarks_label_bounded CHECK (
    label IS NULL OR length(label) BETWEEN 1 AND 500
  ),
  ADD CONSTRAINT bookmarks_anchor_portable CHECK (
    anchor ? 'atom' AND anchor ? 'cfi'
    AND jsonb_typeof(anchor->'atom') = 'number'
    AND (anchor->>'atom')::numeric = trunc((anchor->>'atom')::numeric)
    AND (anchor->>'atom')::numeric BETWEEN 1 AND 2147483647
    AND length(anchor->>'cfi') BETWEEN 1 AND 4096
    AND anchor->>'cfi' LIKE 'epubcfi(%)'
  );
