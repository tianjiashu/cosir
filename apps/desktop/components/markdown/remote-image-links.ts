import type { Element, ElementContent, Root } from "hast";

import { isLoopbackHost } from "@/electron/shared/local-network-host";

/** 图片地址指向本机回环以外的网站时给出主机名；相对地址、本机地址与其他协议为 null，照常交给渲染器。 */
function remoteHost(src: unknown): string | null {
  if (typeof src !== "string") return null;
  let url: URL;
  try {
    url = new URL(src);
  } catch {
    return null;
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") return null;
  return isLoopbackHost(url.hostname) ? null : url.hostname.toLowerCase();
}

function placeholder(image: Element, host: string, insideLink: boolean): ElementContent {
  const alt = typeof image.properties.alt === "string" ? image.properties.alt.trim() : "";
  const text: ElementContent = { type: "text", value: `图片：${alt || host}` };
  // 已在链接里（徽章写法 `[![](图)](链接)`）只留文字，不在链接里套链接，点击仍去外层链接。
  if (insideLink) return text;
  return { type: "element", tagName: "a", properties: { href: image.properties.src as string }, children: [text] };
}

function replaceImages(node: Root | Element, insideLink: boolean): void {
  node.children = node.children.map((child) => {
    if (child.type !== "element") return child;
    const host = child.tagName === "img" ? remoteHost(child.properties.src) : null;
    if (host) return placeholder(child, host, insideLink);
    replaceImages(child, insideLink || child.tagName === "a");
    return child;
  });
}

/**
 * rehype 插件：把指向网站的 Markdown 图片（含内嵌 HTML 的 `<img>`）换成「图片：说明」的链接，由链接组件交给打开器。
 *
 * 界面不能直接请求网站：宿主拦下了界面的全部公网请求（`electron/main/renderer-request-policy.ts`），否则模型写的
 * 图片地址一显示就把路径和参数送出去。这里只管呈现——不换的话图片被拦成一块破图；真正的防线在宿主。
 * 放在清洗之后，只看清洗留下的元素。
 */
export function rehypeRemoteImageLinks() {
  return (tree: Root) => replaceImages(tree, false);
}
