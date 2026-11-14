import type { CdpSession } from "./cdp-session";
import type { Box } from "./input/element";
import { browserError } from "./errors";
import { readGeometry } from "./page-geometry";

/** 整页截图的最大高度（CSS 像素）：再长的页面模型也看不清，按需滚动后再截。 */
const FULL_PAGE_MAX_HEIGHT = 4_000;

/** 元素截图四周留的边（CSS 像素）：贴边截看不出元素和周围的关系。 */
const ELEMENT_MARGIN = 8;
/** 元素截图较短的一边至少放大到这么多像素，至多放大 4 倍：小图标按原尺寸截，模型看不清。 */
const ELEMENT_MIN_SIDE = 160;
const ELEMENT_MAX_ZOOM = 4;

/**
 * `element`：只截这个元素（主视口的 CSS 像素，`input/element.ts` 的 `box`），四周加边。`region`：只截 Agent 给的
 * 这一块（同一坐标系），不加边，必须整块落在可视视口内（否则 `OUT_OF_VIEWPORT`：裁剪后的图与给的坐标对不上）。
 * 三者互斥；小的两种按同一规则放大。
 */
export type CaptureOptions = { quality?: number; fullPage?: boolean; element?: Box; region?: Box; devicePixels?: boolean; timeoutMs?: number };

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
    : options.region
      ? regionClip(options.region, visual)
      : options.fullPage
        ? { x: 0, y: 0, width: visual.width, height: Math.min(content.height, FULL_PAGE_MAX_HEIGHT) }
        : visual;
  const zoom = options.element || options.region ? Math.min(ELEMENT_MAX_ZOOM, Math.max(1, ELEMENT_MIN_SIDE / Math.min(clip.width, clip.height))) : 1;
  const { data } = await send<{ data: string }>("Page.captureScreenshot", {
    format,
    ...(options.quality === undefined ? {} : { quality: options.quality }),
    clip: { ...clip, scale: zoom * (options.devicePixels ? 1 : 1 / deviceRatio) },
    // 元素可能比视口大（长表格）：超出视口的部分也要画出来。
    captureBeyondViewport: Boolean(options.fullPage || options.element),
  }, timing);
  return `data:image/${format};base64,${data}`;
}

/** 区域（视口坐标）换到文档坐标；任何一部分在可视视口外都拒绝。 */
function regionClip(region: Box, visual: Box): Box {
  if (region.x + region.width > visual.width || region.y + region.height > visual.height) {
    throw browserError("OUT_OF_VIEWPORT", `the region reaches outside the ${Math.round(visual.width)}×${Math.round(visual.height)} viewport; scroll first or give a smaller region`);
  }
  return { x: visual.x + region.x, y: visual.y + region.y, width: region.width, height: region.height };
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
