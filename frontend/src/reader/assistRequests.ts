const pending = new Map<string, Promise<unknown>>();

/** Share one paid POST across React StrictMode's development-only effect replay. */
export function assistRequest<T>(key: string, start: () => Promise<T>): Promise<T> {
  const existing = pending.get(key) as Promise<T> | undefined;
  if (existing) return existing;
  const request = start().finally(() => pending.delete(key));
  pending.set(key, request);
  return request;
}
