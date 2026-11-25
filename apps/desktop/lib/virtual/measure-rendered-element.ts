import { measureElement, type Virtualizer } from "@tanstack/react-virtual";

/**
 * 虚拟列表的行高测量：行没有被渲染时（自身或祖先 `display:none`）不采用这次读数，沿用缓存高度，
 * 没有缓存就用估计高度。按实测行高布局（行挂 `measureElement` ref）的虚拟列表都经它测量；只用固定
 * 估计高度的列表（如文件搜索结果）不测量，不需要它。
 *
 * 为什么不能用隐藏时的读数：列表所在区域被隐藏（窄窗口的会话观测详情、浏览器铺满时的对话区）
 * 时，ResizeObserver 把每一行报成 0，TanStack 会把 0 写进行高缓存；重新显示时各行从 0 长回真实
 * 高度，被当成「重新测量」，而「行顶 + 旧高度 ≤ 滚动位置」的行会被视为在视口上方并补偿滚动——
 * 旧高度全是 0、行顶挤在一起，于是逐行补偿，列表一次跳出好几行。没有缓存时也不能回退成 0，
 * 隐藏期间新挂载的行同样会被记成 0。
 *
 * `onMeasured` 只在真正测得高度时调用，供调用方维护自己的行高记忆（不能把隐藏时的 0 记进去）。
 *
 * 读数与当前尺寸（估计或记忆）恰好相等时，TanStack 的 `resizeItem` 不写 `itemSizeCache`（只在差值非零时写），
 * 这一行就一直算「没测过」：尺寸跟着 `estimateSize` 走，内容一变（回复落定、行高记忆失效）就悄悄换成新估计、
 * 不做补偿，下一次真实变化又被当成首次测量去补偿跨过视口的行，正在读的位置因此跳走。估计越准、按记忆回到对话
 * 时越容易相等，所以这里在相等时把读数记成已测量；不相等的仍交给 `resizeItem` 按差值补偿后写入。
 */
export function measureRenderedElement<TScrollElement extends Element | Window, TItemElement extends Element>(
  element: TItemElement,
  entry: ResizeObserverEntry | undefined,
  instance: Virtualizer<TScrollElement, TItemElement>,
  onMeasured?: (size: number) => void,
): number {
  if (!element.checkVisibility()) {
    const index = instance.indexFromElement(element);
    return instance.itemSizeCache.get(instance.options.getItemKey(index)) ?? instance.options.estimateSize(index);
  }
  const size = measureElement(element, entry, instance);
  const index = instance.indexFromElement(element);
  const key = instance.options.getItemKey(index);
  if (!instance.itemSizeCache.has(key) && instance.getVirtualItems().find((item) => item.index === index)?.size === size) {
    instance.itemSizeCache.set(key, size);
  }
  onMeasured?.(size);
  return size;
}
