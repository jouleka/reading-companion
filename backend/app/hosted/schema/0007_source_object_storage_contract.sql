-- LIT-43: hosted source objects have one live, opaque, encrypted EPUB identity per book.
ALTER TABLE source_objects
  ADD CONSTRAINT source_objects_provider_ck
    CHECK (storage_provider IN ('filesystem', 's3')),
  ADD CONSTRAINT source_objects_opaque_key_ck
    CHECK (storage_key ~ '^[0-9a-f]{32}$' AND storage_key = replace(id::text, '-', '')),
  ADD CONSTRAINT source_objects_epub_media_type_ck
    CHECK (media_type = 'application/epub+zip'),
  ADD CONSTRAINT source_objects_nonempty_ck
    CHECK (byte_size > 0),
  ADD CONSTRAINT source_objects_encrypted_ck
    CHECK (encryption_key_id IS NOT NULL AND length(encryption_key_id) > 0),
  ADD CONSTRAINT source_objects_live_verified_ck
    CHECK (deleted_at IS NOT NULL OR verified_at IS NOT NULL);

CREATE UNIQUE INDEX ux_source_objects_one_live_per_book
  ON source_objects (owner_id, book_id, book_incarnation)
  WHERE deleted_at IS NULL;
