import { renderToStaticMarkup } from "react-dom/server";
import { Streamdown } from "streamdown";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { confirm, openUserTab } = vi.hoisted(() => ({
  confirm: vi.fn<() => Promise<boolean>>(),
  openUserTab: vi.fn<() => Promise<string>>(),
}));
vi.mock("@/lib/confirm/confirm-store", () => ({ requestConfirmation: confirm }));
vi.mock("@/lib/browser/open-tab", () => ({ openUserTab }));
vi.mock("@/lib/desktop-host", () => ({ desktopHost: {} }));
vi.mock("@/lib/logging/frontend-log", () => ({ frontendLog: vi.fn() }));
// 打开文件前会先读文件信息确认存在：src/a.ts 存在，其余路径按后端的 FILE_NOT_FOUND 返回。
const getFileInfo = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api/workspace-files", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api/workspace-files")>()),
  getFileInfo,
}));
const notifyInfo = vi.hoisted(() => vi.fn());
vi.mock("@/lib/notify", () => ({ notify: { info: notifyInfo, error: vi.fn(), success: vi.fn() } }));

import { useMarkdownLinkComponents } from "@/components/links/markdown-links";
import { LinkTaskContext, useOpenLink, type LinkOpener } from "@/components/links/use-open-link";
import { markdownRehypePlugins } from "@/components/markdown-rendering-config";
import { ReferenceBridgeProvider } from "@/components/workspace-references/reference-bridge";
import { useDockStore } from "@/lib/layout/dock-store";
import { useFilesStore } from "@/lib/workspace-files/store";
import { HttpError } from "@/lib/http/errors";
import { createQueryClient } from "@/lib/query/query-client";
import { QueryClientProvider } from "@tanstack/react-query";

function inWorkspace(node: React.ReactNode) {
  return (
    <QueryClientProvider client={createQueryClient()}>
      <ReferenceBridgeProvider target={null} workspaceId={7} workspaceRoot="/repo" workspaceName="repo">
        {node}
      </ReferenceBridgeProvider>
    </QueryClientProvider>
  );
}

function captureOpener(taskId: number | null): LinkOpener {
  let opener: LinkOpener | null = null;
  function Probe() {
    opener = useOpenLink("message");
    return null;
  }
  renderToStaticMarkup(inWorkspace(<LinkTaskContext.Provider value={taskId}><Probe /></LinkTaskContext.Provider>));
  return opener!;
}

const windowOpen = vi.fn();
beforeEach(() => {
  vi.stubGlobal("window", { open: windowOpen });
  useFilesStore.setState({ sessions: {} });
  useDockStore.setState({ panel: null });
  getFileInfo.mockImplementation(async (_id: number, path: string) => {
    if (path === "src/a.ts") return { path, name: "a.ts", kind: "text", version: "v1" };
    throw new HttpError("文件不存在", { status: 404, code: "FILE_NOT_FOUND" });
  });
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("useOpenLink", () => {
  it("opens workspace files in the file panel at the linked lines", async () => {
    const opener = captureOpener(3);
    opener.open(opener.resolve("src/a.ts#L4-L6")!);
    await vi.waitFor(() => expect(useFilesStore.getState().sessions[7]?.active).toBe("src/a.ts"));
    expect(useFilesStore.getState().sessions[7].positions["src/a.ts"].reveal).toEqual({ start: 4, end: 6 });
    expect(useDockStore.getState().panel).toBe("files");
  });

  it("does not open a tab for a missing file: a mention searches, an address notifies", async () => {
    const opener = captureOpener(3);
    opener.open({ kind: "workspace", path: "task.ts", directory: false, mention: true });
    await vi.waitFor(() => expect(useFilesStore.getState().sessions[7]?.searchRequest?.query).toBe("task.ts"));
    opener.open(opener.resolve("src/gone.ts")!);
    await vi.waitFor(() => expect(notifyInfo).toHaveBeenCalledWith("工作区里没有 src/gone.ts", expect.anything()));
    expect(useFilesStore.getState().sessions[7].tabs).toEqual([]);
  });

  it("opens local servers in the task's built-in browser without confirmation", async () => {
    openUserTab.mockResolvedValue("tab-1");
    const opener = captureOpener(3);
    opener.open(opener.resolve("http://localhost:5173")!);
    await vi.waitFor(() => expect(useDockStore.getState().panel).toBe("browser"));
    expect(openUserTab).toHaveBeenCalledWith(3, "http://localhost:5173");
    expect(confirm).not.toHaveBeenCalled();
    expect(windowOpen).not.toHaveBeenCalled();
  });

  it("hands local servers to the system browser when there is no task", async () => {
    const opener = captureOpener(null);
    opener.open(opener.resolve("http://localhost:5173")!);
    await vi.waitFor(() => expect(windowOpen).toHaveBeenCalledWith("http://localhost:5173", "_blank", "noopener,noreferrer"));
    expect(openUserTab).not.toHaveBeenCalled();
  });

  it("confirms external links before opening them", async () => {
    confirm.mockResolvedValueOnce(false).mockResolvedValueOnce(true);
    const opener = captureOpener(3);
    const target = opener.resolve("https://example.com")!;
    opener.open(target);
    await vi.waitFor(() => expect(confirm).toHaveBeenCalledTimes(1));
    expect(windowOpen).not.toHaveBeenCalled();
    opener.open(target);
    await vi.waitFor(() => expect(windowOpen).toHaveBeenCalledWith("https://example.com", "_blank", "noopener,noreferrer"));
  });
});

function renderMarkdown(text: string, options?: { documentDir?: string; webOnly?: boolean }) {
  function Doc() {
    const components = useMarkdownLinkComponents("document", options);
    return <Streamdown mode="static" rehypePlugins={markdownRehypePlugins} components={components}>{text}</Streamdown>;
  }
  return renderToStaticMarkup(inWorkspace(<Doc />));
}

describe("markdown links", () => {
  it("keeps relative hrefs intact through sanitizing and links them to the file panel", () => {
    const html = renderMarkdown("[a](src/a.ts#L42) [up](../up.md)", { documentDir: "docs/plan" });
    expect(html).toContain('href="src/a.ts#L42"');
    expect(html).toContain('title="在文件面板打开 docs/plan/src/a.ts"');
    expect(html).toContain('title="在文件面板打开 docs/up.md"');
  });

  it("renders unsafe or unresolvable links as plain text", () => {
    const html = renderMarkdown("[x](javascript:alert(1)) [y](/etc/passwd) [z](cosir-file:a.ts)", { webOnly: true });
    expect(html).not.toContain("<a");
    expect(html).toContain(">x<");
  });

  it("makes file paths in inline code clickable", () => {
    expect(renderMarkdown("see `src/app.ts:42`")).toContain('title="在文件面板打开 src/app.ts"');
    expect(renderMarkdown("run `npm test`")).not.toContain("<button");
  });
});
