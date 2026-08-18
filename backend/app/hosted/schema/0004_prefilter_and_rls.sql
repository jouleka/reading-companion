-- LIT-38: safe pgvector query boundary and RLS defense-in-depth foundations.
CREATE FUNCTION app_current_owner_id()
RETURNS UUID
LANGUAGE sql
STABLE
AS $$
  SELECT NULLIF(current_setting('app.owner_id', true), '')::uuid
$$;

CREATE FUNCTION search_chunks_prefiltered(
  p_owner_id UUID,
  p_book_id UUID,
  p_book_incarnation UUID,
  p_effective_bookmark INTEGER,
  p_embedding_model TEXT,
  p_embedding_dimension INTEGER,
  p_embedding_space TEXT,
  p_distance_metric TEXT,
  p_query VECTOR,
  p_limit INTEGER
)
RETURNS TABLE (chunk_id UUID, distance DOUBLE PRECISION)
LANGUAGE sql
STABLE
AS $$
  WITH eligible AS MATERIALIZED (
    SELECT c.id AS chunk_id, e.embedding
    FROM chunks AS c
    JOIN chapters AS chapter
      ON (chapter.owner_id, chapter.book_id, chapter.book_incarnation, chapter.id)
       = (c.owner_id, c.book_id, c.book_incarnation, c.chapter_id)
    JOIN ingested_chapters AS receipt
      ON (receipt.owner_id, receipt.book_id, receipt.book_incarnation, receipt.chapter_id)
       = (c.owner_id, c.book_id, c.book_incarnation, c.chapter_id)
    JOIN chunk_embeddings AS e
      ON (e.owner_id, e.book_id, e.book_incarnation, e.chunk_id)
       = (c.owner_id, c.book_id, c.book_incarnation, c.id)
    WHERE c.owner_id = p_owner_id
      AND c.book_id = p_book_id
      AND c.book_incarnation = p_book_incarnation
      AND c.revealed_at <= p_effective_bookmark
      AND c.retracted_at IS NULL
      AND chapter.retracted_at IS NULL
      AND receipt.retracted_at IS NULL
      AND receipt.completed_at IS NOT NULL
      AND e.retracted_at IS NULL
      AND e.embedding_model = p_embedding_model
      AND e.embedding_dimension = p_embedding_dimension
      AND e.embedding_space = p_embedding_space
      AND e.distance_metric = p_distance_metric
      AND vector_dims(p_query) = p_embedding_dimension
  )
  SELECT eligible.chunk_id,
         CASE p_distance_metric
           WHEN 'cosine' THEN eligible.embedding <=> p_query
           WHEN 'l2' THEN eligible.embedding <-> p_query
           WHEN 'inner_product' THEN eligible.embedding <#> p_query
         END AS distance
  FROM eligible
  ORDER BY distance, eligible.chunk_id
  LIMIT greatest(0, least(p_limit, 100))
$$;

ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE users FORCE ROW LEVEL SECURITY;
CREATE POLICY users_owner_isolation ON users
  USING (id = app_current_owner_id())
  WITH CHECK (id = app_current_owner_id());

ALTER TABLE external_identities ENABLE ROW LEVEL SECURITY;
ALTER TABLE external_identities FORCE ROW LEVEL SECURITY;
CREATE POLICY external_identities_owner_isolation ON external_identities
  USING (owner_id = app_current_owner_id())
  WITH CHECK (owner_id = app_current_owner_id());

ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE sessions FORCE ROW LEVEL SECURITY;
CREATE POLICY sessions_owner_isolation ON sessions
  USING (owner_id = app_current_owner_id())
  WITH CHECK (owner_id = app_current_owner_id());

ALTER TABLE books ENABLE ROW LEVEL SECURITY;
ALTER TABLE books FORCE ROW LEVEL SECURITY;
CREATE POLICY books_owner_isolation ON books
  USING (owner_id = app_current_owner_id())
  WITH CHECK (owner_id = app_current_owner_id());

ALTER TABLE source_objects ENABLE ROW LEVEL SECURITY;
ALTER TABLE source_objects FORCE ROW LEVEL SECURITY;
CREATE POLICY source_objects_owner_isolation ON source_objects
  USING (owner_id = app_current_owner_id())
  WITH CHECK (owner_id = app_current_owner_id());

ALTER TABLE reading_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE reading_state FORCE ROW LEVEL SECURITY;
CREATE POLICY reading_state_owner_isolation ON reading_state
  USING (owner_id = app_current_owner_id())
  WITH CHECK (owner_id = app_current_owner_id());

ALTER TABLE reader_preferences ENABLE ROW LEVEL SECURITY;
ALTER TABLE reader_preferences FORCE ROW LEVEL SECURITY;
CREATE POLICY reader_preferences_owner_isolation ON reader_preferences
  USING (owner_id = app_current_owner_id())
  WITH CHECK (owner_id = app_current_owner_id());

ALTER TABLE chapters ENABLE ROW LEVEL SECURITY;
ALTER TABLE chapters FORCE ROW LEVEL SECURITY;
CREATE POLICY chapters_owner_isolation ON chapters
  USING (owner_id = app_current_owner_id())
  WITH CHECK (owner_id = app_current_owner_id());

ALTER TABLE ingested_chapters ENABLE ROW LEVEL SECURITY;
ALTER TABLE ingested_chapters FORCE ROW LEVEL SECURITY;
CREATE POLICY ingested_chapters_owner_isolation ON ingested_chapters
  USING (owner_id = app_current_owner_id())
  WITH CHECK (owner_id = app_current_owner_id());

ALTER TABLE chapter_summaries ENABLE ROW LEVEL SECURITY;
ALTER TABLE chapter_summaries FORCE ROW LEVEL SECURITY;
CREATE POLICY chapter_summaries_owner_isolation ON chapter_summaries
  USING (owner_id = app_current_owner_id())
  WITH CHECK (owner_id = app_current_owner_id());

ALTER TABLE entities ENABLE ROW LEVEL SECURITY;
ALTER TABLE entities FORCE ROW LEVEL SECURITY;
CREATE POLICY entities_owner_isolation ON entities
  USING (owner_id = app_current_owner_id())
  WITH CHECK (owner_id = app_current_owner_id());

ALTER TABLE aliases ENABLE ROW LEVEL SECURITY;
ALTER TABLE aliases FORCE ROW LEVEL SECURITY;
CREATE POLICY aliases_owner_isolation ON aliases
  USING (owner_id = app_current_owner_id())
  WITH CHECK (owner_id = app_current_owner_id());

ALTER TABLE edges ENABLE ROW LEVEL SECURITY;
ALTER TABLE edges FORCE ROW LEVEL SECURITY;
CREATE POLICY edges_owner_isolation ON edges
  USING (owner_id = app_current_owner_id())
  WITH CHECK (owner_id = app_current_owner_id());

ALTER TABLE events ENABLE ROW LEVEL SECURITY;
ALTER TABLE events FORCE ROW LEVEL SECURITY;
CREATE POLICY events_owner_isolation ON events
  USING (owner_id = app_current_owner_id())
  WITH CHECK (owner_id = app_current_owner_id());

ALTER TABLE event_participants ENABLE ROW LEVEL SECURITY;
ALTER TABLE event_participants FORCE ROW LEVEL SECURITY;
CREATE POLICY event_participants_owner_isolation ON event_participants
  USING (owner_id = app_current_owner_id())
  WITH CHECK (owner_id = app_current_owner_id());

ALTER TABLE themes ENABLE ROW LEVEL SECURITY;
ALTER TABLE themes FORCE ROW LEVEL SECURITY;
CREATE POLICY themes_owner_isolation ON themes
  USING (owner_id = app_current_owner_id())
  WITH CHECK (owner_id = app_current_owner_id());

ALTER TABLE entity_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE entity_state FORCE ROW LEVEL SECURITY;
CREATE POLICY entity_state_owner_isolation ON entity_state
  USING (owner_id = app_current_owner_id())
  WITH CHECK (owner_id = app_current_owner_id());

ALTER TABLE entity_corrections ENABLE ROW LEVEL SECURITY;
ALTER TABLE entity_corrections FORCE ROW LEVEL SECURITY;
CREATE POLICY entity_corrections_owner_isolation ON entity_corrections
  USING (owner_id = app_current_owner_id())
  WITH CHECK (owner_id = app_current_owner_id());

ALTER TABLE chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE chunks FORCE ROW LEVEL SECURITY;
CREATE POLICY chunks_owner_isolation ON chunks
  USING (owner_id = app_current_owner_id())
  WITH CHECK (owner_id = app_current_owner_id());

ALTER TABLE chunk_embeddings ENABLE ROW LEVEL SECURITY;
ALTER TABLE chunk_embeddings FORCE ROW LEVEL SECURITY;
CREATE POLICY chunk_embeddings_owner_isolation ON chunk_embeddings
  USING (owner_id = app_current_owner_id())
  WITH CHECK (owner_id = app_current_owner_id());

ALTER TABLE provider_credentials ENABLE ROW LEVEL SECURITY;
ALTER TABLE provider_credentials FORCE ROW LEVEL SECURITY;
CREATE POLICY provider_credentials_owner_isolation ON provider_credentials
  USING (owner_id = app_current_owner_id())
  WITH CHECK (owner_id = app_current_owner_id());

ALTER TABLE provider_model_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE provider_model_settings FORCE ROW LEVEL SECURITY;
CREATE POLICY provider_model_settings_owner_isolation ON provider_model_settings
  USING (owner_id = app_current_owner_id())
  WITH CHECK (owner_id = app_current_owner_id());

ALTER TABLE jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE jobs FORCE ROW LEVEL SECURITY;
CREATE POLICY jobs_owner_isolation ON jobs
  USING (owner_id = app_current_owner_id())
  WITH CHECK (owner_id = app_current_owner_id());

ALTER TABLE job_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE job_attempts FORCE ROW LEVEL SECURITY;
CREATE POLICY job_attempts_owner_isolation ON job_attempts
  USING (owner_id = app_current_owner_id())
  WITH CHECK (owner_id = app_current_owner_id());

ALTER TABLE cost_ledger ENABLE ROW LEVEL SECURITY;
ALTER TABLE cost_ledger FORCE ROW LEVEL SECURITY;
CREATE POLICY cost_ledger_owner_isolation ON cost_ledger
  USING (owner_id = app_current_owner_id())
  WITH CHECK (owner_id = app_current_owner_id());

ALTER TABLE cost_reservations ENABLE ROW LEVEL SECURITY;
ALTER TABLE cost_reservations FORCE ROW LEVEL SECURITY;
CREATE POLICY cost_reservations_owner_isolation ON cost_reservations
  USING (owner_id = app_current_owner_id())
  WITH CHECK (owner_id = app_current_owner_id());

ALTER TABLE highlights ENABLE ROW LEVEL SECURITY;
ALTER TABLE highlights FORCE ROW LEVEL SECURITY;
CREATE POLICY highlights_owner_isolation ON highlights
  USING (owner_id = app_current_owner_id())
  WITH CHECK (owner_id = app_current_owner_id());

ALTER TABLE annotations ENABLE ROW LEVEL SECURITY;
ALTER TABLE annotations FORCE ROW LEVEL SECURITY;
CREATE POLICY annotations_owner_isolation ON annotations
  USING (owner_id = app_current_owner_id())
  WITH CHECK (owner_id = app_current_owner_id());

ALTER TABLE bookmarks ENABLE ROW LEVEL SECURITY;
ALTER TABLE bookmarks FORCE ROW LEVEL SECURITY;
CREATE POLICY bookmarks_owner_isolation ON bookmarks
  USING (owner_id = app_current_owner_id())
  WITH CHECK (owner_id = app_current_owner_id());

ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_events FORCE ROW LEVEL SECURITY;
CREATE POLICY audit_events_owner_isolation ON audit_events
  USING (owner_id = app_current_owner_id())
  WITH CHECK (owner_id = app_current_owner_id());
