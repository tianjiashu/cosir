import { FileXIcon, FolderXIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { EmptyState, ErrorState } from "@/components/ui/state-panel";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { fileReadProblem } from "@/lib/workspace-files/read-problem";
import { useFilesStore } from "@/lib/workspace-files/workspace-files-store";

/**
 * 阅读器读不到文件时的统一展示（分类规则见 `lib/workspace-files/read-problem.ts`）。
 *
 * 「文件不存在」「工作区不存在」是目标的状态，用中性空状态：说明发生了什么并给出下一步（查找同名文件、
 * 关闭标签），不给「重新读取」——文件恢复时文件监听会让这里自动刷新。只有真正的读取故障才用错误样式与重试。
 */
export function FileReadProblem({ workspaceId, path, error, onRetry }: {
  workspaceId: number;
  path: string;
  error: unknown;
  onRetry: () => void;
}) {
  const problem = fileReadProblem(error);
  if (problem === "missing-workspace")
    return (
      <EmptyState className="m-4" icon={<FolderXIcon />} title="工作区已不存在"
        description="工作区目录已被删除或移动，文件面板里的内容都无法读取。" />
    );
  if (problem === "missing-file") {
    const name = path.split("/").at(-1)!;
    const { searchFilename, close } = useFilesStore.getState();
    return (
      <EmptyState
        className="m-4"
        icon={<FileXIcon />}
        title="文件不存在"
        description={<><span className="font-mono break-all">{path}</span> 已被删除、移动，或从未存在。文件恢复时这里会自动刷新。</>}
        action={(
          <div className="flex justify-center gap-2">
            <Button size="sm" variant="outline" onClick={() => searchFilename(workspaceId, name)}>查找同名文件</Button>
            <Button size="sm" variant="ghost" onClick={() => close(workspaceId, path)}>关闭标签</Button>
          </div>
        )}
      />
    );
  }
  return <ErrorState className="m-4" message={userErrorMessage(error, "文件读取失败")} onRetry={onRetry} retryLabel="重新读取" />;
}
