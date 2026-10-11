import { useEffect, useRef, useState } from "react";
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
    <div className="flex min-w-10 shrink-0 flex-col items-center justify-center gap-1" aria-hidden="true">
      {step?.business_status && (
        <span
          className="inline-flex max-w-28 items-center gap-1 truncate rounded-full border px-1.5 py-0.5 text-[10px] text-foreground"
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
      <ArrowRightIcon className="size-4 text-muted-foreground/70" />
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

function ExecutionStepCard({
  step,
  isCurrent,
  onOpenAgent,
}: {
  step: ExecutionStep;
  isCurrent: boolean;
  onOpenAgent: AgentTeamExecutionPipelineProps["onOpenAgent"];
}) {
  const accentColor = step.business_status_color ?? undefined;

  return (
    <article
      className={`w-64 shrink-0 overflow-hidden rounded-xl border border-border/70 bg-card shadow-sm ${isCurrent ? "ring-2 ring-primary/30" : ""}`}
      style={accentColor ? { borderTopColor: accentColor, borderTopWidth: 3 } : undefined}
      aria-current={isCurrent ? "step" : undefined}
      aria-label={`${step.name}，第 ${step.node_visit} 次访问，${STATUS_LABELS[step.status] ?? step.status}`}
    >
      <header className="flex items-start justify-between gap-2 border-b border-border/60 px-3 py-2.5">
        <div className="min-w-0">
          <p className="truncate text-sm font-semibold" title={step.name}>{step.name}</p>
          <p className="mt-0.5 text-[11px] text-muted-foreground">
            第 {step.node_visit} 次访问 · 步骤 {step.step_number}
          </p>
        </div>
        <RuntimeState status={step.status} />
      </header>

      <div className="space-y-2.5 p-3">
        <div className="flex min-w-0 items-center gap-1.5">
          <BotIcon className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
          <Button
            type="button"
            variant="link"
            className="h-auto min-w-0 justify-start truncate p-0 text-xs font-medium"
            disabled={step.task_id === null}
            title={step.agent_id}
            onClick={() => step.task_id !== null && onOpenAgent(step.task_id, step.name, step.agent_id)}
          >
            {step.agent_id}
          </Button>
        </div>

        {step.business_status && (
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[11px] text-muted-foreground">节点状态</span>
            <span
              className="inline-flex max-w-full items-center gap-1.5 rounded-full border bg-background/70 px-2 py-0.5 text-[11px] font-medium text-foreground"
              style={step.business_status_color
                ? {
                    borderColor: step.business_status_color,
                    backgroundColor: `color-mix(in srgb, ${step.business_status_color} 10%, transparent)`,
                  }
                : undefined}
            >
              {step.business_status}
            </span>
          </div>
        )}

        {step.goal && (
          <details className="group border-t border-border/60 pt-2">
            <summary className="cursor-pointer text-[11px] text-muted-foreground outline-none hover:text-foreground focus-visible:rounded focus-visible:ring-2 focus-visible:ring-ring">
              查看节点目标
            </summary>
            <p className="mt-1.5 max-h-28 overflow-y-auto whitespace-pre-wrap text-[11px] leading-relaxed text-muted-foreground">
              {step.goal}
            </p>
          </details>
        )}

        {step.child_agents.length > 0 && (
          <div className="space-y-1.5 border-t border-border/60 pt-2">
            <p className="text-[11px] text-muted-foreground">
              本次委派 · {step.child_agents.length}
            </p>
            <div className="flex flex-col gap-1">
              {step.child_agents.map((agent) => (
                <Button
                  key={agent.task_id}
                  type="button"
                  variant="secondary"
                  size="sm"
                  className="h-7 min-w-0 justify-start gap-1.5 px-2 text-[11px]"
                  onClick={() => onOpenAgent(agent.task_id, agent.title, "子 Agent")}
                >
                  <BotIcon className="size-3 shrink-0" aria-hidden="true" />
                  <span className="min-w-0 flex-1 truncate text-left">{agent.title}</span>
                  <span className="shrink-0 text-muted-foreground">
                    {STATUS_LABELS[agent.status] ?? agent.status}
                  </span>
                </Button>
              ))}
            </div>
          </div>
        )}
      </div>
    </article>
  );
}

function TerminalMarker({ run }: { run: AgentTeamRun }) {
  if (run.status === "completed") {
    return (
      <div className="flex w-24 shrink-0 flex-col items-center gap-2" aria-label="流程完成">
        <div className="grid size-11 place-items-center rounded-full border border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400">
          <CheckIcon className="size-5" aria-hidden="true" />
        </div>
        <span className="text-xs font-medium text-emerald-700 dark:text-emerald-400">END</span>
      </div>
    );
  }
  if (run.status === "failed" || run.status === "cancelled") {
    const label = run.status === "failed" ? "执行失败" : "已取消";
    return (
      <div className="flex w-28 shrink-0 flex-col items-center gap-2" aria-label={label}>
        <div className="grid size-11 place-items-center rounded-full border border-destructive/40 bg-destructive/10 text-destructive">
          <XIcon className="size-5" aria-hidden="true" />
        </div>
        <span className="text-center text-xs font-medium text-destructive">{label}</span>
      </div>
    );
  }
  return null;
}

export function AgentTeamExecutionPipeline({ run, onOpenAgent }: AgentTeamExecutionPipelineProps) {
  const terminal = <TerminalMarker run={run} />;
  const showTerminal = run.status === "completed" || run.status === "failed" || run.status === "cancelled";
  const scrollerRef = useRef<HTMLDivElement>(null);
  const followLatestRef = useRef(true);
  const [showLatestButton, setShowLatestButton] = useState(false);
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
    setShowLatestButton(!followLatestRef.current);
  };

  const jumpToLatest = () => {
    followLatestRef.current = true;
    setShowLatestButton(false);
    scrollerRef.current?.scrollTo({ left: scrollerRef.current.scrollWidth, behavior: "smooth" });
  };

  return (
    <section aria-label="实际执行流水线" className="rounded-lg border border-border/60 bg-muted/10 p-3">
      {showLatestButton && (
        <div className="mb-2 flex justify-end">
          <Button type="button" size="xs" variant="outline" onClick={jumpToLatest}>
            跳到最新步骤
          </Button>
        </div>
      )}
      <div ref={scrollerRef} onScroll={handleScroll} className="overflow-x-auto pb-2 [scrollbar-width:thin]">
        <ol className="flex min-w-max items-center gap-2" aria-label="节点访问顺序">
          <li className="flex w-20 shrink-0 flex-col items-center gap-2" aria-label="流程开始">
            <div className="grid size-10 place-items-center rounded-full border border-border bg-background text-muted-foreground">
              <span className="text-[10px] font-semibold">START</span>
            </div>
            <span className="text-[11px] text-muted-foreground">开始</span>
          </li>
          {run.execution_steps.map((step, index) => {
            const nextStep = run.execution_steps[index + 1];
            return (
              <li key={`${step.conversation_run_id ?? `queued-${step.node_id}`}-${step.step_number}`} className="flex items-center gap-2">
                {index === 0 && <TransitionConnector />}
                <ExecutionStepCard
                  step={step}
                  isCurrent={currentStep?.step_number === step.step_number}
                  onOpenAgent={onOpenAgent}
                />
                {nextStep && <TransitionConnector step={step} />}
              </li>
            );
          })}
          {showTerminal && (
            <li className="flex items-center gap-2">
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
      {run.execution_steps.length > 0 && (
        <p className="mt-1 text-[11px] text-muted-foreground">
          按执行顺序展示节点访问；回环会作为新的访问步骤出现。
        </p>
      )}
    </section>
  );
}
