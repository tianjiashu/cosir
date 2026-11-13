import { createContext, useContext, useMemo } from "react";

import { useOpenWorkspacePath } from "@/components/workspace-references/use-open-workspace-path";
import { openUserTab } from "@/lib/browser/open-tab";
import { requestConfirmation } from "@/lib/confirm/confirm-store";
import { desktopHost } from "@/lib/desktop-host";
import { resolveLink, type LinkTarget } from "@/lib/links/resolve-link";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";

/** 链接出现在哪类界面；只用于日志排查。 */
export type LinkSource = "message" | "document" | "terminal" | "tool" | "configuration";

/**
 * 链接所属的 Session。对话线程、终端会话属于某个 Session，本机与局域网地址、本机网页文件据此在该 Session 的内置浏览器打开；
 * 没有 Session 的界面（文件面板、配置页）不提供：本机地址交给系统浏览器，本机网页文件退回文件面板。
 */
export const LinkSessionContext = createContext<number | null>(null);

export type LinkOpener = {
  /** 按当前工作区（与文件面板一致）解析；`documentDir` 只在工作区文档里给出。 */
  resolve: (href: string, documentDir?: string) => LinkTarget | null;
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
    return {
      workspaceRoot,
      resolve: (href, documentDir) => {
        const target = resolveLink(href, { workspaceRoot, documentDir });
        return target?.kind === "page" && browserSession === null ? target.workspace : target;
      },
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
