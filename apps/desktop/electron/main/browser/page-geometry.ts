import type { Box } from "./input/element";

/**
 * 页面几何：坐标系与视口的唯一来源，一次 `Page.getLayoutMetrics` 读全。截图的范围与像素、坐标落点的范围、高亮框的坐标、
 * 快照的视口几何都从这里取，不各自再问页面。
 *
 * 几种坐标（实测）：
 * - 可视视口（CSS 像素、文档坐标）是 Agent 看到的那块：视口截图按它出图，坐标落点按它判断越界。手机模拟下没写
 *   viewport 声明的网页按 980 宽排版、整体缩小显示，可视视口是 980 宽，而不是模拟的 390——截图与坐标都按 980 算。
 * - 合成鼠标输入用视口里的 CSS 像素，不乘页面缩放，只乘面板的显示缩放（`TabEmulation.toInput`，那份事实归它）。
 * - 高亮框（Overlay）的坐标是 CSS 像素乘页面缩放、再除以设备像素比，与显示缩放无关（Retina 上不除会落到两倍处，
 *   照合成输入那样乘显示缩放则落到 1.12 倍处；手机模拟下页面缩到约 0.4 倍时，不乘页面缩放会画到可视区域之外）。
 * - `DOMSnapshot` 的元素框与滚动是设备像素（`deviceRegion` 把可视视口换到同一单位）。
 * 设备像素比取布局视口的设备像素宽与 CSS 宽之比，与页面的 `devicePixelRatio` 一致（含面板缩放、手机模拟两种情况）。
 */

/** `Page.getLayoutMetrics` 里本模块用到的字段。 */
export type LayoutMetrics = {
  layoutViewport: { clientWidth: number };
  cssLayoutViewport: { clientWidth: number };
  cssVisualViewport: { pageX: number; pageY: number; clientWidth: number; clientHeight: number; scale: number };
  cssContentSize: { width: number; height: number };
};

export type PageGeometry = {
  /** 可视视口：CSS 像素、文档坐标。 */
  visual: Box;
  /** 一个 CSS 像素合几个设备像素。 */
  deviceRatio: number;
  /** 页面缩放：手机模拟下按更宽的布局排版、缩小显示时小于 1，其余为 1。 */
  pageScale: number;
  /** 整个文档的 CSS 尺寸。 */
  content: { width: number; height: number };
};

type Send = <T>(method: string, params?: Record<string, unknown>) => Promise<T>;

export async function readGeometry(send: Send): Promise<PageGeometry> {
  return geometryOf(await send<LayoutMetrics>("Page.getLayoutMetrics"));
}

export function geometryOf(metrics: LayoutMetrics): PageGeometry {
  const view = metrics.cssVisualViewport;
  return {
    visual: { x: view.pageX, y: view.pageY, width: view.clientWidth, height: view.clientHeight },
    deviceRatio: metrics.layoutViewport.clientWidth / metrics.cssLayoutViewport.clientWidth,
    pageScale: view.scale,
    content: { width: metrics.cssContentSize.width, height: metrics.cssContentSize.height },
  };
}

/** 可视视口换成设备像素、文档坐标：与 `DOMSnapshot` 的元素框同一单位。 */
export function deviceRegion({ visual, deviceRatio }: PageGeometry): Box {
  return { x: visual.x * deviceRatio, y: visual.y * deviceRatio, width: visual.width * deviceRatio, height: visual.height * deviceRatio };
}

/** 视口里的一块（CSS 像素）→ 高亮框的四个角。 */
export function overlayQuad(area: Box, { deviceRatio, pageScale }: PageGeometry): number[] {
  const [x, y, right, bottom] = [area.x, area.y, area.x + area.width, area.y + area.height].map((value) => (value * pageScale) / deviceRatio);
  return [x, y, right, y, right, bottom, x, bottom];
}
