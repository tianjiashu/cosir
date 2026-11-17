import { describe, expect, it } from "vitest";

import { HttpError } from "@/lib/http/http-error";
import { fileReadProblem } from "@/lib/workspace-files/read-problem";

describe("fileReadProblem", () => {
  it("按后端错误码区分文件不存在、工作区不存在、无法访问与读取故障", () => {
    expect(fileReadProblem(new HttpError("文件不存在", { status: 404, code: "FILE_NOT_FOUND" }))).toBe("missing-file");
    expect(fileReadProblem(new HttpError("工作区已不存在", { status: 404, code: "WORKSPACE_NOT_FOUND" }))).toBe("missing-workspace");
    // 403 分不清是越界链接、没有读权限还是系统拒绝，单列一类，界面用中性的「无法访问」。
    expect(fileReadProblem(new HttpError("无法访问文件", { status: 403 }))).toBe("denied");
    expect(fileReadProblem(new HttpError("未知 404", { status: 404 }))).toBe("error");
    expect(fileReadProblem(new Error("网络错误"))).toBe("error");
  });
});
