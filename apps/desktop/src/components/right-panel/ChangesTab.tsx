/**
 * 变更集 Tab 组件。
 *
 * 组合检查点选择、批量工具栏与单行文件条目，提供 task 级文件变更的查看、
 * 保留与撤销交互。数据与操作来自 ``useChanges`` hook。
 *
 * @module components/right-panel/ChangesTab
 */

import { useCallback, useEffect, useState } from "react";
import { ChangeFileRow } from "@/components/right-panel/ChangeFileRow";
import { ChangeCheckpointSelect } from "@/components/right-panel/ChangeCheckpointSelect";
import { ChangesToolbar } from "@/components/right-panel/ChangesToolbar";
import { useChanges } from "@/hooks/useChanges";

/** ChangesTab 组件属性。 */
interface ChangesTabProps {
  /** 当前任务标识；为 null 时不加载。 */
  taskId: string | null;
}

/**
 * 变更集 Tab。
 *
 * @param props - 组件属性。
 * @returns 变更集 Tab 内容。
 */
export function ChangesTab({ taskId }: ChangesTabProps) {
  const { changeSet, checkpoint, setCheckpoint, revert, keep, refresh, loading, error } =
    useChanges(taskId);

  // 多选状态：每次变更集整体替换后清空选中，避免残留已不存在的路径。
  const [selected, setSelected] = useState<Set<string>>(new Set());
  useEffect(() => {
    setSelected(new Set());
  }, [changeSet]);

  const toggleSelect = useCallback((path: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(path)) {
        next.delete(path);
      } else {
        next.add(path);
      }
      return next;
    });
  }, []);

  const keepSelected = useCallback(() => {
    if (selected.size === 0) {
      return;
    }
    void keep([...selected]);
  }, [keep, selected]);

  const revertSelected = useCallback(() => {
    if (selected.size === 0) {
      return;
    }
    void revert([...selected]);
  }, [revert, selected]);

  const files = changeSet?.files ?? [];

  return (
    <div className="space-y-2 p-3">
      <div className="space-y-2 border-b border-border pb-2">
        <ChangeCheckpointSelect
          checkpoints={changeSet?.checkpoints ?? []}
          value={checkpoint}
          onChange={setCheckpoint}
        />
        <ChangesToolbar
          selectedCount={selected.size}
          onKeepSelected={keepSelected}
          onRevertSelected={revertSelected}
          onRefresh={() => void refresh()}
        />
      </div>

      {loading && <p className="py-4 text-center text-xs text-muted-foreground">加载中…</p>}

      {error && (
        <div className="rounded-md border border-dashed border-destructive/50 p-3 text-xs text-destructive">
          {error}
        </div>
      )}

      {!loading && !error && files.length === 0 && (
        <p className="py-8 text-center text-xs text-muted-foreground">暂无文件变更</p>
      )}

      {!loading && files.length > 0 && (
        <div className="space-y-1">
          {files.map((file) => (
            <ChangeFileRow
              key={file.path}
              file={file}
              selected={selected.has(file.path)}
              onToggleSelect={toggleSelect}
              onKeep={(path) => void keep([path])}
              onRevert={(path) => void revert([path])}
            />
          ))}
        </div>
      )}
    </div>
  );
}
