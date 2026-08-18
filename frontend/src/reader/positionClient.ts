const CLIENT_ID_KEY = "litlet.position.client-id";
const CLIENT_SEQUENCE_KEY = "litlet.position.client-sequence";
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

let memoryClientId: string | null = null;
let memorySequence = 0;

function newClientId(): string {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  const bytes = new Uint8Array(16);
  globalThis.crypto?.getRandomValues?.(bytes);
  if (!bytes.some(Boolean)) for (let index = 0; index < bytes.length; index += 1) bytes[index] = Math.random() * 256;
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export function positionClientId(): string {
  try {
    const stored = localStorage.getItem(CLIENT_ID_KEY);
    if (stored && UUID_PATTERN.test(stored)) return stored;
    const created = newClientId();
    localStorage.setItem(CLIENT_ID_KEY, created);
    return created;
  } catch {
    memoryClientId ??= newClientId();
    return memoryClientId;
  }
}

export function nextPositionClientSequence(): number {
  try {
    const stored = Number(localStorage.getItem(CLIENT_SEQUENCE_KEY) ?? 0);
    const next = Number.isSafeInteger(stored) && stored >= 0 ? stored + 1 : 1;
    localStorage.setItem(CLIENT_SEQUENCE_KEY, String(next));
    return next;
  } catch {
    memorySequence += 1;
    return memorySequence;
  }
}
