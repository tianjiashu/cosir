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

import { ExternalLinkRow } from "@/components/conversation/tools/call-views/detail-parts";
import { useMarkdownLinkComponents } from "@/components/links/markdown-links";
import { LinkSessionContext, useOpenLink, type LinkOpener } from "@/components/links/use-open-link";
import { markdownRehypePlugins } from "@/components/markdown/markdown-rendering-config";
import { ReferenceBridgeProvider } from "@/components/workspace-references/reference-bridge";
import { useDockStore } from "@/lib/layout/dock-store";
import { useFilesStore } from "@/lib/workspace-files/workspace-files-store";
import { HttpError } from "@/lib/http/http-error";
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

function captureOpener(sessionId: number | null): LinkOpener {
  let opener: LinkOpener | null = null;
  function Probe() {
    opener = useOpenLink("message");
    return null;
  }
  renderToStaticMarkup(inWorkspace(<LinkSessionContext.Provider value={sessionId}><Probe /></LinkSessionContext.Provider>));
  return opener!;
}

const windowOpen = vi.fn();
beforeEach(() => {
  vi.stubGlobal("window", { open: windowOpen });
  useFilesStore.setState({ byWorkspace: {} });
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
    await vi.waitFor(() => expect(useFilesStore.getState().byWorkspace[7]?.active).toBe("src/a.ts"));
    expect(useFilesStore.getState().byWorkspace[7].positions["src/a.ts"].reveal).toEqual({ start: 4, end: 6 });
    expect(useDockStore.getState().panel).toBe("files");
  });

  it("filename searches never probe root files; missing addresses notify", async () => {
    const opener = captureOpener(3);
    opener.open({ kind: "filename-search", query: "session.ts", lines: { start: 42, end: 42 } });
    await vi.waitFor(() => expect(useFilesStore.getState().byWorkspace[7]?.searchRequest?.query).toBe("session.ts"));
    expect(getFileInfo).not.toHaveBeenCalled();
    expect(useFilesStore.getState().byWorkspace[7].searchRequest?.navigation?.lines).toEqual({ start: 42, end: 42 });
    opener.open(opener.resolve("src/gone.ts")!);
    await vi.waitFor(() => expect(notifyInfo).toHaveBeenCalledWith("工作区里没有 src/gone.ts", expect.anything()));
    expect(useFilesStore.getState().byWorkspace[7].tabs).toEqual([]);
  });

  it("opens local servers in the session's built-in browser without confirmation", async () => {
    openUserTab.mockResolvedValue("tab-1");
    const opener = captureOpener(3);
    opener.open(opener.resolve("http://localhost:5173")!);
    // 新开的标签由 `openUserTab` 摆到面前（`revealBrowserTab`），链接这一层不再自己显示面板。
    await vi.waitFor(() => expect(openUserTab).toHaveBeenCalledWith(3, "http://localhost:5173"));
    expect(confirm).not.toHaveBeenCalled();
    expect(windowOpen).not.toHaveBeenCalled();
  });

  it("hands local servers to the system browser when there is no session", async () => {
    const opener = captureOpener(null);
    opener.open(opener.resolve("http://localhost:5173")!);
    await vi.waitFor(() => expect(windowOpen).toHaveBeenCalledWith("http://localhost:5173", "_blank", "noopener,noreferrer"));
    expect(openUserTab).not.toHaveBeenCalled();
  });

  // 目的：Agent 写的 report.html 点开就是渲染结果，局域网设备与本机开发服务同等对待；潜在缺陷：只能看源码、工作区外
  // 点不了，或局域网地址弹外链确认后交给系统浏览器。
  it("opens local HTML files and LAN servers in the session's built-in browser", async () => {
    openUserTab.mockResolvedValue("tab-1");
    const opener = captureOpener(3);
    opener.open(opener.resolve("out/report.html")!);
    await vi.waitFor(() => expect(openUserTab).toHaveBeenCalledWith(3, "file:///repo/out/report.html"));
    opener.open(opener.resolve("/Users/x/测试结果/a.html")!);
    await vi.waitFor(() => expect(openUserTab).toHaveBeenCalledWith(3, "file:///Users/x/%E6%B5%8B%E8%AF%95%E7%BB%93%E6%9E%9C/a.html"));
    opener.open(opener.resolve("http://192.168.1.5:5173")!);
    await vi.waitFor(() => expect(openUserTab).toHaveBeenCalledWith(3, "http://192.168.1.5:5173"));
    expect(confirm).not.toHaveBeenCalled();
    expect(windowOpen).not.toHaveBeenCalled();
    expect(useDockStore.getState().panel).toBeNull();
  });

  // 目的：没有所属 Session 的界面不给点了没反应的入口；潜在缺陷：file 地址交给系统浏览器被宿主拦下，静默失败。
  it("falls back to the file panel for local HTML when there is no session", () => {
    const opener = captureOpener(null);
    expect(opener.resolve("out/report.html")).toEqual({ kind: "workspace", path: "out/report.html", directory: false });
    expect(opener.resolve("/Users/x/a.html")).toBeNull();
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

  it("titles local HTML links by where they open", () => {
    // 文件面板的 Markdown 没有所属 Session，本机网页退回文件面板。
    expect(renderMarkdown("[r](out/report.html)")).toContain('title="在文件面板打开 out/report.html"');
    function InSession() {
      const components = useMarkdownLinkComponents("message");
      return <Streamdown mode="static" rehypePlugins={markdownRehypePlugins} components={components}>{"[r](out/report.html)"}</Streamdown>;
    }
    const html = renderToStaticMarkup(inWorkspace(<LinkSessionContext.Provider value={3}><InSession /></LinkSessionContext.Provider>));
    expect(html).toContain('title="在内置浏览器打开 /repo/out/report.html"');
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

it("renders search affordances separately and treats removed reference schemes as plain text", () => {
  const html = renderMarkdown("`API.md` `./guide.md` [file](cosir-file:src/a.ts) [dir](cosir-dir:src) [snippet](cosir-snippet:src/a.ts#L2-L3)", { documentDir: "docs" });
  expect(html).toContain('aria-label="查找文件 API.md"');
  expect(html).not.toContain('title="在文件面板打开 API.md"');
  expect(html).toContain('title="在文件面板打开 docs/guide.md"');
  // 引用改成正文里的胶囊标签，不再是 Markdown 链接：旧 scheme 渲染为纯文本，不带地址。
  expect(html).not.toContain("<a");
  expect(html).not.toContain("cosir-");
});

it("discards late opens after a newer click or closing the dock", async () => {
  let finish!: () => void;
  getFileInfo.mockImplementationOnce(() => new Promise((resolve) => { finish = () => resolve({ path: "slow.ts", kind: "text" }); }));
  const opener = captureOpener(3);
  opener.open(opener.resolve("slow.ts")!);
  opener.open(opener.resolve("src/a.ts")!);
  await vi.waitFor(() => expect(useFilesStore.getState().byWorkspace[7]?.active).toBe("src/a.ts"));
  finish();
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(useFilesStore.getState().byWorkspace[7].active).toBe("src/a.ts");
  getFileInfo.mockImplementationOnce(() => new Promise((resolve) => { finish = () => resolve({ path: "slow.ts", kind: "text" }); }));
  opener.open(opener.resolve("slow-again.ts")!);
  useDockStore.getState().close();
  finish();
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(useDockStore.getState().panel).toBeNull();
  expect(useFilesStore.getState().byWorkspace[7].active).toBe("src/a.ts");
});

it("keeps code-styled explicit link labels free of nested search buttons", () => {
  const html = renderMarkdown("[`API.md`](docs/brand/API.md)");
  expect(html).toContain('href="docs/brand/API.md"');
  expect(html).not.toContain("<button");
});

it("selecting the already active file cancels an older pending link", async () => {
  useFilesStore.getState().open(7, "src/a.ts");
  let finish!: () => void;
  getFileInfo.mockImplementationOnce(() => new Promise((resolve) => { finish = () => resolve({ path: "slow.ts", kind: "text" }); }));
  const opener = captureOpener(3);
  opener.open(opener.resolve("slow.ts")!);
  useFilesStore.getState().open(7, "src/a.ts", true);
  finish();
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(useFilesStore.getState().byWorkspace[7].active).toBe("src/a.ts");
});

describe("link icons", () => {
  function renderLinks(icons: boolean, text: string) {
    function Doc() {
      const components = useMarkdownLinkComponents("message", { icons });
      return <Streamdown mode="static" rehypePlugins={markdownRehypePlugins} components={components}>{text}</Streamdown>;
    }
    return renderToStaticMarkup(inWorkspace(<Doc />));
  }

  it("shows object icons only when the surface asks, so reading a document sends no requests", () => {
    const text = "[pr](https://github.com/a/b/pull/1) [file](src/a.ts)";
    expect(renderLinks(true, text).match(/<svg/g)).toHaveLength(2);
    expect(renderLinks(false, text)).not.toContain("<svg");
  });

  it("keeps inline code literal, without an object icon", () => {
    const html = renderLinks(true, "`src/a.ts`");
    expect(html).toContain("workspace-code-link");
    expect(html).not.toContain("<svg");
  });

  it("gives anchors no icon, because they do not point at an object", () => {
    expect(renderLinks(true, "[note](#fn-1)")).not.toContain("<svg");
  });
});

describe("ExternalLinkRow", () => {
  it("does not hand the link straight to the browser", () => {
    const html = renderToStaticMarkup(inWorkspace(<ExternalLinkRow rawUrl="https://example.com/a" title="Example" />));
    expect(html).toContain('href="https://example.com/a"');
    expect(html).not.toContain("target=");
  });

  it("leaves addresses that are not web pages as text", () => {
    const html = renderToStaticMarkup(inWorkspace(<ExternalLinkRow rawUrl="javascript:alert(1)" title="bad" />));
    expect(html).not.toContain("<a");
    expect(html).toContain("bad");
  });
});
