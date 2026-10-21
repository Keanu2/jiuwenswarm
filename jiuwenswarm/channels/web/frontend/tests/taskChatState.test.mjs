import assert from 'node:assert/strict';
import test from 'node:test';

import { getTaskActivityAt, mergeSidebarEntries, shouldHideHandedOverSession } from '../node_modules/.cache/task-chat-state/features/taskChatState.js';

// ── getTaskActivityAt ────────────────────────────────────────────────────────

test('getTaskActivityAt normalizes seconds and milliseconds like sessions do', () => {
  // 秒（< 1e11）→ 毫秒；毫秒原样。与会话侧 getSessionActivityAt 同规则。
  assert.equal(getTaskActivityAt({ updated_at: 1_700_000_000 }), 1_700_000_000_000);
  assert.equal(getTaskActivityAt({ updated_at: 1_700_000_000_000 }), 1_700_000_000_000);
});

test('getTaskActivityAt falls back to created_at, then 0', () => {
  assert.equal(getTaskActivityAt({ updated_at: undefined, created_at: 500 }), 500_000);
  assert.equal(getTaskActivityAt({}), 0);
});

// ── shouldHideHandedOverSession ─────────────────────────────────────────────

test('hides a handed-over session only while its task is visible', () => {
  const ids = new Set([42]);
  assert.equal(shouldHideHandedOverSession({ handover_task_id: 42 }, ids, true), true);
  // 任务不在列表（被 retire）：条目回归。
  assert.equal(shouldHideHandedOverSession({ handover_task_id: 7 }, ids, true), false);
});

test('never hides when tasks are unavailable (graceful degrade)', () => {
  const ids = new Set([42]);
  assert.equal(shouldHideHandedOverSession({ handover_task_id: 42 }, ids, false), false);
});

test('unmarked or malformed sessions are never hidden', () => {
  const ids = new Set([42, 1]);
  assert.equal(shouldHideHandedOverSession({}, ids, true), false);
  assert.equal(shouldHideHandedOverSession({ handover_task_id: null }, ids, true), false);
  assert.equal(shouldHideHandedOverSession({ handover_task_id: 0 }, ids, true), false);
  assert.equal(shouldHideHandedOverSession({ handover_task_id: -1 }, ids, true), false);
});

// ── mergeSidebarEntries ──────────────────────────────────────────────────────

test('merges sessions and tasks by interleaved activity, newest first', () => {
  const sessions = [
    { session_id: 's_old', last_user_message_at: 1_000 }, // 1e6 ms
    { session_id: 's_new', last_user_message_at: 5_000_000_000 }, // 5e12 ms
  ];
  const tasks = [
    { task_id: 1, updated_at: 3_000 }, // 3e6 ms → 中间
    { task_id: 2, updated_at: 9_000_000_000 }, // 9e12 ms → 最前
  ];
  const merged = mergeSidebarEntries(sessions, tasks, { tasksAvailable: true });
  assert.deepEqual(
    merged.map(entry => (entry.kind === 'session' ? entry.session.session_id : `task:${entry.task.task_id}`)),
    ['task:2', 's_new', 'task:1', 's_old'],
  );
});

test('hides a handed-over session and shows its task instead', () => {
  const sessions = [
    { session_id: 's_handed', last_user_message_at: 2_000, handover_task_id: 42 },
    { session_id: 's_plain', last_user_message_at: 1_000 },
  ];
  const tasks = [{ task_id: 42, updated_at: 9_000 }];
  const merged = mergeSidebarEntries(sessions, tasks, { tasksAvailable: true });
  assert.deepEqual(
    merged.map(entry => (entry.kind === 'session' ? entry.session.session_id : `task:${entry.task.task_id}`)),
    ['task:42', 's_plain'],
  );
});

test('archived tasks are filtered out and their sessions come back', () => {
  const sessions = [{ session_id: 's_handed', last_user_message_at: 2_000, handover_task_id: 42 }];
  const tasks = [{ task_id: 42, updated_at: 9_000, archived: true }];
  const merged = mergeSidebarEntries(sessions, tasks, { tasksAvailable: true });
  assert.deepEqual(
    merged.map(entry => (entry.kind === 'session' ? entry.session.session_id : `task:${entry.task.task_id}`)),
    ['s_handed'],
  );
});

test('tasks unavailable degrades to a plain session list', () => {
  const sessions = [{ session_id: 's_handed', last_user_message_at: 2_000, handover_task_id: 42 }];
  const tasks = [{ task_id: 42, updated_at: 9_000 }];
  const merged = mergeSidebarEntries(sessions, tasks, { tasksAvailable: false });
  assert.deepEqual(
    merged.map(entry => (entry.kind === 'session' ? entry.session.session_id : `task:${entry.task.task_id}`)),
    ['s_handed'],
  );
});

test('empty inputs produce an empty list', () => {
  assert.deepEqual(mergeSidebarEntries([], [], { tasksAvailable: true }), []);
  assert.deepEqual(mergeSidebarEntries([], [], { tasksAvailable: false }), []);
});

test('sessions without any timestamps sort after dated tasks, not before', () => {
  // 无时间戳的会话回退 Date.now()（最新），与 sidebarModel 行为一致；
  // 无时间戳的任务回退 0（沉底）。
  const sessions = [{ session_id: 's_no_time' }];
  const tasks = [{ task_id: 1, updated_at: 100 }];
  const merged = mergeSidebarEntries(sessions, tasks, { tasksAvailable: true });
  assert.equal(merged[0].kind, 'session');
  assert.equal(merged[1].kind, 'task');
});
