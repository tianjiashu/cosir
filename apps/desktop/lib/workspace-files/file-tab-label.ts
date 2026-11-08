/** 同名文件仅补足可区分的父目录；根目录文件保留原路径。 */
export function fileTabLabel(path: string, paths: string[]): string {
  const parts = path.split("/");
  for (let count = 1;count < parts.length;count++) {
    const suffix = parts.slice(-count).join("/");
    if (!paths.some(other => other !== path && other.split("/").slice(-count).join("/") === suffix)) return suffix;
  }
  return path;
}
