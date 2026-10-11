import { useEffect, useRef } from "react";
import { ArrowRightIcon, BotIcon, CheckIcon, CircleIcon, LoaderCircleIcon, XIcon } from "lucide-react";

import type { AgentTeamRun } from "@/lib/api/agent-teams";
import { Button } from "@/components/ui/button";

type ExecutionStep = AgentTeamRun["execution_steps"][number];

type AgentTeamExecutionPipelineProps = {
  run: AgentTeamRun;
  onOpenAgent: (taskId: number, title: string, role: string) => void;
};

function TransitionConnector({ step }: { step?: ExecutionStep }) {
  return (
    <div className="flex min-w-8 shrink-0 flex-col items-center justify-center gap-1 pt-1" aria-hidden="true">
      {step?.business_status && (
        <span
          className="inline-flex max-w-24 items-center gap-1 truncate rounded-full border px-1.5 py-0.5 text-[10px] text-foreground"
          style={step.business_status_color
            ? {
                borderColor: step.business_status_color,
                backgroundColor: `color-mix(in srgb, ${step.business_status_color} 10%, transparent)`,
              }
            : undefined}
          title={step.business_status}
        >
          {step.business_status_color && (
            <span className="inline-block size-1.5 shrink-0 rounded-full" style={{ backgroundColor: step.business_status_color }} />
          )}
          {step.business_status}
        </span>
      )}
      <span className="flex items-center" aria-hidden="true">
        <span className="h-px w-3 bg-border" />
        <ArrowRightIcon className="-ml-1 size-3.5 text-muted-foreground/70" />
      </span>
    </div>
  );
}

const STATUS_LABELS: Record<string, string> = {
  queued: "等待中",
  pending: "等待中",
  running: "执行中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

function RuntimeState({ status }: { status: string }) {
  const label = STATUS_LABELS[status] ?? status;
  if (status === "running") {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-primary/10 px-2 py-0.5 text-[11px] text-primary">
        <LoaderCircleIcon className="size-3 animate-spin" aria-hidden="true" />{label}
      </span>
    );
  }
  if (status === "completed") {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-emerald-500/10 px-2 py-0.5 text-[11px] text-emerald-700 dark:text-emerald-400">
        <CheckIcon className="size-3" aria-hidden="true" />{label}
      </span>
    );
  }
  if (status === "failed" || status === "cancelled") {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-destructive/10 px-2 py-0.5 text-[11px] text-destructive">
        <XIcon className="size-3" aria-hidden="true" />{label}
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 rounded-full bg-muted px-2 py-0.5 text-[11px] text-muted-foreground">
      <CircleIcon className="size-3" aria-hidden="true" />{label}
    </span>
  );
}

function ExecutionStage({
  step,
  isCurrent,
  onOpenAgent,
}: {
  step: ExecutionStep;
  isCurrent: boolean;
  onOpenAgent: AgentTeamExecutionPipelineProps["onOpenAgent"];
}) {
  const visibleChildAgents = step.child_agents.slice(0, 2);
  const remainingChildAgents = step.child_agents.slice(2);

  return (
    <div className="w-48 shrink-0" aria-current={isCurrent ? "step" : undefined}>
      <div className="mb-2 flex min-h-9 items-start gap-2">
        <span
          className={`mt-1.5 size-3 shrink-0 rounded-full border-2 ${isCurrent ? "border-primary bg-primary ring-4 ring-primary/15" : step.status === "completed" ? "border-emerald-600 bg-emerald-500 dark:border-emerald-400" : step.status === "failed" || step.status === "cancelled" ? "border-destructive bg-destructive" : "border-muted-foreground/50 bg-background"}`}
          aria-hidden="true"
        />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-1.5 gap-y-0.5">
            <p className="truncate text-xs font-semibold" title={step.goal_summary}>{step.goal_summary || step.name}</p>
            {step.node_visit > 1 && (
              <span className="shrink-0 rounded bg-muted px-1 py-0.5 text-[9px] text-muted-foreground">第 {step.node_visit} 次</span>
            )}
          </div>
          <div className="mt-0.5 flex items-center gap-1.5">
            <span className="max-w-24 truncate text-[10px] text-muted-foreground" title={step.name}>{step.name}</span>
            <span className="text-[10px] text-muted-foreground">· 步骤 {step.step_number}</span>
            <RuntimeState status={step.status} />
          </div>
        </div>
      </div>

      <div className={`ml-5 space-y-1.5 border-l pl-2.5 ${isCurrent ? "border-primary/50" : "border-border/70"}`}>
        <AgentChip
          title={step.name}
          role={step.agent_id}
          taskId={step.task_id}
          onOpenAgent={onOpenAgent}
        />
        {visibleChildAgents.map((agent) => (
          <AgentChip
            key={agent.task_id}
            title={agent.title}
            role="子 Agent"
            taskId={agent.task_id}
            status={agent.status}
            onOpenAgent={onOpenAgent}
          />
        ))}
        {remainingChildAgents.length > 0 && (
          <details className="group">
            <summary className="w-fit cursor-pointer rounded-full bg-muted px-2 py-1 text-[10px] text-muted-foreground outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring">
              还有 {remainingChildAgents.length} 个
            </summary>
            <div className="mt-1.5 space-y-1.5">
              {remainingChildAgents.map((agent) => (
                <AgentChip
                  key={agent.task_id}
                  title={agent.title}
                  role="子 Agent"
                  taskId={agent.task_id}
                  status={agent.status}
                  onOpenAgent={onOpenAgent}
                />
              ))}
            </div>
          </details>
        )}
        {step.goal && (
          <details className="group pt-0.5">
            <summary className="cursor-pointer text-[10px] text-muted-foreground outline-none hover:text-foreground focus-visible:rounded focus-visible:ring-2 focus-visible:ring-ring">
              查看节点目标
            </summary>
            <p className="mt-1 max-h-24 overflow-y-auto whitespace-pre-wrap pr-1 text-[10px] leading-relaxed text-muted-foreground">
              {step.goal}
            </p>
          </details>
        )}
      </div>
    </div>
  );
}

function AgentChip({
  title,
  role,
  taskId,
  status,
  onOpenAgent,
}: {
  title: string;
  role: string;
  taskId: number | null;
  status?: string;
  onOpenAgent: AgentTeamExecutionPipelineProps["onOpenAgent"];
}) {
  return (
    <Button
      type="button"
      variant="secondary"
      size="sm"
      className="h-7 w-full min-w-0 justify-start gap-1.5 rounded-full px-2 text-[10px]"
      disabled={taskId === null}
      title={title}
      onClick={() => taskId !== null && onOpenAgent(taskId, title, role)}
    >
      <BotIcon className="size-3 shrink-0 text-primary" aria-hidden="true" />
      <span className="min-w-0 flex-1 truncate text-left">{title}</span>
      {status && <span className="shrink-0 text-muted-foreground">{STATUS_LABELS[status] ?? status}</span>}
    </Button>
  );
}

function TerminalMarker({ run }: { run: AgentTeamRun }) {
  if (run.status === "completed") {
    return <div className="flex h-9 w-10 shrink-0 items-center justify-center gap-1 text-[10px] font-medium text-emerald-700 dark:text-emerald-400" aria-label="流程完成"><CheckIcon className="size-3.5" aria-hidden="true" />END</div>;
  }
  if (run.status === "failed" || run.status === "cancelled") {
    const label = run.status === "failed" ? "执行失败" : "已取消";
    return <div className="flex h-9 w-14 shrink-0 items-center justify-center gap-1 text-[10px] font-medium text-destructive" aria-label={label}><XIcon className="size-3.5" aria-hidden="true" />{label}</div>;
  }
  return null;
}

export function AgentTeamExecutionPipeline({ run, onOpenAgent }: AgentTeamExecutionPipelineProps) {
  const terminal = <TerminalMarker run={run} />;
  const showTerminal = run.status === "completed" || run.status === "failed" || run.status === "cancelled";
  const scrollerRef = useRef<HTMLDivElement>(null);
  const followLatestRef = useRef(true);
  const currentStep = run.status === "running" && run.current_node_id
    ? [...run.execution_steps].reverse().find((step) => step.node_id === run.current_node_id)
    : undefined;

  useEffect(() => {
    const scroller = scrollerRef.current;
    if (!scroller || !followLatestRef.current) return;
    scroller.scrollTo({ left: scroller.scrollWidth, behavior: "smooth" });
  }, [run.execution_steps.length]);

  const handleScroll = () => {
    const scroller = scrollerRef.current;
    if (!scroller) return;
    followLatestRef.current = scroller.scrollWidth - scroller.clientWidth - scroller.scrollLeft < 24;
  };

  return (
    <section aria-label="实际执行流水线" className="rounded-lg border border-border/60 bg-muted/10 p-3">
      <div ref={scrollerRef} onScroll={handleScroll} className="overflow-x-auto pb-2 [scrollbar-width:thin]">
        <ol className="flex min-w-max items-start gap-2" aria-label="节点访问顺序">
          <li className="w-16 shrink-0" aria-label="流程开始">
            <div className="mb-2 flex h-9 items-center gap-2">
              <span className="size-3 shrink-0 rounded-full border-2 border-muted-foreground/50 bg-background" aria-hidden="true" />
              <span className="text-[10px] font-semibold text-muted-foreground">START</span>
            </div>
            <span className="ml-5 text-[10px] text-muted-foreground">开始</span>
          </li>
          {run.execution_steps.map((step, index) => {
            const nextStep = run.execution_steps[index + 1];
            return (
              <li key={`${step.conversation_run_id ?? `queued-${step.node_id}`}-${step.step_number}`} className="flex items-start gap-2">
                {index === 0 && <TransitionConnector />}
                <ExecutionStage
                  step={step}
                  isCurrent={currentStep?.step_number === step.step_number}
                  onOpenAgent={onOpenAgent}
                />
                {nextStep && <TransitionConnector step={step} />}
              </li>
            );
          })}
          {showTerminal && (
            <li className="flex items-start gap-2">
              <TransitionConnector
                step={run.status === "completed" && run.execution_steps.at(-1)?.transition_target_node_id === "END"
                  ? run.execution_steps.at(-1)
                  : undefined}
              />
              {terminal}
            </li>
          )}
          {!showTerminal && run.execution_steps.length === 0 && (
            <li className="px-2 text-xs text-muted-foreground">正在准备入口节点…</li>
          )}
        </ol>
      </div>
    </section>
  );
}
