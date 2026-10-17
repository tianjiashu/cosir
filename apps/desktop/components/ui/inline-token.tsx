import { useId, type ReactNode } from "react";
import { XIcon } from "lucide-react";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

type InlineTokenProps = {
  icon: ReactNode;
  name: string;
  detail?: string;
  description: string;
  preserveExtension?: boolean;
  tone?: "neutral" | "skill";
  state?: "ready" | "pending" | "error";
  open?: { label: string; disabled?: boolean; onClick: () => void };
  remove?: { label: string; disabled?: boolean; onClick: () => void };
};

/** 仅承载行内标签的排版和操作外观；文档编辑、附件事实及打开路径由调用方拥有。 */
export function InlineToken({
  icon, name, detail, description, preserveExtension = false,
  tone = "neutral", state = "ready", open, remove,
}: InlineTokenProps) {
  const descriptionId = useId();
  const extensionAt = preserveExtension ? name.lastIndexOf(".") : -1;
  const hasExtension = extensionAt > 0 && extensionAt < name.length - 1;
  const label = (
    <>
      <span className="cosir-inline-token-icon" aria-hidden="true">{icon}</span>
      <span className="cosir-inline-token-name">
        <span className="cosir-inline-token-stem">{hasExtension ? name.slice(0, extensionAt) : name}</span>
        {hasExtension && <span className="cosir-inline-token-extension">{name.slice(extensionAt)}</span>}
      </span>
      {detail && <span className="cosir-inline-token-detail">{detail}</span>}
    </>
  );
  return (
    <Tooltip>
      <TooltipTrigger
        render={<span />}
        className="cosir-inline-token"
        data-inline-token="true"
        data-tone={tone}
        data-state={state}
        contentEditable={false}
        delay={500}
      >
        {open ? (
          <button
            type="button"
            className="cosir-inline-token-label cosir-inline-token-open"
            aria-label={open.label}
            aria-describedby={descriptionId}
            disabled={open.disabled}
            onMouseDown={(event) => event.preventDefault()}
            onClick={open.onClick}
          >{label}</button>
        ) : <span className="cosir-inline-token-label">{label}</span>}
        {remove && (
          <button
            type="button"
            className="cosir-inline-token-remove"
            aria-label={remove.label}
            aria-describedby={descriptionId}
            disabled={remove.disabled}
            onMouseDown={(event) => event.preventDefault()}
            onClick={remove.onClick}
          ><XIcon aria-hidden="true" /></button>
        )}
      </TooltipTrigger>
      <TooltipContent id={descriptionId} role="tooltip" className="break-all">{description}</TooltipContent>
    </Tooltip>
  );
}
