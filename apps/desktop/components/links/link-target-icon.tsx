import { FileIcon, FolderIcon, GlobeIcon, ImageIcon, MailIcon, MonitorIcon } from "lucide-react";

import { resolveLink, type LinkTarget, type WebLink } from "@/lib/links/resolve-link";
import { brandIconPath } from "@/lib/links/site-icon";
import { cn } from "@/lib/utils";

/** 跟随所在文字的字号，调用方可用 `size-*` 改写；子元素铺满，文件、站点、通用图标大小一致。 */
const ICON_CLASS = "inline-flex size-[1em] shrink-0 align-[-0.125em] [&>*]:size-full";

function WebIcon({ target }: { target: WebLink }) {
  if (target.reach === "mail") return <MailIcon />;
  if (target.reach === "local") return <MonitorIcon />;
  const brandPath = brandIconPath(target.host!);
  if (!brandPath) return <GlobeIcon />;
  return <svg viewBox="0 0 24 24" fill="currentColor"><path d={brandPath} /></svg>;
}

/** 图片只影响图标，不代表文件可预览；是否是图片以文件服务读到的结果为准。 */
const IMAGE_FILE = /\.(?:avif|bmp|gif|ico|jpe?g|png|svg|tiff?|webp)$/i;

/**
 * 阅读内容里的对象图标（对话正文、工具详情）：单色、跟随所在文字，不和正文抢注意力，只分文件、图片、目录；
 * 彩色类型图标留给文件浏览和胶囊（`WorkspaceFileIcon`）。网页看站点。页内锚点和只有文件名的提及不指向确定的
 * 对象，不显示图标。
 */
export function LinkTargetIcon({ target, className }: { target: LinkTarget; className?: string }) {
  if (target.kind === "anchor" || target.kind === "filename-search" || target.kind === "reveal") return null;
  return (
    <span className={cn(ICON_CLASS, className)} aria-hidden="true">
      {target.kind === "web"
        ? <WebIcon target={target} />
        : target.kind === "page" ? <FileIcon />
          : target.directory ? <FolderIcon /> : IMAGE_FILE.test(target.path) ? <ImageIcon /> : <FileIcon />}
    </span>
  );
}

/** 网址的图标（工具结果、浏览器标签）：本机文件用文件图标；解析不出网页地址时（空白页、其他协议）用通用图标。 */
export function WebPageIcon({ url, className }: { url: string; className?: string }) {
  const target = resolveLink(url, { workspaceRoot: null });
  if (target?.kind === "web") return <LinkTargetIcon target={target} className={className} />;
  return <span className={cn(ICON_CLASS, className)} aria-hidden="true">{url.startsWith("file:") ? <FileIcon /> : <GlobeIcon />}</span>;
}
