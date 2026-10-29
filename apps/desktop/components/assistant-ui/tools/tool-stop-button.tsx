import { LoaderCircleIcon, SquareIcon } from "lucide-react";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/**
 * 工具卡片上「停止」请求的 UI 状态：请求中、失败提示，以及目标落定后的复位。
 *
 * 停止命令只是投递信号：`request` 返回 null 表示信号已发出，此后保持「停止中」直到 snapshot 里的
 * canonical 状态离开活动态（`isActive` 变假）才复位，不乐观改状态；返回字符串表示请求失败，
 * 作为提示展示并允许重试。终端命令与子 Agent 卡片共用，取消按什么寻址由调用方的 `request` 决定。
 */
export function useStopRequest(isActive: boolean) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!isActive) {
      setPending(false);
      setError(null);
    }
  }, [isActive]);

  const stop = async (request: () => Promise<string | null>): Promise<boolean> => {
    if (pending) return false;
    setPending(true);
    setError(null);
    const failure = await request();
    if (failure === null) return true;
    setPending(false);
    setError(failure);
    return false;
  };

  return { pending, error, stop };
}

/** 卡片表头的纯图标停止按钮；文案只在 aria-label / title 中，给命令或标题留出宽度。 */
export function ToolStopButton({ label, pending, onClick, className }: {
  /** 停止对象的可读描述，如「终端命令」「子 Agent：审查代码」。 */
  label: string;
  pending: boolean;
  onClick: () => void;
  className?: string;
}) {
  return (
    <Button
      type="button"
      variant="ghost"
      size="icon-xs"
      aria-label={pending ? `正在停止${label}` : `停止${label}`}
      title={pending ? "正在停止…" : "停止"}
      disabled={pending}
      onClick={(event) => {
        event.stopPropagation();
        onClick();
      }}
      className={cn("text-muted-foreground hover:bg-destructive/10 hover:text-destructive", className)}
    >
      {pending
        ? <LoaderCircleIcon className="size-3.5 animate-spin" aria-hidden="true" />
        : <SquareIcon className="size-3 fill-current" aria-hidden="true" />}
    </Button>
  );
}
