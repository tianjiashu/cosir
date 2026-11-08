import type { CdpSession } from "./cdp-session";
import type { Box } from "./input/element";
import { readGeometry } from "./page-geometry";

/** 整页截图的最大高度（CSS 像素）：再长的页面模型也看不清，按需滚动后再截。 */
const FULL_PAGE_MAX_HEIGHT = 4_000;

/** 元素截图四周留的边（CSS 像素）：贴边截看不出元素和周围的关系。 */
const ELEMENT_MARGIN = 8;
/** 元素截图较短的一边至少放大到这么多像素，至多放大 4 倍：小图标按原尺寸截，模型看不清。 */
const ELEMENT_MIN_SIDE = 160;
const ELEMENT_MAX_ZOOM = 4;

/** `element`：只截这块（主视口的 CSS 像素，`input/element.ts` 的 `box`），与 `fullPage` 互斥。 */
export type CaptureOptions = { quality?: number; fullPage?: boolean; element?: Box; devicePixels?: boolean; timeoutMs?: number };

/**
 * 截图，返回 data URL。用 CDP 而不是 `capturePage`：视图摘离窗口时后者不可用（§8）。截区（`clip`）是文档坐标：
 * 视口与元素的位置都要加上滚动。
 *
 * 按 CSS 像素出图（`clip.scale = 1 / 设备像素比`）：可视视口多大就是多大的图，不随屏幕倍率翻倍——模型按
 * 分辨率计费，多出来的像素看不出更多东西；Agent 给的坐标（`input/pointer.ts`）按同一个可视视口判断，因此与图上的
 * 像素一致（几何见 `page-geometry.ts`）。`fullPage` 截整页，高度封顶。`devicePixels` 按屏幕像素出图、`timeoutMs`
 * 缩短每条命令的时限，都只给浏览器面板的冻结帧用。
 */
export async function captureScreenshot(send: CdpSession["send"], format: "jpeg" | "png", options: CaptureOptions = {}): Promise<string> {
  const timing = { timeoutMs: options.timeoutMs };
  const { visual, content, deviceRatio } = await readGeometry((method, params) => send(method, params, timing));
  const clip = options.element
    ? elementClip(options.element, visual)
    : options.fullPage
      ? { x: 0, y: 0, width: visual.width, height: Math.min(content.height, FULL_PAGE_MAX_HEIGHT) }
      : visual;
  const zoom = options.element ? Math.min(ELEMENT_MAX_ZOOM, Math.max(1, ELEMENT_MIN_SIDE / Math.min(clip.width, clip.height))) : 1;
  const { data } = await send<{ data: string }>("Page.captureScreenshot", {
    format,
    ...(options.quality === undefined ? {} : { quality: options.quality }),
    clip: { ...clip, scale: zoom * (options.devicePixels ? 1 : 1 / deviceRatio) },
    // 元素可能比视口大（长表格）：超出视口的部分也要画出来。
    captureBeyondViewport: Boolean(options.fullPage || options.element),
  }, timing);
  return `data:image/${format};base64,${data}`;
}

/** 元素（视口坐标）换到文档坐标，四周加一圈边，不越过页面左上角，高度与整页截图同一上限。 */
function elementClip(element: Box, visual: Box): Box {
  const x = Math.max(0, visual.x + element.x - ELEMENT_MARGIN);
  const y = Math.max(0, visual.y + element.y - ELEMENT_MARGIN);
  return {
    x,
    y,
    width: visual.x + element.x + element.width + ELEMENT_MARGIN - x,
    height: Math.min(visual.y + element.y + element.height + ELEMENT_MARGIN - y, FULL_PAGE_MAX_HEIGHT),
  };
}
