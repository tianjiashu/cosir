import { parseReferenceUrl } from "@/lib/workspace-references/codec";

/** 行范围，1 起算、闭区间。 */
export type LineRange = { start: number; end: number };

/**
 * 工作区内的文件或目录；`path` 是工作区相对路径（POSIX 分隔符、无前导 `./`）。
 *
 * `mention` 表示这不是地址而是「提到了一个文件名」（行内代码里不带目录的 `task.ts`）：它在哪个目录并不知道，
 * 按根目录换算出的 `path` 只是第一个候选，根目录下没有时打开入口改为按文件名查找，而不是报不存在。
 */
export type WorkspaceLink = { kind: "workspace"; path: string; directory: boolean; lines?: LineRange; mention?: true };

/** 网页或邮件地址；`local` 表示本机开发服务（localhost / 回环地址）。 */
export type WebLink = { kind: "web"; url: string; local: boolean };

/** 同一篇 Markdown 里的位置（脚注引用与返回、标题锚点）；`id` 是已解码的目标元素 id。 */
export type AnchorLink = { kind: "anchor"; id: string };

export type LinkTarget = WorkspaceLink | WebLink | AnchorLink;

/**
 * 解析基准。`workspaceRoot` 为 null 时（配置页等没有工作区的界面）只认网页链接；`documentDir` 只在
 * 工作区文档里给出（文件面板的 Markdown 预览），相对路径按文档所在目录解析，`/` 开头视为工作区根。
 */
export type LinkBase = { workspaceRoot: string | null; documentDir?: string };

const WINDOWS_DRIVE = /^[A-Za-z]:[\\/]/;
const SCHEME = /^[a-z][a-z\d+.-]*:/i;
const LINE_FRAGMENT = /^L([1-9]\d*)(?:-L?([1-9]\d*))?$/;
const LINE_SUFFIX = /:([1-9]\d*)(?::\d+)?$/;

/**
 * 把链接地址换算成可打开的目标；所有界面（对话正文、文件面板 Markdown、终端、工具卡片）共用这一套规则。
 *
 * - 网页只认 http(s) 与 mailto；其余协议、协议相对地址返回 null，调用方渲染为纯文本。
 * - `#…` 是同一篇 Markdown 里的位置（脚注等），在原文里滚动过去，不离开页面。
 * - 工作区路径：相对路径、工作区内绝对路径、`cosir-file:` / `cosir-dir:` 引用地址；越出工作区返回 null。
 * - 行号：规范写法是片段 `#L42` / `#L42-L60`（GitHub、VS Code 同款，也是提示词约定的写法）；同时容忍
 *   模型与编译器习惯的 `:42` / `:42:7` 后缀（列号不用）。以 `/` 结尾的是目录。
 *
 * 只做字符串换算，不检查文件是否存在——打开后由文件面板按实际读取结果展示。
 */
export function resolveLink(href: string, base: LinkBase): LinkTarget | null {
  const raw = href.trim();
  if (!raw || raw.startsWith("//")) return null;
  if (raw.startsWith("#")) return raw.length > 1 ? { kind: "anchor", id: decodePath(raw.slice(1)) } : null;
  if (/^(?:https?|mailto):/i.test(raw)) return webLink(raw);
  if (/^cosir-(?:file|dir|snippet|excerpt):/.test(raw)) {
    const reference = parseReferenceUrl(raw);
    if (!reference || base.workspaceRoot === null) return null;
    // 引用地址的路径已是工作区相对路径；根目录引用的路径为空串。
    return {
      kind: "workspace",
      path: reference.path,
      directory: reference.kind === "directory",
      ...(reference.startLine ? { lines: { start: reference.startLine, end: reference.endLine! } } : {}),
    };
  }
  if (base.workspaceRoot === null) return null;

  const hash = raw.indexOf("#");
  let location = hash < 0 ? raw : raw.slice(0, hash);
  const fragment = LINE_FRAGMENT.exec(hash < 0 ? "" : raw.slice(hash + 1));
  let lines = fragment ? lineRange(fragment[1], fragment[2]) : undefined;
  const suffix = lines ? null : LINE_SUFFIX.exec(location);
  if (suffix) {
    location = location.slice(0, suffix.index);
    lines = lineRange(suffix[1]);
  }
  if (SCHEME.test(location) && !WINDOWS_DRIVE.test(location)) return null;

  const decoded = decodePath(location);
  const directory = /[\\/]$/.test(decoded);
  const path = workspacePathOf(base, decoded);
  if (path === null) return null;
  return { kind: "workspace", path, directory, ...(lines && !directory ? { lines } : {}) };
}

/**
 * 不含 `/` 的裸文件名只有带常见源码 / 文档扩展名时才当作路径，避免把 `obj.method` 误判成文件；
 * 带 `/` 的也要求末段有扩展名，避免 `text/plain`、`/api/tasks`、包名之类被当成文件。
 */
const BARE_FILE_EXTENSIONS = new Set([
  "c", "cc", "cpp", "cs", "css", "go", "h", "hpp", "html", "java", "js", "json", "jsx", "kt", "lock",
  "md", "mdx", "mjs", "py", "rb", "rs", "scss", "sh", "sql", "swift", "toml", "ts", "tsx", "txt",
  "vue", "xml", "yaml", "yml",
]);

/**
 * 对话正文行内代码里的文件路径（如 `src/app.ts:42`）。行内代码本来不是链接，所以先按形状判断，
 * 只认像文件的文本，再交给 `resolveLink` 换算；网址、命令、成员访问等保持原样。不带目录的裸文件名
 * 标为 `mention`：模型在行文里提到文件名时并不给出位置，不能当成根目录下的地址。
 */
export function inlineCodeLink(text: string, workspaceRoot: string | null): WorkspaceLink | null {
  const candidate = text.trim().replace(/:\d+(?::\d+)?$/, "");
  if (!candidate || candidate.includes("://") || !/^[\w.@~\-/\\:]+$/.test(candidate)) return null;
  const name = candidate.split(/[/\\]/).at(-1) ?? "";
  const extension = /^[^.].*\.([A-Za-z0-9]{1,10})$/.exec(name)?.[1]?.toLowerCase();
  if (!extension || (name === candidate && !BARE_FILE_EXTENSIONS.has(extension))) return null;
  const target = resolveLink(text, { workspaceRoot });
  if (target?.kind !== "workspace") return null;
  return name === candidate ? { ...target, mention: true } : target;
}

/**
 * 把模型或工具给出的路径换算成文件面板使用的工作区相对路径（POSIX 分隔符、无前导 `./`）。
 *
 * 工具展示数据里的路径是模型调用时传入的原值，可能是相对路径，也可能是工作区内的
 * 绝对路径；指向工作区外的绝对路径、`..` 越界或空路径返回 null，调用方据此不提供打开入口。
 */
export function toWorkspacePath(workspaceRoot: string, rawPath: string): string | null {
  const path = rawPath.trim().replace(/\\/g, "/");
  if (!path) return null;
  const root = workspaceRoot.replace(/\\/g, "/").replace(/\/+$/, "");
  let relative: string;
  if (isAbsolute(path)) {
    // Windows 盘符大小写不敏感，其余部分按原样比较。
    const sameRoot = root !== "" && (path === root || path.toLowerCase().startsWith(`${root.toLowerCase()}/`));
    if (!sameRoot) return null;
    relative = path.slice(root.length + 1);
  } else {
    relative = path;
  }
  const segments: string[] = [];
  for (const segment of relative.split("/")) {
    if (segment === "" || segment === ".") continue;
    if (segment === "..") {
      if (segments.length === 0) return null;
      segments.pop();
      continue;
    }
    segments.push(segment);
  }
  return segments.length > 0 ? segments.join("/") : null;
}

function isAbsolute(path: string): boolean {
  return path.startsWith("/") || WINDOWS_DRIVE.test(path);
}

function workspacePathOf(base: LinkBase, path: string): string | null {
  const root = base.workspaceRoot!;
  if (base.documentDir === undefined) return toWorkspacePath(root, path);
  if (!isAbsolute(path)) return toWorkspacePath(root, base.documentDir ? `${base.documentDir}/${path}` : path);
  // 文档里 `/docs/a.md` 按仓库文档惯例指工作区根；真正落在工作区内的绝对路径也照常认。
  return toWorkspacePath(root, path) ?? (path.startsWith("/") ? toWorkspacePath(root, path.slice(1)) : null);
}

function lineRange(start: string, end?: string): LineRange {
  const first = Number(start);
  const last = end ? Number(end) : first;
  return { start: first, end: Math.max(first, last) };
}

/** Markdown 链接的地址经过百分号编码（空格、中文）；行内代码与终端文本不编码，解码失败按原文用。 */
function decodePath(path: string): string {
  try {
    return decodeURIComponent(path);
  } catch {
    return path;
  }
}

const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]", "0.0.0.0"]);

function webLink(url: string): WebLink | null {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  const host = parsed.hostname.toLowerCase();
  const local = parsed.protocol !== "mailto:" && (LOCAL_HOSTS.has(host) || host.endsWith(".localhost"));
  return { kind: "web", url, local };
}
