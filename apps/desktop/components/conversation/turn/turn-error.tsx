import { CircleAlertIcon } from "lucide-react";
import { ErrorPrimitive, MessagePrimitive } from "@assistant-ui/react";

/** 真正运行失败的错误详情。取消由 Turn 的中性结局呈现，不进入 assistant-ui 的 error 分支。 */
export function TurnError() {
  return (
    <MessagePrimitive.Error>
      <ErrorPrimitive.Root
        data-slot="turn-error"
        className="text-destructive flex items-center gap-2 rounded-(--radius) bg-[color-mix(in_oklab,var(--destructive)_7%,transparent)] px-3 py-2 text-2sm"
      >
        <CircleAlertIcon className="size-3.5 shrink-0" aria-hidden="true" />
        <ErrorPrimitive.Message className="line-clamp-2 min-w-0" />
      </ErrorPrimitive.Root>
    </MessagePrimitive.Error>
  );
}
