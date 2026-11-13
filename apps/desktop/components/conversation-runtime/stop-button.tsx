import { commandPending, useCommandRequest } from "@/lib/assistant/command-request-store";
import { forwardRef, type ComponentPropsWithoutRef } from "react";
import { LoaderCircleIcon, SquareIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cancelSession } from "@/lib/assistant/session-commands";
import { UserFacingError } from "@/lib/errors/user-facing-error";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";
import { cn } from "@/lib/utils";

type StopButtonProps = Omit<ComponentPropsWithoutRef<typeof Button>, "onClick"> & {
  sessionId: number;
  isCancelling?: boolean;
};

export const StopButton = forwardRef<HTMLButtonElement, StopButtonProps>(
  function StopButton({
    sessionId,
    className,
    disabled,
    "aria-label": ariaLabel,
    isCancelling = false,
    ...props
  }, ref) {
    const request = useCommandRequest(sessionId, "session");
    const requesting = commandPending(request);
    const stopping = requesting || (isCancelling && !request?.error);

    const stop = async () => {
      void frontendLog("DEBUG", "assistant_stop_requested", "用户点击停止运行", {
        data: { sessionId },
      });
      const result = await cancelSession(sessionId);
      void frontendLog(result.accepted ? "DEBUG" : "WARNING", "assistant_stop_result", result.accepted ? "后端已接受停止运行" : "后端拒绝停止运行", {
        data: { sessionId, accepted: result.accepted, reason: result.accepted ? null : result.reason },
      });
      if (!result.accepted) {
        // 网络结果未知不宣告执行失败；幂等停止允许用户明确重试。
        notify.error(result.reason === "network" ? "停止结果待确认" : "停止请求被拒绝", {
          error: new UserFacingError(result.message),
          action: { label: "重试停止", onClick: () => void stop() },
        });
      }
    };

    return (
      <Button
        {...props}
        ref={ref}
        type="button"
        variant="default"
        size="icon"
        className={cn("aui-composer-cancel size-8 rounded-full", className)}
        aria-label={ariaLabel ?? (stopping ? "正在停止" : request?.error ? "重试停止" : "停止")}
        title={request?.error ?? (request?.phase === "syncing" ? "停止请求已发送，等待状态同步" : stopping ? "正在发送停止请求" : "停止")}
        disabled={disabled || stopping}
        // 只发取消信号：Run 的终态由后端落定，经 Session 长订阅到达后才解除「取消中」。
        onClick={() => void stop()}
      >
        {stopping ? <LoaderCircleIcon className="size-3.5 animate-spin" /> : <SquareIcon className="size-3.5 fill-current" />}
      </Button>
    );
  },
);
