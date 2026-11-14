import { ArrowLeftIcon, ChevronLeftIcon, ChevronRightIcon, RefreshCwIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { NativeSelect } from "@/components/ui/native-select";

/**
 * 观测范围：哪个会话、看全部回合还是其中一个。会话由入口带进来（侧栏入口带当前会话，回复上的
 * 诊断入口带会话与回合），这里只显示它是谁；回合在这里切换。
 */
export function QueryBar({
  sessionId,
  title,
  turn,
  lastTurn,
  busy,
  onTurn,
  onRefresh,
  onBack,
}: {
  sessionId: number;
  /** 读到结果之前还不知道标题。 */
  title: string | undefined;
  turn: number | undefined;
  /** 读到结果之前还不知道有多少回合。 */
  lastTurn: number | undefined;
  busy: boolean;
  onTurn: (turn: number | undefined) => void;
  onRefresh: () => void;
  onBack?: () => void;
}) {
  // 地址里的回合可以超出已知范围（记录被撤回或地址写错），仍要能在下拉里显示出来。
  const turns = Math.max(lastTurn ?? 0, turn ?? 0);
  return (
    <div
      className="flex shrink-0 flex-wrap items-center gap-2 border-b bg-muted/20 px-5 py-3"
      role="group"
      aria-label="观测范围"
    >
      {onBack && (
        <Button variant="ghost" size="sm" onClick={onBack} data-testid="session-observer-back">
          <ArrowLeftIcon />
          返回会话
        </Button>
      )}
      <p className="min-w-0 truncate text-sm font-medium" data-testid="session-observer-title">
        {title ?? "会话"}
        <span className="ml-2 font-mono text-xs font-normal text-muted-foreground">#{sessionId}</span>
      </p>
      <div className="ml-auto flex items-center gap-1">
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label="上一个回合"
          disabled={turn === undefined || turn <= 1}
          onClick={() => onTurn(turn! - 1)}
        >
          <ChevronLeftIcon />
        </Button>
        <NativeSelect
          size="sm"
          wrapperClassName="w-auto"
          aria-label="回合"
          value={turn ?? "all"}
          onChange={(event) => onTurn(event.target.value === "all" ? undefined : Number(event.target.value))}
        >
          <option value="all">全部回合{lastTurn ? `（共 ${lastTurn} 个）` : ""}</option>
          {Array.from({ length: turns }, (_, index) => (
            <option key={index + 1} value={index + 1}>
              回合 {index + 1}
            </option>
          ))}
        </NativeSelect>
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label="下一个回合"
          disabled={turn === undefined || lastTurn === undefined || turn >= lastTurn}
          onClick={() => onTurn(turn! + 1)}
        >
          <ChevronRightIcon />
        </Button>
        <Button size="sm" variant="ghost" onClick={onRefresh} disabled={busy}>
          <RefreshCwIcon className={busy ? "animate-spin motion-reduce:animate-none" : ""} />
          {busy ? "正在读取" : "刷新"}
        </Button>
      </div>
    </div>
  );
}
