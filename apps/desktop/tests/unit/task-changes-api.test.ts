import { afterEach, describe, expect, it, vi } from "vitest";

import { getTaskChanges, keepTaskChanges, revertTaskChanges } from "@/lib/api/changes";

const changeSet = { task_id: 12, files: [] };

describe("task ChangeSet API", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("loads task-scoped changes through the shared HTTP client and forwards AbortSignal", async () => {
    const controller = new AbortController();
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(changeSet), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(getTaskChanges(12, { signal: controller.signal })).resolves.toEqual(changeSet);

    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls[0][0]).toBe("http://127.0.0.1:8000/tasks/12/changes");
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ signal: controller.signal, cache: "no-store" });
  });

  it("uses change_ids for both one item and a batch", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ task_id: 12, results: [], change_set: changeSet }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ task_id: 12, results: [], change_set: changeSet }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    await keepTaskChanges(12, ["chg_one"]);
    await keepTaskChanges(12, ["chg_one", "chg_two"]);

    expect(fetchMock.mock.calls[0][0]).toBe("http://127.0.0.1:8000/tasks/12/changes/keep");
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({ change_ids: ["chg_one"] });
    expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toEqual({ change_ids: ["chg_one", "chg_two"] });
  });

  it("posts Revert to the task endpoint and forwards its per-item result and canonical ChangeSet", async () => {
    const response = {
      task_id: 12,
      results: [{ change_id: "chg_one", outcome: "reverted" }],
      change_set: { ...changeSet, files: [{ change_id: "chg_one", status: "reverted" }] },
    };
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(response), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(revertTaskChanges(12, ["chg_one"])).resolves.toEqual(response);

    expect(fetchMock.mock.calls[0][0]).toBe("http://127.0.0.1:8000/tasks/12/changes/revert");
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({ change_ids: ["chg_one"] });
  });
});
