// 检测器：吞掉异常、原样重抛与吞掉 Promise 拒绝的位置（基于 TypeScript 语法树，项目已有依赖）。
//
// 对应条款与档位（都是二档，需按标准 §4.3 四步判定确认；吞掉的失败可能发生时只能按 L3 建议）：
//   catch 块里没有 throw，且只做了记录或返回空值类默认值（null / undefined / false / [] / {} / ""） → F4；
//   catch 块只有一句 `throw <捕获的变量>`                                                         → F3；
//   `.catch(() => {})` 或回调只返回空值类默认值                                                    → F4。
// 「记录」按调用名识别：console.*、frontendLog、log.*、logger.*（本项目的结构化日志入口）。
// eslint-defense 的 no-empty 只抓空块；这里补上「只记录不处理」与 Promise.catch 两类。
// 已知局限：只看 catch 块本身，不追踪调用方是否依赖默认值；有意的失败安全（日志写入、退出清理）也会被报出，
//           确认时对照项目画像的「质量要求」排除。
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { emit, loadScope, repositoryRoot, requireFromDesktop, scopeFiles } from "./lib.mjs";

const DETECTOR = "catch-swallow";
const ts = requireFromDesktop("typescript");
const scope = loadScope();
const LOGGERS = /^(console\.\w+|frontendLog|log\.\w+|logger\.\w+)$/;

const targets = scopeFiles(scope).filter((file) => /^apps\/desktop\/(?!tests\/|scripts\/|node_modules\/).+\.tsx?$/.test(file));

const isEmptyValue = (node) =>
  node === undefined ||
  node.kind === ts.SyntaxKind.NullKeyword ||
  node.kind === ts.SyntaxKind.FalseKeyword ||
  (ts.isIdentifier(node) && node.text === "undefined") ||
  (ts.isArrayLiteralExpression(node) && node.elements.length === 0) ||
  (ts.isObjectLiteralExpression(node) && node.properties.length === 0) ||
  (ts.isStringLiteral(node) && node.text === "");

const isLogCall = (statement, source) =>
  ts.isExpressionStatement(statement) &&
  ts.isCallExpression(statement.expression) &&
  LOGGERS.test(statement.expression.expression.getText(source));

function containsThrow(node) {
  let found = false;
  const visit = (child) => {
    if (found || ts.isFunctionLike(child)) return;
    if (ts.isThrowStatement(child)) found = true;
    else ts.forEachChild(child, visit);
  };
  ts.forEachChild(node, visit);
  return found;
}

function report(file, source, node, transform, evidence) {
  const line = source.getLineAndCharacterOfPosition(node.getStart(source)).line + 1;
  emit(DETECTOR, { transform, tier: 2, path: file, line, evidence });
}

for (const file of targets) {
  const source = ts.createSourceFile(file, readFileSync(join(repositoryRoot, file), "utf8"), ts.ScriptTarget.Latest, true);

  const visit = (node) => {
    if (ts.isCatchClause(node)) {
      const statements = node.block.statements;
      const caught = node.variableDeclaration?.name.getText(source);
      if (
        statements.length === 1 &&
        ts.isThrowStatement(statements[0]) &&
        caught !== undefined &&
        statements[0].expression?.getText(source) === caught
      ) {
        report(file, source, node, "F3", "catch 块只把捕获的异常原样抛出");
      } else if (statements.length > 0 && !containsThrow(node.block)) {
        const onlyLogsOrDefaults = statements.every(
          (statement) => isLogCall(statement, source) || (ts.isReturnStatement(statement) && isEmptyValue(statement.expression)),
        );
        if (onlyLogsOrDefaults) report(file, source, node, "F4", "catch 块只记录或返回空值类默认值，异常没有继续传播");
      }
    }
    if (
      ts.isCallExpression(node) &&
      ts.isPropertyAccessExpression(node.expression) &&
      node.expression.name.text === "catch" &&
      node.arguments.length === 1 &&
      (ts.isArrowFunction(node.arguments[0]) || ts.isFunctionExpression(node.arguments[0]))
    ) {
      const body = node.arguments[0].body;
      const swallows = ts.isBlock(body) ? body.statements.length === 0 : isEmptyValue(body);
      if (swallows) report(file, source, node, "F4", "Promise.catch 回调吞掉拒绝，只返回空值或什么都不做");
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
}
