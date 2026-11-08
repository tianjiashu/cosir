import { useEffect, useMemo, useState } from "react";

import { ansiColor, xtermCardTheme } from "@/components/terminal/xterm-appearance";
import { useThemeState } from "@/lib/theme/theme-preference";

import { parseTerminalOutput, type OutputRun, type ParsedOutput } from "./text";

/** 解析是异步的：新结果到达前继续显示上一版，流式输出不闪空。 */
export function useParsedOutput(output: string): ParsedOutput | null {
  const [parsed, setParsed] = useState<ParsedOutput | null>(null);
  useEffect(() => {
    let current = true;
    void parseTerminalOutput(output).then((next) => { if (current) setParsed(next); });
    return () => { current = false; };
  }, [output]);
  return parsed;
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
