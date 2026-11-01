import { useMemo } from "react";
import { useQueryClient } from "@tanstack/react-query";
import type { WorkspaceReference } from "@/lib/workspace-references/codec";
import { notify } from "@/lib/notify";
import { fileInfoQueryOptions } from "@/lib/query/query-options";
import { fileReadProblem } from "@/lib/workspace-files/read-problem";
import { useDockStore } from "@/lib/layout/dock-store";
import type { LineRange } from "@/lib/links/resolve-link";
import { toWorkspacePath } from "@/lib/links/resolve-link";
import { parentsOf, useFilesStore } from "@/lib/workspace-files/store";
import { useReferenceBridge } from "./reference-bridge";

export type OpenFileOptions = {
  /** 摘录引用的快照：文件面板先展示这段正文，用户可再切到当前文件。 */
  excerpt?: WorkspaceReference;
  /** 打开后选中并居中的行范围。 */
  lines?: LineRange;
  /**
   * 文件不存在时怎么办：`notify`（默认）提示「工作区里没有这个文件」并附「查找同名文件」；`search` 直接在
   * 文件面板按文件名查找——用于只提到文件名、不知道位置的链接（`WorkspaceLink.mention`）。
   */
  ifMissing?: "notify" | "search";
};

/** `opened`：已打开标签；`searched`：文件不存在，已改为按文件名查找；`missing`：文件不存在，只做了提示。 */
export type OpenFileOutcome = "opened" | "searched" | "missing";

export type WorkspacePathOpener = {
  /** 当前工作区根目录；链接解析（`lib/links`）以它为基准。 */
  workspaceRoot: string;
  /** 换算成工作区相对路径；不可打开（工作区外、越界）时返回 null。 */
  resolve: (rawPath: string) => string | null;
  /** 在文件面板打开文件标签；`path` 须是工作区相对路径。先确认文件存在，不存在的目标不会变成标签。 */
  openFile: (path: string, options?: OpenFileOptions) => Promise<OpenFileOutcome>;
  /** 在文件树里展开并定位目录。 */
  openDirectory: (path: string) => void;
};

/**
 * 在文件面板里打开当前工作区路径的唯一入口（对话链接、行内代码、工具卡片、终端路径、引用胶囊、文档链接共用）。
 *
 * 当前工作区来自 `ReferenceBridgeProvider`；没有活动工作区时返回 null，调用方渲染为纯文本。
 *
 * 打开文件前先读一次文件信息（与阅读器同一份查询缓存）确认目标存在：链接来自模型输出、工具结果、终端或
 * 旧消息，路径可能写错、文件可能已被删除或移动；先开标签再让阅读器发现不存在，会留下一个坏标签。文件不存在
 * 时不开标签，按 `ifMissing` 提示或改为查找；其他读取问题（工作区不可用、无权限、读取出错）照常打开，由
 * 阅读器展示真实错误。目录不预先确认：打开不存在的目录只是在文件树里展开不到内容。
 */
export function useOpenWorkspacePath(): WorkspacePathOpener | null {
  const bridge = useReferenceBridge();
  const workspaceId = bridge?.workspaceId ?? null;
  const workspaceRoot = bridge?.workspaceRoot ?? "";

  const queryClient = useQueryClient();

  return useMemo(() => {
    if (workspaceId === null || !workspaceRoot) return null;
    // 文件面板显示当前工作区的文件状态（`useFilesStore` 按工作区存放），这里只需把它亮出来。
    const showFiles = () => useDockStore.getState().show("files");
    const searchFilename = (path: string) => {
      showFiles();
      useFilesStore.getState().searchFilename(workspaceId, path.split("/").at(-1)!);
    };
    return {
      workspaceRoot,
      resolve: (rawPath) => toWorkspacePath(workspaceRoot, rawPath),
      openFile: async (path, options = {}) => {
        // 摘录引用展示的是消息里保存的那段正文，文件已不在也能看，不先确认。
        if (!options.excerpt) {
          try {
            await queryClient.fetchQuery(fileInfoQueryOptions(workspaceId, path));
          } catch (error) {
            if (fileReadProblem(error) === "missing-file") {
              if (options.ifMissing === "search") {
                searchFilename(path);
                return "searched";
              }
              notify.info(`工作区里没有 ${path}`, {
                description: "文件可能已被删除、移动，或路径写错了。",
                action: { label: "查找同名文件", onClick: () => searchFilename(path) },
              });
              return "missing";
            }
          }
        }
        showFiles();
        const files = useFilesStore.getState();
        files.open(workspaceId, path, true);
        if (options.excerpt) files.patch(workspaceId, { reference: options.excerpt });
        // 行范围记在该文件的阅读位置上，由文本预览消费一次（见 `ReadingPosition.reveal`）。
        if (options.lines) files.position(workspaceId, path, { offset: 0, before: undefined, tail: false, reveal: options.lines });
        return "opened";
      },
      openDirectory: (path) => {
        showFiles();
        useFilesStore.getState().patch(workspaceId, {
          browsing: true,
          treeVisible: true,
          expanded: [...parentsOf(path), path],
          reference: null,
        });
      },
    };
  }, [queryClient, workspaceId, workspaceRoot]);
}
