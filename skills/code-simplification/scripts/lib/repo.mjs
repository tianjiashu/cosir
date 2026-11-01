// 三个脚本共用的仓库工具：仓库根、git 调用、测试路径判定。
//
// 从任意工作目录运行都可以：仓库根由 git 确定，与本 skill 安装在哪里无关。
// 生产与测试的分界线只在这里定义一次（metrics 的行数统计、rank 的代码量、scope 的输出都用它）。
import { execFileSync } from "node:child_process";

export const repositoryRoot = execFileSync("git", ["rev-parse", "--show-toplevel"], {
  encoding: "utf8",
}).trim();

// 关闭 core.quotePath：否则非 ASCII 路径会以转义加引号的形式输出，与磁盘上的真实路径对不上。
export function git(...args) {
  return execFileSync("git", ["-c", "core.quotePath=false", ...args], {
    cwd: repositoryRoot,
    encoding: "utf8",
  }).trim();
}

// 按路径约定识别测试文件，语言无关；生产与测试代码分开统计（标准 E9）。
const TEST_PATH =
  /(^|\/)(tests?|__tests__|spec)\/|\.(test|spec)\.[^/]+$|(^|\/)test_[^/]+\.py$|_test\.[^/]+$/;

export function isTest(path) {
  return TEST_PATH.test(path);
}

// 参数或前提不成立时统一以退出码 2 结束：调用方（Agent）能据此停下并给出修复指引。
export function fail(message) {
  process.stderr.write(`${message}\n`);
  process.exit(2);
}
