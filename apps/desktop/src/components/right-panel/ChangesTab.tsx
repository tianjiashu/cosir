/**
 * 变更集面板组件。
 *
 * 组合检查点选择、批量工具栏与单行文件条目，提供 task 级文件变更的查看、
 * 单文件保留 / 撤销与批量保留 / 撤销交互。数据与操作来自 ``useChanges`` hook。
 *
 * 设计取舍：仅展示 ``status === 'pending'`` 的文件——保留 / 撤销完成后该文件从面板
 * 消失（符合用户「保留即清空、撤销即清空」的心智模型）。后端仍返回全量状态以利排查，
 * 前端按需过滤。
 *
 * 该面板为纯内容体，被对话上方折叠区的 ChangesDrawer 复用；其高度 / 容器由调用方通过
 * ``className`` 决定（当前主入口为 ChangesDrawer，不再由 RightPanel 包裹）。
 *
 * 运行中变更：后端在工具执行完成时即广播 ``file_change_updated``，故本面板会实时出现
 * 仍在运行的 turn 所产生的变更。此类文件行由 ``turnRunning`` 标出，提示用户撤销的是
 * 「运行中的中间态结果」，后续工具可能再次修改同一文件。
 *
 * @module components/right-panel/ChangesTab
 */

import { useCallback, useMemo } from "react";
import { ChangeFileRow } from "@/components/right-panel/ChangeFileRow";
import { ChangeCheckpointSelect } from "@/components/right-panel/ChangeCheckpointSelect";
import { ChangesToolbar } from "@/components/right-panel/ChangesToolbar";
import { VirtualList } from "@/lib/virtual/VirtualList";
import { useChanges } from "@/hooks/useChanges";
import { useTurnStore } from "@/stores/turnStore";

/** ChangesPanel 组件属性。 */
interface ChangesPanelProps {
  /** 当前任务标识；为 null 时不加载。 */
  taskId: string | null;
  /** 自定义外层容器类名（Drawer 内需要去掉 h-full，改为自适应高度）。 */
  className?: string;
}

/**
 * 变更集面板主体（无卡片容器）。
 *
 * 组合检查点选择、批量工具栏与单行文件条目，提供 task 级文件变更的查看、
 * 单文件保留 / 撤销与批量保留 / 撤销交互。数据与操作来自 ``useChanges`` hook。
 *
 * 设计为可复用的纯内容体，由对话上方的 ``ChangesDrawer`` 折叠区复用，
 * 避免与折叠容器重复实现交互逻辑。
 *
 * @param props - 组件属性。
 * @returns 变更集面板内容。
 */
export function ChangesPanel({ taskId, className }: ChangesPanelProps) {
  const { changeSet, checkpoint, setCheckpoint, revert, keep, loading, error } =
    useChanges(taskId);

  const keepOne = useCallback((path: string) => void keep([path]), [keep]);
  const revertOne = useCallback((path: string) => void revert([path]), [revert]);

  // 运行中 turn 集合：用于标记「该变更对应的轮次仍在运行」，此时撤销的是中间态结果。
  // selector 只取 store 中的稳定引用（数组本身），Set 的构造放在 useMemo 里，
  // 避免 selector 每次返回新 Set 引用导致 zustand 无限重渲染。
  const turns = useTurnStore((s) => s.turnsByTaskId[taskId ?? ""]);
  const runningTurnIds = useMemo(
    () =>
      new Set(
        (turns ?? []).filter((turn) => turn.status === "running").map((turn) => turn.turn_id),
      ),
    [turns],
  );

  // 仅展示 pending 文件：保留 / 撤销完成后该行从面板消失（保持「保留即清空」心智模型）。
  // 用 useMemo 收敛引用，配合 memo 化的 ChangeFileRow 避免大列表整列重渲染。
  const files = useMemo(
    () => (changeSet?.files ?? []).filter((file) => file.status === "pending"),
    [changeSet]
  );

  const keepAll = useCallback(() => {
    if (files.length === 0) {
      return;
    }
    void keep(files.map((file) => file.path));
  }, [keep, files]);

  const revertAll = useCallback(() => {
    if (files.length === 0) {
      return;
    }
    void revert(files.map((file) => file.path));
  }, [revert, files]);

  return (
    <div className={className ?? "flex h-full flex-col gap-2 p-3"}>
      <div className="space-y-2 border-b border-border pb-2">
        <ChangeCheckpointSelect
          checkpoints={changeSet?.checkpoints ?? []}
          value={checkpoint}
          onChange={setCheckpoint}
        />
        <ChangesToolbar
          hasFiles={files.length > 0}
          onKeepAll={keepAll}
          onRevertAll={revertAll}
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
        <VirtualList
          items={files}
          getKey={(file) => file.path}
          renderItem={(file) => (
            <ChangeFileRow
              file={file}
              onKeep={keepOne}
              onRevert={revertOne}
              turnRunning={file.last_turn_id ? runningTurnIds.has(file.last_turn_id) : false}
            />
          )}
          className="min-h-0 flex-1"
          estimateSize={32}
        />
      )}
    </div>
  );
}