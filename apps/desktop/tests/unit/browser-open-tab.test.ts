import { afterEach, describe, expect, it, vi } from "vitest";

const { requestJson, open } = vi.hoisted(() => ({ requestJson: vi.fn(), open: vi.fn() }));
vi.mock("@/lib/http/client", () => ({ requestJson }));
vi.mock("@/lib/desktop-host", () => ({ desktopHost: { browser: { open } } }));
vi.mock("@/lib/logging/frontend-log", () => ({ frontendLog: vi.fn() }));

import { openUserTab } from "@/lib/browser/open-tab";
import { useBrowserPanelStore } from "@/lib/browser/panel-store";

afterEach(() => {
  vi.clearAllMocks();
  useBrowserPanelStore.setState({ taskId: null, selected: {} });
});

describe("openUserTab", () => {
  it("按 Task 现读后端给的上限与下载目录，转成宿主契约的字段名交给宿主，并把面板切到新标签", async () => {
    requestJson.mockResolvedValue({ limits: { per_task: 5, total: 20 }, download_dir: "/ws/.cosir/downloads" });
    open.mockResolvedValue("t4");

    await expect(openUserTab(7, "https://example.com")).resolves.toBe("t4");

    expect(requestJson).toHaveBeenCalledWith("/tasks/7/browser-context");
    expect(open).toHaveBeenCalledWith(7, { limits: { perTask: 5, total: 20 }, downloadDir: "/ws/.cosir/downloads" }, "https://example.com");
    expect(useBrowserPanelStore.getState()).toMatchObject({ taskId: 7, selected: { 7: "t4" } });
  });

  // 潜在缺陷：读不到上下文时仍带着空上限去开标签，绕过配置中心的约束。
  it("读取上下文失败时不开标签，错误交给调用方提示", async () => {
    requestJson.mockRejectedValue(new Error("Task 不存在"));
    await expect(openUserTab(7)).rejects.toThrow("Task 不存在");
    expect(open).not.toHaveBeenCalled();
  });
});
