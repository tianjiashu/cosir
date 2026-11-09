import type { ReactNode } from "react";

type ComposerToolbarProps = {
  /** 往正文里放内容的入口（「+」添加菜单）。 */
  add: ReactNode;
  /** 本次运行的配置（工具、模型）；模型名可能很长，这一格可收窄截断。 */
  config: ReactNode;
  /** 只读的状态信息（任务页的上下文用量）；新对话页没有。 */
  status?: ReactNode;
  /** 发送、停止、继续运行等主操作。 */
  action: ReactNode;
};

/**
 * 输入框底栏：新对话页与任务页共用的布局，位置固定为「添加 · 配置 ｜ 状态 · 操作」。
 *
 * 只负责摆放，不持有状态：两处输入框的状态来源不同（新对话页是本地草稿，任务页是 assistant-ui
 * composer），各位置的内容由所在输入框按自己的状态装配。顺序与间距只在这里定义，两处不会再各自走样。
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
