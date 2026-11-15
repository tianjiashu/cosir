import type { ButtonHTMLAttributes } from "react";

import { cn } from "@/lib/utils";

type SwitchProps = Omit<ButtonHTMLAttributes<HTMLButtonElement>, "role" | "aria-checked"> & {
  checked: boolean;
  /** `sm` 用在长列表的每一行：一列大开关的分量会压过行里的名称。 */
  size?: "default" | "sm";
};

const SIZES = {
  default: { track: "h-6 w-11", thumb: "size-5", on: "translate-x-5" },
  sm: { track: "h-4 w-7", thumb: "size-3", on: "translate-x-3.5" },
} as const;

/**
 * 可访问的开关控件。
 *
 * 组件只负责展示受控状态并派发点击事件，不保存状态，也不承载具体业务语义。
 * 调用方需要通过 ``checked`` 和 ``onClick`` 管理值，并可使用 ``disabled`` 禁用交互。
 */
export function Switch({ checked, size = "default", className, ...props }: SwitchProps) {
  const sizing = SIZES[size];
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      className={cn(
        "relative inline-flex shrink-0 cursor-pointer items-center rounded-full transition-colors focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-50",
        sizing.track,
        checked ? "bg-primary" : "bg-muted-foreground/30",
        className,
      )}
      {...props}
    >
      <span
        aria-hidden="true"
        className={cn(
          "pointer-events-none block rounded-full bg-background shadow-sm transition-transform",
          sizing.thumb,
          checked ? sizing.on : "translate-x-0.5",
        )}
      />
    </button>
  );
}
