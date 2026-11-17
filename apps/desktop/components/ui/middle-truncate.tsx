import { fileExtension } from "@/lib/files/file-name";
import { cn } from "@/lib/utils";

/** 扩展名之前额外保留的字符数：`_v2.csv`、`-final.md` 这类结尾正是区分同名文件的地方。 */
const KEPT_STEM_TAIL = 4;

/**
 * 中间截断的文件名：放不下时省略号出现在中间，扩展名（`fileExtension`，有长度上限）与它前面几个字符
 * 始终可见；没有扩展名时尾段只留最后几个字符。全仓文件名截断只用这一条规则
 * （交付物胶囊、输入框与引用里的行内胶囊）。用两段 flex 实现（前段 `truncate`、后段不收缩），宽度由外层约束；
 * 按码点切分，中文与单个表情不会被切坏（组合表情可能被拆在两段，只影响显示）。无障碍名称由调用方另给，这里只管显示。
 */
export function MiddleTruncate({ text, className }: { text: string; className?: string }) {
  const chars = Array.from(text);
  const extension = fileExtension(text);
  const tailLength = Math.min(chars.length - 1, (extension ? extension.length + 1 : 0) + KEPT_STEM_TAIL);
  const head = chars.slice(0, chars.length - tailLength).join("");
  const tail = chars.slice(chars.length - tailLength).join("");
  return (
    <span data-slot="middle-truncate" className={cn("flex min-w-0", className)}>
      <span className="truncate">{head}</span>
      {tail && <span className="shrink-0 whitespace-pre">{tail}</span>}
    </span>
  );
}
