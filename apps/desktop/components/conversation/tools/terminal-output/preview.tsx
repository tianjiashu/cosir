import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ansiColor, xtermCardTheme } from "@/components/terminal/xterm-appearance";
import { frontendLog } from "@/lib/logging/frontend-log";
import { useThemeState } from "@/lib/theme/theme-preference";

import { TerminalOutputParser } from "./output-parser";
import type { OutputRun, OutputTail } from "./text";

/**
 * 卡片预览的解析结果：末尾几行与按需读取的全文。解析器随卡片挂载常驻、增量写入（见 `TerminalOutputParser`）；
 * 解析是异步的，新结果到达前继续显示上一版，流式输出不闪空。
 */
export function useParsedOutput(output: string): { tail: OutputTail | null; readText: () => string } {
  const parser = useRef<TerminalOutputParser | null>(null);
  const [tail, setTail] = useState<OutputTail | null>(null);
  useEffect(() => {
    const current = new TerminalOutputParser(setTail, (error) => {
      void frontendLog("WARNING", "terminal_preview_parse_failed", "终端输出预览解析失败", { error });
    });
    parser.current = current;
    return () => {
      current.dispose();
      parser.current = null;
    };
  }, []);
  useEffect(() => {
    parser.current?.update(output);
  }, [output]);
  const readText = useCallback(() => parser.current?.text() ?? "", []);
  return { tail, readText };
}

/** 卡片里的输出摘要：末尾几行带颜色的文字，完整输出与可点的链接在阅读器。 */
export function TerminalOutputPreview({ lines }: { lines: OutputRun[][] }) {
  const { resolvedTheme } = useThemeState();
  const theme = useMemo(() => xtermCardTheme(resolvedTheme), [resolvedTheme]);
  return (
    <pre data-terminal-mode="preview" className="font-mono break-words whitespace-pre-wrap" style={{ color: theme.foreground }}>
      {lines.map((runs, index) => <div key={index} className="min-h-[1lh]">
        {runs.map((run, runIndex) => <span key={runIndex} style={{
          color: run.color === null ? undefined : ansiColor(run.color, theme),
          fontWeight: run.bold ? 600 : undefined,
          opacity: run.dim ? 0.7 : undefined,
        }}>{run.text}</span>)}
      </div>)}
    </pre>
  );
}
