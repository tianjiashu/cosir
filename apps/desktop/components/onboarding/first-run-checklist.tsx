"use client";

import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { CheckIcon, FolderPlusIcon, Loader2Icon, SparklesIcon } from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { Button, buttonVariants } from "@/components/ui/button";
import type { ModelSetupState } from "@/components/onboarding/start-readiness";
import { cn } from "@/lib/utils";

const MODEL_STEP_DESCRIPTION: Record<Exclude<ModelSetupState, "ready">, string> = {
  loading: "正在检查可用模型…",
  empty: "添加至少一个可用的模型连接",
  error: "模型列表加载失败，可在配置中心检查模型连接",
};

type FirstRunChecklistProps = {
  hasWorkspace: boolean;
  modelState: ModelSetupState;
  onAddWorkspace: () => void;
  addingWorkspace: boolean;
  addWorkspaceError: string | null;
};

/**
 * 新对话页的首次使用引导：工作区与模型是开始对话的两个前置条件。
 *
 * 只负责展示与跳转，「何时出现」由 start-readiness 的 shouldShowFirstRunChecklist 判定。
 * 添加工作区复用工作区选择器的同一创建流程，配置模型跳到配置中心的模型标签。
 */
export function FirstRunChecklist({
  hasWorkspace,
  modelState,
  onAddWorkspace,
  addingWorkspace,
  addWorkspaceError,
}: FirstRunChecklistProps) {
  return (
    <section aria-label="开始前的准备" className="border-border/60 bg-muted/40 mb-3 rounded-xl border px-3 py-2.5">
      <p className="text-muted-foreground mb-2 text-xs">开始对话前，先完成以下准备：</p>
      <ol className="space-y-1.5">
        <ChecklistStep
          index={1}
          done={hasWorkspace}
          title="添加工作区"
          description="选择一个项目文件夹，对话和文件修改都在其中进行"
          action={(
            <Button type="button" size="sm" variant="outline" onClick={onAddWorkspace} disabled={addingWorkspace}>
              {addingWorkspace ? <Loader2Icon className="animate-spin" /> : <FolderPlusIcon />}添加工作区
            </Button>
          )}
        />
        <ChecklistStep
          index={2}
          done={modelState === "ready"}
          title="配置模型"
          description={modelState === "ready" ? "" : MODEL_STEP_DESCRIPTION[modelState]}
          action={modelState !== "loading" && (
            <Link to="/settings/models" className={buttonVariants({ variant: "outline", size: "sm" })}>
              <SparklesIcon />去配置
            </Link>
          )}
        />
      </ol>
      {addWorkspaceError && <Alert tone="destructive" className="mt-2 text-xs">{addWorkspaceError}</Alert>}
    </section>
  );
}

function ChecklistStep({ index, done, title, description, action }: {
  index: number;
  done: boolean;
  title: string;
  description: string;
  action?: ReactNode;
}) {
  return (
    <li className="flex items-center gap-2.5">
      <span
        aria-hidden="true"
        className={cn(
          "flex size-5 shrink-0 items-center justify-center rounded-full text-2xs font-medium tabular-nums",
          done ? "bg-success/15 text-success" : "border-border text-muted-foreground border",
        )}
      >
        {done ? <CheckIcon className="size-3" /> : index}
      </span>
      <span className="min-w-0 flex-1">
        <span className={cn("block text-sm", done && "text-muted-foreground line-through")}>
          {title}
          <span className="sr-only">{done ? "（已完成）" : "（未完成）"}</span>
        </span>
        {!done && <span className="text-muted-foreground block truncate text-xs">{description}</span>}
      </span>
      {!done && action}
    </li>
  );
}
