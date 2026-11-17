import type { ReactNode } from "react";
import { FileIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import type { FileInfo } from "@/lib/api/workspace-files";
import { FILE_EXITS } from "@/lib/desktop-host/file-exits";
import { formatByteSize } from "@/lib/format/byte-size";
import { workspacePathText } from "@/lib/workspace-files/workspace-files-store";
import { useSystemThumbnail } from "@/lib/workspace-files/use-system-thumbnail";
import { fileExtension } from "@/lib/files/file-name";
import { AddToChatButton } from "./add-to-chat-button";
import { ReadingToolbar, type ReadingNavigation } from "./reading-toolbar";

/** 空态缩略图最高 136（设计 §11.3），按两倍清晰度向系统要。 */
const THUMBNAIL_EDGE = 272;

/** 「XLSX 文件」这类写法：后缀比猜出来的 MIME 更好懂，也不随机器变化；没有后缀时写「文件」。 */
function typeLabel(name: string): string {
  const extension = fileExtension(name);
  return extension ? `${extension.toUpperCase()} 文件` : "文件";
}

/**
 * 「看不了的文件」：二进制文件，以及预览器显示不了的文件（`reason` 写明原因，如格式不支持、内容损坏）。所有「显示
 * 不了」都落到这里，由 `FilePreview` 一处决定；读取失败（404、403、5xx）不在此列，仍走可重试的读取问题界面。
 *
 * 居中一列：系统缩略图（系统认识的格式才有，取不到退回文件图标）、文件名、「类型 · 大小」、一句说明、把文件交给系统
 * 的出口按钮（`FILE_EXITS`，平台不支持的不出现）。说明只按 `system_preview` 分两种（设计 §4.4），出口对所有文件
 * 都一样。「以文本方式查看」几乎总是乱码，降为文字链接。
 */
export function BinaryPreview({
  workspaceId,
  info,
  navigation,
  reason,
  view,
  onViewAsText,
}: {
  workspaceId: number;
  info: FileInfo;
  navigation: ReadingNavigation;
  reason: string | null;
  view?: ReactNode;
  onViewAsText: () => void;
}) {
  const absolutePath = workspacePathText(info.root_path, info.path, true);
  const thumbnail = useSystemThumbnail(info.system_preview ? absolutePath : null, info.version, THUMBNAIL_EDGE);
  const message = reason ?? (info.system_preview ? "这种文件没有内置预览" : "这个文件不是文本，无法预览");
  return (
    <>
      <ReadingToolbar navigation={navigation} info={info} view={view}
        actions={<AddToChatButton workspaceId={workspaceId} path={info.path} />} />
      <div data-slot="binary-preview" className="flex flex-col items-center gap-3 px-6 py-10 text-center text-sm">
        {thumbnail
          ? <img src={thumbnail} alt="" data-slot="system-thumbnail" className="max-h-[136px] max-w-full rounded-md shadow-md" />
          : <FileIcon className="text-muted-foreground size-12" strokeWidth={1.25} aria-hidden="true" />}
        <div className="space-y-1">
          <p className="font-medium break-all">{info.name}</p>
          <p className="text-muted-foreground tabular-nums">{typeLabel(info.name)} · {formatByteSize(info.size)}</p>
        </div>
        <p>{message}</p>
        {FILE_EXITS.length > 0 && (
          <div className="flex flex-wrap justify-center gap-2">
            {FILE_EXITS.map((exit, index) => (
              <Button key={exit.id} size="sm" variant={index === 0 ? "default" : "outline"} onClick={() => exit.run(absolutePath)}>{exit.label}</Button>
            ))}
          </div>
        )}
        <button type="button" onClick={onViewAsText} className="text-muted-foreground hover:text-foreground text-xs underline-offset-4 hover:underline">以文本方式查看</button>
      </div>
    </>
  );
}
