/**
 * 浏览器工具交给模型的文本超过上限时怎么截：截到上限并标出截掉了多少（`…(+N chars)`）。模型据这个标记知道
 * 读不到的部分是工具的上限、不是页面上只有这么多；快照、标签日志与弹窗消息共用这一个写法。
 */
export function clipText(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max)}…(+${text.length - max} chars)` : text;
}
