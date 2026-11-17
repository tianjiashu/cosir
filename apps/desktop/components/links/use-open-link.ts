import { createContext, useContext, useMemo } from "react";

import { useOpenWorkspacePath } from "@/components/workspace-references/use-open-workspace-path";
import { openUserTab } from "@/lib/browser/open-tab";
import { requestConfirmation } from "@/lib/confirm/confirm-store";
import { desktopHost } from "@/lib/desktop-host";
import { FILE_EXITS } from "@/lib/desktop-host/file-exits";
import type { TransportDeliverable } from "@/lib/assistant/contract";
import { inlineCodeLink, resolveDeliverable, resolveLink, type LinkTarget } from "@/lib/links/resolve-link";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";

/** 链接出现在哪类界面；只用于日志排查。 */
export type LinkSource = "message" | "document" | "terminal" | "tool" | "configuration";

/**
 * 链接所属的 Session。对话线程、终端会话属于某个 Session，本机与局域网地址、本机网页文件据此在该 Session 的内置浏览器打开；
 * 没有 Session 的界面（文件面板、配置页）不提供：本机地址交给系统浏览器，本机网页文件退回文件面板。
 */
export const LinkSessionContext = createContext<number | null>(null);

/**
 * 一种「打开方式」：解析、描述与打开都归它，识别链接的界面（Markdown 链接组件、终端等）只调用它。
 * 不同界面要不同的打开方式时换一个打开器（见 `useWebOnlyLinkOpener`、Skill 详情页的打开器），不给识别层加开关。
 */
export type LinkOpener = {
  /** 按当前工作区（与文件面板一致）解析；`documentDir` 只在工作区文档里给出。 */
  resolve: (href: string, documentDir?: string) => LinkTarget | null;
  /** 交付记录的打开目标（见 `resolveDeliverable`）；当前界面打不开时返回 null。 */
  resolveDeliverable: (deliverable: TransportDeliverable) => LinkTarget | null;
  /** 行内代码里的文件路径能否打开、打开什么；不认路径的打开方式返回 null。 */
  resolveCode: (text: string, documentDir?: string) => LinkTarget | null;
  /** 悬停提示：点了会在哪里打开。 */
  describe: (target: LinkTarget) => string | undefined;
  /** 当前工作区根目录；没有工作区时为 null，工作区路径一律不可打开。 */
  workspaceRoot: string | null;
  /** `origin` 是被点的元素：页内锚点只在它所在的那篇 Markdown 里找目标（多条消息的脚注 id 会重名）。 */
  open: (target: LinkTarget, origin?: Element) => void;
};

/**
 * 打开链接的唯一入口：各界面只负责识别链接，解析交给 `resolveLink`，分派与安全策略都在这里。
 *
 * - 页内锚点 → 在链接所在的那篇 Markdown 里滚动到目标（脚注来回跳转）。
 * - 工作区路径 → 文件面板（带行号时选中并居中）；目录在文件树里展开。
 * - 「在文件夹中显示」（只来自交付记录）→ 宿主的系统文件管理器；浏览器开发模式没有宿主，解析时就不可点。
 * - 本机与局域网地址、本机网页文件，且有所属 Session、运行在桌面宿主里 → 该 Session 的内置浏览器（Agent 也看得到，
 *   便于一起验证）。打不开内置浏览器时本机网页文件退回文件面板（工作区外的在 `resolve` 里就不可点）。
 * - 其余网页与邮件地址 → 先确认，再经 `window.open` 交给宿主用系统浏览器打开（宿主另有协议白名单）；
 *   本机地址不确认。链接来自模型输出或工作区文件，都不可信，所以外部地址一律在这一处确认。
 */
export function useOpenLink(source: LinkSource, sessionId?: number | null): LinkOpener {
  const files = useOpenWorkspacePath();
  const scopedSessionId = useContext(LinkSessionContext);
  const owner = sessionId === undefined ? scopedSessionId : sessionId;

  return useMemo(() => {
    const workspaceRoot = files?.workspaceRoot ?? null;
    const log = (target: LinkTarget, data: Record<string, unknown> = {}) =>
      void frontendLog("INFO", "link_opened", "打开链接", {
        data: { source, kind: target.kind, ...data },
      });
    // 能在内置浏览器打开的 Session：有所属 Session 且运行在桌面宿主里（浏览器开发模式没有宿主）。
    const browserSession = owner !== null && desktopHost ? owner : null;
    const openWeb = async (url: string, local: boolean) => {
      if (local && browserSession !== null) {
        await openUserTab(browserSession, url);
        return "browser";
      }
      if (!local && !(await requestConfirmation({ title: "打开外部链接？", description: url, confirmLabel: "打开" }))) {
        return "declined";
      }
      window.open(url, "_blank", "noopener,noreferrer");
      return "system";
    };
    // 打不开内置浏览器时本机网页退回文件面板（工作区外的没有退路）；没有宿主时「在文件夹中显示」不可用。
    const reachable = (target: LinkTarget | null): LinkTarget | null =>
      target?.kind === "page" && browserSession === null ? target.workspace
        : target?.kind === "reveal" && !desktopHost ? null : target;
    return {
      workspaceRoot,
      resolve: (href, documentDir) => reachable(resolveLink(href, { workspaceRoot, documentDir })),
      resolveDeliverable: (deliverable) => reachable(resolveDeliverable(deliverable, { workspaceRoot })),
      resolveCode: (text, documentDir) => inlineCodeLink(text, { workspaceRoot, documentDir }),
      describe: describeLinkTarget,
      open: (target, origin) => {
        if (target.kind === "anchor") {
          const markdownRoot = origin?.closest(".aui-md");
          markdownRoot?.querySelector(`#${CSS.escape(target.id)}`)?.scrollIntoView({ block: "center", behavior: "smooth" });
          return;
        }
        if (target.kind === "filename-search") {
          files!.searchFilename(target.query, target.lines);
          log(target, { query: target.query, outcome: "searched" });
          return;
        }
        if (target.kind === "workspace") {
          // 解析时已要求有工作区，这里的 files 恒存在。
          const data = { path: target.path, lines: target.lines !== undefined };
          if (target.directory) {
            files!.openDirectory(target.path);
            log(target, { ...data, outcome: "opened" });
            return;
          }
          files!.openFile(target.path, { lines: target.lines }).then(
            (outcome) => log(target, { ...data, outcome }),
            (error: unknown) => notify.error("无法打开文件", { error }),
          );
          return;
        }
        if (target.kind === "reveal") {
          // 与胶囊菜单、文件面板空态同一个出口（失败提示与日志在那里）；`resolve` 已保证有宿主。
          FILE_EXITS.find((exit) => exit.id === "reveal")!.run(target.path);
          log(target);
          return;
        }
        if (target.kind === "page") {
          // `resolve` 已把打不开内置浏览器时的本机网页换成文件面板退路，到这里一定有 browserSession。
          openUserTab(browserSession!, target.url).then(
            () => log(target, { path: target.path, via: "browser" }),
            (error: unknown) => notify.error("无法打开网页", { error }),
          );
          return;
        }
        openWeb(target.url, target.local).then(
          (via) => log(target, { local: target.local, via }),
          (error: unknown) => notify.error("无法打开链接", { error }),
        );
      },
    };
  }, [files, owner, source]);
}

function describeLinkTarget(target: LinkTarget): string | undefined {
  if (target.kind === "workspace") return `在文件面板打开 ${target.path || "."}`;
  if (target.kind === "filename-search") return `查找文件 ${target.query}`;
  if (target.kind === "page") return `在内置浏览器打开 ${target.path}`;
  if (target.kind === "web") return target.url;
  if (target.kind === "reveal") return `在文件夹中显示 ${target.path}`;
  return undefined;
}

/**
 * 只认网页的打开方式：用于与当前工作区无关的文本（Agent 提示词等），其中的相对链接与行内代码路径
 * 不指向当前工作区，按纯文本显示；网页与页内锚点照常交给 `useOpenLink`。
 */
export function useWebOnlyLinkOpener(source: LinkSource): LinkOpener {
  const opener = useOpenLink(source);
  return useMemo(() => ({
    ...opener,
    workspaceRoot: null,
    resolve: (href) => resolveLink(href, { workspaceRoot: null }),
    resolveDeliverable: (deliverable) => (deliverable.type === "url" ? opener.resolveDeliverable(deliverable) : null),
    resolveCode: () => null,
  }), [opener]);
}
