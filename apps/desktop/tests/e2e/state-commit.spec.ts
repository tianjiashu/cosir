import { expect, test } from "@playwright/test";
import { API_ORIGIN } from "./e2e-env";
import { activity, assistantMessage, transportState } from "../unit/transport-fixtures";

test("canonical commits occur once before settled observers, independent of callback rerenders", async ({ page, request }) => {
  await request.post(`${API_ORIGIN}/__test__/reset`);
  const initial = transportState();
  const active = transportState({ messages: [assistantMessage("a", 1, [{ type: "text", text: "hello" }])], activity: activity(1, "running") });
  const frames = [
    { task_id: 907, kind: "full", state: active, mutations: [] },
    { task_id: 907, kind: "mutation", mutations: [{ kind: "append-text", path: ["messages", 0, "parts", 0, "text"], value: " world" }] },
    { task_id: 907, kind: "mutation", mutations: [{ kind: "set", path: ["activity"], value: activity(1, "completed") }] },
  ];
  await page.route("**/tasks/907/assistant/attach", route => route.fulfill({ contentType: "text/event-stream", body: frames.map(frame => `data: ${JSON.stringify(frame)}\n\n`).join("") }));
  await page.goto("/");
  await page.evaluate(async initialState => {
    const path = "/tests/e2e/fixtures/state-commit-probe.tsx";
    const { mountStateCommitProbe } = await import(/* @vite-ignore */ path);
    Object.assign(window, { commitProbe: mountStateCommitProbe(907, initialState) });
  }, initial);
  const expected = ["commit:empty", "commit:running", "commit:completed", "settled:completed:latest=completed", "disconnected"];
  const events = () => page.evaluate(() => (window as unknown as { commitProbe: { events: string[] } }).commitProbe.events);
  await expect.poll(events).toEqual(expected);
  await page.evaluate(async () => {
    (window as unknown as { commitProbe: { rerender: () => void } }).commitProbe.rerender();
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  });
  expect(await events()).toEqual(expected);
  await page.evaluate(() => (window as unknown as { commitProbe: { unmount: () => void } }).commitProbe.unmount());
});
