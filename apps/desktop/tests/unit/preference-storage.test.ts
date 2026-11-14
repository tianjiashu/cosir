import { describe, expect, it, vi } from "vitest";

const notifyError = vi.fn();
vi.mock("@/lib/notify", () => ({ notify: { error: notifyError } }));
vi.mock("@/lib/logging/frontend-log", () => ({ frontendLog: vi.fn(async () => undefined) }));

const { preferenceStorage } = await import("@/lib/storage/preference-storage");

describe("preference storage", () => {
  it("treats unavailable storage as no preference and reports a failed write once", () => {
    const fail = () => { throw new Error("storage unavailable"); };
    Object.assign(globalThis, { window: { localStorage: { getItem: fail, setItem: fail, removeItem: fail } } });
    const storage = preferenceStorage<{ value: number }>();
    expect(storage.getItem("cosir:test")).toBeNull();
    expect(() => storage.setItem("cosir:test", { state: { value: 1 }, version: 0 })).not.toThrow();
    expect(() => storage.setItem("cosir:test", { state: { value: 2 }, version: 0 })).not.toThrow();
    expect(notifyError).toHaveBeenCalledTimes(1);
  });
});
