"use client";

import { useState, type FC } from "react";
import { useAssistantTransportState } from "@assistant-ui/react";
import { LoaderCircleIcon, SquareIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cancelRun } from "@/lib/assistant/cancel-run";

export const StopButton: FC<{ taskId: number | null }> = ({ taskId }) => {
  const runId = useAssistantTransportState((state) => state.run?.runId ?? null);
  const [requesting, setRequesting] = useState(false);
  const [failureMessage, setFailureMessage] = useState<string | null>(null);

  const stop = async () => {
    if (requesting || runId == null) return;
    setRequesting(true);
    setFailureMessage(null);
    const result = await cancelRun(taskId, runId);
    if (!result.accepted) setFailureMessage(result.message);
    setRequesting(false);
  };

  return (
    <Button
      type="button"
      variant="default"
      size="icon"
      className="aui-composer-cancel size-7 rounded-full"
      aria-label={requesting ? "正在停止" : failureMessage ? "停止请求失败" : "停止"}
      title={failureMessage ?? (requesting ? "正在停止" : "停止")}
      disabled={requesting}
      onClick={() => void stop()}
    >
      {requesting ? <LoaderCircleIcon className="size-3.5 animate-spin" /> : <SquareIcon className="size-3.5 fill-current" />}
    </Button>
  );
};
