import fs from "node:fs";
import path from "node:path";

/**
 * 交给系统「打开」就会运行的后缀，按平台列。包括程序与安装包、脚本、指向程序的快捷方式（`.lnk`、`.fileloc`），
 * 以及会装成系统组件的捆绑包（`.prefpane`、`.saver`）。宁多勿漏：多列一项只是多一次确认。
 */
const LAUNCHABLE_EXTENSIONS: Record<string, ReadonlySet<string>> = {
  darwin: new Set([
    ".app", ".command", ".tool", ".terminal", ".sh", ".workflow", ".action", ".fileloc", ".inetloc",
    ".pkg", ".mpkg", ".prefpane", ".saver", ".jar",
  ]),
  win32: new Set([
    ".exe", ".com", ".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".msi", ".msp",
    ".scr", ".hta", ".cpl", ".msc", ".reg", ".lnk", ".url", ".pif", ".jar", ".appref-ms", ".application",
  ]),
  linux: new Set([".desktop", ".sh", ".run", ".appimage", ".jar"]),
};

/**
 * 没有后缀、带可执行权限的文件：访达双击会在终端里运行，Linux 的文件管理器也可能直接执行。有后缀时系统按后缀选应用，
 * 带执行权限的 `.py` 照样在编辑器里打开，所以只认无后缀的。Windows 没有执行权限位。
 */
function executableWithoutExtension(target: string, platform: string): boolean {
  if (platform === "win32" || path.extname(target) !== "") return false;
  const stat = fs.statSync(target);
  return stat.isFile() && (stat.mode & 0o111) !== 0;
}

/**
 * 这个文件交给系统打开（`shell.openPath`）会不会被运行。路径本身与符号链接解析后的真实路径都看：叫 `report.pdf`
 * 的链接可以指向程序。只按后缀与执行权限判断，不读文件内容；调用方已确认路径存在。
 */
export function isLaunchable(target: string, platform: string = process.platform): boolean {
  const extensions = LAUNCHABLE_EXTENSIONS[platform] ?? LAUNCHABLE_EXTENSIONS.linux;
  const candidates = new Set([target, fs.realpathSync(target)]);
  return [...candidates].some((candidate) =>
    extensions.has(path.extname(candidate).toLowerCase()) || executableWithoutExtension(candidate, platform));
}
