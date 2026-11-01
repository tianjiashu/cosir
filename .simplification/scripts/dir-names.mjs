// 检测器：无语义目录名与膨胀的通用目录（纯路径检查，不读文件内容）。
//
// 对应条款与档位：
//   目录名属于无语义名单（misc、temp、tmp、new、old、v2、common2、stuff、other 等） → S8，一档；
//   utils / util / helpers / common / shared 目录直接包含的源文件超过阈值 → C4，二档（是否该按能力拆分需要判断）。
// 只检查本轮范围内文件所在的目录及其上级（到 apps/desktop 为止），不扫 node_modules 与构建产物。
import { readdirSync } from "node:fs";
import { basename, dirname, join } from "node:path";
import { emit, loadScope, repositoryRoot, scopeFiles } from "./lib.mjs";

const DETECTOR = "dir-names";
const MEANINGLESS = /^(misc|temp|tmp|new|old|stuff|other|others|v\d+|(common|utils?|helpers?|new|old)\d+)$/i;
const GENERIC = /^(utils?|helpers?|common|shared)$/i;
const GENERIC_FILE_LIMIT = 12;
const scope = loadScope();

const files = scopeFiles(scope).filter((file) => file.startsWith("apps/desktop/"));

const directories = new Set();
for (const file of files) {
  for (let dir = dirname(file); dir.startsWith("apps/desktop/"); dir = dirname(dir)) directories.add(dir);
}

for (const dir of [...directories].sort()) {
  const name = basename(dir);
  if (MEANINGLESS.test(name)) {
    emit(DETECTOR, { transform: "S8", tier: 1, path: dir, evidence: `目录名「${name}」不表达内容` });
  }
  if (GENERIC.test(name)) {
    const count = readdirSync(join(repositoryRoot, dir), { withFileTypes: true }).filter(
      (entry) => entry.isFile() && /\.(ts|tsx|mjs|js)$/.test(entry.name),
    ).length;
    if (count > GENERIC_FILE_LIMIT) {
      emit(DETECTOR, { transform: "C4", tier: 2, path: dir, evidence: `通用目录直接包含 ${count} 个源文件（阈值 ${GENERIC_FILE_LIMIT}），检查是否该按能力拆分` });
    }
  }
}
