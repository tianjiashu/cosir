import type { ReactNode } from "react";

/**
 * 输入框框外右下角的一行：本次运行怎么跑（模型与推理强度、终端权限）与只读状态（上下文占用环）。
 *
 * 与输入框分开摆：输入框只装「写什么」，配置在需要时找得到、平时不抢打字区。按钮统一用
 * `COMPOSER_CONFIG_BUTTON_CLASS_NAME`。收窄时按输入框所在容器的宽度换行（`@container` 在二者共同的外层），
 * 模型名先截断。只负责摆放，不持有状态。
 */
export function ComposerConfigBar({ config, status }: {
  /** 本次运行的配置（模型、终端权限）；模型名可能很长，这一格可收窄截断。 */
  config: ReactNode;
  /** 只读的状态信息（上下文用量）。 */
  status: ReactNode;
}) {
  return (
    <div className="flex min-w-0 flex-wrap items-center justify-end gap-0.5 px-2">
      <div className="flex min-w-0 items-center gap-0.5">{config}</div>
      {status}
    </div>
  );
}
