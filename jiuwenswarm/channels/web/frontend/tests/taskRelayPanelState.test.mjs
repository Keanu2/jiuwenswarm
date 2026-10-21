import assert from 'node:assert/strict';
import test from 'node:test';

import {
  awaitingReplyCleared,
  canSend,
  isAssistantSide,
  isTerminalState,
  latestSeq,
  mergeTaskMessages,
  payloadText,
  taskStateKey,
  toHeldTaskIds,
  toSeq,
  toTaskItems,
  toTaskMessages,
} from '../node_modules/.cache/task-relay-panel-state/features/taskRelayPanelState.js';

// ── task items (task.list) ──────────────────────────────────────────────────

test('toTaskItems keeps well-formed tasks and drops the rest', () => {
  const items = toTaskItems([
    { task_id: 3, title: '调研', state: 'working', lease_holder: 'u_me', lease_fence: 2 },
    { task_id: 0 },
    { title: '没有 id' },
    'junk',
    null,
    { task_id: '9', title: '字符串 id', state: 'pending' },
  ]);
  assert.deepEqual(items.map((t) => t.task_id), [3, 9]);
  assert.equal(items[0].state, 'working');
  assert.equal(items[0].lease_holder, 'u_me');
  assert.equal(items[0].lease_fence, 2);
  assert.equal(items[1].task_id, 9);
});

test('toTaskItems rejects a non-array', () => {
  assert.deepEqual(toTaskItems(undefined), []);
  assert.deepEqual(toTaskItems({ tasks: [] }), []);
});

test('taskStateKey accepts only relay states, else falls back to pending', () => {
  for (const state of ['pending', 'working', 'input_required', 'completed', 'failed', 'canceled']) {
    assert.equal(taskStateKey(state), state);
  }
  // 未知值回落 pending，面板不能被新后端的值清空。
  assert.equal(taskStateKey('bogus'), 'pending');
  assert.equal(taskStateKey(undefined), 'pending');
});

test('isTerminalState marks the states no turn may run on', () => {
  assert.equal(isTerminalState('completed'), true);
  assert.equal(isTerminalState('failed'), true);
  assert.equal(isTerminalState('canceled'), true);
  assert.equal(isTerminalState('working'), false);
  assert.equal(isTerminalState('input_required'), false);
  assert.equal(isTerminalState('pending'), false);
});

// ── messages (task.message frames / task.message.list) ──────────────────────

test('mergeTaskMessages orders by seq and is idempotent under reconnect overlap', () => {
  const first = mergeTaskMessages([], [{ seq: 3 }, { seq: 1 }]);
  const second = mergeTaskMessages(first, [{ seq: 2 }, { seq: 1 }]);
  assert.deepEqual(second.map((m) => m.seq), [1, 2, 3]);
  const third = mergeTaskMessages(second, [{ seq: 2 }, { seq: 3 }]);
  assert.equal(third.length, 3);
});

test('mergeTaskMessages drops entries without a usable seq', () => {
  const merged = mergeTaskMessages([], [{ seq: 0 }, { seq: 'x' }, null, { seq: 5 }]);
  assert.deepEqual(merged.map((m) => m.seq), [5]);
});

test('toTaskMessages accepts a raw page and rejects non-arrays', () => {
  assert.deepEqual(toTaskMessages(undefined), []);
  assert.deepEqual(toTaskMessages([{ seq: '7' }]).map((m) => m.seq), [7]);
});

test('latestSeq reports the resume cursor', () => {
  assert.equal(latestSeq([]), 0);
  assert.equal(latestSeq([{ seq: 4 }, { seq: 11 }]), 11);
});

// ── payload text (live turns vs handover imports) ───────────────────────────

test('payloadText reads both payload shapes', () => {
  // 实时对话：{"text"}。
  assert.equal(payloadText({ seq: 1, payload: { text: '继续' } }), '继续');
  // 会话交接导入：{"role","text"}——role 是元数据，不进正文。
  assert.equal(payloadText({ seq: 2, payload: { role: 'user', text: '旧问题' } }), '旧问题');
  assert.equal(payloadText({ seq: 3, payload: 'plain' }), 'plain');
  assert.equal(payloadText({ seq: 4, payload: { content: '内容字段' } }), '内容字段');
});

test('payloadText never throws on hostile payloads', () => {
  const cyclic = {};
  cyclic.self = cyclic;
  assert.equal(payloadText({ seq: 1, payload: cyclic }), '');
  assert.equal(payloadText({ seq: 1, payload: null }), '');
});

// ── bubble side (reply_to discriminator vs imported role) ──────────────────

test('isAssistantSide: reply_to marks the bridge answer; imported role marks a handover turn', () => {
  // 本面板 v1 不设 reply_to：无 reply_to 的 text 是用户侧。
  assert.equal(isAssistantSide({ seq: 41, reply_to: null, payload: { text: '问' } }), false);
  // bridge 的回答带 reply_to → 助手侧（回环防护的同一判别式）。
  assert.equal(isAssistantSide({ seq: 42, reply_to: 41, payload: { text: '答' } }), true);
  // 导入历史无 reply_to，按 payload.role 归侧。
  assert.equal(isAssistantSide({ seq: 1, reply_to: null, payload: { role: 'assistant', text: '旧答' } }), true);
  assert.equal(isAssistantSide({ seq: 2, reply_to: null, payload: { role: 'user', text: '旧问' } }), false);
});

// ── gating (panel never tracks fences) ──────────────────────────────────────

test('canSend gates on the bridge\'s live held list', () => {
  const held = toHeldTaskIds([7, '9', 0, 'junk', null]);
  assert.deepEqual(held, [7, 9]);
  assert.equal(canSend(7, held), true);
  assert.equal(canSend(8, held), false);
  // 非法入参不会意外开门。
  assert.equal(canSend(0, held), false);
  assert.equal(canSend(7, toHeldTaskIds(undefined)), false);
});

// ── awaiting-reply bookkeeping ──────────────────────────────────────────────

test('awaitingReplyCleared turns off once a reply_to entry lands', () => {
  assert.equal(awaitingReplyCleared([]), false);
  assert.equal(awaitingReplyCleared([{ seq: 41, reply_to: null }]), false);
  assert.equal(awaitingReplyCleared([{ seq: 41, reply_to: null }, { seq: 42, reply_to: 41 }]), true);
});

// ── toSeq ────────────────────────────────────────────────────────────────────

test('toSeq parses numbers and numeric strings, defaults to 0', () => {
  assert.equal(toSeq(5), 5);
  assert.equal(toSeq('12'), 12);
  assert.equal(toSeq(undefined), 0);
  assert.equal(toSeq('x'), 0);
});
