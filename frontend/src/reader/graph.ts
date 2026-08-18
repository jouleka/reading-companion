/** Pure spoiler-safe relationship helpers over the chapter-clamped graph payload. */
import type { Graph } from "../api";

export type StoryThreadStep = {
  key: string;
  from: number;
  to: number;
  labels: StoryThreadLabel[];
};

export type StoryThreadLabel = {
  text: string;
  revealedAt: number;
  src: number;
  dst: number;
};

export type StoryThread = {
  nodes: Graph["characters"];
  steps: StoryThreadStep[];
};

function edgeLabel(edge: Graph["relationships"][number]) {
  return (edge.label || edge.rel_type || "connection").trim();
}

/** Conservative semantic key used only to collapse obvious extraction paraphrases. Direction is
 * intentionally NOT part of this function; callers include src/dst in their key so inverse statements
 * such as “father of” and “owes money to” can never be folded into the same proposition. */
export function relationshipLabelKey(text: string): string {
  const normalized = text
    .toLocaleLowerCase()
    .replace(/\b(relationship|relation|tie)\b/g, " ")
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
  if (normalized === "father of" || normalized === "father son") return "father-child";
  if (normalized === "mother of" || normalized === "mother son" || normalized === "mother daughter") return "mother-child";
  if (["brother of", "sister of", "brothers", "sisters", "sibling", "siblings"].includes(normalized)) return "siblings";
  return normalized;
}

/** The shortest visible chain between two people. The input graph is already clamped to the reader's
 * chapter frontier, so the returned path cannot introduce a future person or relationship. Parallel
 * records are bundled into one hop while preserving their labels for the textual thread reader. */
export function storyThread(graph: Graph, sourceId: number, targetId: number): StoryThread | null {
  const byId = new Map(graph.characters.map((character) => [character.entity_id, character]));
  if (!byId.has(sourceId) || !byId.has(targetId)) return null;
  if (sourceId === targetId) return { nodes: [byId.get(sourceId)!], steps: [] };

  type Link = { key: string; a: number; b: number; labels: StoryThreadLabel[] };
  const links = new Map<string, Link>();
  for (const edge of graph.relationships) {
    if (!byId.has(edge.src_entity) || !byId.has(edge.dst_entity)) continue;
    const a = Math.min(edge.src_entity, edge.dst_entity);
    const b = Math.max(edge.src_entity, edge.dst_entity);
    const key = `${a}:${b}`;
    const label: StoryThreadLabel = {
      text: edgeLabel(edge),
      revealedAt: edge.revealed_at,
      src: edge.src_entity,
      dst: edge.dst_entity,
    };
    const existing = links.get(key);
    if (existing) {
      const duplicate = existing.labels.find(
        (candidate) => candidate.src === label.src
          && candidate.dst === label.dst
          && relationshipLabelKey(candidate.text) === relationshipLabelKey(label.text),
      );
      if (duplicate) duplicate.revealedAt = Math.min(duplicate.revealedAt, label.revealedAt);
      else existing.labels.push(label);
    } else {
      links.set(key, { key, a, b, labels: [label] });
    }
  }

  const adjacency = new Map<number, { other: number; link: Link }[]>();
  for (const id of byId.keys()) adjacency.set(id, []);
  for (const link of links.values()) {
    adjacency.get(link.a)?.push({ other: link.b, link });
    adjacency.get(link.b)?.push({ other: link.a, link });
  }

  const parent = new Map<number, { previous: number; link: Link } | null>([[sourceId, null]]);
  const queue = [sourceId];
  for (let cursor = 0; cursor < queue.length && !parent.has(targetId); cursor += 1) {
    const current = queue[cursor];
    for (const next of adjacency.get(current) ?? []) {
      if (parent.has(next.other)) continue;
      parent.set(next.other, { previous: current, link: next.link });
      queue.push(next.other);
      if (next.other === targetId) break;
    }
  }
  if (!parent.has(targetId)) return null;

  const nodeIds = [targetId];
  const steps: StoryThreadStep[] = [];
  let current = targetId;
  while (current !== sourceId) {
    const hit = parent.get(current);
    if (!hit) return null;
    steps.push({
      key: hit.link.key,
      from: hit.previous,
      to: current,
      labels: hit.link.labels.map((label) => ({ ...label })),
    });
    current = hit.previous;
    nodeIds.push(current);
  }
  nodeIds.reverse();
  steps.reverse();
  return { nodes: nodeIds.map((id) => byId.get(id)!), steps };
}

/** The default focus on open: the most-connected visible character. Ties resolve to the first
 * character in server order; null is returned when the cast is empty. */
export function mostConnected(graph: Graph): number | null {
  if (graph.characters.length === 0) return null;
  const neighbours = new Map<number, Set<number>>();
  for (const character of graph.characters) neighbours.set(character.entity_id, new Set());
  for (const edge of graph.relationships) {
    if (neighbours.has(edge.src_entity) && neighbours.has(edge.dst_entity)) {
      neighbours.get(edge.src_entity)!.add(edge.dst_entity);
      neighbours.get(edge.dst_entity)!.add(edge.src_entity);
    }
  }
  let best = graph.characters[0].entity_id;
  let bestDegree = -1;
  for (const character of graph.characters) {
    const currentDegree = neighbours.get(character.entity_id)!.size;
    if (currentDegree > bestDegree) {
      best = character.entity_id;
      bestDegree = currentDegree;
    }
  }
  return best;
}
