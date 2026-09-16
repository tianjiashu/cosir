"use client";

import {
  forwardRef,
  useState,
  type ComponentPropsWithoutRef,
  type MouseEventHandler,
} from "react";
import { LoaderCircleIcon, SquareIcon } from "lucide-react";
import { useAuiState } from "@assistant-ui/react";
import { Button } from "@/components/ui/button";
import { cancelRun } from "@/lib/assistant/cancel-run";
import { getTransportRunId } from "@/lib/assistant/conversation-actions";
import { frontendLog } from "@/lib/logging/frontend-log";
import { cn } from "@/lib/utils";

type StopButtonProps = Omit<ComponentPropsWithoutRef<typeof Button>, "onClick"> & {
  taskId: number | null;
  onClick?: MouseEventHandler<HTMLButtonElement>;
  onCancelRequested?: (runId: number) => void;
  onCancelResult?: (runId: number, accepted: boolean) => void;
  isCancelling?: boolean;
};

export const StopButton = forwardRef<HTMLButtonElement, StopButtonProps>(
  function StopButton({
    taskId,
    className,
    disabled,
    "aria-label": ariaLabel,
    onCancelRequested,
    onCancelResult,
    isCancelling = false,
    ...props
  }, ref) {
    const runId = useAuiState((state) => getTransportRunId(state.thread.state));
    const [requesting, setRequesting] = useState(false);
    const [failureMessage, setFailureMessage] = useState<string | null>(null);

    const stop = async () => {
      if (requesting || runId == null) return;
      setRequesting(true);
      setFailureMessage(null);
      onCancelRequested?.(runId);
      void frontendLog("DEBUG", "assistant_stop_requested", "用户点击停止运行", {
        data: { taskId, runId },
      });
      const result = await cancelRun(taskId, runId);
      onCancelResult?.(runId, result.accepted);
      void frontendLog(result.accepted ? "DEBUG" : "WARNING", "assistant_stop_result", result.accepted ? "后端已接受停止运行" : "后端拒绝停止运行", {
        data: { taskId, runId, accepted: result.accepted, reason: result.accepted ? null : result.reason },
      });
      if (!result.accepted) setFailureMessage(result.message);
      setRequesting(false);
      return result;
    };

    const handleClick = () => {
      // 停止按钮只负责发送取消信号：ConversationRun 的终态由后端 workflow 落定，前端必须保持
      // transport 订阅直到该终态快照到达，才能解除「取消中」。后端在 run 到达终态后会自行结束
      // SSE（见 transport_stream_service 的 assistant_sse_stream_terminal），所以这里**不再**
      // 触发 assistant-ui 注入的 Cancel —— 它会立刻 abort 前端请求，使终态快照无人接收，
      // 界面将永久停在「取消中」。
      void stop();
    };

    return (
      <Button
        {...props}
        ref={ref}
        type="button"
        variant="default"
        size="icon"
        className={cn("aui-composer-cancel size-7 rounded-full", className)}
        aria-label={ariaLabel ?? (isCancelling || requesting ? "正在停止" : failureMessage ? "停止请求失败" : "停止")}
        title={failureMessage ?? (isCancelling || requesting ? "正在停止" : "停止")}
        disabled={disabled || requesting || isCancelling}
        onClick={handleClick}
      >
        {isCancelling || requesting ? <LoaderCircleIcon className="size-3.5 animate-spin" /> : <SquareIcon className="size-3.5 fill-current" />}
      </Button>
    );
  },
);
