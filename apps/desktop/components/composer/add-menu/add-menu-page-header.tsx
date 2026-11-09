import type { ReactNode } from "react";
import { ChevronLeftIcon } from "lucide-react";

/** 「+」菜单二级页的标题行：返回第一层、页名，右侧可放本页的操作（如提示词页的「管理」）。 */
export function AddMenuPageHeader({ title, onBack, trailing }: { title: string; onBack: () => void; trailing?: ReactNode }) {
  return (
    <div className="border-border/70 flex items-center gap-1 border-b px-1 py-1">
      <button
        type="button"
        onClick={onBack}
        aria-label="返回"
        className="text-muted-foreground hover:text-foreground hover:bg-muted flex size-7 items-center justify-center rounded-md focus-ring"
      >
        <ChevronLeftIcon className="size-4" />
      </button>
      <p className="min-w-0 flex-1 truncate text-sm font-medium">{title}</p>
      {trailing}
    </div>
  );
}
