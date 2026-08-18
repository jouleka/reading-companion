import { describe, expect, test } from "vitest";
import type { Graph } from "../api";
import { mostConnected, storyThread } from "./graph";

const graph: Graph = {
  as_of_chapter: 5,
  characters: [
    { entity_id: 1, canonical_name: "Fyodor", type: "character", revealed_at: 1 },
    { entity_id: 2, canonical_name: "Mitya", type: "character", revealed_at: 1 },
    { entity_id: 3, canonical_name: "Ivan", type: "character", revealed_at: 2 },
    { entity_id: 4, canonical_name: "Grigory", type: "character", revealed_at: 3 },
  ],
  relationships: [
    { edge_id: 10, src_entity: 1, dst_entity: 2, rel_type: "father", label: "father of", revealed_at: 1, invalid_at: null },
    { edge_id: 11, src_entity: 1, dst_entity: 3, rel_type: "father", label: "father of", revealed_at: 2, invalid_at: null },
    { edge_id: 12, src_entity: 2, dst_entity: 3, rel_type: "sibling", label: "brother of", revealed_at: 2, invalid_at: null },
    { edge_id: 13, src_entity: 3, dst_entity: 4, rel_type: "acquaintance", label: "knows", revealed_at: 3, invalid_at: null },
  ],
};

describe("graph relationship helpers", () => {
  test("mostConnected picks the highest-degree visible person and returns null for an empty cast", () => {
    expect(mostConnected(graph)).toBe(3);
    expect(mostConnected({ ...graph, characters: [], relationships: [] })).toBeNull();
  });

  test("mostConnected counts distinct people rather than parallel relationship records", () => {
    const dense: Graph = {
      as_of_chapter: 5,
      characters: [1, 2, 3, 4, 5].map((entity_id) => ({
        entity_id, canonical_name: `Person ${entity_id}`, type: "character", revealed_at: 1,
      })),
      relationships: [
        { edge_id: 1, src_entity: 1, dst_entity: 2, rel_type: "family", label: "father of", revealed_at: 1, invalid_at: null },
        { edge_id: 2, src_entity: 1, dst_entity: 2, rel_type: "conflict", label: "argues with", revealed_at: 2, invalid_at: null },
        { edge_id: 3, src_entity: 1, dst_entity: 2, rel_type: "money", label: "owes", revealed_at: 3, invalid_at: null },
        { edge_id: 4, src_entity: 3, dst_entity: 4, rel_type: "social", label: "knows", revealed_at: 1, invalid_at: null },
        { edge_id: 5, src_entity: 3, dst_entity: 5, rel_type: "social", label: "knows", revealed_at: 1, invalid_at: null },
      ],
    };
    expect(mostConnected(dense)).toBe(3);
  });

  test("storyThread finds the shortest spoiler-clamped path with bundled labels", () => {
    const thread = storyThread(graph, 4, 2);
    expect(thread?.nodes.map((node) => node.entity_id)).toEqual([4, 3, 2]);
    expect(thread?.steps).toMatchObject([
      { key: "3:4", from: 4, to: 3,
        labels: [{ text: "knows", revealedAt: 3, src: 3, dst: 4 }] },
      { key: "2:3", from: 3, to: 2,
        labels: [{ text: "brother of", revealedAt: 2, src: 2, dst: 3 }] },
    ]);
  });

  test("storyThread preserves direction and chapter for every label on a bundled hop", () => {
    const multi: Graph = {
      ...graph,
      relationships: [
        ...graph.relationships,
        { edge_id: 14, src_entity: 2, dst_entity: 1, rel_type: "debt", label: "owes money to", revealed_at: 4, invalid_at: null },
      ],
    };
    expect(storyThread(multi, 2, 1)?.steps[0]).toMatchObject({
      from: 2,
      to: 1,
      labels: [
        { text: "father of", revealedAt: 1, src: 1, dst: 2 },
        { text: "owes money to", revealedAt: 4, src: 2, dst: 1 },
      ],
    });
  });

  test("storyThread returns null when two visible people have no path", () => {
    const disconnected: Graph = {
      ...graph,
      characters: [
        ...graph.characters,
        { entity_id: 5, canonical_name: "Isolated", type: "character", revealed_at: 5 },
      ],
    };
    expect(storyThread(disconnected, 1, 5)).toBeNull();
  });
});
