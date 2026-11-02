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
 * 链接所属的 Task。对话线程、终端会话属于某个 Task，本机地址据此在该 Task 的内置浏览器打开；
 * 没有 Task 的界面（文件面板、配置页）不提供，本机地址交给系统浏览器。
 */
export const LinkTaskContext = createContext<number | null>(null);

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
 * - 本机地址且有所属 Task、运行在桌面宿主里 → 该 Task 的内置浏览器（Agent 也看得到，便于一起验证）。
 * - 其余网页与邮件地址 → 先确认，再经 `window.open` 交给宿主用系统浏览器打开（宿主另有协议白名单）；
 *   本机地址不确认。链接来自模型输出或工作区文件，都不可信，所以外部地址一律在这一处确认。
 */
export function useOpenLink(source: LinkSource, taskId?: number | null): LinkOpener {
  const files = useOpenWorkspacePath();
  const scopedTaskId = useContext(LinkTaskContext);
  const owner = taskId === undefined ? scopedTaskId : taskId;

  return useMemo(() => {
    const workspaceRoot = files?.workspaceRoot ?? null;
    const log = (target: LinkTarget, data: Record<string, unknown> = {}) =>
      void frontendLog("INFO", "link_opened", "打开链接", {
        data: { source, kind: target.kind, ...data },
      });
    const openWeb = async (url: string, local: boolean) => {
      if (local && owner !== null && desktopHost) {
        await openUserTab(owner, url);
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
      resolve: (href, documentDir) => resolveLink(href, { workspaceRoot, documentDir }),
      open: (target, origin) => {
        if (target.kind === "anchor") {
          const markdownRoot = origin?.closest(".aui-md");
          markdownRoot?.querySelector(`#${CSS.escape(target.id)}`)?.scrollIntoView({ block: "center", behavior: "smooth" });
          return;
        }
        if (target.kind === "workspace") {
          // 解析时已要求有工作区，这里的 files 恒存在。
          const data = { path: target.path, lines: target.lines !== undefined, mention: target.mention === true };
          if (target.directory) {
            files!.openDirectory(target.path);
            log(target, { ...data, outcome: "opened" });
            return;
          }
          files!.openFile(target.path, { lines: target.lines, ifMissing: target.mention ? "search" : "notify" }).then(
            (outcome) => log(target, { ...data, outcome }),
            (error: unknown) => notify.error("无法打开文件", { error }),
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
