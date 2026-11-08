/**
 * 浏览器标签在界面上怎么称呼（标签条、地址栏、工具卡片共用）。空白页（`about:blank`，新开还没输入地址）没有可显示的
 * 地址：地址栏显示为空，标签名回落到「新标签页」。
 */

const BLANK = "about:blank";

/** 地址栏里显示的地址：空白页为空。 */
export function displayedAddress(url: string): string {
  return url === BLANK ? "" : url;
}

/** 标签名：标题，没有标题时用地址，空白页叫「新标签页」。 */
export function tabLabel(title: string, url: string): string {
  return title || displayedAddress(url) || "新标签页";
}
