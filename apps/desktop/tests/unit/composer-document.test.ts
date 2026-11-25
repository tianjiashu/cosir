import { isMacKeyboard } from "@/lib/keyboard/shortcuts";
import { platform } from "@/lib/platform";
import { serializeCapsule } from "@/lib/assistant/capsules";
import { describe, expect, it } from "vitest";
import type { JSONContent } from "@tiptap/core";
import {
  resolveComposerInsertionPosition,
  composerSubmitIntent,
} from "@/components/composer/composer-editor-policy";
import {
  insertionCommandContent,
  parseComposerDocument,
  serializeComposerDocument,
  shouldPreservePendingEditorValue,
} from "@/components/composer/composer-document";

describe("composer document conversion", () => {
  it("摘录胶囊是一个原子节点：多行原文、代码与标签都在节点里，往返后正文不变（撤回、草稿据此还原）", () => {
    const quote = { kind: "quote" as const, text: "## 标题\n```py\nx = `a`\n```\n<file path=\"/x\" />" };
    const value = `第一行\n${serializeCapsule(quote)} 这里改一下`;
    const document = parseComposerDocument(value);

    expect(document.content).toHaveLength(2);
    expect(document.content?.[1]?.content?.map((node) => node.type)).toEqual(["capsuleToken", "text"]);
    expect(document.content?.[1]?.content?.[0]?.attrs?.capsule).toEqual(quote);
    expect(serializeComposerDocument(document)).toBe(value);
  });


  it.each([
    "",
    "普通文本",
    "第一行\n第二行",
    "第一行\n\n末行\n",
    "<script>alert(1)</script> & 保持为普通文本",
    '<file path="/tmp/missing.md" 是手写文本',
    '<file path = "/tmp/x.md" /> 语法不合法保持文本',
    '<ref path="a.md" lines="9-2" /> 行范围不合法保持文本',
  ])("纯文本往返保持不变：%j", (text) => {
    expect(serializeComposerDocument(parseComposerDocument(text))).toBe(text);
  });

  it("把胶囊标签解析为同一个原子节点并保持顺序与类型", () => {
    const value = '开头 <file path="/tmp/notes.md" /> <ref path="src/a.py" lines="12-14" /> <skill name="review-code" /> 结尾';
    const document = parseComposerDocument(value);

    expect(document.content?.[0]?.content?.map((node) => node.type)).toEqual([
      "text", "capsuleToken", "text", "capsuleToken", "text", "capsuleToken", "text",
    ]);
    expect(document.content?.[0]?.content?.filter((node) => node.type === "capsuleToken").map((node) => node.attrs?.capsule)).toEqual([
      { kind: "file", path: "/tmp/notes.md" },
      { kind: "ref", path: "src/a.py", lines: "12-14" },
      { kind: "skill", name: "review-code" },
    ]);
    expect(serializeComposerDocument(document)).toBe(value);
  });

  it("代码块与行内代码里的同形标签保持字面量", () => {
    const value = '示例 `<file path="/tmp/a.md" />`\n\n```\n<skill name="review-code" />\n```';
    const document = parseComposerDocument(value);

    expect(document.content?.flatMap((node) => node.content ?? []).map((node) => node.type)).not.toContain("capsuleToken");
    expect(serializeComposerDocument(document)).toBe(value);
  });

  it("胶囊序列化为规范标签：属性顺序、转义与目录尾斜杠", () => {
    const document = parseComposerDocument(
      '<file path="/tmp/a &amp; b.md" /><ref path="src/x.pdf" page="3" /><skill name="s" path="/abs/SKILL.md" /><file path="/tmp/dir/" />',
    );
    expect(serializeComposerDocument(document)).toBe(
      '<file path="/tmp/a &amp; b.md" /><ref path="src/x.pdf" page="3" /><skill name="s" path="/abs/SKILL.md" /><file path="/tmp/dir/" />',
    );
  });

  it("把外部插入命令转换成有序的行内节点", () => {
    const commands = [
      insertionCommandContent({ type: "insert-text", text: "多行\n提示" }),
      insertionCommandContent({
        type: "insert-capsules",
        capsules: [
          { kind: "file", path: "/tmp/notes.md" },
          { kind: "skill", name: "research" },
        ],
      }),
    ];
    const document: JSONContent = {
      type: "doc",
      content: [{ type: "paragraph", content: commands.flat() }],
    };

    expect(serializeComposerDocument(document)).toBe(
      '多行\n提示<file path="/tmp/notes.md" /><skill name="research" />',
    );
  });

  it("受控值追上内部编辑前保留本地草稿", () => {
    const lastPropValue = "初始";
    const pending = '初始<file path="/tmp/notes.md" />';

    expect(shouldPreservePendingEditorValue("初始", lastPropValue, pending)).toBe(true);
    expect(shouldPreservePendingEditorValue(pending, lastPropValue, pending)).toBe(false);
  });
});

describe("composer Enter submission policy", () => {
  function event(overrides: Partial<{
    key: string;
    shiftKey: boolean;
    metaKey: boolean;
    ctrlKey: boolean;
    altKey: boolean;
    defaultPrevented: boolean;
    isComposing: boolean;
    keyCode: number;
  }> = {}) {
    return {
      key: overrides.key ?? "Enter",
      code: "Enter",
      shiftKey: overrides.shiftKey ?? false,
      metaKey: overrides.metaKey ?? false,
      ctrlKey: overrides.ctrlKey ?? false,
      altKey: overrides.altKey ?? false,
      defaultPrevented: overrides.defaultPrevented ?? false,
      nativeEvent: {
        isComposing: overrides.isComposing ?? false,
        keyCode: overrides.keyCode ?? 13,
      },
    };
  }

  it("普通 Enter 发送，未被输入法或其它处理器占用时才提交", () => {
    expect(composerSubmitIntent(event(), false)).toBe("send");
    expect(composerSubmitIntent(event({ shiftKey: true }), false)).toBeNull();
    expect(composerSubmitIntent(event({ isComposing: true }), false)).toBeNull();
    expect(composerSubmitIntent(event({ keyCode: 229 }), false)).toBeNull();
    expect(composerSubmitIntent(event(), true)).toBeNull();
    expect(composerSubmitIntent(event({ defaultPrevented: true }), false)).toBeNull();
  });

  it("主修饰键 + Enter 加入队列，其余修饰组合不提交", () => {
    const mac = isMacKeyboard(platform);
    const primary = mac ? { metaKey: true } : { ctrlKey: true };
    const other = mac ? { ctrlKey: true } : { metaKey: true };
    expect(composerSubmitIntent(event(primary), false)).toBe("queue");
    expect(composerSubmitIntent(event({ ...primary, isComposing: true }), false)).toBeNull();
    expect(composerSubmitIntent(event(other), false)).toBeNull();
    expect(composerSubmitIntent(event({ altKey: true }), false)).toBeNull();
  });
});

describe("composer external insertion position", () => {
  it("未捕获用户选区时追加到文档末尾", () => {
    expect(resolveComposerInsertionPosition(false, 1, 19)).toBe(18);
  });

  it("已捕获用户选区时保留当前锚点", () => {
    expect(resolveComposerInsertionPosition(true, 5, 19)).toBe(5);
  });
});
