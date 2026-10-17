import { useState, type FormEvent } from "react";
import { ArrowLeftIcon, RefreshCwIcon, SearchIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { ContextMessageQuery } from "@/lib/api/context-messages";
import { parseId } from "./message-model";

export function QueryBar({
  task,
  run,
  busy,
  hasQuery,
  onQuery,
  onRefresh,
  onBack,
}: {
  task: string;
  run: string;
  busy: boolean;
  hasQuery: boolean;
  onQuery: (query: ContextMessageQuery) => void;
  onRefresh: () => void;
  onBack?: () => void;
}) {
  const [taskInput, setTaskInput] = useState(task);
  const [runInput, setRunInput] = useState(run);
  const [error, setError] = useState<string | null>(null);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    const taskValue = taskInput.trim();
    const runValue = runInput.trim();
    const taskId = parseId(taskValue);
    const runId = parseId(runValue);
    if ((taskValue && !taskId) || (runValue && !runId)) {
      setError("Task ID 和 Run ID 必须是正整数。");
      return;
    }
    if (!taskId && !runId) {
      setError("请输入 Task ID 或 Run ID。");
      return;
    }
    setError(null);
    onQuery({ taskId, runId });
  };
  return (
    <form
      onSubmit={submit}
      className="shrink-0 border-b bg-muted/20 px-5 py-3"
      aria-label="查询上下文记录"
    >
      <div className="flex flex-wrap items-center gap-2">
        {onBack && (
          <Button
            variant="ghost"
            size="sm"
            type="button"
            onClick={onBack}
            data-testid="context-observer-back"
          >
            <ArrowLeftIcon />
            返回对话
          </Button>
        )}
        <label htmlFor="context-task-id" className="text-xs font-medium">
          Task
        </label>
        <Input
          id="context-task-id"
          aria-label="Task ID"
          className="w-28 font-mono text-xs"
          inputMode="numeric"
          placeholder="任务 ID"
          value={taskInput}
          onChange={(event) => setTaskInput(event.target.value)}
        />
        <label htmlFor="context-run-id" className="ml-1 text-xs font-medium">
          Run
        </label>
        <Input
          id="context-run-id"
          aria-label="Run ID"
          className="w-32 font-mono text-xs"
          inputMode="numeric"
          placeholder="轮次 ID，可选"
          value={runInput}
          onChange={(event) => setRunInput(event.target.value)}
        />
        <Button type="submit" size="sm">
          <SearchIcon />
          查询
        </Button>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          onClick={onRefresh}
          disabled={busy || !hasQuery}
        >
          <RefreshCwIcon
            className={busy ? "animate-spin motion-reduce:animate-none" : ""}
          />
          {busy ? "正在读取" : "刷新"}
        </Button>
        <span className="text-xs text-muted-foreground sm:ml-auto">
          可单填任一 ID；同时填写时限定所属关系
        </span>
      </div>
      {error && (
        <p role="alert" className="mt-2 text-xs text-destructive">
          {error}
        </p>
      )}
    </form>
  );
}
