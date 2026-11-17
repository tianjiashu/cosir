/**
 * 文件名的扩展名（不含点，保留原大小写）；没有时为 null。全仓「什么算扩展名」只在这里：点不在开头（`.env` 不算），
 * 其后 1–10 位字母数字。长度上限让「最后一个点之后的任意内容」（`报告.final draft for review`）不被当成扩展名——
 * 中间截断的尾段、空态的类型文案与胶囊角标都依赖它有上限。
 */
/** 路径的最后一段：`/` 与 `\\` 都算分隔符，目录的尾分隔符不计；全是分隔符时原样返回。 */
export function baseName(path: string): string {
  const trimmed = path.replace(/[\\/]+$/, "");
  return trimmed.split(/[\\/]/).at(-1) || trimmed || path;
}

export function fileExtension(name: string): string | null {
  return /^[^.][\s\S]*\.([A-Za-z0-9]{1,10})$/.exec(name)?.[1] ?? null;
}
