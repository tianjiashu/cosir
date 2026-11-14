import { useMemo } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { beginFileNavigation } from "@/lib/workspace-files/navigation";
import { notify } from "@/lib/notify";
import { fileInfoQueryOptions } from "@/lib/query/query-options";
import { fileReadProblem } from "@/lib/workspace-files/read-problem";
import { useDockStore } from "@/lib/layout/dock-store";
import type { LineRange } from "@/lib/links/resolve-link";
import { toWorkspacePath } from "@/lib/links/resolve-link";
import { parentsOf, useFilesStore } from "@/lib/workspace-files/workspace-files-store";
import { useReferenceBridge } from "./reference-bridge";

export type OpenFileOptions = {
  /** 打开后选中并居中的行范围。 */
  lines?: LineRange;
  /** PDF 打开后跳到的页码。 */
  page?: number;
  /** 搜索候选保持临时标签；明确引用默认固定标签。 */
  pinned?: boolean;
};

/** opened 已打开标签；missing 已提示缺失；superseded 导航被后续操作撤销。 */
export type OpenFileOutcome = "opened" | "missing" | "superseded";

export type WorkspacePathOpener = {
  /** 当前工作区根目录；链接解析（`lib/links`）以它为基准。 */
  workspaceRoot: string;
  /** 换算成工作区相对路径；不可打开（工作区外、越界）时返回 null。 */
  resolve: (rawPath: string) => string | null;
  /** 在文件面板打开文件标签；`path` 须是工作区相对路径。先确认文件存在，不存在的目标不会变成标签。 */
  openFile: (path: string, options?: OpenFileOptions) => Promise<OpenFileOutcome>;
  /** 显式查找文件名，选择候选后才打开。 */
  searchFilename: (query: string, lines?: LineRange) => void;
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
 * 时不开标签，提示并提供用户主动查找入口；其他读取问题（工作区不可用、无权限、读取出错）照常打开，由
 * 阅读器展示真实错误。目录不预先确认：打开不存在的目录只是在文件树里展开不到内容。
 */
export function useOpenWorkspacePath(): WorkspacePathOpener | null {
  const bridge = useReferenceBridge();
  const workspaceId = bridge?.workspaceId ?? null;
  const workspaceRoot = bridge?.workspaceRoot ?? "";

  const isCurrentWorkspace = bridge?.isCurrentWorkspace;
  const queryClient = useQueryClient();

  return useMemo(() => {
    if (workspaceId === null || !workspaceRoot || !isCurrentWorkspace) return null;
    // 文件面板显示当前工作区的文件状态（`useFilesStore` 按工作区存放），这里只需把它亮出来。
    const showFiles = () => useDockStore.getState().show("files");
    const searchFilename = (query: string, lines?: LineRange) => {
      beginFileNavigation().release();
      showFiles();
      useFilesStore.getState().searchFilename(workspaceId, query, { lines });
    };
    return {
      workspaceRoot,
      searchFilename,
      resolve: (rawPath) => toWorkspacePath(workspaceRoot, rawPath),
      openFile: async (path, options = {}) => {
        const navigation = beginFileNavigation();
        // 用户点的链接不随发起组件卸载失效（虚拟行滚走、切 Session），只有换工作区或被后续导航取代才放弃。
        const isCurrent = () => isCurrentWorkspace(workspaceId) && navigation.isCurrent();
        try {
          // 引用是路径指针，先确认目标还在：文件删除后打开会留下一个坏标签。
          try {
            await queryClient.fetchQuery(fileInfoQueryOptions(workspaceId, path));
          } catch (error) {
            if (!isCurrent()) return "superseded";
            if (fileReadProblem(error) === "missing-file") {
              notify.info(`工作区里没有 ${path}`, {
                description: "文件可能已被删除、移动，或路径写错了。",
                action: {
                  label: "查找同名文件",
                  onClick: () => {
                    const name = path.split("/").at(-1)!;
                    if (isCurrentWorkspace(workspaceId)) searchFilename(name, options.lines);
                    else notify.info(`已离开原工作区，没有查找 ${name}`);
                  },
                },
              });
              return "missing";
            }
          }
          if (!isCurrent()) return "superseded";
          showFiles();
          const files = useFilesStore.getState();
          files.open(workspaceId, path, options.pinned ?? true);
          // 行范围 / PDF 页码记在该文件的阅读位置上，由阅读器消费一次（见 `ReadingPosition.reveal`）。
          if (options.lines) files.position(workspaceId, path, { offset: 0, before: undefined, tail: false, reveal: options.lines });
          if (options.page !== undefined) files.position(workspaceId, path, { revealPage: options.page });
          return "opened";
        } finally {
          navigation.release();
        }
      },
      openDirectory: (path) => {
        beginFileNavigation().release();
        showFiles();
        useFilesStore.getState().patch(workspaceId, {
          browsing: true,
          treeVisible: true,
          expanded: [...parentsOf(path), path],
        });
      },
    };
  }, [queryClient, workspaceId, workspaceRoot, isCurrentWorkspace]);
}
