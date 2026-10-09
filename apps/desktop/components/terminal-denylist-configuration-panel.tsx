"use client";

import { useEffect, useState } from "react";
import { Loader2Icon, SaveIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  getTerminalDenylistConfiguration,
  updateTerminalDenylistConfiguration,
} from "@/lib/api/configuration";

type LoadState = "loading" | "ready" | "error";

/** 系统级终端命令拒绝规则编辑器；每行是一条忽略大小写的正则表达式。 */
export function TerminalDenylistConfigurationPanel() {
  const [patternsText, setPatternsText] = useState("");
  const [loadState, setLoadState] = useState<LoadState>("loading");
  const [isSaving, setIsSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    void getTerminalDenylistConfiguration()
      .then(({ patterns }) => {
        if (!active) return;
        setPatternsText(patterns.join("\n"));
        setLoadState("ready");
      })
      .catch(() => {
        if (!active) return;
        setLoadState("error");
        setError("读取终端 deny-list 失败，请检查配置或重新打开页面。");
      });
    return () => {
      active = false;
    };
  }, []);

  async function savePatterns() {
    const patterns = patternsText
      .split(/\r?\n/)
      .filter((pattern) => pattern.trim().length > 0);
    setIsSaving(true);
    setError(null);
    setMessage(null);
    try {
      const saved = await updateTerminalDenylistConfiguration(patterns);
      setPatternsText(saved.patterns.join("\n"));
      setMessage("配置已保存，下一次 execute_terminal 调用立即生效。");
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : "保存终端 deny-list 失败。");
    } finally {
      setIsSaving(false);
    }
  }

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

      {loadState === "loading" ? (
        <div className="text-muted-foreground flex items-center gap-2 py-8 text-sm">
          <Loader2Icon className="size-4 animate-spin" />正在读取规则…
        </div>
      ) : loadState === "error" ? (
        <div role="alert" className="text-destructive rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm">
          {error}
        </div>
      ) : (
        <>
          <Textarea
            aria-label="终端 deny-list 正则列表"
            className="min-h-[28rem] font-mono text-xs"
            disabled={isSaving}
            spellCheck={false}
            value={patternsText}
            onChange={(event) => {
              setPatternsText(event.target.value);
              setMessage(null);
              setError(null);
            }}
            placeholder="\\brm\\b"
          />
          {error && <div role="alert" className="text-destructive text-sm">{error}</div>}
          {message && <div role="status" className="text-sm text-emerald-700 dark:text-emerald-400">{message}</div>}
          <div className="flex justify-end">
            <Button onClick={() => void savePatterns()} disabled={isSaving}>
              {isSaving ? <Loader2Icon className="animate-spin" /> : <SaveIcon />}
              保存规则
            </Button>
          </div>
        </>
      )}
    </section>
  );
}
