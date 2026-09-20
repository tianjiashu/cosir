import type { ReactNode } from "react";

type TaskWorkspaceLayoutProps = {
  children: ReactNode;
  bottomPanel?: ReactNode;
};

/**
 * 任务页的稳定布局边界。
 *
 * 主内容和可选底部面板必须沿纵轴排列；面板不是普通的横向 sibling，
 * 否则一个有 intrinsic width 的 surface 会把 Assistant 内容挤到不可见。
 * 该组件只负责布局，不拥有 Assistant、终端或其它 surface 的业务状态。
 */
export function TaskWorkspaceLayout({ children, bottomPanel }: TaskWorkspaceLayoutProps) {
  return (
    <div className="relative flex h-full min-h-0 min-w-0 flex-col overflow-hidden">
      <main className="min-h-0 min-w-0 flex-1 overflow-hidden">
        {children}
      </main>
      {bottomPanel}
    </div>
  );
}
