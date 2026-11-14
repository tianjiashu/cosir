import { useState, type FormEvent } from "react";
import { ArrowLeftIcon, RefreshCwIcon, SearchIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { ContextMessageQuery } from "@/lib/api/context-messages";
import { parseId } from "./message-model";

export function QueryBar({
  session,
  turn,
  busy,
  hasQuery,
  onQuery,
  onRefresh,
  onBack,
}: {
  session: string;
  turn: string;
  busy: boolean;
  hasQuery: boolean;
  onQuery: (query: ContextMessageQuery) => void;
  onRefresh: () => void;
  onBack?: () => void;
}) {
  const [sessionInput, setSessionInput] = useState(session);
  const [turnInput, setTurnInput] = useState(turn);
  const [error, setError] = useState<string | null>(null);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    const sessionValue = sessionInput.trim();
    const turnValue = turnInput.trim();
    const sessionId = parseId(sessionValue);
    const turnNumber = parseId(turnValue);
    if ((sessionValue && !sessionId) || (turnValue && !turnNumber)) {
      setError("Session ID 和回合必须是正整数。");
      return;
    }
    if (!sessionId) {
      setError("请输入 Session ID。");
      return;
    }
    setError(null);
    onQuery({ sessionId, turn: turnNumber });
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
            返回会话
          </Button>
        )}
        <label htmlFor="context-session-id" className="text-xs font-medium">
          Session
        </label>
        <Input
          id="context-session-id"
          aria-label="Session ID"
          className="w-28 font-mono text-xs"
          inputMode="numeric"
          placeholder="会话 ID"
          value={sessionInput}
          onChange={(event) => setSessionInput(event.target.value)}
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
