import { describe, expect, it, vi } from "vitest";

import { EditorState } from "@codemirror/state";

import {
  continueMarkdownList,
  handleMarkdownKeydown,
  indentMarkdownList,
  insertMarkdownLink,
  toggleBlockMarkdown,
  toggleInlineMarkdown,
} from "@/components/configuration/markdown-editor-shortcuts";

function applyTransaction(state: EditorState, transaction: ReturnType<typeof toggleInlineMarkdown> | null) {
  return transaction ? state.update(transaction).state : state;
}

function createState(doc: string, from?: number, to = from) {
  return EditorState.create({
    doc,
    selection: from === undefined ? undefined : { anchor: from, head: to },
  });
}

describe("markdown editor shortcuts", () => {
  it("wraps selected text with inline markers and toggles them off", () => {
    const state = createState("hello", 0, 5);
    const bold = applyTransaction(state, toggleInlineMarkdown(state, "**"));

    expect(bold.doc.toString()).toBe("**hello**");
    expect(bold.selection.main.from).toBe(2);
    expect(bold.selection.main.to).toBe(7);

    const unbold = applyTransaction(bold, toggleInlineMarkdown(bold, "**"));
    expect(unbold.doc.toString()).toBe("hello");
  });

  it("inserts an empty inline pair with the cursor inside", () => {
    const state = createState("");
    const next = applyTransaction(state, toggleInlineMarkdown(state, "`"));

    expect(next.doc.toString()).toBe("``");
    expect(next.selection.main.from).toBe(1);
    expect(next.selection.main.to).toBe(1);
  });

  it("formats selected paragraphs as a numbered list and toggles it off", () => {
    const state = createState("one\ntwo", 0, 7);
    const listed = applyTransaction(state, toggleBlockMarkdown(state, "ordered"));

    expect(listed.doc.toString()).toBe("1. one\n2. two");

    const unlisted = applyTransaction(listed, toggleBlockMarkdown(listed, "ordered"));
    expect(unlisted.doc.toString()).toBe("one\ntwo");
  });

  it("converts an existing list type instead of nesting another marker", () => {
    const state = createState("- one\n- two", 0, 11);
    const quoted = applyTransaction(state, toggleBlockMarkdown(state, "quote"));

    expect(quoted.doc.toString()).toBe("> one\n> two");
  });

  it("creates a link and selects the URL placeholder", () => {
    const state = createState("文档", 0, 2);
    const linked = applyTransaction(state, insertMarkdownLink(state));

    expect(linked.doc.toString()).toBe("[文档](url)");
    expect(linked.sliceDoc(linked.selection.main.from, linked.selection.main.to)).toBe("url");
  });

  it("continues non-empty list items and exits empty list items", () => {
    const state = createState("- first", 7);
    const continued = applyTransaction(state, continueMarkdownList(state));
    expect(continued.doc.toString()).toBe("- first\n- ");

    const empty = createState("- ", 2);
    const exited = applyTransaction(empty, continueMarkdownList(empty));
    expect(exited.doc.toString()).toBe("\n");
  });

  it("indents and outdents selected list lines", () => {
    const state = createState("- one\n- two", 0, 11);
    const indented = applyTransaction(state, indentMarkdownList(state));
    expect(indented.doc.toString()).toBe("  - one\n  - two");

    const outdented = applyTransaction(indented, indentMarkdownList(indented, true));
    expect(outdented.doc.toString()).toBe("- one\n- two");
  });

  it("handles shifted digit shortcuts through KeyboardEvent.code", () => {
    let currentState = createState("one");
    const dispatch = vi.fn((transaction: Parameters<EditorState["update"]>[0]) => {
      currentState = currentState.update(transaction).state;
    });
    const view = {
      get state() {
        return currentState;
      },
      dispatch,
    } as never;
    const event = {
      key: "*",
      code: "Digit8",
      metaKey: true,
      ctrlKey: false,
      altKey: false,
      shiftKey: true,
      preventDefault: vi.fn(),
    } as unknown as KeyboardEvent;

    expect(handleMarkdownKeydown(view, event)).toBe(true);
    expect(currentState.doc.toString()).toBe("- one");
    expect(event.preventDefault).toHaveBeenCalledOnce();
  });
});
