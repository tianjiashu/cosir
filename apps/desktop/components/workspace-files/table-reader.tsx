import { useEffect, useRef, type ReactNode } from "react";

import { RenderIsolationBoundary } from "@/components/render-isolation/render-isolation-boundary";
import { Button } from "@/components/ui/button";
import { ErrorState, LoadingState } from "@/components/ui/state-panel";
import type { FileInfo } from "@/lib/api/workspace-files";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { useReadingCommand } from "@/lib/workspace-files/reading-commands";
import { useTextPage } from "@/lib/workspace-files/use-text-page";
import { useFilesStore } from "@/lib/workspace-files/workspace-files-store";
import { AddToChatButton } from "./add-to-chat-button";
import { EncodingSelect } from "./encoding-select";
import { ReadingToolbar, type ReadingNavigation } from "./reading-toolbar";
import { TablePreview } from "./table-preview";

/**
 * CSV / TSV 渲染视图的阅读器：读文件开头那一段（读取规则与源码共用 `useTextPage`），交给 `TablePreview` 显示。源码
 * 视图仍是 `TextPreview`，由 `FilePreview` 按 `filePreviewMode` 分发。
 *
 * 表格是虚拟滚动，大部分行不在 DOM 里，DOM 内查找查不全；表格里的「查找」就是筛选行，所以 Ctrl/Cmd+F 把焦点交给
 * 筛选框。表格只认文件开头那一段：停在中间分段时（从源码视图切回来）表头不在，提示回到开头。
 */
export function TableReader({
  workspaceId,
  info,
  navigation,
  view,
  onUnviewable,
}: {
  workspaceId: number;
  info: FileInfo;
  navigation: ReadingNavigation;
  view?: ReactNode;
  /** 表格解析抛错时上报，由 `FilePreview` 降级到「看不了的文件」。 */
  onUnviewable: (reason: string) => void;
}) {
  const { page, encoding, go, setEncoding } = useTextPage(workspaceId, info);
  const setPreference = useFilesStore((s) => s.preference);
  const filter = useRef<HTMLInputElement>(null);
  useReadingCommand("find", () => {
    filter.current?.focus();
    filter.current?.select();
  });
  return (
    <div className="flex h-full min-h-0 flex-col">
      <ReadingToolbar navigation={navigation} info={info} view={view}
        actions={<AddToChatButton workspaceId={workspaceId} path={info.path} selection={null} />}
        options={<EncodingSelect value={encoding} onChange={setEncoding} />}
      />
      <div className="min-h-0 min-w-0 flex-1">
        {page.error ? (
          <ErrorState className="m-4" message={userErrorMessage(page.error, "文本读取失败")} onRetry={() => void page.refetch()} />
        ) : page.isPending ? (
          <LoadingState label="正在读取文件…" />
        ) : page.data.offset === 0 ? (
          <RenderIsolationBoundary
            scope="part"
            resetKey={`table:${info.version}:${encoding}`}
            metadata={{ renderer: "table-preview", path: info.path }}
            fallback={() => <TableFailure onFailure={onUnviewable} />}
          >
            <TablePreview
              filterRef={filter}
              text={page.data.text}
              tsv={info.name.toLowerCase().endsWith(".tsv")}
              truncated={page.data.next_offset !== null}
              fileSize={info.size}
              onShowSource={() => setPreference(workspaceId, info.path, { view: "source" })}
            />
          </RenderIsolationBoundary>
        ) : (
          <div className="space-y-3 p-6 text-sm">
            <p>表格只显示文件开头一段。</p>
            <Button variant="outline" onClick={() => go(0)}>回到开头</Button>
          </div>
        )}
      </div>
    </div>
  );
}

/** 表格渲染抛错（解析器异常）时的兜底：挂载即上报，由 `FilePreview` 切到「看不了的文件」。 */
function TableFailure({ onFailure }: { onFailure: (reason: string) => void }) {
  useEffect(() => {
    onFailure("表格内容无法解析");
  }, [onFailure]);
  return null;
}
