import type { ReactNode } from "react";
import { FolderIcon, GlobeIcon } from "lucide-react";

import type { TransportDeliverable } from "@/lib/assistant/contract";
import { baseName, fileExtension } from "@/lib/files/file-name";
import { formatByteSize } from "@/lib/format/byte-size";

/** 交付物的显示要素：名称、大小一格（交付时的记录）、完整路径（提示条用）与角标内容。 */
export type DeliverableLabel = { name: string; meta: string; path: string; badge: ReactNode };


/**
 * 交付物怎么称呼：回复下方的胶囊与侧栏「产出」卡共用这一份规则，两处说法一致。文件的角标是大写后缀，目录与网址换成
 * 图标；网址没有说明时用主机加路径作名称。只看交付时的记录，现读的大小由胶囊另行覆盖。
 */
export function describeDeliverable(deliverable: TransportDeliverable): DeliverableLabel {
  if (deliverable.type === "url") {
    const url = new URL(deliverable.url);
    return { name: deliverable.caption ?? `${url.host}${url.pathname === "/" ? "" : url.pathname}`, meta: url.host, path: deliverable.url, badge: <GlobeIcon className="size-3" /> };
  }
  const path = deliverable.workspacePath ?? deliverable.absolutePath;
  const name = baseName(deliverable.workspacePath && deliverable.workspacePath !== "." ? deliverable.workspacePath : deliverable.absolutePath);
  if (deliverable.type === "directory") return { name, meta: "文件夹", path, badge: <FolderIcon className="size-3" /> };
  return { name, meta: formatByteSize(deliverable.size), path, badge: fileExtension(name)?.toUpperCase() ?? "FILE" };
}

/** 描边的圆角小角标（大写后缀或图标），胶囊与侧栏行同一外观。 */
export function DeliverableBadge({ children }: { children: ReactNode }) {
  return (
    <span aria-hidden="true" className="text-muted-foreground grid h-5 min-w-5 shrink-0 place-items-center rounded-full border px-1.5 text-3xs font-semibold tracking-[0.04em]">
      {children}
    </span>
  );
}
