import type { ReactNode } from "react";

type ComposerToolbarProps = {
  /** 往正文里放内容的入口（「+」添加菜单）。 */
  add: ReactNode;
  /** 发送、停止、继续运行等主操作。 */
  action: ReactNode;
};

/**
 * 输入框里正文下方的一行，位置固定为「添加 ｜ 操作」。只放写东西相关的入口：本次运行的配置与状态
 * 在框外的配置行（`ComposerConfigBar`），打字区因此保持安静。
 *
 * 只负责摆放，不持有状态：各位置的内容由输入框按 assistant-ui composer 的状态装配。
 */
export function ComposerToolbar({ add, action }: ComposerToolbarProps) {
  return (
    <div className="flex items-center justify-between gap-2 px-1">
      {add}
      {action}
    </div>
  );
}
