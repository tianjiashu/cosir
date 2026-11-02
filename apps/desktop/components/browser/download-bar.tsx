import { CircleAlertIcon, CircleCheckIcon, FolderOpenIcon } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { TooltipIconButton } from "@/components/ui/tooltip-icon-button";
import type { BrowserDownload, CosirHost } from "@/electron/shared/host-contract";
import { formatByteSize } from "@/lib/format/byte-size";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";

/**
 * Task 最近的下载：浏览器面板底部的一条，平时只显示最新一次，多于一次时可展开全部（新的在上）。
 *
 * 下载列表是宿主事实（随浏览器状态推送，旧的在前），下载归 Task 不归标签，触发它的标签关掉后仍在；这里只渲染，
 * 文件落在 Task 所在工作区的下载目录。落盘路径随推送到达渲染进程，只用作文件名的悬停提示；「在文件夹中显示」
 * 只按下载 id 请宿主定位，宿主不接受渲染进程给的任意路径。
 */
export function DownloadBar({ host, downloads }: { host: CosirHost; downloads: readonly BrowserDownload[] }) {
  const [expanded, setExpanded] = useState(false);
  const shown = expanded ? [...downloads].reverse() : downloads.slice(-1);

  const reveal = (download: BrowserDownload) => {
    host.browser.revealDownload(download.id).catch((error: unknown) => {
      void frontendLog("WARNING", "browser_download_reveal_failed", "在文件夹中显示下载失败", { data: { taskId: download.taskId, downloadId: download.id }, error });
      notify.error("无法在文件夹中显示", { error });
    });
  };

  return (
    <div role="region" aria-label="下载" className="bg-muted/40 flex shrink-0 items-start gap-1 px-2 py-1 text-xs">
      <ul className="flex min-w-0 flex-1 flex-col">
        {shown.map((download) => (
          <li key={download.id} className="flex h-7 min-w-0 items-center gap-2">
            <DownloadStateIcon state={download.state} />
            <span className="min-w-0 truncate" title={download.path}>{download.filename}</span>
            <span className="text-muted-foreground shrink-0 tabular-nums">{downloadStatus(download)}</span>
            {download.state === "completed" && (
              <TooltipIconButton tooltip="在文件夹中显示" className="size-6 shrink-0" aria-label={`在文件夹中显示 ${download.filename}`} onClick={() => reveal(download)}>
                <FolderOpenIcon className="size-3.5" />
              </TooltipIconButton>
            )}
          </li>
        ))}
      </ul>
      {downloads.length > 1 && (
        <Button type="button" size="xs" variant="ghost" className="text-muted-foreground mt-0.5" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>
          {expanded ? "收起" : `全部 ${downloads.length} 个`}
        </Button>
      )}
    </div>
  );
}

function DownloadStateIcon({ state }: { state: BrowserDownload["state"] }) {
  switch (state) {
    case "progressing":
      return <Spinner className="size-3.5 shrink-0" />;
    case "completed":
      return <CircleCheckIcon className="text-success size-3.5 shrink-0" aria-hidden="true" />;
    case "cancelled":
    case "interrupted":
      return <CircleAlertIcon className="text-muted-foreground size-3.5 shrink-0" aria-hidden="true" />;
  }
}

/** 进行中显示已收 / 总量与百分比（总量未知时只显示已收；断线续传中如实说明），终态显示中文结局。 */
function downloadStatus(download: BrowserDownload): string {
  switch (download.state) {
    case "progressing":
      if (download.stalled) return "连接中断，正在重试";
      return download.totalBytes > 0
        ? `${formatByteSize(download.receivedBytes)} / ${formatByteSize(download.totalBytes)} · ${Math.floor((download.receivedBytes / download.totalBytes) * 100)}%`
        : formatByteSize(download.receivedBytes);
    case "completed":
      return "已完成";
    case "cancelled":
      return "已取消";
    case "interrupted":
      return "下载失败";
  }
}
