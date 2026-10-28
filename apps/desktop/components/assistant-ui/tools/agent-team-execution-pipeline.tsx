import { Fragment } from "react";
import {
  ArrowRightIcon,
  BanIcon,
  BotIcon,
  CheckIcon,
  CircleIcon,
  Clock3Icon,
  LoaderCircleIcon,
  PauseCircleIcon,
  XIcon,
} from "lucide-react";

import type { AgentTeamRun } from "@/lib/api/agent-teams";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

type TeamNode = AgentTeamRun["nodes"][number];
type NodeVisit = TeamNode["visits"][number];
type TeamAgent = NodeVisit["agents"][number];

type AgentTeamExecutionPipelineProps = {
  run: AgentTeamRun;
  onOpenAgent: (taskId: number, title: string) => void;
};

const STATUS_LABELS: Record<string, string> = {
  queued: "待执行",
  pending: "等待中",
  running: "执行中",
  suspended: "等待输入",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  skipped: "未执行",
};

const HEADER_ROW_CLASS = "flex h-8 min-w-0";
const RAIL_ROW_CLASS = "flex h-3 items-center";

function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status;
}

function AgentStatusIcon({ status }: { status: string }) {
  const label = statusLabel(status);
  if (status === "running") {
    return <LoaderCircleIcon className="size-4 shrink-0 animate-spin text-primary motion-reduce:animate-none" aria-label={label} />;
  }
  if (status === "completed") {
    return <CheckIcon className="size-4 shrink-0 text-success" aria-label={label} />;
  }
  if (status === "failed") {
    return <XIcon className="size-4 shrink-0 text-destructive" aria-label={label} />;
  }
  if (status === "cancelled") {
    return <BanIcon className="size-4 shrink-0 text-muted-foreground" aria-label={label} />;
  }
  if (status === "suspended") {
    return <PauseCircleIcon className="size-4 shrink-0 animate-pulse text-warning motion-reduce:animate-none" aria-label={label} />;
  }
  if (status === "pending" || status === "queued") {
    return <Clock3Icon className="size-4 shrink-0 text-muted-foreground" aria-label={label} />;
  }
  return <CircleIcon className="size-4 shrink-0 text-muted-foreground" aria-label={label} />;
}

function AgentChip({
  agent,
  onOpenAgent,
}: {
  agent: TeamAgent;
  onOpenAgent: AgentTeamExecutionPipelineProps["onOpenAgent"];
}) {
  return (
    <Button
      type="button"
      variant="secondary"
      size="sm"
      className="h-7 w-full min-w-0 justify-start gap-1.5 rounded-full border-transparent bg-secondary/60 px-2 text-2xs hover:bg-secondary"
      disabled={agent.task_id === null}
      title={[agent.role, agent.agent_id, statusLabel(agent.status), agent.title].filter(Boolean).join(" · ")}
      aria-label={`${agent.role}，${statusLabel(agent.status)}`}
      onClick={() => agent.task_id !== null && onOpenAgent(agent.task_id, agent.title)}
    >
      <BotIcon
        className={`size-4 shrink-0 ${agent.status === "running" ? "animate-pulse text-primary motion-reduce:animate-none" : "text-muted-foreground"}`}
        aria-hidden="true"
      />
      <span className="min-w-0 flex-1 truncate text-left">{agent.role}</span>
      <AgentStatusIcon status={agent.status} />
    </Button>
  );
}

function NodeMarker({ node, isCurrent }: { node: TeamNode; isCurrent: boolean }) {
  const label = statusLabel(node.status);
  const tone = isCurrent
    ? "border-primary bg-primary ring-4 ring-primary/15"
    : node.status === "completed"
      ? "border-success bg-success"
      : node.status === "failed" || node.status === "cancelled"
        ? "border-destructive bg-destructive"
        : "border-muted-foreground/50 bg-background";

  return (
    <span
      className={`size-3 shrink-0 cursor-help rounded-full border-2 ${tone}`}
      title={label}
      aria-label={label}
      role="img"
    />
  );
}

function TransitionConnector() {
  return (
    <li className="min-w-0" aria-hidden="true">
      <div className={`${HEADER_ROW_CLASS} items-center`} />
      <div className={`${RAIL_ROW_CLASS} min-w-0`}>
        <span className="h-px flex-1 bg-border" />
        <ArrowRightIcon className="size-3 shrink-0 translate-x-0.5 text-muted-foreground/60" />
      </div>
    </li>
  );
}

function HistoricalVisits({
  visits,
  onOpenAgent,
}: {
  visits: NodeVisit[];
  onOpenAgent: AgentTeamExecutionPipelineProps["onOpenAgent"];
}) {
  if (visits.length === 0) return null;
  return (
    <details className="group pt-0.5">
      <summary className="focus-ring cursor-pointer rounded text-2xs text-muted-foreground hover:text-foreground">
        历史访问（{visits.length}）
      </summary>
      <div className="mt-1.5 space-y-2">
        {visits.map((visit) => (
          <div key={visit.conversation_run_id ?? visit.visit_number} className="space-y-1 border-l border-border/70 pl-2">
            <p className="text-2xs text-muted-foreground">
              第 {visit.visit_number} 次 · {statusLabel(visit.status)}
            </p>
            {visit.agents.map((agent) => (
              <AgentChip key={agent.task_id} agent={agent} onOpenAgent={onOpenAgent} />
            ))}
          </div>
        ))}
      </div>
    </details>
  );
}

function PipelineNode({
  node,
  isCurrent,
  onOpenAgent,
}: {
  node: TeamNode;
  isCurrent: boolean;
  onOpenAgent: AgentTeamExecutionPipelineProps["onOpenAgent"];
}) {
  const latestVisit = node.visits.at(-1);
  const previousVisits = node.visits.slice(0, -1);
  const title = node.goal_summary || node.name;
  const configuredAgent = {
    task_id: null,
    title: node.name,
    agent_id: node.agent_id,
    role: node.role,
    status: node.status,
  } as const;

  return (
    <div className="w-full min-w-0" aria-current={isCurrent ? "step" : undefined}>
      <div className={`${HEADER_ROW_CLASS} items-start`}>
        <div className="flex min-w-0 flex-1 items-center gap-1.5">
          <p className="truncate text-xs font-semibold" title={title}>{title}</p>
          <span className={`shrink-0 text-2xs ${node.status === "failed" ? "text-destructive" : "text-muted-foreground"}`}>
            {statusLabel(node.status)}
          </span>
          {latestVisit && latestVisit.visit_number > 1 && (
            <Badge shape="tag" size="sm">
              第 {latestVisit.visit_number} 次
            </Badge>
          )}
        </div>
      </div>
      <div className={RAIL_ROW_CLASS}>
        <NodeMarker node={node} isCurrent={isCurrent} />
        <span className="h-px flex-1 bg-border" aria-hidden="true" />
      </div>
      <div className="mt-2 space-y-1.5">
        {(latestVisit?.agents ?? [configuredAgent]).map((agent) => (
          <AgentChip key={agent.task_id ?? `configured-${node.node_id}`} agent={agent} onOpenAgent={onOpenAgent} />
        ))}
        <HistoricalVisits visits={previousVisits} onOpenAgent={onOpenAgent} />
      </div>
    </div>
  );
}

function StartMarker() {
  return (
    <li className="min-w-0" aria-label="流程开始">
      <div className={`${HEADER_ROW_CLASS} items-center`}>
        <span className="text-2xs font-semibold text-muted-foreground">START</span>
      </div>
      <div className={RAIL_ROW_CLASS}>
        <span className="size-3 shrink-0 rounded-full border-2 border-muted-foreground/50 bg-background" aria-hidden="true" />
        <span className="h-px flex-1 bg-border" aria-hidden="true" />
      </div>
    </li>
  );
}

function EndMarker({ run }: { run: AgentTeamRun }) {
  const completed = run.status === "completed";
  const label = completed ? "流程完成" : "未到达";
  const tone = completed ? "text-success" : "text-muted-foreground";
  return (
    <li className="min-w-0" aria-label={label}>
      <div className={`${HEADER_ROW_CLASS} items-center gap-1`}>
        {completed
          ? <CheckIcon className={`size-3.5 shrink-0 ${tone}`} aria-hidden="true" />
          : null}
        <span className={`text-2xs font-medium ${tone}`}>END</span>
      </div>
      <div className={RAIL_ROW_CLASS}>
        <span
          className={`size-3 shrink-0 rounded-full border-2 ${completed
            ? "border-success bg-success"
            : "border-muted-foreground/50 bg-background"}`}
          title={label}
          aria-hidden="true"
        />
      </div>
    </li>
  );
}

export function AgentTeamExecutionPipeline({ run, onOpenAgent }: AgentTeamExecutionPipelineProps) {
  const stages = run.nodes.length + 2;
  const columns = Array.from({ length: stages * 2 - 1 }, (_, index) =>
    index % 2 === 0 ? "minmax(0, 1fr)" : "1.25rem",
  ).join(" ");

  return (
    <section
      aria-label="Agent Team 流水线，可横向滚动查看全部节点"
      tabIndex={0}
      className="focus-ring overflow-x-auto rounded-lg border border-border/60 bg-muted/10 p-3"
    >
      <ol
        className="grid w-full min-w-0 items-start"
        style={{ gridTemplateColumns: columns, minWidth: `${stages * 150}px` }}
        aria-label="按配置顺序排列的节点"
      >
        <StartMarker />
        {run.nodes.map((node) => (
          <Fragment key={node.node_id}>
            <TransitionConnector />
            <li className="min-w-0">
              <PipelineNode
                node={node}
                isCurrent={run.status === "running" && run.current_node_id === node.node_id}
                onOpenAgent={onOpenAgent}
              />
            </li>
          </Fragment>
        ))}
        <TransitionConnector />
        <EndMarker run={run} />
      </ol>
    </section>
  );
}
