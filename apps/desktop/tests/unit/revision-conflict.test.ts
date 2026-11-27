import { beforeEach, describe, expect, it, vi } from "vitest";

import { HttpError } from "@/lib/http/http-error";
import { saveResolvingConflicts } from "@/lib/configuration/revision-conflict";

type Choice = "primary" | "alternative" | "cancel";
const confirmation = vi.hoisted(() => ({ choice: "primary" as Choice, asked: 0 }));
vi.mock("@/lib/confirm/confirm-store", () => ({
  requestConfirmation: async (request: { action?: () => Promise<unknown>; alternative?: { action: () => Promise<unknown> } }) => {
    confirmation.asked += 1;
    if (confirmation.choice === "cancel") return false;
    await (confirmation.choice === "primary" ? request.action : request.alternative?.action)?.();
    return true;
  },
}));
vi.mock("@/lib/logging/frontend-log", () => ({ frontendLog: vi.fn() }));

const conflict = (current: string) => new HttpError("配置文件已在别处修改，保存未生效", {
  status: 409, code: "configuration_revision_conflict", detail: { current_revision: current },
});

describe("saveResolvingConflicts", () => {
  beforeEach(() => { confirmation.asked = 0; });

  it("没有冲突时不打扰用户", async () => {
    const save = vi.fn(async (base: string | null) => `saved@${base}`);
    await expect(saveResolvingConflicts("文档", "r0", save)).resolves.toEqual({ saved: "saved@r0" });
    expect(confirmation.asked).toBe(0);
  });

  it("覆盖：以后端读到的当前版本为基准重存", async () => {
    confirmation.choice = "primary";
    const save = vi.fn<(base: string | null) => Promise<string>>()
      .mockRejectedValueOnce(conflict("r1"))
      .mockResolvedValueOnce("ok");
    await expect(saveResolvingConflicts("文档", "r0", save)).resolves.toEqual({ saved: "ok" });
    expect(save.mock.calls).toEqual([["r0"], ["r1"]]);
  });

  it("载入：交给调用方丢弃草稿重读", async () => {
    confirmation.choice = "alternative";
    const save = vi.fn().mockRejectedValueOnce(conflict("r1"));
    await expect(saveResolvingConflicts("文档", "r0", save)).resolves.toBe("reload");
    expect(save).toHaveBeenCalledTimes(1);
  });

  it("取消与其他失败原样抛出", async () => {
    confirmation.choice = "cancel";
    const stale = conflict("r1");
    await expect(saveResolvingConflicts("文档", "r0", vi.fn().mockRejectedValueOnce(stale))).rejects.toBe(stale);
    const other = new HttpError("同名配置已存在", { status: 409, code: "configuration_conflict" });
    await expect(saveResolvingConflicts("文档", "r0", vi.fn().mockRejectedValueOnce(other))).rejects.toBe(other);
    expect(confirmation.asked).toBe(1);
  });
});
