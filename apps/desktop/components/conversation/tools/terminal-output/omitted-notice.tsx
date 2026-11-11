import { resolvePathText, type LinkTarget } from "@/lib/links/resolve-link";

/** 打开完整日志需要的最小链接能力：工作区根 + 打开入口（与卡片「启动目录」按钮同一条链路）。 */
export type OmittedNoticeLinks = {
  workspaceRoot: string | null | undefined;
  open: (target: LinkTarget) => void;
};

/**
 * 命令卡片输出被截断时的提示：只说明「前面省略 N 行」，完整日志在工作区里时给一个打开入口。
 *
 * 后端卡片只保留最近一段原始输出（窗口规则见 `core/tool/terminal_output_window.py`），超过模型
 * 预算时完整输出边跑边写进工作区日志；这里不自己截断，只读展示数据。
 */
export function OmittedOutputNotice({
  omittedLines,
  logPath,
  links,
}: {
  omittedLines?: number;
  logPath?: string;
  links: OmittedNoticeLinks;
}) {
  if (omittedLines === undefined || omittedLines <= 0) return null;
  const target = logPath ? resolvePathText(logPath, { workspaceRoot: links.workspaceRoot ?? null }) : null;
  return (
    <p className="text-muted-foreground flex flex-wrap items-center gap-2 text-xs" data-terminal-output-omitted={omittedLines}>
      <span>前面省略 {omittedLines} 行</span>
      {target && <button type="button" className="hover:text-foreground underline-offset-2 hover:underline" onClick={() => links.open(target)}>打开完整日志</button>}
    </p>
  );
}
