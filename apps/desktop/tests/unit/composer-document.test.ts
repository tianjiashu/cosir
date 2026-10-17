import { describe, expect, it } from "vitest";
import type { JSONContent } from "@tiptap/core";
import {
  resolveComposerInsertionPosition,
  shouldSubmitOnEnter,
} from "@/components/composer/composer-editor-policy";
import {
  insertionCommandContent,
  parseComposerDocument,
  serializeComposerDocument,
  shouldPreservePendingEditorValue,
} from "@/components/composer/composer-document";

describe("composer document conversion", () => {
  it.each([
    "",
    "普通文本",
    "第一行\n第二行",
    "第一行\n\n末行\n",
    "<script>alert(1)</script> & 保持为普通文本",
    "[[cosir-file:missing]] 是手写文本",
  ])("纯文本往返保持不变：%j", (text) => {
    expect(serializeComposerDocument(parseComposerDocument(text))).toBe(text);
  });

  it("把附件、图片和 Skill token 解析为原子节点并保持顺序", () => {
    const value = "开头 [[cosir-file:file-1]] [[cosir-image:image-1]] [[cosir-skill:research%20notes]] 结尾";
    const document = parseComposerDocument(value);

    expect(document.content?.[0]?.content?.map((node) => node.type)).toEqual([
      "text", "attachmentToken", "text", "attachmentToken", "text", "skillToken", "text",
    ]);
    expect(serializeComposerDocument(document)).toBe(value);
  });

  it("编辑 canonical 消息时去掉附件 token 外层注释", () => {
    const canonical = "查看 <!-- [[cosir-file:file-1]] -->\n下一行";

    expect(serializeComposerDocument(parseComposerDocument(canonical)))
      .toBe("查看 [[cosir-file:file-1]]\n下一行");
  });

  it("把外部插入命令转换成有序的行内节点", () => {
    const commands = [
      insertionCommandContent({ type: "insert-text", text: "多行\n提示" }),
      insertionCommandContent({
        type: "insert-attachments",
        attachments: [{ id: "file-1", name: "notes.md" }, { id: "image-1", name: "shot.png", kind: "image" }],
      }),
      insertionCommandContent({ type: "insert-skill", skill: { name: "research" } }),
    ];
    const document: JSONContent = {
      type: "doc",
      content: [{ type: "paragraph", content: commands.flat() }],
    };

    expect(serializeComposerDocument(document)).toBe(
      "多行\n提示[[cosir-file:file-1]][[cosir-image:image-1]][[cosir-skill:research]]",
    );
  });

  it("受控值追上内部编辑前保留本地草稿", () => {
    const lastPropValue = "初始";
    const pending = "初始[[cosir-file:file-1]]";

    expect(shouldPreservePendingEditorValue("初始", lastPropValue, pending)).toBe(true);
    expect(shouldPreservePendingEditorValue(pending, lastPropValue, pending)).toBe(false);
  });
});

describe("composer Enter submission policy", () => {
  function event(overrides: Partial<{
    key: string;
    shiftKey: boolean;
    defaultPrevented: boolean;
    isComposing: boolean;
    keyCode: number;
  }> = {}) {
    return {
      key: overrides.key ?? "Enter",
      shiftKey: overrides.shiftKey ?? false,
      defaultPrevented: overrides.defaultPrevented ?? false,
      nativeEvent: {
        isComposing: overrides.isComposing ?? false,
        keyCode: overrides.keyCode ?? 13,
      },
    };
  }

  it("只提交未被输入法或其它处理器占用的普通 Enter", () => {
    expect(shouldSubmitOnEnter(event(), false)).toBe(true);
    expect(shouldSubmitOnEnter(event({ shiftKey: true }), false)).toBe(false);
    expect(shouldSubmitOnEnter(event({ isComposing: true }), false)).toBe(false);
    expect(shouldSubmitOnEnter(event({ keyCode: 229 }), false)).toBe(false);
    expect(shouldSubmitOnEnter(event(), true)).toBe(false);
    expect(shouldSubmitOnEnter(event({ defaultPrevented: true }), false)).toBe(false);
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
