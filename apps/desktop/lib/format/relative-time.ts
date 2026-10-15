/**
 * 「3 分钟前 / 昨天 / 10月3日」式的相对时间，用于侧栏等密集列表。
 *
 * 用浏览器内置的 Intl.RelativeTimeFormat / DateTimeFormat（zh-CN），不引入日期库；
 * 超过 7 天显示日期，跨年时带年份。`now` 可注入以便测试。
 */
const RELATIVE = new Intl.RelativeTimeFormat("zh-CN", { numeric: "auto" });
const SAME_YEAR = new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric" });
const OTHER_YEAR = new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "numeric", day: "numeric" });

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

export function formatRelativeTime(value: string | Date, now: Date = new Date()): string {
  const date = typeof value === "string" ? new Date(value) : value;
  const diff = date.getTime() - now.getTime();
  const elapsed = Math.abs(diff);
  if (Number.isNaN(elapsed)) return "";
  if (elapsed < MINUTE) return "刚刚";
  if (elapsed < HOUR) return RELATIVE.format(Math.round(diff / MINUTE), "minute");
  if (elapsed < DAY) return RELATIVE.format(Math.round(diff / HOUR), "hour");
  if (elapsed < 7 * DAY) return RELATIVE.format(Math.round(diff / DAY), "day");
  return (date.getFullYear() === now.getFullYear() ? SAME_YEAR : OTHER_YEAR).format(date);
}
