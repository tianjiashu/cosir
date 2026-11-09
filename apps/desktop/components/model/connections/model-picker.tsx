import { useMemo, useRef, useState, type KeyboardEvent, type MouseEvent } from "react";
import { CheckIcon, SearchIcon } from "lucide-react";

import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import { toggleSelection } from "@/lib/selection/range-selection";
import { cn } from "@/lib/utils";
import type { ModelDiscovery } from "./use-model-discovery";

/**
 * 从远端模型目录里勾选要加入的模型，外加手动输入（目录拉不到、或目录里没有时用）。
 *
 * 勾选沿用文件管理器的手势，但以复选框为主入口：单击切换（误点不会清掉已选的），Shift+单击连选一段，
 * ⌘/Ctrl+单击与单击相同；键盘 ↑↓ 移动、空格切换、Shift+空格连选，⌘/Ctrl+A 全选当前搜索结果。
 * 已在连接下的模型显示为「已添加」且不可选。选中集合由调用方持有。
 */
export function ModelPicker({
  discovery,
  selected,
  existing,
  onChange,
  disabled = false,
}: {
  discovery: ModelDiscovery;
  selected: ReadonlySet<string>;
  existing: ReadonlySet<string>;
  onChange: (next: Set<string>) => void;
  disabled?: boolean;
}) {
  const [query, setQuery] = useState("");
  const [manual, setManual] = useState("");
  const anchor = useRef<string | null>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const discovered = discovery.status === "ready" ? discovery.models : [];
  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return needle ? discovered.filter((name) => name.toLowerCase().includes(needle)) : discovered;
  }, [discovered, query]);
  const selectable = visible.filter((name) => !existing.has(name));

  const toggle = (name: string, range: boolean) => {
    onChange(toggleSelection(selectable, selected, name, { anchor: anchor.current, range }));
    anchor.current = name;
  };
  const setVisible = (checked: boolean) => {
    const next = new Set(selected);
    for (const name of selectable) {
      if (checked) next.add(name);
      else next.delete(name);
    }
    onChange(next);
  };
  const moveFocus = (event: KeyboardEvent<HTMLDivElement>) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "a") {
      event.preventDefault();
      setVisible(true);
      return;
    }
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    event.preventDefault();
    const rows = [...(listRef.current?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)") ?? [])];
    const index = rows.indexOf(document.activeElement as HTMLButtonElement);
    rows[Math.max(0, Math.min(rows.length - 1, index + (event.key === "ArrowDown" ? 1 : -1)))]?.focus();
  };
  const addManual = () => {
    const name = manual.trim();
    if (!name || existing.has(name)) return;
    onChange(new Set([...selected, name]));
    setManual("");
  };

  return (
    <div className="flex min-h-0 min-w-0 flex-col gap-2">
      {discovery.status === "ready" ? (
        <>
          <div className="relative">
            <SearchIcon className="text-muted-foreground pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2" />
            <Input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder={`搜索 ${discovered.length} 个模型`}
              aria-label="搜索模型"
              className="h-8 pl-8 text-xs"
              disabled={disabled}
            />
          </div>
          <div className="text-muted-foreground flex items-center gap-2 text-xs">
            <span className="flex-1">Shift+单击可连选</span>
            <button type="button" className="hover:text-foreground focus-ring rounded px-1" disabled={disabled || selectable.length === 0} onClick={() => setVisible(true)}>全选</button>
            <button type="button" className="hover:text-foreground focus-ring rounded px-1" disabled={disabled} onClick={() => setVisible(false)}>全不选</button>
          </div>
          <div
            ref={listRef}
            role="group"
            aria-label="可选模型"
            onKeyDown={moveFocus}
            className="border-border/70 max-h-72 min-h-24 overflow-y-auto rounded-md border p-1"
          >
            {visible.length === 0 && <p className="text-muted-foreground px-2 py-4 text-center text-xs">没有匹配的模型</p>}
            {visible.map((name) => {
              const added = existing.has(name);
              const checked = added || selected.has(name);
              return (
                <button
                  key={name}
                  type="button"
                  role="checkbox"
                  aria-checked={checked}
                  disabled={disabled || added}
                  // Shift 会让浏览器顺带选中文字，按下时阻止默认行为即可，不影响点击。
                  onMouseDown={(event: MouseEvent) => { if (event.shiftKey) event.preventDefault(); }}
                  onClick={(event) => toggle(name, event.shiftKey)}
                  className={cn(
                    "hover:bg-muted focus-ring flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-sm disabled:hover:bg-transparent",
                    added && "text-muted-foreground",
                  )}
                >
                  <span
                    aria-hidden="true"
                    className={cn(
                      "flex size-4 shrink-0 items-center justify-center rounded-sm border",
                      checked ? "border-primary bg-primary text-primary-foreground" : "border-input",
                      added && "opacity-50",
                    )}
                  >
                    {checked && <CheckIcon className="size-3" />}
                  </span>
                  <span className="min-w-0 flex-1 truncate" title={name}>{name}</span>
                  {added && <span className="text-xs">已添加</span>}
                </button>
              );
            })}
          </div>
        </>
      ) : (
        <div className="text-muted-foreground border-border/70 flex min-h-24 items-center justify-center gap-2 rounded-md border border-dashed px-3 py-4 text-center text-xs">
          {discovery.status === "loading" && <Spinner className="size-3.5" label={null} />}
          {discovery.status === "idle" && "填写 API Base URL 和 API Key 后自动获取模型列表"}
          {discovery.status === "loading" && "正在获取模型列表…"}
          {discovery.status === "unavailable" && discovery.reason}
        </div>
      )}
      <Input
        value={manual}
        onChange={(event) => setManual(event.target.value)}
        onKeyDown={(event) => {
          if (event.key !== "Enter" || event.nativeEvent.isComposing) return;
          event.preventDefault();
          addManual();
        }}
        placeholder="手动输入模型名称，回车加入"
        aria-label="手动输入模型名称"
        className="h-8 text-xs"
        disabled={disabled}
      />
    </div>
  );
}
