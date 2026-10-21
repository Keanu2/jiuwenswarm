/**
 * Pure state logic for the 跨设备任务 (cross-device task) panel.
 *
 * Mirrors ``projectsPanelState``: kept free of React and of the WebSocket
 * client so the merge rules — where the bugs live — can be tested directly.
 *
 * The relay is the single authority for task state; this module only owns
 * normalization, ordering, dedupe and the presentation gating the panel needs:
 *
 * - `task.list` items (tasks with their lease state), and
 * - `task.message` frames from a relay subscription — a `backlog` page on
 *   (re)connect or a single new log entry.
 *
 * Message payloads come in two shapes and both must read as text:
 * a live turn carries ``{"text"}``, while a session imported by
 * ``task.handover`` carries ``{"role","text"}`` (the role is preserved so the
 * panel can attribute imported turns to a side).
 */

export interface TaskItem {
  task_id: number;
  title: string;
  state: string;
  owner_dataset?: string;
  lease_holder?: string;
  lease_fence?: number | null;
  lease_expires_at?: number | null;
  archived?: boolean;
  created_at?: number;
  updated_at?: number;
}

export interface TaskMessage {
  seq: number;
  sender_id?: string;
  kind?: string;
  payload?: unknown;
  reply_to?: number | null;
  created_at?: number;
  deleted_at?: number | null;
}

export type TaskState =
  | 'pending'
  | 'working'
  | 'input_required'
  | 'completed'
  | 'failed'
  | 'canceled';

const TASK_STATES: readonly TaskState[] = [
  'pending',
  'working',
  'input_required',
  'completed',
  'failed',
  'canceled',
];

/** Parse a task_id (or any wire seq) that may arrive as a string or a number. */
export function toSeq(value: unknown): number {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  const parsed = Number.parseInt(String(value ?? ''), 10);
  return Number.isFinite(parsed) ? parsed : 0;
}

/**
 * Normalize the relay's task wire shape, dropping anything without a usable
 * `task_id` so a malformed row cannot poison the list selection.
 */
export function toTaskItems(raw: unknown): TaskItem[] {
  if (!Array.isArray(raw)) return [];
  const out: TaskItem[] = [];
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue;
    const rec = item as Record<string, unknown>;
    const taskId = toSeq(rec.task_id);
    if (taskId <= 0) continue;
    out.push({
      task_id: taskId,
      title: typeof rec.title === 'string' ? rec.title : '',
      state: taskStateKey(rec.state),
      owner_dataset: typeof rec.owner_dataset === 'string' ? rec.owner_dataset : undefined,
      lease_holder: typeof rec.lease_holder === 'string' ? rec.lease_holder : undefined,
      lease_fence:
        typeof rec.lease_fence === 'number' && Number.isFinite(rec.lease_fence)
          ? rec.lease_fence
          : null,
      lease_expires_at:
        typeof rec.lease_expires_at === 'number' && Number.isFinite(rec.lease_expires_at)
          ? rec.lease_expires_at
          : null,
      archived: rec.archived === true,
      created_at: typeof rec.created_at === 'number' ? rec.created_at : undefined,
      updated_at: typeof rec.updated_at === 'number' ? rec.updated_at : undefined,
    });
  }
  return out;
}

/**
 * Validate a relay task state; unknown values fall back to `pending` rather
 * than being dropped, so a newer backend cannot blank the panel.
 */
export function taskStateKey(raw: unknown): TaskState {
  const state = typeof raw === 'string' ? raw.trim() : '';
  return (TASK_STATES as readonly string[]).includes(state) ? (state as TaskState) : 'pending';
}

/** Whether a state ends the task: no further turns may run on it. */
export function isTerminalState(state: string): boolean {
  return state === 'completed' || state === 'failed' || state === 'canceled';
}

/**
 * Render one task message payload as display text. Payloads are JSON.
 *
 * Handover-imported entries carry `{"role","text"}` — the role is metadata,
 * not prose, so only `text` is shown (the role decides the bubble's side).
 */
export function payloadText(message: TaskMessage): string {
  const payload = message.payload;
  if (typeof payload === 'string') return payload;
  if (payload && typeof payload === 'object') {
    const rec = payload as Record<string, unknown>;
    for (const key of ['text', 'content', 'message']) {
      const value = rec[key];
      if (typeof value === 'string' && value.trim()) return value;
    }
    try {
      return JSON.stringify(payload);
    } catch {
      return '';
    }
  }
  if (payload === null || payload === undefined) return '';
  return String(payload);
}

/**
 * The side a task message belongs on.
 *
 * `reply_to` is the discriminator the bridge uses to mark its own answers, so
 * a message *with* `reply_to` is the agent's answer and sits on the left; a
 * message without it is a user turn and sits on the right. Handover imports
 * carry `{"role"}` and are attributed by that instead — the panel's own sends
 * never set `reply_to` in v1 (see the backend's `should_trigger_turn`).
 */
export function isAssistantSide(message: TaskMessage): boolean {
  if (message.reply_to != null && message.reply_to > 0) return true;
  const payload = message.payload;
  if (payload && typeof payload === 'object') {
    const rec = payload as Record<string, unknown>;
    return rec.role === 'assistant';
  }
  return false;
}

function normalizeMessage(raw: unknown): TaskMessage | null {
  if (!raw || typeof raw !== 'object') return null;
  const rec = raw as Record<string, unknown>;
  const seq = toSeq(rec.seq);
  if (seq <= 0) return null;
  return {
    seq,
    sender_id: typeof rec.sender_id === 'string' ? rec.sender_id : undefined,
    kind: typeof rec.kind === 'string' ? rec.kind : undefined,
    payload: rec.payload,
    reply_to: typeof rec.reply_to === 'number' ? rec.reply_to : null,
    created_at: typeof rec.created_at === 'number' ? rec.created_at : undefined,
    deleted_at: typeof rec.deleted_at === 'number' ? rec.deleted_at : null,
  };
}

/**
 * Merge incoming messages into an ordered list.
 *
 * Dedupe is by `seq` because reconnect overlap re-sends entries the panel
 * already has (duplicates are harmless, gaps are not), so merging must be
 * idempotent. Later entries win so a deleted entry updates in place.
 */
export function mergeTaskMessages(
  existing: readonly TaskMessage[],
  incoming: readonly unknown[],
): TaskMessage[] {
  const bySeq = new Map<number, TaskMessage>();
  for (const message of existing) {
    bySeq.set(message.seq, message);
  }
  for (const raw of incoming) {
    const message = normalizeMessage(raw);
    if (message) bySeq.set(message.seq, message);
  }
  return Array.from(bySeq.values()).sort((a, b) => a.seq - b.seq);
}

export function toTaskMessages(raw: unknown): TaskMessage[] {
  return Array.isArray(raw) ? mergeTaskMessages([], raw) : [];
}

/**
 * Whether this device's bridge holds the task's lease.
 *
 * The panel cannot track the fencing token (it is deliberately kept
 * relay-authoritative), so "may type here" is decided by `held_task_ids` from
 * `task.local_status` — the bridge's live view of what it holds. A task held
 * by *another* device is read-only here until this device takes it over.
 */
export function canSend(taskId: number, heldTaskIds: readonly number[]): boolean {
  return heldTaskIds.some((id) => toSeq(id) === taskId && taskId > 0);
}

/**
 * Merge a `task.local_status` payload's `held_task_ids` into a number list.
 */
export function toHeldTaskIds(raw: unknown): number[] {
  if (!Array.isArray(raw)) return [];
  const out: number[] = [];
  for (const item of raw) {
    const id = toSeq(item);
    if (id > 0) out.push(id);
  }
  return out;
}

/**
 * Whether the task log's newest entry is the bridge's pending answer.
 *
 * The panel optimistically shows "answering…" after a send; the bridge's reply
 * arrives as a message *with* `reply_to`, which is what clears it. An error
 * from the send itself also clears it — the turn never ran.
 */
export function awaitingReplyCleared(messages: readonly TaskMessage[]): boolean {
  const last = messages[messages.length - 1];
  return Boolean(last && last.reply_to != null && last.reply_to > 0);
}

/** Highest seq in a list; the resume cursor for the next subscribe. */
export function latestSeq(messages: readonly TaskMessage[]): number {
  let best = 0;
  for (const message of messages) {
    if (message.seq > best) best = message.seq;
  }
  return best;
}
