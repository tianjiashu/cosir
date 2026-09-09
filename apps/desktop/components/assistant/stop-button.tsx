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
};

export const StopButton = forwardRef<HTMLButtonElement, StopButtonProps>(
  function StopButton({
    taskId,
    onClick: assistantOnClick,
    className,
    disabled,
    "aria-label": ariaLabel,
    ...props
  }, ref) {
    const runId = useAuiState((state) => getTransportRunId(state.thread.state));
    const [requesting, setRequesting] = useState(false);
    const [failureMessage, setFailureMessage] = useState<string | null>(null);

    const stop = async () => {
      if (requesting || runId == null) return;
      setRequesting(true);
      setFailureMessage(null);
      void frontendLog("DEBUG", "assistant_stop_requested", "用户点击停止运行", {
        data: { taskId, runId },
      });
      const result = await cancelRun(taskId, runId);
      void frontendLog(result.accepted ? "DEBUG" : "WARNING", "assistant_stop_result", result.accepted ? "后端已接受停止运行" : "后端拒绝停止运行", {
        data: { taskId, runId, accepted: result.accepted, reason: result.accepted ? null : result.reason },
      });
      if (!result.accepted) setFailureMessage(result.message);
      setRequesting(false);
      return result;
    };

    const handleClick: MouseEventHandler<HTMLButtonElement> = (event) => {
      // The backend is the canonical owner of ConversationRun cancellation.
      // Wait for its acknowledgement before invoking assistant-ui's Cancel;
      // otherwise the primitive can immediately unmount this component and a
      // rejected backend cancellation has nowhere to display its error.
      void stop().then((result) => {
        if (!result?.accepted) return;
        // Keep assistant-ui's injected Cancel action: it aborts the active
        // transport/runtime request on the client side.
        assistantOnClick?.(event);
      });
    };

    return (
      <Button
        {...props}
        ref={ref}
        type="button"
        variant="default"
        size="icon"
        className={cn("aui-composer-cancel size-7 rounded-full", className)}
        aria-label={ariaLabel ?? (requesting ? "正在停止" : failureMessage ? "停止请求失败" : "停止")}
        title={failureMessage ?? (requesting ? "正在停止" : "停止")}
        disabled={disabled || requesting}
        onClick={handleClick}
      >
        {requesting ? <LoaderCircleIcon className="size-3.5 animate-spin" /> : <SquareIcon className="size-3.5 fill-current" />}
      </Button>
    );
  },
);
