/**
 * 把模型或工具给出的路径换算成文件面板使用的工作区相对路径（POSIX 分隔符、无前导 `./`）。
 *
 * 工具展示数据里的路径是模型调用时传入的原值，可能是相对路径，也可能是工作区内的
 * 绝对路径；指向工作区外的绝对路径、`..` 越界或空路径返回 null，调用方据此不提供打开入口。
 * 只做字符串换算，不检查文件是否存在——打开后由文件面板按实际读取结果展示。
 */
export function toWorkspacePath(workspaceRoot: string, rawPath: string): string | null {
  const path = rawPath.trim().replace(/\\/g, "/");
  if (!path) return null;
  const root = workspaceRoot.replace(/\\/g, "/").replace(/\/+$/, "");
  const isAbsolute = path.startsWith("/") || /^[A-Za-z]:\//.test(path);
  let relative: string;
  if (isAbsolute) {
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
 * 从对话正文的行内代码里识别文件路径（可带 `:行号[:列号]` 后缀）。
 *
 * 只做形状判断，返回去掉行号后的路径；能否打开仍由 `toWorkspacePath` 按工作区根决定。
 * 行号暂不使用：文件面板按字节偏移分页，还没有按行定位的入口。
 */
export function pathFromInlineCode(text: string): string | null {
  const candidate = text.trim().replace(/:\d+(?::\d+)?$/, "");
  if (!candidate || candidate.includes("://") || !/^[\w.@~\-/\\:]+$/.test(candidate)) return null;
  const name = candidate.split(/[/\\]/).at(-1) ?? "";
  const extension = /^[^.].*\.([A-Za-z0-9]{1,10})$/.exec(name)?.[1]?.toLowerCase();
  if (!extension) return null;
  return name !== candidate || BARE_FILE_EXTENSIONS.has(extension) ? candidate : null;
}
