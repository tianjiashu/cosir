/**
 * 通用虚拟列表原语。
 *
 * 基于 `@tanstack/react-virtual` 封装，为桌面端所有长列表（日志条目、工具调用结果、
 * 搜索/文件列表等）提供统一的「滚动容器 + 动态高度测量 + 空态 + overscan」能力。
 * 本组件只负责虚拟滚动的通用机制，单条内容如何渲染由调用方通过 `renderItem` 注入，
 * 不承载任何业务渲染逻辑，符合「通用 UI 工具放 lib、不污染 service 领域层」的约定。
 *
 * 设计约束：
 * - 滚动容器使用原生 `overflow-auto` 的 div，不使用 Radix `ScrollArea`；
 *   Radix Viewport 的内部结构会干扰 `useVirtualizer` 的 `scrollOffset` 测量，导致跳动。
 * - 动态高度依靠 `measureElement` + `data-index` 在每次布局变化时重新测量，
 *   `estimateSize` 仅作首帧占位，不允许写死业务高度（turn/diff/列表项高度方差极大）。
 * - `getKey` 必须返回全局唯一且稳定的值：**禁止纯数组下标**（否则头部插入历史数据会导致
 *   index 整体偏移、滚动错位、全量重渲染）。当条目无内在唯一标识时，可用「内在字段 + 下标」
 *   兜底（如 `filePath ?? name::idx`），但要求该列表**不在头部插入**新数据。
 *
 * @module lib/virtual/VirtualList
 */

import { useRef, type ReactNode, type UIEvent, type Ref } from "react";
import { useVirtualizer, type Virtualizer } from "@tanstack/react-virtual";
import { cn } from "@/lib/utils";

/** 虚拟列表单条渲染函数。 */
export type VirtualItemRenderer<T> = (item: T, index: number) => ReactNode;

/** 虚拟列表属性。 */
export interface VirtualListProps<T> {
  /** 列表数据（保持引用稳定以触发重渲染；调用方负责按需 memo）。 */
  items: T[];
  /** 稳定 key 提取器：必须返回全局唯一值，禁止纯数组下标；内在字段不足唯一性时可叠加下标兜底（见模块注释）。 */
  getKey: (item: T, index: number) => string;
  /** 单条渲染函数。 */
  renderItem: VirtualItemRenderer<T>;
  /** 视口外预渲染条数，默认 8。 */
  overscan?: number;
  /** 传给滚动容器的 className（如高度约束 `max-h-*` / `h-full`）。 */
  className?: string;
  /** 空数据时的占位节点。 */
  emptyState?: ReactNode;
  /** 滚动事件透传（供「滚到顶预取」使用；不阻断内部测量）。 */
  onScroll?: (event: UIEvent<HTMLDivElement>) => void;
  /**
   * 单条首帧预估高度（px）。仅用于首帧占位，真实高度由 `measureElement` 动态修正。
   * 默认 80。测试环境下可显式传入固定值以绕过布局测量。
   */
  estimateSize?: number;
  /**
   * 外部转发滚动容器 ref（可选）。调用方可用它直接操作滚动位置
   * （如流式期「滚动到底部」），不影响内部 `useVirtualizer` 测量。
   */
  scrollContainerRef?: Ref<HTMLDivElement>;
}

/**
 * 通用虚拟列表组件。
 *
 * 把长列表渲染收敛到唯一的虚拟滚动实现，避免各业务列表重复编写 `useVirtualizer`。
 *
 * 参数:
 *   props - 见 {@link VirtualListProps}。
 *
 * 返回:
 *   原生 `overflow-auto` 滚动容器，内部仅渲染视口附近的条目（绝对定位 + `measureElement`）。
 *
 * @sideeffect 无（纯渲染；`onScroll` 透传由调用方决定副作用）。
 */
export function VirtualList<T>({
  items,
  getKey,
  renderItem,
  overscan = 8,
  className,
  emptyState,
  onScroll,
  estimateSize = 80,
  scrollContainerRef,
}: VirtualListProps<T>) {
  const parentRef = useRef<HTMLDivElement | null>(null);

  const virtualizer: Virtualizer<HTMLDivElement, Element> = useVirtualizer({
    count: items.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => estimateSize,
    overscan,
    measureElement: (element) => element.getBoundingClientRect().height,
  });

  const virtualItems = virtualizer.getVirtualItems();
  const totalSize = virtualizer.getTotalSize();

  // 空数据：直接渲染占位，不挂载虚拟列表的滚动容器与 0 高占位 div。
  if (items.length === 0) {
    return (
      <div ref={mergeRefs(parentRef, scrollContainerRef)} className={cn("overflow-auto", className)} onScroll={onScroll}>
        {emptyState ?? null}
      </div>
    );
  }

  return (
    <div
      ref={mergeRefs(parentRef, scrollContainerRef)}
      className={cn("overflow-auto", className)}
      onScroll={onScroll}
    >
      <div style={{ height: totalSize, position: "relative", width: "100%" }}>
        {virtualItems.map((virtualItem) => (
          <div
            key={getKey(items[virtualItem.index], virtualItem.index)}
            data-index={virtualItem.index}
            ref={virtualizer.measureElement}
            style={{
              position: "absolute",
              top: 0,
              left: 0,
              width: "100%",
              transform: `translateY(${virtualItem.start}px)`,
            }}
          >
            {renderItem(items[virtualItem.index], virtualItem.index)}
          </div>
        ))}
      </div>
    </div>
  );
}

/**
 * 合并多个 ref 为一个回调 ref（内部 ref + 外部透传 ref）。
 *
 * 目的:
 *   让 VirtualList 内部的 `parentRef`（供 useVirtualizer 测量）与外部调用方传入的
 *   `scrollContainerRef`（供操作滚动位置）指向同一 DOM 节点，互不影响。
 *
 * 参数:
 *   refs - 任意数量的 ref（RefObject 或回调 ref 或 null）。
 *
 * 返回:
 *   合并后的回调 ref。
 *
 * 异常:
 *   不抛出。
 *
 * @sideeffect 无（纯函数）。
 */
function mergeRefs<T>(...refs: Array<Ref<T> | undefined>): (node: T | null) => void {
  return (node: T | null) => {
    for (const ref of refs) {
      if (!ref) continue;
      if (typeof ref === "function") {
        ref(node);
      } else {
        (ref as { current: T | null }).current = node;
      }
    }
  };
}
