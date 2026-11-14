import { ArrowUpRightIcon, FolderOpenIcon } from "lucide-react";

import { LinkTargetIcon } from "@/components/links/link-target-icon";
import { useOpenLink } from "@/components/links/use-open-link";
import { SURFACE_CARD_CLASS } from "@/components/conversation/turn/turn-tokens";
import { Button } from "@/components/ui/button";
import { TooltipIconButton } from "@/components/ui/tooltip-icon-button";
import { desktopHost } from "@/lib/desktop-host";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";
import { cn } from "@/lib/utils";
import type { ArtifactCardProps } from "../artifact-card-props";
import { readDeliverables, type DeliverableFile } from "./reply-presentation-display";

/** 路径当链接地址用之前逐段编码：文件名里的 `#`、`%`、空格否则会被解析成行号片段或转义。 */
function pathHref(path: string): string {
  return path.split("/").map(encodeURIComponent).join("/");
}

function fileName(path: string): string {
  return path.split(/[\\/]/).pop() || path;
}

/**
 * 产物区的交付物卡：Agent 用 `present_files` 声明的成果文件。哪些文件算成果只读展示数据，不从正文链接或
 * 文件操作里推断。「打开」走链接的统一入口（工作区文件进文件面板，网页文件进内置浏览器）；工作区外的
 * 非网页文件没有可打开的界面，只提供「在文件夹中显示」（桌面宿主里才有）。继承来的历史同样可打开。
 */
export function DeliverablesCard({ artifact, sessionId }: ArtifactCardProps) {
  const files = readDeliverables(artifact.display_data);
  const links = useOpenLink("tool", sessionId);
  if (!files) return null;

  const reveal = (file: DeliverableFile) => {
    desktopHost!.files.reveal(file.absolutePath).catch((error: unknown) => {
      void frontendLog("WARNING", "deliverable_reveal_failed", "在文件夹中显示交付物失败", { data: { sessionId }, error });
      notify.error("无法在文件夹中显示", { error });
    });
  };

  return (
    <section data-slot="deliverables-card" aria-label="交付的文件" className={cn(SURFACE_CARD_CLASS, "py-1")}>
      <ul className="m-0 list-none p-0">
        {files.map((file) => {
          const target = links.resolve(pathHref(file.path));
          const name = fileName(file.path);
          return (
            <li key={file.absolutePath} className="flex min-h-11 items-center gap-2.5 px-3 py-1.5">
              <LinkTargetIcon target={{ kind: "workspace", path: file.path, directory: false }} className="size-4 shrink-0" />
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm font-medium" title={file.absolutePath}>{name}</div>
                <p className="text-muted-foreground truncate text-xs">{file.caption ?? file.path}</p>
              </div>
              {desktopHost && (
                <TooltipIconButton tooltip="在文件夹中显示" className="size-7 shrink-0" aria-label={`在文件夹中显示 ${name}`} onClick={() => reveal(file)}>
                  <FolderOpenIcon className="size-3.5" />
                </TooltipIconButton>
              )}
              {target && (
                <Button type="button" variant="outline" size="sm" className="h-7 shrink-0 gap-1 px-2" aria-label={`打开 ${name}`} onClick={() => links.open(target)}>
                  打开<ArrowUpRightIcon className="size-3.5" aria-hidden="true" />
                </Button>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
