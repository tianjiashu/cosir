"use client";

import { memo, useState } from "react";
import { CheckIcon, ChevronDownIcon, LoaderIcon, XCircleIcon } from "lucide-react";
import {
  useToolCallElapsed,
  type ToolCallMessagePartComponent,
  type ToolCallMessagePartStatus,
} from "@assistant-ui/react";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";

export type ToolFallbackRootProps = React.ComponentProps<typeof Collapsible> & {
  defaultOpen?: boolean;
};

const formatDuration = (ms: number) => (ms < 1000 ? "<1s" : `${Math.floor(ms / 1000)}s`);

function ToolFallbackRoot({ className, ...props }: ToolFallbackRootProps) {
  return <Collapsible className={cn("w-full", className)} {...props} />;
}

function ToolFallbackDuration() {
  const elapsed = useToolCallElapsed();
  return elapsed === undefined ? null : <span className="text-xs text-muted-foreground">{formatDuration(elapsed)}</span>;
}

function ToolFallbackTrigger({ toolName, status, isError, className, ...props }: React.ComponentProps<typeof CollapsibleTrigger> & { toolName: string; status?: ToolCallMessagePartStatus; isError?: boolean }) {
  const running = status?.type === "running";
  const cancelled = status?.type === "incomplete" && status.reason === "cancelled";
  const failed = isError || (status?.type === "incomplete" && status.reason === "error");
  const Icon = failed ? XCircleIcon : running ? LoaderIcon : cancelled ? XCircleIcon : CheckIcon;
  return (
    <CollapsibleTrigger className={cn("flex items-center gap-2 py-1.5 text-sm text-muted-foreground", className)} {...props}>
      <Icon className={cn("size-4", running && "animate-spin", failed && "text-destructive")} />
      <span>{failed ? "Failed tool" : cancelled ? "Cancelled tool" : "Used tool"}: <b>{toolName}</b></span>
      <ToolFallbackDuration />
      <ChevronDownIcon className="size-4" />
    </CollapsibleTrigger>
  );
}

function ToolFallbackContent({ className, ...props }: React.ComponentProps<typeof CollapsibleContent>) {
  return <CollapsibleContent className={cn("space-y-2 pb-2", className)} {...props} />;
}

function ToolFallbackArgs({ argsText, className, ...props }: React.ComponentProps<"pre"> & { argsText?: string }) {
  return argsText ? <pre className={cn("overflow-auto rounded bg-muted p-2 text-xs", className)} {...props}>{argsText}</pre> : null;
}

function ToolFallbackResult({ result, className, ...props }: React.ComponentProps<"pre"> & { result?: unknown }) {
  return result === undefined ? null : <pre className={cn("overflow-auto rounded bg-muted p-2 text-xs", className)} {...props}>{typeof result === "string" ? result : JSON.stringify(result, null, 2)}</pre>;
}

function ToolFallbackError({ error, className, ...props }: React.ComponentProps<"div"> & { error?: string }) {
  return error ? <div className={cn("text-sm text-destructive", className)} {...props}>{error}</div> : null;
}

const ToolFallbackImpl: ToolCallMessagePartComponent = ({ toolName, argsText, result, status, isError }) => {
  const [open, setOpen] = useState(status?.type === "running");
  return (
    <ToolFallbackRoot open={open} onOpenChange={setOpen}>
      <ToolFallbackTrigger toolName={toolName} status={status} isError={isError} />
      <ToolFallbackContent>
        <ToolFallbackArgs argsText={argsText} />
        <ToolFallbackResult result={result} />
      </ToolFallbackContent>
    </ToolFallbackRoot>
  );
};

const ToolFallback = memo(ToolFallbackImpl) as unknown as ToolCallMessagePartComponent & {
  Root: typeof ToolFallbackRoot;
  Trigger: typeof ToolFallbackTrigger;
  Content: typeof ToolFallbackContent;
  Args: typeof ToolFallbackArgs;
  Result: typeof ToolFallbackResult;
  Error: typeof ToolFallbackError;
};

ToolFallback.Root = ToolFallbackRoot;
ToolFallback.Trigger = ToolFallbackTrigger;
ToolFallback.Content = ToolFallbackContent;
ToolFallback.Args = ToolFallbackArgs;
ToolFallback.Result = ToolFallbackResult;
ToolFallback.Error = ToolFallbackError;

export { ToolFallback, ToolFallbackRoot, ToolFallbackTrigger, ToolFallbackContent, ToolFallbackArgs, ToolFallbackResult, ToolFallbackError };
