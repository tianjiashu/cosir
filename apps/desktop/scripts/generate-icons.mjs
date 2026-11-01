import { Buffer } from "node:buffer";
import { execFileSync } from "node:child_process";
import console from "node:console";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { chromium } from "@playwright/test";

import { desktopRoot } from "./project-paths.mjs";

/**
 * 由 `electron/resources/icons/source/` 下的两个 SVG 生成全部位图图标；改图标只改 SVG，再手动运行本脚本
 * （`node scripts/generate-icons.mjs`），产物提交进仓库，构建不依赖它。
 *
 * 栅格化借 Playwright 的 Chromium，免得为一次性生成引入 SVG 渲染依赖；`.icns` 用 macOS 自带的 `iconutil`，
 * 所以只能在 macOS 上运行。
 *
 * - `cosir-icon.svg`：满版圆角方块。Windows（`.ico`、窗口与托盘）直接使用；macOS（`.icns`、Dock）按
 *   Apple 图标网格缩到 1024 画布中央 824 的主体，否则在 Dock 里比其他应用大一圈。
 * - `cosir-tray.svg`：只有字形的黑色剪影，作 macOS 菜单栏模板图（系统按明暗着色）。
 */

const iconsDir = path.join(desktopRoot, "electron", "resources", "icons");
const sourceDir = path.join(iconsDir, "source");
const MACOS_BODY_RATIO = 824 / 1024;

const appSvg = fs.readFileSync(path.join(sourceDir, "cosir-icon.svg"), "utf8");
const traySvg = fs.readFileSync(path.join(sourceDir, "cosir-tray.svg"), "utf8");

const browser = await chromium.launch();
const page = await browser.newPage();

/** 把 SVG 渲染成 `size` 见方的透明底 PNG；`bodyRatio` < 1 时主体居中缩小、四周留白。 */
async function renderPng(svg, size, bodyRatio = 1) {
  const body = Math.round(size * bodyRatio);
  const src = `data:image/svg+xml;base64,${Buffer.from(svg).toString("base64")}`;
  await page.setViewportSize({ width: size, height: size });
  await page.setContent(
    `<html><body style="margin:0;background:transparent;display:grid;place-items:center;width:${size}px;height:${size}px">`
    + `<img src="${src}" width="${body}" height="${body}"></body></html>`,
  );
  await page.locator("img").evaluate((img) => img.decode());
  return page.screenshot({ omitBackground: true, clip: { x: 0, y: 0, width: size, height: size } });
}

/** ICO 容器：每个尺寸直接内嵌 PNG（Vista 起支持），256 在目录项里记作 0。 */
function buildIco(images) {
  const header = Buffer.alloc(6);
  header.writeUInt16LE(1, 2);
  header.writeUInt16LE(images.length, 4);
  let offset = 6 + images.length * 16;
  const entries = images.map(({ size, png }) => {
    const entry = Buffer.alloc(16);
    entry.writeUInt8(size >= 256 ? 0 : size, 0);
    entry.writeUInt8(size >= 256 ? 0 : size, 1);
    entry.writeUInt16LE(1, 4);
    entry.writeUInt16LE(32, 6);
    entry.writeUInt32LE(png.length, 8);
    entry.writeUInt32LE(offset, 12);
    offset += png.length;
    return entry;
  });
  return Buffer.concat([header, ...entries, ...images.map(({ png }) => png)]);
}

function write(relativePath, data) {
  const target = path.join(iconsDir, relativePath);
  fs.writeFileSync(target, data);
  console.log(`wrote ${path.relative(desktopRoot, target)}`);
}

try {
  const icoImages = [];
  for (const size of [16, 24, 32, 48, 64, 128, 256]) icoImages.push({ size, png: await renderPng(appSvg, size) });
  const ico = buildIco(icoImages);
  write("icon.ico", ico);
  fs.writeFileSync(path.join(desktopRoot, "public", "favicon.ico"), buildIco(icoImages.filter(({ size }) => size <= 48)));
  console.log("wrote public/favicon.ico");

  write("icon.png", await renderPng(appSvg, 512));
  write("dock.png", await renderPng(appSvg, 1024, MACOS_BODY_RATIO));

  const iconset = fs.mkdtempSync(path.join(os.tmpdir(), "cosir-")) + "/cosir.iconset";
  fs.mkdirSync(iconset);
  for (const size of [16, 32, 128, 256, 512]) {
    fs.writeFileSync(path.join(iconset, `icon_${size}x${size}.png`), await renderPng(appSvg, size, MACOS_BODY_RATIO));
    fs.writeFileSync(path.join(iconset, `icon_${size}x${size}@2x.png`), await renderPng(appSvg, size * 2, MACOS_BODY_RATIO));
  }
  execFileSync("iconutil", ["-c", "icns", iconset, "-o", path.join(iconsDir, "icon.icns")]);
  fs.rmSync(path.dirname(iconset), { recursive: true, force: true });
  console.log("wrote electron/resources/icons/icon.icns");

  // 菜单栏图标 18pt（字形外接方框已留出边距），Windows 托盘 16px；`@2x` 由 nativeImage 按屏幕倍率自动选用。
  write("tray-macos.png", await renderPng(traySvg, 18));
  write("tray-macos@2x.png", await renderPng(traySvg, 36));
  write("tray-windows.png", await renderPng(appSvg, 16));
  write("tray-windows@2x.png", await renderPng(appSvg, 32));
} finally {
  await browser.close();
}
