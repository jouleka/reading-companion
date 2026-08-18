import { beforeEach, describe, expect, test } from "vitest";
import { nextPositionClientSequence, positionClientId } from "./positionClient";

describe("cross-device position client clock", () => {
  beforeEach(() => localStorage.clear());

  test("keeps a stable UUID for this browser profile", () => {
    const first = positionClientId();
    expect(positionClientId()).toBe(first);
    expect(first).toMatch(/^[0-9a-f-]{36}$/i);
  });

  test("persists a strictly increasing client sequence", () => {
    expect(nextPositionClientSequence()).toBe(1);
    expect(nextPositionClientSequence()).toBe(2);
  });
});
