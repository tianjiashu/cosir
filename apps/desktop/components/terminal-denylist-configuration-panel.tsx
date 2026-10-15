"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2Icon, SaveIcon } from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ErrorState, LoadingState } from "@/components/ui/state-panel";
import { Textarea } from "@/components/ui/textarea";
import {
  getTerminalDenylistConfiguration,
  updateTerminalDenylistConfiguration,
} from "@/lib/api/configuration";
import { userErrorMessage } from "@/lib/errors/user-error-message";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";
import { queryKeys } from "@/lib/query/query-keys";
import { useUnsavedChanges } from "@/lib/unsaved-changes";

/** 系统级终端命令拒绝规则编辑器；每行是一条忽略大小写的正则表达式。 */
export function TerminalDenylistConfigurationPanel() {
  const query = useQuery({
    queryKey: queryKeys.terminalDenylist(),
    queryFn: ({ signal }) => getTerminalDenylistConfiguration({ signal }),
  });

  return (
    <section className="space-y-5" aria-labelledby="terminal-denylist-heading">
      <header className="space-y-2">
        <h2 id="terminal-denylist-heading" className="text-lg font-semibold">终端命令 deny-list</h2>
        <p className="text-muted-foreground text-sm">
          每行填写一条正则表达式，忽略大小写；命令文本命中任意一条时会被拒绝。空行会被忽略。
          此配置只应用于 execute_terminal，不检查交互终端。
        </p>
        <p className="text-muted-foreground text-sm">
          清空列表会关闭 deny-list 命令拦截。正则语法错误、匹配超时或命令过长会阻止命令执行。
        </p>
      </header>

      {query.data ? (
        <TerminalDenylistEditor savedText={query.data.patterns.join("\n")} />
      ) : query.isError ? (
        <ErrorState
          message={userErrorMessage(query.error, "读取终端 deny-list 失败，请检查配置或重新打开页面。")}
          onRetry={() => void query.refetch()}
          retryLabel="重新读取"
        />
      ) : (
        <LoadingState label="正在读取规则…" className="py-8" />
      )}
    </section>
  );
}

/**
 * 规则正文编辑副本。草稿为 null 时跟随服务端（查询缓存是事实源），
 * 因此后台重新读取不会覆盖正在编辑的内容。
 */
function TerminalDenylistEditor({ savedText }: { savedText: string }) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<string | null>(null);
  const patternsText = draft ?? savedText;
  useUnsavedChanges("settings", "终端命令规则", draft !== null && draft !== savedText);

  const mutation = useMutation({
    mutationFn: (text: string) => updateTerminalDenylistConfiguration(
      text.split(/\r?\n/).filter((pattern) => pattern.trim().length > 0),
    ),
    onSuccess: (saved) => {
      queryClient.setQueryData(queryKeys.terminalDenylist(), saved);
      setDraft(null);
      notify.success("配置已保存，下一次 execute_terminal 调用立即生效。");
    },
    onError: (error) => {
      void frontendLog("ERROR", "terminal_denylist_save_failed", "保存终端 deny-list 失败", { error });
    },
  });

  return (
    <>
      <Textarea
        aria-label="终端 deny-list 正则列表"
        className="min-h-[28rem] font-mono text-xs"
        disabled={mutation.isPending}
        spellCheck={false}
        value={patternsText}
        onChange={(event) => {
          setDraft(event.target.value);
          if (mutation.isError) mutation.reset();
        }}
        placeholder="\\brm\\b"
      />
      {mutation.isError && <Alert tone="destructive">{userErrorMessage(mutation.error, "保存终端 deny-list 失败。")}</Alert>}
      <div className="flex justify-end">
        <Button onClick={() => mutation.mutate(patternsText)} disabled={mutation.isPending}>
          {mutation.isPending ? <Loader2Icon className="animate-spin" /> : <SaveIcon />}
          保存规则
        </Button>
      </div>
    </>
  );
}
