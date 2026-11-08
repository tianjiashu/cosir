import { useState, type FormEvent } from "react";
import { ArrowLeftIcon, RefreshCwIcon, SearchIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { ContextMessageQuery } from "@/lib/api/context-messages";
import { parseId } from "./message-model";

export function QueryBar({
  task,
  turn,
  busy,
  hasQuery,
  onQuery,
  onRefresh,
  onBack,
}: {
  task: string;
  turn: string;
  busy: boolean;
  hasQuery: boolean;
  onQuery: (query: ContextMessageQuery) => void;
  onRefresh: () => void;
  onBack?: () => void;
}) {
  const [taskInput, setTaskInput] = useState(task);
  const [turnInput, setTurnInput] = useState(turn);
  const [error, setError] = useState<string | null>(null);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    const taskValue = taskInput.trim();
    const turnValue = turnInput.trim();
    const taskId = parseId(taskValue);
    const turnNumber = parseId(turnValue);
    if ((taskValue && !taskId) || (turnValue && !turnNumber)) {
      setError("Task ID 和回合必须是正整数。");
      return;
    }
    if (!taskId) {
      setError("请输入 Task ID。");
      return;
    }
    setError(null);
    onQuery({ taskId, turn: turnNumber });
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
        <label htmlFor="context-turn" className="ml-1 text-xs font-medium">
          回合
        </label>
        <Input
          id="context-turn"
          aria-label="回合"
          className="w-32 font-mono text-xs"
          inputMode="numeric"
          placeholder="回合号，可选"
          value={turnInput}
          onChange={(event) => setTurnInput(event.target.value)}
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
      </div>
      {error && (
        <p role="alert" className="mt-2 text-xs text-destructive">
          {error}
        </p>
      )}
    </form>
  );
}
