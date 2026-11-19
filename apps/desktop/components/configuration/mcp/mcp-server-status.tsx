import { KeyRoundIcon, Loader2Icon } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { isTestInProgress, type ServerTestState } from "@/lib/mcp/server-test-controller";
import type { MCPServerStatus } from "@/lib/api/extensions";

// 只给需要用户做点什么的状态出徽标：「可用」不说话，「已停用」由整行变灰与开关表达。
const STATUS_LABELS: Partial<Record<MCPServerStatus, { label: string; tone: "warning" | "neutral" }>> = {
  untested: { label: "待测试", tone: "neutral" },
  missing_variables: { label: "需要填写变量", tone: "warning" },
  invalid_configuration: { label: "配置有误", tone: "warning" },
  authorization_required: { label: "需要登录授权", tone: "warning" },
};

const STATUS_EXPLAINED_FAILURES = new Set(["authorization_required", "missing_variables"]);

/**
 * 一个服务的主状态与下一步：列表行和编辑框共用。没有要说的（状态正常、也没有测试进展）时不渲染。
 *
 * `status` 是后端由本机事实推出的主状态（编辑框里的草稿没有，传 `null`）；`testState` 是界面上的临时进展。
 * 主状态或本次测试结果要求授权时给出「登录授权」（草稿没有主状态，只看测试结果）。授权进行中可以取消，
 * 不会锁住别的服务。`readOnly` 用于工作区里继承来的系统级服务（只能在系统配置里操作）。
 */
export function MCPServerStatusLine({
  status,
  testState,
  readOnly = false,
  onAuthorize,
  onCancel,
}: {
  status: MCPServerStatus | null;
  testState: ServerTestState | undefined;
  readOnly?: boolean;
  onAuthorize: () => void;
  onCancel: () => void;
}) {
  const result = testState?.phase === "done" ? testState.result : null;
  const needsSignIn = status === "authorization_required" || result?.needs_sign_in === true;
  // 主状态已经说了「需要登录授权」「需要填写变量」时，不再把同一件事当成失败原因重复一遍。
  const showResult = result !== null && !(status !== null && STATUS_EXPLAINED_FAILURES.has(result.failure_kind ?? ""));
  const badge = status !== null ? STATUS_LABELS[status] : undefined;
  if (!badge && !testState) return null;
  return (
    <div className="mt-1 space-y-2 text-xs">
      <div className="flex flex-wrap items-center gap-2 empty:hidden">
        {badge && <Badge size="sm" tone={badge.tone}>{badge.label}</Badge>}
        {testState?.phase === "testing" && <span className="text-muted-foreground inline-flex items-center gap-1"><Loader2Icon className="size-3 animate-spin" />测试中…</span>}
        {testState?.phase === "authorizing" && (
          <>
            <span className="text-muted-foreground inline-flex items-center gap-1">
              <Loader2Icon className="size-3 animate-spin" />
              {testState.host ? `已在浏览器打开 ${testState.host}，完成登录后回到这里` : "正在打开登录授权页…"}
            </span>
            <Button variant="ghost" size="xs" onClick={onCancel}>取消</Button>
          </>
        )}
        {needsSignIn && !readOnly && !isTestInProgress(testState) && (
          <Button variant="outline" size="xs" onClick={onAuthorize}><KeyRoundIcon />登录授权</Button>
        )}
      </div>
      {showResult && (
        <p className={result.success ? "text-success" : "text-destructive"}>
          {result.success ? `本次测试成功 · 发现 ${result.tools.length} 个工具` : `本次测试失败 · ${result.error || "请检查服务配置"}`}
        </p>
      )}
      {result?.success && result.tools.length > 0 && (
        <details className="rounded-lg bg-muted/40 px-3 py-2">
          <summary className="cursor-pointer font-medium">查看发现的工具</summary>
          <ul className="mt-2 max-h-48 space-y-1.5 overflow-y-auto">
            {result.tools.map((tool) => (
              <li key={tool.name}>
                <span className="font-mono font-medium">{tool.name}</span>
                {tool.description && <span className="text-muted-foreground ml-2">{tool.description}</span>}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}
