import type { ReactNode } from "react";

type ComposerToolbarProps = {
  /** 往正文里放内容的入口（「+」添加菜单）。 */
  add: ReactNode;
  /** 本次运行的配置（工具、模型）；模型名可能很长，这一格可收窄截断。 */
  config: ReactNode;
  /** 只读的状态信息（上下文用量）。 */
  status: ReactNode;
  /** 发送、停止、继续运行等主操作。 */
  action: ReactNode;
};

/**
 * 输入框底栏的布局，位置固定为「添加 · 配置 ｜ 状态 · 操作」。
 *
 * 只负责摆放，不持有状态：各位置的内容由输入框按 assistant-ui composer 的状态装配。
 * 收窄时整行换行，判断依据是输入框自身宽度（外层 `ComposerSurface` 是 `@container`）。
 */
export function ComposerToolbar({ add, config, status, action }: ComposerToolbarProps) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 px-1">
      <div className="flex min-w-0 flex-1 items-center gap-1">
        {add}
        <div className="flex min-w-0 flex-1 items-center gap-1">{config}</div>
      </div>
      <div className="flex shrink-0 items-center gap-1">
        {status}
        {action}
      </div>
    </div>
  );
}
