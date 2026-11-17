const UNITS = ["KB", "MB", "GB"] as const;

/**
 * 字节数的展示文案（1024 进制）：文件大小、下载进度等共用，全仓只此一份规则。
 * 小于 10 保留一位小数、整数时不写「.0」，10 及以上取整（187 B、1.2 KB、12 KB、318 KB、2 MB），与 Finder 一致；
 * 大数的小数位不帮用户区分什么，只让一组胶囊或进度显得杂乱。
 */
export function formatByteSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  let value = bytes / 1024;
  let unit = 0;
  // 按取整后的值进位：1048575 B 是 1023.999 KB，取整成 1024 时应写成 1 MB。
  while (Math.round(value) >= 1024 && unit < UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const text = value < 10 ? String(Number(value.toFixed(1))) : String(Math.round(value));
  return `${text} ${UNITS[unit]}`;
}
