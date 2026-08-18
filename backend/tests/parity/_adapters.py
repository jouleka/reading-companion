"""Backend adapters for the LIT-39 shared spoiler-parity corpus.

These are deliberately test-harness adapters, not hosted runtime repositories. LIT-41 owns the
production repository/API boundary. The PostgreSQL side nevertheless executes real owner-scoped SQL
against the committed schema and pgvector function, while the SQLite side uses the production DAL.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.catalog.catalog import Catalog
from app.eval.spoiler_gate.cache import validity_snapshot
from app.memory.dal import _now
from app.memory.store import Store


_NAMESPACE = uuid.UUID("86972df8-f593-4fc7-bd99-4dce00c889c7")


def load_corpus(path: Path) -> dict:
    corpus = json.loads(path.read_text(encoding="utf-8"))
    if corpus.get("schema_version") != 1:
        raise ValueError("unsupported parity corpus schema")
    chapter_keys = [chapter["key"] for chapter in corpus["chapters"]]
    ordinals = [chapter["ordinal"] for chapter in corpus["chapters"]]
    if len(chapter_keys) != len(set(chapter_keys)) or ordinals != list(range(1, len(ordinals) + 1)):
        raise ValueError("parity chapters must have unique keys and contiguous ordinals")
    return corpus


def _digest(corpus: dict) -> str:
    payload = json.dumps(corpus, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _content_hash(text: str, length: int) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:length]


def _uuid(kind: str, key: str) -> uuid.UUID:
    return uuid.uuid5(_NAMESPACE, f"{kind}:{key}")


class _SnapshotMixin:
    def snapshot(self, bookmark: int) -> dict:
        chapters = self.chapters(bookmark)
        entities = self.entities(bookmark)
        relationships = self.relationships(bookmark)
        events = self.timeline(bookmark)
        entity_keys = [entity["key"] for entity in entities]
        event_keys = [event["key"] for event in events]
        return {
            "chapters": chapters,
            "entities": entities,
            "entity_keys": entity_keys,
            "aliases": {key: self.aliases_of(bookmark, key) for key in entity_keys},
            "relationships": relationships,
            "events": events,
            "participants": {key: self.participants_of(bookmark, key) for key in event_keys},
            "events_for": {key: self.events_for(bookmark, key) for key in entity_keys},
            "themes": self.themes(bookmark),
            "states": {key: self.current_state(bookmark, key) for key in entity_keys},
            "summaries": self.chapter_summaries(bookmark),
            "bios": {key: self.bio(bookmark, key) for key in entity_keys},
            "catch_me_up": self.catch_me_up(bookmark),
        }


class SQLiteParityAdapter(_SnapshotMixin):
    def __init__(self, root: Path, corpus: dict):
        self.corpus = corpus
        self.corpus_digest = _digest(corpus)
        self.book_key = corpus["book"]["key"]
        self.embedding_model = f'{corpus["embedding"]["model"]}@{corpus["embedding"]["space"]}'
        root.mkdir(parents=True, exist_ok=True)
        self.store = Store(str(root / "data"), vector_backend="vec0")
        self.catalog = Catalog(str(root / "catalog.db"))
        self.catalog.add_book(
            self.book_key,
            title=corpus["book"]["title"],
            author=corpus["book"]["author"],
        )
        self._entity_ids: dict[str, int] = {}
        self._entity_keys: dict[int, str] = {}
        self._event_ids: dict[str, int] = {}
        self._event_keys: dict[int, str] = {}
        self._event_reveals = {
            item["key"]: item["revealed_at"] for item in corpus["events"]
        }
        self._edge_keys: dict[int, str] = {}
        self._theme_keys: dict[int, str] = {}
        self._state_keys: dict[int, str] = {}
        self._chunk_keys_by_text = {chunk["text"]: chunk["key"] for chunk in corpus["chunks"]}
        self._seed()

    def _seed(self) -> None:
        meta = {
            "title": self.corpus["book"]["title"],
            "author": self.corpus["book"]["author"],
            "source": "fixture",
            "source_id": "lit39",
        }
        chunks_by_chapter: dict[str, list[dict]] = {}
        for chunk in self.corpus["chunks"]:
            chunks_by_chapter.setdefault(chunk["chapter"], []).append(chunk)

        with self.store.book(self.book_key, meta=meta) as mem:
            mem.pin_models(
                embed_model=self.embedding_model,
                embed_dim=self.corpus["embedding"]["dimension"],
                embed_canary=[1.0, 0.0, 0.0],
            )
            for chapter in self.corpus["chapters"]:
                content_hash = _content_hash(chapter["text"], 16)
                with mem.transaction():
                    mem.add_chapter(
                        chapter["key"],
                        chapter["ordinal"],
                        href=f'{chapter["key"]}.xhtml',
                        title=chapter["title"],
                        content_hash=content_hash,
                    )
                    mem.add_raw(
                        chapter["key"],
                        chapter["ordinal"],
                        chapter["text"],
                        content_hash=content_hash,
                    )
                    mem.add_summary(
                        chapter["key"], chapter["ordinal"], chapter["summary"], kind="chapter"
                    )
                    mem.add_summary(
                        chapter["key"],
                        chapter["ordinal"],
                        chapter["rolling_summary"],
                        kind="rolling-recap",
                    )
                    for chunk in chunks_by_chapter.get(chapter["key"], []):
                        mem.add_chunk(
                            chapter["key"],
                            chunk["revealed_at"],
                            chunk["text"],
                            chunk["vector"],
                            embed_model=self.embedding_model,
                            embed_dim=self.corpus["embedding"]["dimension"],
                        )
                    mem.mark_chapter_ingested(chapter["key"], content_hash)

            for entity in self.corpus["entities"]:
                entity_id = mem.add_entity(
                    entity["name"], entity["type"], entity["revealed_at"]
                )
                self._entity_ids[entity["key"]] = entity_id
                self._entity_keys[entity_id] = entity["key"]
                if entity.get("invalid_at") is not None:
                    with mem._writer():
                        mem._conn.execute(
                            "UPDATE entities SET invalid_at=? WHERE entity_id=? AND book_id=?",
                            (entity["invalid_at"], entity_id, self.book_key),
                        )

            for alias in self.corpus["aliases"]:
                mem.add_alias(
                    self._entity_ids[alias["entity"]], alias["surface"], alias["revealed_at"]
                )

            for edge in self.corpus["edges"]:
                edge_id = mem.add_edge(
                    self._entity_ids[edge["src"]],
                    self._entity_ids[edge["dst"]],
                    edge["type"],
                    edge["label"],
                    edge["revealed_at"],
                    invalid_at=edge.get("invalid_at"),
                )
                self._edge_keys[edge_id] = edge["key"]

            for event in self.corpus["events"]:
                event_id = mem.add_event(
                    event["summary"],
                    event["revealed_at"],
                    event["order_idx"],
                    kind=event["kind"],
                    invalid_at=event.get("invalid_at"),
                    participants=[
                        (self._entity_ids[item["entity"]], item["role"])
                        for item in event["participants"]
                    ],
                )
                self._event_ids[event["key"]] = event_id
                self._event_keys[event_id] = event["key"]

            for theme in self.corpus["themes"]:
                theme_id = mem.add_theme(
                    theme["name"], theme["description"], theme["revealed_at"]
                )
                self._theme_keys[theme_id] = theme["key"]
                if theme.get("invalid_at") is not None:
                    with mem._writer():
                        mem._conn.execute(
                            "UPDATE themes SET invalid_at=? WHERE theme_id=? AND book_id=?",
                            (theme["invalid_at"], theme_id, self.book_key),
                        )

            for state in self.corpus["states"]:
                state_id = mem.add_state(
                    self._entity_ids[state["entity"]],
                    state["revealed_at"],
                    state["status"],
                    invalid_at=state.get("invalid_at"),
                )
                self._state_keys[state_id] = state["key"]

            mem._ins(
                "entity_corrections",
                book_id=self.book_key,
                kind="split",
                revealed_at=3,
                source_entity_ids_json=json.dumps([self._entity_ids["alexander"]]),
                target_entity_ids_json=json.dumps([self._entity_ids["alexandra"]]),
                assignments_json=json.dumps({"fixture": "alexander-to-alexandra"}),
                reason="parity correction fixture",
                schema_version=1,
                recorded_at=_now(),
                retracted_at=None,
            )

            for chapter in self.corpus["chapters"]:
                if chapter.get("retracted"):
                    mem.retract_chapter(chapter["key"])

        state = self.corpus["reading_state"]
        self.catalog.set_position(
            self.book_key,
            state["cfi"],
            state["bookmark"],
            expected_epoch=state["position_epoch"],
        )
        self.catalog.set_ingest_progress(self.book_key, self.completion_frontier())

    def close(self) -> None:
        self.catalog.close()
        self.store.close()

    def _view(self, bookmark: int, callback):
        with self.store.book(self.book_key) as mem:
            return callback(mem, mem.view(bookmark))

    def chapters(self, bookmark: int) -> list[dict]:
        return self._view(
            bookmark,
            lambda _mem, view: [
                {
                    "key": row["chapter_key"],
                    "revealed_at": row["revealed_at"],
                    "title": row["title"],
                    "part_label": row["part_label"] or None,
                }
                for row in view.chapters()
            ],
        )

    def entities(self, bookmark: int) -> list[dict]:
        def read(_mem, view):
            rows = list(view.characters())
            for entity_type in ("place", "faction", "object"):
                rows.extend(view.entities_of_type(entity_type))
            result = [
                {
                    "key": self._entity_keys[row["entity_id"]],
                    "name": row["canonical_name"],
                    "type": row["type"],
                    "revealed_at": row["revealed_at"],
                }
                for row in rows
            ]
            return sorted(result, key=lambda item: (item["revealed_at"], item["key"]))

        return self._view(bookmark, read)

    def aliases_of(self, bookmark: int, entity_key: str) -> list[dict]:
        return self._view(
            bookmark,
            lambda _mem, view: [
                {"surface": row["surface_form"], "revealed_at": row["revealed_at"]}
                for row in view.aliases_of(self._entity_ids[entity_key])
            ],
        )

    def relationships(self, bookmark: int) -> list[dict]:
        def read(_mem, view):
            result = [
                {
                    "key": self._edge_keys[row["edge_id"]],
                    "src": self._entity_keys[row["src_entity"]],
                    "dst": self._entity_keys[row["dst_entity"]],
                    "type": row["rel_type"],
                    "label": row["label"],
                    "revealed_at": row["revealed_at"],
                    "invalid_at": row["invalid_at"],
                }
                for row in view.relationships()
            ]
            return sorted(result, key=lambda item: (item["revealed_at"], item["key"]))

        return self._view(bookmark, read)

    def timeline(self, bookmark: int) -> list[dict]:
        return self._view(
            bookmark,
            lambda _mem, view: [
                {
                    "key": self._event_keys[row["event_id"]],
                    "revealed_at": row["revealed_at"],
                    "order_idx": row["order_idx"],
                    "summary": row["summary"],
                    "kind": row["kind"],
                }
                for row in view.timeline()
            ],
        )

    def participants_of(self, bookmark: int, event_key: str) -> list[dict]:
        result = self._view(
            bookmark,
            lambda _mem, view: [
                {
                    "key": self._entity_keys[row["entity_id"]],
                    "name": row["canonical_name"],
                    "type": row["type"],
                }
                for row in view.participants_of(self._event_ids[event_key])
            ],
        )
        return sorted(result, key=lambda item: item["key"])

    def events_for(self, bookmark: int, entity_key: str) -> list[dict]:
        result = self._view(
            bookmark,
            lambda _mem, view: [
                {
                    "key": self._event_keys[row["event_id"]],
                    "revealed_at": row["revealed_at"],
                    "summary": row["summary"],
                }
                for row in view.events_for(self._entity_ids[entity_key])
            ],
        )
        return sorted(result, key=lambda item: (item["revealed_at"], item["key"]))

    def themes(self, bookmark: int) -> list[dict]:
        return self._view(
            bookmark,
            lambda _mem, view: [
                {
                    "key": self._theme_keys[row["theme_id"]],
                    "name": row["name"],
                    "description": row["description"],
                    "revealed_at": row["revealed_at"],
                }
                for row in view.themes()
            ],
        )

    def current_state(self, bookmark: int, entity_key: str) -> dict | None:
        def read(_mem, view):
            row = view.current_state(self._entity_ids[entity_key])
            if row is None:
                return None
            return {
                "key": self._state_keys[row["state_id"]],
                "revealed_at": row["revealed_at"],
                "status": json.loads(row["status_json"]),
            }

        return self._view(bookmark, read)

    def chapter_summaries(self, bookmark: int) -> list[dict]:
        return self._view(
            bookmark,
            lambda _mem, view: [
                {
                    "chapter": row["chapter_key"],
                    "revealed_at": row["revealed_at"],
                    "summary": row["summary"],
                }
                for row in view.chapter_summaries()
            ],
        )

    def bio(self, bookmark: int, entity_key: str) -> dict | None:
        def read(_mem, view):
            value = view.bio(self._entity_ids[entity_key])
            if value is None:
                return None
            return {
                "name": value["name"],
                "type": value["type"],
                "first_seen": value["first_seen"],
                "aliases": value["aliases"],
                "state": value["state"],
                "appears_in_events": sorted(
                    (self._event_keys[event] for event in value["appears_in_events"]),
                    key=lambda key: (self._event_reveals[key], key),
                ),
            }

        return self._view(bookmark, read)

    def catch_me_up(self, bookmark: int) -> dict:
        return self._view(bookmark, lambda _mem, view: view.catch_me_up())

    def search(self, bookmark: int, query: dict) -> list[dict]:
        def read(_mem, view):
            return [
                {
                    "key": self._chunk_keys_by_text[text],
                    "chapter": chapter,
                    "revealed_at": revealed_at,
                    "text": text,
                }
                for _score, text, revealed_at, chapter in view.search(
                    query["vector"], query["k"], embed_model=self.embedding_model
                )
            ]

        return self._view(bookmark, read)

    def receipts(self) -> list[str]:
        def read(mem, _view):
            return sorted(row["chapter_key"] for row in mem._audit_all("ingested_chapters"))

        return self._view(0, read)

    def completion_frontier(self) -> int:
        atoms = [
            {"key": chapter["key"], "ordinal": chapter["ordinal"]}
            for chapter in self.corpus["chapters"]
        ]
        return self._view(0, lambda mem, _view: mem.completion_frontier(atoms))

    def reading_state(self) -> dict:
        state = self.catalog.get_state(self.book_key)
        return {
            "bookmark": state["bookmark"],
            "cfi": state["cfi"],
            "position_epoch": state["position_epoch"],
            "receipt_count": len(self.receipts()),
        }

    def reset_position(self, expected_epoch: int) -> dict:
        self.catalog.reset_position(self.book_key, expected_epoch=expected_epoch)
        return self.reading_state()

    def advance_position(self, bookmark: int, cfi: str, expected_epoch: int) -> bool:
        try:
            self.catalog.set_position(
                self.book_key, cfi, bookmark, expected_epoch=expected_epoch
            )
        except ValueError:
            return False
        return True

    def cache_token(self, bookmark: int) -> str:
        return self._view(bookmark, lambda mem, _view: validity_snapshot(mem, bookmark))

    def reextract_summary(self, chapter_key: str, summary: str) -> None:
        chapter = next(item for item in self.corpus["chapters"] if item["key"] == chapter_key)
        self._view(
            chapter["ordinal"],
            lambda mem, _view: mem.reextract_summary(
                chapter_key, chapter["ordinal"], summary, kind="chapter"
            ),
        )


class PostgresParityAdapter(_SnapshotMixin):
    def __init__(self, dsn: str, corpus: dict):
        self.corpus = corpus
        self.corpus_digest = _digest(corpus)
        self.owner_id = _uuid("owner", "parity")
        self.book_id = _uuid("book", corpus["book"]["key"])
        self.incarnation = _uuid("incarnation", corpus["book"]["key"])
        self._chapter_ids = {item["key"]: _uuid("chapter", item["key"]) for item in corpus["chapters"]}
        self._entity_ids = {item["key"]: _uuid("entity", item["key"]) for item in corpus["entities"]}
        self._entity_keys = {value: key for key, value in self._entity_ids.items()}
        self._event_ids = {item["key"]: _uuid("event", item["key"]) for item in corpus["events"]}
        self._event_keys = {value: key for key, value in self._event_ids.items()}
        self._event_reveals = {
            item["key"]: item["revealed_at"] for item in corpus["events"]
        }
        self._edge_ids = {item["key"]: _uuid("edge", item["key"]) for item in corpus["edges"]}
        self._edge_keys = {value: key for key, value in self._edge_ids.items()}
        self._theme_ids = {item["key"]: _uuid("theme", item["key"]) for item in corpus["themes"]}
        self._theme_keys = {value: key for key, value in self._theme_ids.items()}
        self._state_ids = {item["key"]: _uuid("state", item["key"]) for item in corpus["states"]}
        self._state_keys = {value: key for key, value in self._state_ids.items()}
        self._chunk_ids = {item["key"]: _uuid("chunk", item["key"]) for item in corpus["chunks"]}
        self._chunk_keys = {value: key for key, value in self._chunk_ids.items()}
        self.conn = psycopg.connect(dsn, row_factory=dict_row)
        self._seed()

    def _scope(self) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
        return self.owner_id, self.book_id, self.incarnation

    def _seed(self) -> None:
        scope = self._scope()
        embedding = self.corpus["embedding"]
        with self.conn.transaction():
            self.conn.execute(
                "INSERT INTO users (id, display_name) VALUES (%s, 'Parity Reader')",
                (self.owner_id,),
            )
            self.conn.execute(
                """
                INSERT INTO books
                  (owner_id,id,incarnation,title,author,schema_version,embedding_model,
                   embedding_dimension,embedding_space)
                VALUES (%s,%s,%s,%s,%s,1,%s,%s,%s)
                """,
                (
                    *scope,
                    self.corpus["book"]["title"],
                    self.corpus["book"]["author"],
                    embedding["model"],
                    embedding["dimension"],
                    embedding["space"],
                ),
            )
            state = self.corpus["reading_state"]
            self.conn.execute(
                """
                INSERT INTO reading_state
                  (owner_id,book_id,book_incarnation,bookmark,high_water_cfi,current_cfi,position_epoch)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                (*scope, state["bookmark"], state["cfi"], state["cfi"], state["position_epoch"]),
            )
            for chapter in self.corpus["chapters"]:
                chapter_id = self._chapter_ids[chapter["key"]]
                content_hash = _content_hash(chapter["text"], 64)
                self.conn.execute(
                    """
                    INSERT INTO chapters
                      (owner_id,book_id,book_incarnation,id,chapter_key,revealed_at,href,title,content_hash)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        *scope,
                        chapter_id,
                        chapter["key"],
                        chapter["ordinal"],
                        f'{chapter["key"]}.xhtml',
                        chapter["title"],
                        content_hash,
                    ),
                )
                self.conn.execute(
                    """
                    INSERT INTO ingested_chapters
                      (owner_id,book_id,book_incarnation,chapter_id,content_hash,completed_at)
                    VALUES (%s,%s,%s,%s,%s,now())
                    """,
                    (*scope, chapter_id, content_hash),
                )
                for kind, summary in (
                    ("chapter", chapter["summary"]),
                    ("rolling-recap", chapter["rolling_summary"]),
                ):
                    self.conn.execute(
                        """
                        INSERT INTO chapter_summaries
                          (owner_id,book_id,book_incarnation,id,source_chapter_id,kind,summary,revealed_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (
                            *scope,
                            _uuid("summary", f'{chapter["key"]}:{kind}'),
                            chapter_id,
                            kind,
                            summary,
                            chapter["ordinal"],
                        ),
                    )

            for entity in self.corpus["entities"]:
                self.conn.execute(
                    """
                    INSERT INTO entities
                      (owner_id,book_id,book_incarnation,id,source_chapter_id,canonical_name,
                       entity_type,revealed_at,invalid_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        *scope,
                        self._entity_ids[entity["key"]],
                        self._chapter_ids[entity["source_chapter"]],
                        entity["name"],
                        entity["type"],
                        entity["revealed_at"],
                        entity.get("invalid_at"),
                    ),
                )

            for index, alias in enumerate(self.corpus["aliases"]):
                self.conn.execute(
                    """
                    INSERT INTO aliases
                      (owner_id,book_id,book_incarnation,id,entity_id,source_chapter_id,
                       surface_form,revealed_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        *scope,
                        _uuid("alias", f'{alias["entity"]}:{index}'),
                        self._entity_ids[alias["entity"]],
                        self._chapter_ids[alias["source_chapter"]],
                        alias["surface"],
                        alias["revealed_at"],
                    ),
                )

            for edge in self.corpus["edges"]:
                self.conn.execute(
                    """
                    INSERT INTO edges
                      (owner_id,book_id,book_incarnation,id,source_chapter_id,src_entity_id,
                       dst_entity_id,relationship_type,label,revealed_at,invalid_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        *scope,
                        self._edge_ids[edge["key"]],
                        self._chapter_ids[edge["source_chapter"]],
                        self._entity_ids[edge["src"]],
                        self._entity_ids[edge["dst"]],
                        edge["type"],
                        edge["label"],
                        edge["revealed_at"],
                        edge.get("invalid_at"),
                    ),
                )

            for event in self.corpus["events"]:
                event_id = self._event_ids[event["key"]]
                source_chapter_id = self._chapter_ids[event["source_chapter"]]
                self.conn.execute(
                    """
                    INSERT INTO events
                      (owner_id,book_id,book_incarnation,id,source_chapter_id,order_idx,
                       summary,kind,revealed_at,invalid_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        *scope,
                        event_id,
                        source_chapter_id,
                        event["order_idx"],
                        event["summary"],
                        event["kind"],
                        event["revealed_at"],
                        event.get("invalid_at"),
                    ),
                )
                for participant in event["participants"]:
                    self.conn.execute(
                        """
                        INSERT INTO event_participants
                          (owner_id,book_id,book_incarnation,event_id,entity_id,source_chapter_id,
                           role,revealed_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (
                            *scope,
                            event_id,
                            self._entity_ids[participant["entity"]],
                            source_chapter_id,
                            participant["role"],
                            event["revealed_at"],
                        ),
                    )

            for theme in self.corpus["themes"]:
                self.conn.execute(
                    """
                    INSERT INTO themes
                      (owner_id,book_id,book_incarnation,id,source_chapter_id,name,description,
                       revealed_at,invalid_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        *scope,
                        self._theme_ids[theme["key"]],
                        self._chapter_ids[theme["source_chapter"]],
                        theme["name"],
                        theme["description"],
                        theme["revealed_at"],
                        theme.get("invalid_at"),
                    ),
                )

            for state in self.corpus["states"]:
                self.conn.execute(
                    """
                    INSERT INTO entity_state
                      (owner_id,book_id,book_incarnation,id,entity_id,source_chapter_id,status,
                       revealed_at,invalid_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        *scope,
                        self._state_ids[state["key"]],
                        self._entity_ids[state["entity"]],
                        self._chapter_ids[state["source_chapter"]],
                        Jsonb(state["status"]),
                        state["revealed_at"],
                        state.get("invalid_at"),
                    ),
                )

            self.conn.execute(
                """
                INSERT INTO entity_corrections
                  (owner_id,book_id,book_incarnation,id,source_chapter_id,correction_kind,
                   source_entity_ids,target_entity_ids,assignments,reason,revealed_at)
                VALUES (%s,%s,%s,%s,%s,'split',%s,%s,%s,%s,3)
                """,
                (
                    *scope,
                    _uuid("correction", "alexander-to-alexandra"),
                    self._chapter_ids["ch3"],
                    Jsonb([str(self._entity_ids["alexander"])]),
                    Jsonb([str(self._entity_ids["alexandra"])]),
                    Jsonb({"fixture": "alexander-to-alexandra"}),
                    "parity correction fixture",
                ),
            )

            for chunk in self.corpus["chunks"]:
                chunk_id = self._chunk_ids[chunk["key"]]
                self.conn.execute(
                    """
                    INSERT INTO chunks
                      (owner_id,book_id,book_incarnation,id,chapter_id,revealed_at,text)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        *scope,
                        chunk_id,
                        self._chapter_ids[chunk["chapter"]],
                        chunk["revealed_at"],
                        chunk["text"],
                    ),
                )
                self.conn.execute(
                    """
                    INSERT INTO chunk_embeddings
                      (owner_id,book_id,book_incarnation,chunk_id,embedding_model,
                       embedding_dimension,embedding_space,distance_metric,embedding)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::vector)
                    """,
                    (
                        *scope,
                        chunk_id,
                        embedding["model"],
                        embedding["dimension"],
                        embedding["space"],
                        embedding["distance_metric"],
                        json.dumps(chunk["vector"]),
                    ),
                )

            for chapter in self.corpus["chapters"]:
                if chapter.get("retracted"):
                    chapter_id = self._chapter_ids[chapter["key"]]
                    self.conn.execute(
                        "UPDATE chapters SET retracted_at=now() "
                        "WHERE (owner_id,book_id,book_incarnation,id)=(%s,%s,%s,%s)",
                        (*scope, chapter_id),
                    )
                    self.conn.execute(
                        "UPDATE chapter_summaries SET retracted_at=now() "
                        "WHERE (owner_id,book_id,book_incarnation,source_chapter_id)=(%s,%s,%s,%s)",
                        (*scope, chapter_id),
                    )
                    self.conn.execute(
                        "UPDATE chunks SET retracted_at=now() "
                        "WHERE (owner_id,book_id,book_incarnation,chapter_id)=(%s,%s,%s,%s)",
                        (*scope, chapter_id),
                    )
                    self.conn.execute(
                        """
                        UPDATE chunk_embeddings AS embedding SET retracted_at=now()
                        FROM chunks AS chunk
                        WHERE (embedding.owner_id,embedding.book_id,embedding.book_incarnation)
                            = (chunk.owner_id,chunk.book_id,chunk.book_incarnation)
                          AND embedding.chunk_id=chunk.id
                          AND (chunk.owner_id,chunk.book_id,chunk.book_incarnation,chunk.chapter_id)
                            = (%s,%s,%s,%s)
                        """,
                        (*scope, chapter_id),
                    )

    def close(self) -> None:
        self.conn.close()

    def _params(self, bookmark: int) -> tuple:
        return *self._scope(), bookmark, bookmark

    def chapters(self, bookmark: int) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT chapter_key AS key,revealed_at,title,part_label
            FROM chapters
            WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s
              AND revealed_at<=%s AND retracted_at IS NULL
            ORDER BY revealed_at,chapter_key
            """,
            (*self._scope(), bookmark),
        ).fetchall()
        return [dict(row) for row in rows]

    def entities(self, bookmark: int) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT id,canonical_name AS name,entity_type AS type,revealed_at
            FROM entities
            WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s
              AND revealed_at<=%s AND retracted_at IS NULL
              AND (invalid_at IS NULL OR invalid_at>%s)
            """,
            self._params(bookmark),
        ).fetchall()
        result = [
            {
                "key": self._entity_keys[row["id"]],
                "name": row["name"],
                "type": row["type"],
                "revealed_at": row["revealed_at"],
            }
            for row in rows
        ]
        return sorted(result, key=lambda item: (item["revealed_at"], item["key"]))

    def aliases_of(self, bookmark: int, entity_key: str) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT alias.surface_form AS surface,alias.revealed_at
            FROM aliases AS alias
            JOIN entities AS entity
              ON (entity.owner_id,entity.book_id,entity.book_incarnation,entity.id)
               = (alias.owner_id,alias.book_id,alias.book_incarnation,alias.entity_id)
            WHERE alias.owner_id=%s AND alias.book_id=%s AND alias.book_incarnation=%s
              AND alias.entity_id=%s AND alias.revealed_at<=%s AND alias.retracted_at IS NULL
              AND entity.revealed_at<=%s AND entity.retracted_at IS NULL
              AND (entity.invalid_at IS NULL OR entity.invalid_at>%s)
            ORDER BY alias.revealed_at,alias.id
            """,
            (*self._scope(), self._entity_ids[entity_key], bookmark, bookmark, bookmark),
        ).fetchall()
        return [dict(row) for row in rows]

    def relationships(self, bookmark: int) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT edge.id,edge.src_entity_id,edge.dst_entity_id,
                   edge.relationship_type AS type,edge.label,edge.revealed_at,edge.invalid_at
            FROM edges AS edge
            JOIN entities AS src
              ON (src.owner_id,src.book_id,src.book_incarnation,src.id)
               = (edge.owner_id,edge.book_id,edge.book_incarnation,edge.src_entity_id)
            JOIN entities AS dst
              ON (dst.owner_id,dst.book_id,dst.book_incarnation,dst.id)
               = (edge.owner_id,edge.book_id,edge.book_incarnation,edge.dst_entity_id)
            WHERE edge.owner_id=%s AND edge.book_id=%s AND edge.book_incarnation=%s
              AND edge.revealed_at<=%s AND edge.retracted_at IS NULL
              AND (edge.invalid_at IS NULL OR edge.invalid_at>%s)
              AND src.revealed_at<=%s AND src.retracted_at IS NULL
              AND (src.invalid_at IS NULL OR src.invalid_at>%s)
              AND dst.revealed_at<=%s AND dst.retracted_at IS NULL
              AND (dst.invalid_at IS NULL OR dst.invalid_at>%s)
            """,
            (*self._scope(), *([bookmark] * 6)),
        ).fetchall()
        result = [
            {
                "key": self._edge_keys[row["id"]],
                "src": self._entity_keys[row["src_entity_id"]],
                "dst": self._entity_keys[row["dst_entity_id"]],
                "type": row["type"],
                "label": row["label"],
                "revealed_at": row["revealed_at"],
                "invalid_at": row["invalid_at"],
            }
            for row in rows
        ]
        return sorted(result, key=lambda item: (item["revealed_at"], item["key"]))

    def timeline(self, bookmark: int) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT id,revealed_at,order_idx,summary,kind
            FROM events
            WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s
              AND revealed_at<=%s AND retracted_at IS NULL
              AND (invalid_at IS NULL OR invalid_at>%s)
            ORDER BY revealed_at,order_idx,id
            """,
            self._params(bookmark),
        ).fetchall()
        return [
            {
                "key": self._event_keys[row["id"]],
                "revealed_at": row["revealed_at"],
                "order_idx": row["order_idx"],
                "summary": row["summary"],
                "kind": row["kind"],
            }
            for row in rows
        ]

    def participants_of(self, bookmark: int, event_key: str) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT entity.id,entity.canonical_name AS name,entity.entity_type AS type,
                   entity.revealed_at
            FROM event_participants AS participant
            JOIN events AS event
              ON (event.owner_id,event.book_id,event.book_incarnation,event.id)
               = (participant.owner_id,participant.book_id,participant.book_incarnation,participant.event_id)
            JOIN entities AS entity
              ON (entity.owner_id,entity.book_id,entity.book_incarnation,entity.id)
               = (participant.owner_id,participant.book_id,participant.book_incarnation,participant.entity_id)
            WHERE participant.owner_id=%s AND participant.book_id=%s AND participant.book_incarnation=%s
              AND participant.event_id=%s AND participant.revealed_at<=%s
              AND participant.retracted_at IS NULL
              AND (participant.invalid_at IS NULL OR participant.invalid_at>%s)
              AND event.revealed_at<=%s AND event.retracted_at IS NULL
              AND (event.invalid_at IS NULL OR event.invalid_at>%s)
              AND entity.revealed_at<=%s AND entity.retracted_at IS NULL
              AND (entity.invalid_at IS NULL OR entity.invalid_at>%s)
            ORDER BY entity.revealed_at,entity.id
            """,
            (*self._scope(), self._event_ids[event_key], *([bookmark] * 6)),
        ).fetchall()
        result = [
            {"key": self._entity_keys[row["id"]], "name": row["name"], "type": row["type"]}
            for row in rows
        ]
        return sorted(result, key=lambda item: item["key"])

    def events_for(self, bookmark: int, entity_key: str) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT event.id,event.revealed_at,event.summary
            FROM event_participants AS participant
            JOIN events AS event
              ON (event.owner_id,event.book_id,event.book_incarnation,event.id)
               = (participant.owner_id,participant.book_id,participant.book_incarnation,participant.event_id)
            JOIN entities AS entity
              ON (entity.owner_id,entity.book_id,entity.book_incarnation,entity.id)
               = (participant.owner_id,participant.book_id,participant.book_incarnation,participant.entity_id)
            WHERE participant.owner_id=%s AND participant.book_id=%s AND participant.book_incarnation=%s
              AND participant.entity_id=%s AND participant.revealed_at<=%s
              AND participant.retracted_at IS NULL
              AND (participant.invalid_at IS NULL OR participant.invalid_at>%s)
              AND event.revealed_at<=%s AND event.retracted_at IS NULL
              AND (event.invalid_at IS NULL OR event.invalid_at>%s)
              AND entity.revealed_at<=%s AND entity.retracted_at IS NULL
              AND (entity.invalid_at IS NULL OR entity.invalid_at>%s)
            ORDER BY event.revealed_at,event.id
            """,
            (*self._scope(), self._entity_ids[entity_key], *([bookmark] * 6)),
        ).fetchall()
        result = [
            {
                "key": self._event_keys[row["id"]],
                "revealed_at": row["revealed_at"],
                "summary": row["summary"],
            }
            for row in rows
        ]
        return sorted(result, key=lambda item: (item["revealed_at"], item["key"]))

    def themes(self, bookmark: int) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT id,name,description,revealed_at
            FROM themes
            WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s
              AND revealed_at<=%s AND retracted_at IS NULL
              AND (invalid_at IS NULL OR invalid_at>%s)
            """,
            self._params(bookmark),
        ).fetchall()
        result = [
            {
                "key": self._theme_keys[row["id"]],
                "name": row["name"],
                "description": row["description"],
                "revealed_at": row["revealed_at"],
            }
            for row in rows
        ]
        return sorted(result, key=lambda item: (item["revealed_at"], item["key"]))

    def current_state(self, bookmark: int, entity_key: str) -> dict | None:
        row = self.conn.execute(
            """
            SELECT state.id,state.revealed_at,state.status
            FROM entity_state AS state
            JOIN entities AS entity
              ON (entity.owner_id,entity.book_id,entity.book_incarnation,entity.id)
               = (state.owner_id,state.book_id,state.book_incarnation,state.entity_id)
            WHERE state.owner_id=%s AND state.book_id=%s AND state.book_incarnation=%s
              AND state.entity_id=%s AND state.revealed_at<=%s AND state.retracted_at IS NULL
              AND (state.invalid_at IS NULL OR state.invalid_at>%s)
              AND entity.revealed_at<=%s AND entity.retracted_at IS NULL
              AND (entity.invalid_at IS NULL OR entity.invalid_at>%s)
            ORDER BY state.revealed_at DESC,state.id DESC
            LIMIT 1
            """,
            (*self._scope(), self._entity_ids[entity_key], *([bookmark] * 4)),
        ).fetchone()
        if row is None:
            return None
        return {
            "key": self._state_keys[row["id"]],
            "revealed_at": row["revealed_at"],
            "status": row["status"],
        }

    def chapter_summaries(self, bookmark: int) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT chapter.chapter_key AS chapter,summary.revealed_at,summary.summary
            FROM chapter_summaries AS summary
            JOIN chapters AS chapter
              ON (chapter.owner_id,chapter.book_id,chapter.book_incarnation,chapter.id)
               = (summary.owner_id,summary.book_id,summary.book_incarnation,summary.source_chapter_id)
            WHERE summary.owner_id=%s AND summary.book_id=%s AND summary.book_incarnation=%s
              AND summary.kind='chapter' AND summary.revealed_at<=%s
              AND summary.retracted_at IS NULL
              AND (summary.invalid_at IS NULL OR summary.invalid_at>%s)
              AND chapter.revealed_at<=%s AND chapter.retracted_at IS NULL
            ORDER BY summary.revealed_at,summary.id
            """,
            (*self._scope(), bookmark, bookmark, bookmark),
        ).fetchall()
        return [dict(row) for row in rows]

    def bio(self, bookmark: int, entity_key: str) -> dict | None:
        entity = next(
            (item for item in self.entities(bookmark) if item["key"] == entity_key), None
        )
        if entity is None:
            return None
        state = self.current_state(bookmark, entity_key)
        return {
            "name": entity["name"],
            "type": entity["type"],
            "first_seen": entity["revealed_at"],
            "aliases": [item["surface"] for item in self.aliases_of(bookmark, entity_key)],
            "state": state["status"] if state else None,
            "appears_in_events": [item["key"] for item in self.events_for(bookmark, entity_key)],
        }

    def catch_me_up(self, bookmark: int) -> dict:
        row = self.conn.execute(
            """
            SELECT summary
            FROM chapter_summaries
            WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s
              AND kind='rolling-recap' AND revealed_at<=%s AND retracted_at IS NULL
              AND (invalid_at IS NULL OR invalid_at>%s)
            ORDER BY revealed_at DESC,id DESC
            LIMIT 1
            """,
            self._params(bookmark),
        ).fetchone()
        return {
            "as_of_chapter": bookmark,
            "recap": row["summary"] if row else None,
            "cast_size": sum(item["type"] == "character" for item in self.entities(bookmark)),
            "open_threads": len(self.relationships(bookmark)),
        }

    def search(self, bookmark: int, query: dict) -> list[dict]:
        embedding = self.corpus["embedding"]
        rows = self.conn.execute(
            """
            SELECT chunk.id,chapter.chapter_key AS chapter,chunk.revealed_at,chunk.text,distance
            FROM search_chunks_prefiltered(%s,%s,%s,%s,%s,%s,%s,%s,%s::vector,%s) AS hit
            JOIN chunks AS chunk
              ON (chunk.owner_id,chunk.book_id,chunk.book_incarnation,chunk.id)
               = (%s,%s,%s,hit.chunk_id)
            JOIN chapters AS chapter
              ON (chapter.owner_id,chapter.book_id,chapter.book_incarnation,chapter.id)
               = (chunk.owner_id,chunk.book_id,chunk.book_incarnation,chunk.chapter_id)
            ORDER BY distance,chunk.id
            """,
            (
                *self._scope(),
                bookmark,
                embedding["model"],
                embedding["dimension"],
                embedding["space"],
                embedding["distance_metric"],
                json.dumps(query["vector"]),
                query["k"],
                *self._scope(),
            ),
        ).fetchall()
        return [
            {
                "key": self._chunk_keys[row["id"]],
                "chapter": row["chapter"],
                "revealed_at": row["revealed_at"],
                "text": row["text"],
            }
            for row in rows
        ]

    def receipts(self) -> list[str]:
        rows = self.conn.execute(
            """
            SELECT chapter.chapter_key
            FROM ingested_chapters AS receipt
            JOIN chapters AS chapter
              ON (chapter.owner_id,chapter.book_id,chapter.book_incarnation,chapter.id)
               = (receipt.owner_id,receipt.book_id,receipt.book_incarnation,receipt.chapter_id)
            WHERE receipt.owner_id=%s AND receipt.book_id=%s AND receipt.book_incarnation=%s
            ORDER BY chapter.revealed_at
            """,
            self._scope(),
        ).fetchall()
        return [row["chapter_key"] for row in rows]

    def completion_frontier(self) -> int:
        rows = self.conn.execute(
            """
            SELECT chapter.revealed_at
            FROM chapters AS chapter
            JOIN ingested_chapters AS receipt
              ON (receipt.owner_id,receipt.book_id,receipt.book_incarnation,receipt.chapter_id)
               = (chapter.owner_id,chapter.book_id,chapter.book_incarnation,chapter.id)
            WHERE chapter.owner_id=%s AND chapter.book_id=%s AND chapter.book_incarnation=%s
              AND chapter.retracted_at IS NULL AND receipt.retracted_at IS NULL
            ORDER BY chapter.revealed_at
            """,
            self._scope(),
        ).fetchall()
        frontier = 0
        for row in rows:
            if row["revealed_at"] != frontier + 1:
                break
            frontier = row["revealed_at"]
        return frontier

    def reading_state(self) -> dict:
        row = self.conn.execute(
            """
            SELECT bookmark,current_cfi,position_epoch
            FROM reading_state
            WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s
            """,
            self._scope(),
        ).fetchone()
        return {
            "bookmark": row["bookmark"],
            "cfi": row["current_cfi"],
            "position_epoch": row["position_epoch"],
            "receipt_count": len(self.receipts()),
        }

    def reset_position(self, expected_epoch: int) -> dict:
        with self.conn.transaction():
            row = self.conn.execute(
                """
                UPDATE reading_state
                SET bookmark=0,high_water_cfi=NULL,current_cfi=NULL,
                    position_epoch=position_epoch+1,updated_at=now()
                WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s
                  AND position_epoch=%s AND position_epoch<9223372036854775807
                RETURNING position_epoch
                """,
                (*self._scope(), expected_epoch),
            ).fetchone()
            if row is None:
                raise ValueError("position epoch changed; the reading position was reset")
        return self.reading_state()

    def advance_position(self, bookmark: int, cfi: str, expected_epoch: int) -> bool:
        with self.conn.transaction():
            row = self.conn.execute(
                """
                UPDATE reading_state
                SET bookmark=GREATEST(bookmark,%s),current_cfi=%s,high_water_cfi=%s,updated_at=now()
                WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s AND position_epoch=%s
                RETURNING bookmark
                """,
                (bookmark, cfi, cfi, *self._scope(), expected_epoch),
            ).fetchone()
        return row is not None

    def cache_token(self, bookmark: int) -> str:
        payload = {
            "snapshot": self.snapshot(bookmark),
            "search": [self.search(bookmark, query) for query in self.corpus["queries"]],
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()[:16]

    def reextract_summary(self, chapter_key: str, summary: str) -> None:
        chapter = next(item for item in self.corpus["chapters"] if item["key"] == chapter_key)
        chapter_id = self._chapter_ids[chapter_key]
        with self.conn.transaction():
            self.conn.execute(
                """
                UPDATE chapter_summaries SET retracted_at=now()
                WHERE owner_id=%s AND book_id=%s AND book_incarnation=%s
                  AND source_chapter_id=%s AND kind='chapter' AND retracted_at IS NULL
                """,
                (*self._scope(), chapter_id),
            )
            self.conn.execute(
                """
                INSERT INTO chapter_summaries
                  (owner_id,book_id,book_incarnation,id,source_chapter_id,kind,summary,
                   revealed_at,extractor_version)
                VALUES (%s,%s,%s,%s,%s,'chapter',%s,%s,'parity-reextract')
                """,
                (
                    *self._scope(),
                    _uuid("summary", f"{chapter_key}:chapter:reextract"),
                    chapter_id,
                    summary,
                    chapter["ordinal"],
                ),
            )
