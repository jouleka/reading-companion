-- LIT-52: turn the reserved per-book preference row into a constrained preset contract.
ALTER TABLE reader_preferences RENAME COLUMN font_family TO typeface;
ALTER TABLE reader_preferences RENAME COLUMN page_width TO measure;

ALTER TABLE reader_preferences
  ALTER COLUMN font_size TYPE TEXT USING (
    CASE
      WHEN font_size IS NULL THEN 'book'
      WHEN font_size <= 16 THEN 'small'
      WHEN font_size <= 19 THEN 'book'
      WHEN font_size <= 22 THEN 'large'
      ELSE 'x-large'
    END
  ),
  ALTER COLUMN line_height TYPE TEXT USING (
    CASE
      WHEN line_height IS NULL THEN 'comfortable'
      WHEN line_height <= 1.45 THEN 'compact'
      WHEN line_height <= 1.70 THEN 'comfortable'
      ELSE 'relaxed'
    END
  ),
  ALTER COLUMN measure TYPE TEXT USING (
    CASE
      WHEN measure IS NULL THEN 'balanced'
      WHEN measure <= 600 THEN 'narrow'
      WHEN measure <= 760 THEN 'balanced'
      ELSE 'wide'
    END
  );

UPDATE reader_preferences
SET theme = CASE WHEN theme IN ('paper','sepia','night','system') THEN theme ELSE 'paper' END,
    typeface = CASE
      WHEN typeface IS NULL THEN 'publisher'
      WHEN lower(typeface) LIKE '%sans%' THEN 'sans'
      WHEN lower(typeface) LIKE '%serif%' THEN 'serif'
      ELSE 'publisher'
    END,
    preferences = '{}'::jsonb;

ALTER TABLE reader_preferences
  ALTER COLUMN font_size SET DEFAULT 'book',
  ALTER COLUMN font_size SET NOT NULL,
  ALTER COLUMN line_height SET DEFAULT 'comfortable',
  ALTER COLUMN line_height SET NOT NULL,
  ALTER COLUMN measure SET DEFAULT 'balanced',
  ALTER COLUMN measure SET NOT NULL,
  ALTER COLUMN typeface SET DEFAULT 'publisher',
  ALTER COLUMN typeface SET NOT NULL,
  ADD COLUMN margins TEXT NOT NULL DEFAULT 'balanced',
  ADD COLUMN preference_version BIGINT NOT NULL DEFAULT 0 CHECK (preference_version >= 0),
  ADD CONSTRAINT ck_reader_preferences_font_size
    CHECK (font_size IN ('small','book','large','x-large')),
  ADD CONSTRAINT ck_reader_preferences_line_height
    CHECK (line_height IN ('compact','comfortable','relaxed')),
  ADD CONSTRAINT ck_reader_preferences_measure
    CHECK (measure IN ('narrow','balanced','wide')),
  ADD CONSTRAINT ck_reader_preferences_theme
    CHECK (theme IN ('paper','sepia','night','system')),
  ADD CONSTRAINT ck_reader_preferences_margins
    CHECK (margins IN ('compact','balanced','generous')),
  ADD CONSTRAINT ck_reader_preferences_typeface
    CHECK (typeface IN ('publisher','serif','sans')),
  ADD CONSTRAINT ck_reader_preferences_public_shape CHECK (preferences = '{}'::jsonb);
