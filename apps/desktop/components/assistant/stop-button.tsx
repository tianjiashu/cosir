"use client";

import {
  forwardRef,
  useRef,
  useState,
  type ComponentPropsWithoutRef,
  type MouseEventHandler,
} from "react";
import { LoaderCircleIcon, SquareIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cancelTask } from "@/lib/assistant/cancel-task";
import { UserFacingError } from "@/lib/errors/user-facing-error";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";
import { cn } from "@/lib/utils";

type StopButtonProps = Omit<ComponentPropsWithoutRef<typeof Button>, "onClick"> & {
  taskId: number;
  onClick?: MouseEventHandler<HTMLButtonElement>;
  onCancelRequested?: () => void;
  onCancelResult?: (accepted: boolean) => void;
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
    const [requesting, setRequesting] = useState(false);
    // toast 的「重试」在按钮重渲染后仍调用旧闭包，防重入必须读 ref 而不是 state。
    const requestingRef = useRef(false);
    const [failureMessage, setFailureMessage] = useState<string | null>(null);

    const stop = async () => {
      if (requestingRef.current) return;
      requestingRef.current = true;
      setRequesting(true);
      setFailureMessage(null);
      onCancelRequested?.();
      void frontendLog("DEBUG", "assistant_stop_requested", "用户点击停止运行", {
        data: { taskId },
      });
      const result = await cancelTask(taskId);
      onCancelResult?.(result.accepted);
      void frontendLog(result.accepted ? "DEBUG" : "WARNING", "assistant_stop_result", result.accepted ? "后端已接受停止运行" : "后端拒绝停止运行", {
        data: { taskId, accepted: result.accepted, reason: result.accepted ? null : result.reason },
      });
      if (!result.accepted) {
        setFailureMessage(result.message);
        // 拒绝原因（网络 / 服务端拒绝）都值得原样重试；文案是 cancelTask 写给用户的固定提示。
        notify.error("停止运行失败", {
          error: new UserFacingError(result.message),
          action: { label: "重试", onClick: () => void stop() },
        });
      }
      requestingRef.current = false;
      setRequesting(false);
      return result;
    };

    const handleClick = () => {
      // 停止按钮只负责发送取消信号：Run 的终态由后端 workflow 落定，经 Task 长订阅到达后才解除
      // 「取消中」。这里不触发 assistant-ui 注入的 Cancel——取消是 Task 命令，不是关闭订阅。
      void stop();
    };

    return (
      <Button
        {...props}
        ref={ref}
        type="button"
        variant="default"
        size="icon"
        className={cn("aui-composer-cancel size-8 rounded-full", className)}
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
