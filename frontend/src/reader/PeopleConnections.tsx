import type { Graph } from "../api";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { relationshipLabelKey, storyThread, type StoryThreadLabel } from "./graph";
import { roman } from "./roman";

export type PeopleConnectionsProps = {
  graph: Graph;
  focusId: number | null;
  onFocus: (entityId: number) => void;
  onOpenCard: (entityId: number, anchorRect: DOMRect) => void;
  onJumpToChapter: (chapter: number) => void;
  onCorrectMemory?: (entityId: number, canonicalName: string, reason: string) => Promise<void>;
};

type ConnectionRow = {
  entityId: number;
  name: string;
  statements: RelationshipStatement[];
  firstRevealedAt: number;
  lastRevealedAt: number;
};

type RelationshipStatement = {
  key: string;
  sourceId: number;
  sourceName: string;
  targetId: number;
  targetName: string;
  text: string;
  relType: string;
  revealedAt: number;
};

const PEOPLE_PREVIEW_LIMIT = 24;

function statementOf(
  relationship: Graph["relationships"][number],
  names: Map<number, string>,
): RelationshipStatement | null {
  const sourceName = names.get(relationship.src_entity);
  const targetName = names.get(relationship.dst_entity);
  if (!sourceName || !targetName) return null;
  const text = (relationship.label || relationship.rel_type || "connection").trim();
  return {
    key: `${relationship.src_entity}:${relationship.dst_entity}:${relationshipLabelKey(text)}`,
    sourceId: relationship.src_entity,
    sourceName,
    targetId: relationship.dst_entity,
    targetName,
    text,
    relType: relationship.rel_type,
    revealedAt: relationship.revealed_at,
  };
}

function connectionRows(graph: Graph, focusId: number): ConnectionRow[] {
  const names = new Map(graph.characters.map((character) => [character.entity_id, character.canonical_name]));
  const rows = new Map<number, ConnectionRow>();

  for (const relationship of graph.relationships) {
    const counterpartId = relationship.src_entity === focusId
      ? relationship.dst_entity
      : relationship.dst_entity === focusId
        ? relationship.src_entity
        : null;
    if (counterpartId == null) continue;
    const name = names.get(counterpartId);
    if (!name) continue;
    const statement = statementOf(relationship, names);
    if (!statement) continue;
    const existing = rows.get(counterpartId);
    if (existing) {
      const duplicate = existing.statements.find((candidate) => candidate.key === statement.key);
      if (duplicate) duplicate.revealedAt = Math.min(duplicate.revealedAt, statement.revealedAt);
      else existing.statements.push(statement);
      existing.firstRevealedAt = Math.min(existing.firstRevealedAt, relationship.revealed_at);
      existing.lastRevealedAt = Math.max(existing.lastRevealedAt, relationship.revealed_at);
    } else {
      rows.set(counterpartId, {
        entityId: counterpartId,
        name,
        statements: [statement],
        firstRevealedAt: relationship.revealed_at,
        lastRevealedAt: relationship.revealed_at,
      });
    }
  }

  return Array.from(rows.values())
    .map((row) => ({ ...row, statements: row.statements.sort((a, b) => a.revealedAt - b.revealedAt) }))
    .sort((a, b) => b.lastRevealedAt - a.lastRevealedAt || a.name.localeCompare(b.name));
}

function registerRows(graph: Graph): RelationshipStatement[] {
  const names = new Map(graph.characters.map((character) => [character.entity_id, character.canonical_name]));
  const rows = new Map<string, RelationshipStatement>();
  for (const relationship of graph.relationships) {
    const statement = statementOf(relationship, names);
    if (!statement) continue;
    const existing = rows.get(statement.key);
    if (existing) existing.revealedAt = Math.min(existing.revealedAt, statement.revealedAt);
    else rows.set(statement.key, statement);
  }
  return Array.from(rows.values()).sort(
    (a, b) => a.sourceName.localeCompare(b.sourceName)
      || a.targetName.localeCompare(b.targetName)
      || a.revealedAt - b.revealedAt,
  );
}

function aliasesOf(character: Graph["characters"][number]): string[] {
  return "aliases" in character && Array.isArray(character.aliases) ? character.aliases : [];
}

function threadStatement(label: StoryThreadLabel, graph: Graph): RelationshipStatement | null {
  const names = new Map(graph.characters.map((character) => [character.entity_id, character.canonical_name]));
  const sourceName = names.get(label.src);
  const targetName = names.get(label.dst);
  if (!sourceName || !targetName) return null;
  return {
    key: `${label.src}:${label.dst}:${relationshipLabelKey(label.text)}`,
    sourceId: label.src,
    sourceName,
    targetId: label.dst,
    targetName,
    text: label.text,
    relType: "",
    revealedAt: label.revealedAt,
  };
}

export function PeopleConnections(props: PeopleConnectionsProps) {
  const [query, setQuery] = useState("");
  const [showAllPeople, setShowAllPeople] = useState(false);
  const [mode, setMode] = useState<"person" | "register">("person");
  const [registerQuery, setRegisterQuery] = useState("");
  const [registerKind, setRegisterKind] = useState("all");
  const [registerOrder, setRegisterOrder] = useState<"recent" | "alphabetical">("recent");
  const [threadOpen, setThreadOpen] = useState(false);
  const [threadTargetId, setThreadTargetId] = useState<number | null>(null);
  const [correctionOpen, setCorrectionOpen] = useState(false);
  const [correctedName, setCorrectedName] = useState("");
  const [correctionReason, setCorrectionReason] = useState("");
  const [correctionBusy, setCorrectionBusy] = useState(false);
  const [correctionMessage, setCorrectionMessage] = useState("");
  const focus = props.focusId == null
    ? null
    : props.graph.characters.find((character) => character.entity_id === props.focusId) ?? null;
  const visibleCast = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase();
    return normalized
      ? props.graph.characters.filter((character) =>
          [character.canonical_name, ...aliasesOf(character)]
            .some((name) => name.toLocaleLowerCase().includes(normalized)),
        )
      : props.graph.characters;
  }, [props.graph.characters, query]);
  useEffect(() => {
    setCorrectionOpen(false);
    setCorrectedName("");
    setCorrectionReason("");
    setCorrectionMessage("");
  }, [focus?.entity_id]);
  const displayedCast = useMemo(() => {
    if (query.trim() || showAllPeople || visibleCast.length <= PEOPLE_PREVIEW_LIMIT) return visibleCast;
    const preview = visibleCast.slice(0, PEOPLE_PREVIEW_LIMIT);
    const selected = visibleCast.find((character) => character.entity_id === props.focusId);
    if (selected && !preview.some((character) => character.entity_id === selected.entity_id)) {
      preview[preview.length - 1] = selected;
    }
    return preview;
  }, [props.focusId, query, showAllPeople, visibleCast]);
  const [roving, setRoving] = useState(() => {
    const selected = props.graph.characters.findIndex((character) => character.entity_id === props.focusId);
    return selected < 0 ? 0 : selected;
  });
  const peopleListRef = useRef<HTMLUListElement>(null);
  const personRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const previousPeopleLengthRef = useRef(displayedCast.length);
  const heldPeopleFocusRef = useRef(false);
  const personDescriptionPrefix = useId();
  // Capture focus ownership during render while React's previously committed people list still exists.
  // If a scrub/filter removes the focused person, the effect below can repair focus without pulling it
  // into the list when the search, scrubber, or chapter breakdown owned it instead (LIT-28 / WCAG 2.4.3).
  heldPeopleFocusRef.current = peopleListRef.current?.contains(document.activeElement)
    ?? heldPeopleFocusRef.current;

  useEffect(() => {
    const clamped = Math.max(0, Math.min(roving, displayedCast.length - 1));
    if (clamped !== roving) setRoving(clamped);
    const shrank = displayedCast.length < previousPeopleLengthRef.current;
    previousPeopleLengthRef.current = displayedCast.length;
    personRefs.current.length = displayedCast.length;
    if (!shrank || displayedCast.length === 0) return;
    const active = document.activeElement;
    const orphaned = !active || active === document.body || active === document.documentElement;
    if (heldPeopleFocusRef.current && orphaned) personRefs.current[clamped]?.focus();
  }, [roving, displayedCast.length]);
  const allRegisterRows = useMemo(() => registerRows(props.graph), [props.graph]);
  const registerKinds = useMemo(
    () => Array.from(new Set(allRegisterRows.map((row) => row.relType).filter(Boolean))).sort(),
    [allRegisterRows],
  );
  const visibleRegisterRows = useMemo(() => {
    const normalized = registerQuery.trim().toLocaleLowerCase();
    const rows = allRegisterRows.filter((row) =>
      (registerKind === "all" || row.relType === registerKind)
      && (!normalized || [row.sourceName, row.targetName, row.text, row.relType]
        .some((value) => value.toLocaleLowerCase().includes(normalized))),
    );
    return rows.sort((a, b) => registerOrder === "recent"
      ? b.revealedAt - a.revealedAt || a.sourceName.localeCompare(b.sourceName)
      : a.sourceName.localeCompare(b.sourceName) || a.targetName.localeCompare(b.targetName));
  }, [allRegisterRows, registerKind, registerOrder, registerQuery]);

  if (!focus) return <p className="quiet people-connections-empty">Choose someone from the cast.</p>;

  const connections = connectionRows(props.graph, focus.entity_id);
  const selectedThreadTargetId = threadTargetId != null
    && threadTargetId !== focus.entity_id
    && props.graph.characters.some((character) => character.entity_id === threadTargetId)
      ? threadTargetId
      : props.graph.characters.find((character) => character.entity_id !== focus.entity_id)?.entity_id ?? null;
  const threadTarget = props.graph.characters.find((character) => character.entity_id === selectedThreadTargetId) ?? null;
  const thread = selectedThreadTargetId == null
    ? null
    : storyThread(props.graph, focus.entity_id, selectedThreadTargetId);
  const focusPerson = (entityId: number) => {
    setMode("person");
    props.onFocus(entityId);
  };
  const movePersonFocus = (index: number) => {
    const next = Math.max(0, Math.min(displayedCast.length - 1, index));
    setRoving(next);
    personRefs.current[next]?.focus();
  };
  const onPeopleKeyDown = (event: React.KeyboardEvent<HTMLUListElement>) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      movePersonFocus(roving + 1);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      movePersonFocus(roving - 1);
    } else if (event.key === "Home") {
      event.preventDefault();
      movePersonFocus(0);
    } else if (event.key === "End") {
      event.preventDefault();
      movePersonFocus(displayedCast.length - 1);
    } else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      const person = displayedCast[roving];
      if (person) focusPerson(person.entity_id);
    }
  };

  return (
    <section className="people-connections" aria-label="people and connections">
      <nav className="people-index" aria-label="Cast index">
        <header>
          <h2>People &amp; connections</h2>
          <p>Choose a name to read their known ties.</p>
        </header>
        <label className="people-search">
          <span className="sr-only">Find a person</span>
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Find a person…"
          />
        </label>
        {query && (
          <button type="button" className="people-search-clear" onClick={() => setQuery("")}>
            Clear people search
          </button>
        )}
        {displayedCast.length > 0 ? (
          <ul aria-label="the cast so far" ref={peopleListRef} onKeyDown={onPeopleKeyDown}>
            {displayedCast.map((character, index) => {
              const descriptionId = `${personDescriptionPrefix}-${character.entity_id}`;
              const knownConnections = connectionRows(props.graph, character.entity_id);
              const description = knownConnections.length > 0
                ? `${character.canonical_name}'s known connections: ${knownConnections
                    .map((connection) => `${connection.name}, ${connection.statements
                      .map((statement) => statement.text).join(", ")}`)
                    .join("; ")}.`
                : `No connections are recorded for ${character.canonical_name} through this chapter.`;
              return (
              <li className="people-index-person" key={character.entity_id}>
                <button
                  type="button"
                  aria-current={character.entity_id === focus.entity_id ? "true" : undefined}
                  aria-describedby={descriptionId}
                  tabIndex={index === roving ? 0 : -1}
                  ref={(element) => { personRefs.current[index] = element; }}
                  onClick={() => {
                    setRoving(index);
                    focusPerson(character.entity_id);
                  }}
                >
                  <span>
                    {character.canonical_name}
                    {aliasesOf(character).length > 0 && (
                      <em className="people-aliases">{aliasesOf(character).join(", ")}</em>
                    )}
                  </span>
                  <small>Introduced Ch. {roman(character.revealed_at)}</small>
                </button>
                <span className="sr-only" id={descriptionId}>{description}</span>
              </li>
              );
            })}
          </ul>
        ) : (
          <p className="quiet people-search-empty" role="status">No people found for “{query}”.</p>
        )}
        {!query.trim() && visibleCast.length > PEOPLE_PREVIEW_LIMIT && (
          <button
            type="button"
            className="people-show-all"
            aria-expanded={showAllPeople}
            onClick={() => setShowAllPeople((shown) => !shown)}
          >
            {showAllPeople ? "Show fewer people" : `Show all ${visibleCast.length} people`}
          </button>
        )}
      </nav>
      {mode === "register" ? (
        <section className="relationship-register" aria-labelledby="relationship-register-title">
          <header>
            <span className="smallcaps person-entry-kicker">known through chapter {props.graph.as_of_chapter}</span>
            <h2 id="relationship-register-title">Relationship register</h2>
            <p>Every recorded tie, written plainly.</p>
            <button type="button" className="person-card-link" onClick={() => setMode("person")}>
              Back to {focus.canonical_name}
            </button>
          </header>
          <div className="relationship-register-tools">
            <label>
              <span className="sr-only">Filter relationships</span>
              <input type="search" value={registerQuery}
                     onChange={(event) => setRegisterQuery(event.target.value)}
                     placeholder="Filter people or ties…" />
            </label>
            <label>
              <span>Kind</span>
              <select aria-label="Relationship kind" value={registerKind}
                      onChange={(event) => setRegisterKind(event.target.value)}>
                <option value="all">All kinds</option>
                {registerKinds.map((kind) => <option value={kind} key={kind}>{kind}</option>)}
              </select>
            </label>
            <label>
              <span>Order</span>
              <select aria-label="Relationship order" value={registerOrder}
                      onChange={(event) => setRegisterOrder(event.target.value as "recent" | "alphabetical")}>
                <option value="recent">Recently learned</option>
                <option value="alphabetical">Alphabetical</option>
              </select>
            </label>
          </div>
          {visibleRegisterRows.length > 0 ? (
            <ul aria-label="Relationship register">
              {visibleRegisterRows.map((row) => (
              <li key={row.key}>
                <button type="button" onClick={() => focusPerson(row.sourceId)}>{row.sourceName}</button>
                <span className="register-labels">
                  <span>
                    <span>{row.text}</span>
                    <button
                      type="button"
                      aria-label={`Chapter ${row.revealedAt}`}
                      onClick={() => props.onJumpToChapter(row.revealedAt)}
                    >
                      Chapter {roman(row.revealedAt)}
                    </button>
                  </span>
                </span>
                <button type="button" onClick={() => focusPerson(row.targetId)}>{row.targetName}</button>
              </li>
              ))}
            </ul>
          ) : (
            <p className="quiet relationship-register-empty" role="status">
              No relationships match these filters.
            </p>
          )}
        </section>
      ) : (
      <article className="person-entry" aria-live="polite" aria-atomic="true">
        <header className="person-entry-head">
          <span className="smallcaps person-entry-kicker">known through chapter {props.graph.as_of_chapter}</span>
          <h2>{focus.canonical_name}</h2>
          {aliasesOf(focus).length > 0 && (
            <p className="person-entry-aliases">Also known as {aliasesOf(focus).join(", ")}</p>
          )}
          <button
            type="button"
            className="person-introduction-link"
            onClick={() => props.onJumpToChapter(focus.revealed_at)}
          >
            Introduced in Chapter {roman(focus.revealed_at)} <span aria-hidden="true">→</span>
          </button>
          <button
            type="button"
            className="person-card-link"
            onClick={(event) => props.onOpenCard(focus.entity_id, event.currentTarget.getBoundingClientRect())}
          >
            Open character card <span aria-hidden="true">↗</span>
          </button>
          <button type="button" className="person-card-link" onClick={() => setMode("register")}>
            Show all relationships
          </button>
          {props.onCorrectMemory && (
            <button
              type="button"
              className="person-card-link"
              aria-expanded={correctionOpen}
              onClick={() => {
                setCorrectionOpen((open) => !open);
                setCorrectionMessage("");
              }}
            >
              {correctionOpen ? "Cancel name correction" : "Correct this name"}
            </button>
          )}
          {correctionOpen && props.onCorrectMemory && (
            <form
              className="memory-correction-form"
              onSubmit={async (event) => {
                event.preventDefault();
                setCorrectionBusy(true);
                setCorrectionMessage("");
                try {
                  await props.onCorrectMemory!(
                    focus.entity_id,
                    correctedName.trim(),
                    correctionReason.trim(),
                  );
                  setCorrectionOpen(false);
                  setCorrectedName("");
                  setCorrectionReason("");
                } catch (error) {
                  setCorrectionMessage(
                    error instanceof Error ? error.message : "The correction could not be saved.",
                  );
                } finally {
                  setCorrectionBusy(false);
                }
              }}
            >
              <p>
                This changes the Codex from the current chapter forward. Earlier chapters keep the
                original name in their history.
              </p>
              <label>
                <span>Correct name</span>
                <input
                  required
                  maxLength={200}
                  value={correctedName}
                  onChange={(event) => setCorrectedName(event.target.value)}
                  autoComplete="off"
                />
              </label>
              <label>
                <span>Why this is a correction</span>
                <textarea
                  required
                  maxLength={1000}
                  rows={3}
                  value={correctionReason}
                  onChange={(event) => setCorrectionReason(event.target.value)}
                />
              </label>
              <button type="submit" disabled={correctionBusy}>
                {correctionBusy ? "Saving correction…" : "Save correction"}
              </button>
              {correctionMessage && <p role="alert">{correctionMessage}</p>}
            </form>
          )}
        </header>

        <section className="connection-ledger">
          <h3 className="smallcaps">known connections</h3>
          {connections.length > 0 ? (
            <ul aria-label={`Known connections for ${focus.canonical_name}`}>
              {connections.map((connection) => (
                <li key={connection.entityId}>
                  <button type="button" onClick={() => focusPerson(connection.entityId)}>
                    {connection.name}
                  </button>
                  <span className="connection-labels">
                    {connection.statements.map((statement) => (
                      <span className="connection-line connection-statement" key={statement.key}>
                        <span className="connection-proposition">
                          <span>{statement.sourceName}</span>
                          <em>{statement.text}</em>
                          <span>{statement.targetName}</span>
                        </span>
                        <button
                          type="button"
                          aria-label={`Chapter ${statement.revealedAt}`}
                          onClick={() => props.onJumpToChapter(statement.revealedAt)}
                        >
                          Chapter {roman(statement.revealedAt)}
                        </button>
                      </span>
                    ))}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="quiet">No connections are recorded through this chapter.</p>
          )}
        </section>

        {props.graph.characters.length > 1 && <section className="thread-reader">
          <button
            type="button"
            className="thread-reader-toggle"
            aria-expanded={threadOpen}
            onClick={() => setThreadOpen((open) => !open)}
          >
            {threadOpen ? "Close thread" : "Trace a thread"}
          </button>
          {threadOpen && (
            <div className="thread-reader-body">
              <label>
                <span>Connect {focus.canonical_name} to</span>
                <select
                  aria-label={`Connect ${focus.canonical_name} to`}
                  value={selectedThreadTargetId ?? ""}
                  onChange={(event) => setThreadTargetId(Number(event.target.value))}
                >
                  {props.graph.characters
                    .filter((character) => character.entity_id !== focus.entity_id)
                    .map((character) => (
                      <option value={character.entity_id} key={character.entity_id}>
                        {character.canonical_name}
                      </option>
                    ))}
                </select>
              </label>

              {thread && threadTarget && thread.steps.length > 0 ? (
                <ol aria-label={`One known connection from ${focus.canonical_name} to ${threadTarget.canonical_name}`}>
                  {thread.steps.flatMap((step) =>
                    step.labels.map((label) => threadStatement(label, props.graph))
                      .filter((statement): statement is RelationshipStatement => statement != null)
                      .map((statement) => (
                        <li key={`${step.key}:${statement.key}`}>
                          <button type="button" onClick={() => focusPerson(statement.sourceId)}>{statement.sourceName}</button>
                          <span>{statement.text}</span>
                          <button type="button" onClick={() => focusPerson(statement.targetId)}>{statement.targetName}</button>
                          <button
                            type="button"
                            aria-label={`Chapter ${statement.revealedAt}`}
                            onClick={() => props.onJumpToChapter(statement.revealedAt)}
                          >
                            Chapter {roman(statement.revealedAt)}
                          </button>
                        </li>
                      )),
                  )}
                </ol>
              ) : (
                <p className="quiet">No known thread through Chapter {props.graph.as_of_chapter}.</p>
              )}
            </div>
          )}
        </section>}
      </article>
      )}
    </section>
  );
}
