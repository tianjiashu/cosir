import { basename, isDirectoryPath, type RefCapsule } from "@/lib/assistant/capsules";
import type { LineRange } from "@/lib/links/resolve-link";

/**
 * 工作区引用胶囊（`<ref path="src/a.py" lines="12-14" />` / `page="3"`）的构造与读取。
 *
 * 引用是「指向工作区路径的指针」：路径是工作区相对路径，目录以 `/` 结尾，根目录为 `/`。行范围与
 * PDF 页码只是定位提示，正文不随引用携带——模型和 Agent 按路径自己读文件。
 */

/** 文件引用；`lines` 是可选的源码行范围定位。 */
export function workspaceFileRef(path: string, lines?: LineRange): RefCapsule {
  return { kind: "ref", path, ...(lines ? { lines: linesText(lines) } : {}) };
}

/** 目录引用：普通目录以 `/` 结尾；工作区根目录用空路径表示（`/` 是绝对路径，不能当引用发给模型）。 */
export function workspaceDirectoryRef(path: string): RefCapsule {
  const trimmed = path.replace(/\/+$/, "");
  return { kind: "ref", path: trimmed ? `${trimmed}/` : "" };
}

/** PDF 引用的页码定位。 */
export function workspacePageRef(path: string, page: number): RefCapsule {
  return { kind: "ref", path, page: String(page) };
}

/** 引用是否指向目录（空路径是工作区根目录，其余以分隔符结尾）。 */
export function refIsDirectory(capsule: RefCapsule): boolean {
  return isDirectoryPath(capsule.path);
}

/** 打开文件 / 在文件树定位目录时的工作区相对路径（去掉目录尾斜杠）。 */
export function refWorkspacePath(capsule: RefCapsule): string {
  return refIsDirectory(capsule) ? capsule.path.replace(/\/+$/, "") : capsule.path;
}

/** 行范围定位；没有或格式不合法时返回 null。 */
export function refLineRange(capsule: RefCapsule): LineRange | null {
  if (capsule.lines === undefined) return null;
  const match = /^(\d+)(?:-(\d+))?$/.exec(capsule.lines);
  if (!match) return null;
  const start = Number(match[1]);
  const end = match[2] === undefined ? start : Number(match[2]);
  return { start, end };
}

/** 胶囊上显示的名称：文件名（目录带 `/`，根目录显示「工作区」）。 */
export function refName(capsule: RefCapsule): string {
  if (refIsDirectory(capsule)) {
    const trimmed = capsule.path.replace(/\/+$/, "");
    return trimmed ? `${basename(trimmed)}/` : "工作区";
  }
  return basename(capsule.path);
}

/** 胶囊上显示的定位说明（` · L12–14` / ` · p.3`）。 */
export function refLocatorText(capsule: RefCapsule): string | undefined {
  const lines = refLineRange(capsule);
  if (lines) {
    return lines.start === lines.end ? ` · L${lines.start}` : ` · L${lines.start}–${lines.end}`;
  }
  return capsule.page !== undefined ? ` · p.${capsule.page}` : undefined;
}

/** 胶囊的完整描述（悬停提示）：路径 + 定位；根目录显示「工作区」。 */
export function refDescription(capsule: RefCapsule): string {
  return `${capsule.path || "工作区"}${refLocatorText(capsule) ?? ""}`;
}

function linesText(lines: LineRange): string {
  const start = Math.max(1, lines.start);
  const end = Math.max(start, lines.end);
  return start === end ? String(start) : `${start}-${end}`;
}
