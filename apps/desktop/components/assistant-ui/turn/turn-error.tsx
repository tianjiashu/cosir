import { CircleAlertIcon } from "lucide-react";
import { ErrorPrimitive, MessagePrimitive } from "@assistant-ui/react";

/**
 * 结局卡：回合以失败或取消结束时写在最后。只用 `destructive` 7% 的着色底面，不加框（设计 §5.8：边框只留给
 * 等待用户卡）。文案来自 assistant-ui 的消息状态（converter 已按结局收口）。
 */
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
