import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  acceptCurrentConfirmation,
  cancelCurrentConfirmation,
  confirmDelete,
  requestConfirmation,
  showErrorDialog,
  useConfirmStore,
} from "@/lib/confirm/confirm-store";
import { UserFacingError } from "@/lib/errors/user-facing-error";

const base = { title: "删除？", description: "不可恢复", confirmLabel: "确认删除" };

describe("requestConfirmation", () => {
  beforeEach(() => useConfirmStore.setState({ queue: [], busy: null, error: null }));

  it("resolves false when the user cancels", async () => {
    const pending = requestConfirmation(base);
    cancelCurrentConfirmation();
    await expect(pending).resolves.toBe(false);
    expect(useConfirmStore.getState().queue).toHaveLength(0);
  });

  it("runs the action once and resolves true after it succeeds", async () => {
    const action = vi.fn().mockResolvedValue(undefined);
    const pending = requestConfirmation({ ...base, action });
    const accepting = acceptCurrentConfirmation();
    // 执行期间再次确认（双击）不会重复执行。
    void acceptCurrentConfirmation();
    expect(useConfirmStore.getState().busy).toBe("primary");
    await accepting;
    await expect(pending).resolves.toBe(true);
    expect(action).toHaveBeenCalledTimes(1);
  });

  it("keeps the dialog open with an error when the action fails, and blocks cancel while busy", async () => {
    let fail: (error: unknown) => void = () => undefined;
    const action = vi.fn(() => new Promise((_, reject) => { fail = reject; }));
    const pending = requestConfirmation({ ...base, action });
    const accepting = acceptCurrentConfirmation();
    cancelCurrentConfirmation();
    expect(useConfirmStore.getState().queue).toHaveLength(1);

    fail(new UserFacingError("该 Agent 正被 Team 引用"));
    await accepting;
    expect(useConfirmStore.getState()).toMatchObject({ busy: null, error: "该 Agent 正被 Team 引用" });

    cancelCurrentConfirmation();
    await expect(pending).resolves.toBe(false);
  });

  it("shows queued requests one at a time in order", async () => {
    const first = requestConfirmation({ ...base, title: "第一个" });
    const second = requestConfirmation({ ...base, title: "第二个" });
    expect(useConfirmStore.getState().queue[0]?.title).toBe("第一个");
    await acceptCurrentConfirmation();
    expect(useConfirmStore.getState().queue[0]?.title).toBe("第二个");
    cancelCurrentConfirmation();
    await expect(first).resolves.toBe(true);
    await expect(second).resolves.toBe(false);
  });

  it("runs only the alternative action when the user picks it", async () => {
    const primary = vi.fn().mockResolvedValue(undefined);
    const alternative = vi.fn().mockResolvedValue(undefined);
    const pending = requestConfirmation({ ...base, action: primary, alternative: { label: "一起删除", action: alternative } });
    const accepting = acceptCurrentConfirmation("alternative");
    expect(useConfirmStore.getState().busy).toBe("alternative");
    await accepting;
    await expect(pending).resolves.toBe(true);
    expect(alternative).toHaveBeenCalledTimes(1);
    expect(primary).not.toHaveBeenCalled();
  });

  it("confirmDelete fills the shared delete wording and spaces Latin targets", async () => {
    const pending = confirmDelete({ target: "MCP 服务「demo」" });
    expect(useConfirmStore.getState().queue[0]).toMatchObject({
      title: "删除 MCP 服务「demo」？",
      confirmLabel: "删除",
      busyLabel: "正在删除…",
      tone: "destructive",
      icon: "delete",
      irreversible: true,
    });
    cancelCurrentConfirmation();
    await expect(pending).resolves.toBe(false);
  });

  it("showErrorDialog is a single-button notice that resolves when acknowledged", async () => {
    const shown = showErrorDialog({ title: "发送失败", message: "后端拒绝" });
    expect(useConfirmStore.getState().queue[0]).toMatchObject({ title: "发送失败", confirmLabel: "知道了", notice: true });
    await acceptCurrentConfirmation();
    await expect(shown).resolves.toBeUndefined();
  });
});

