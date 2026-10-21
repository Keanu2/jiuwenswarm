/**
 * Pure state logic for the 项目 (projects) panel's group chat view.
 *
 * Kept free of React and of the WebSocket client so the merge rules — which is
 * where the bugs live — can be tested directly. The panel owns rendering; this
 * owns ordering, dedupe and cursor bookkeeping.
 *
 * Two incoming shapes matter:
 *
 * - `team.group.message` frames from a relay subscription, either a `backlog`
 *   (catch-up page on (re)connect) or a single new entry.
 * - `team.group.state` frames reporting subscription lifecycle, so the panel
 *   can tell "quiet group" from "connection broken".
 */

export interface GroupMessage {
  seq: number;
  sender_id?: string;
  kind?: string;
  payload?: unknown;
  mentions?: string[];
  reply_to?: number | null;
  created_at?: number;
  deleted_at?: number | null;
}

export interface GroupMember {
  principal_id: string;
  role?: string;
  status?: string;
  muted?: boolean;
  joined_at?: number | null;
  /**
   * Whether this member is the local instance's agent rather than a person.
   *
   * The relay only knows principal ids, so the backend resolves this — it is
   * the process holding the agent's credential that can tell them apart.
   * Absent when the agent's identity could not be resolved.
   */
  is_agent?: boolean;
  /** The agent's configured display name; only set on the agent's own row. */
  display_name?: string;
  /**
   * The principal that invited this member, when the relay reports one.
   *
   * For an agent this is its owner — agents join on a targeted invitation from
   * the principal they belong to — which is what lets a message addressed only
   * to my own agent be recognised as an instruction to myself.
   */
  invited_by?: string;
}

export type GroupSubscriptionState = 'idle' | 'connecting' | 'live' | 'reconnecting' | 'unavailable' | 'deleted';

/** Render one message payload as display text. Payloads are JSON, not strings. */
export function messageText(message: GroupMessage): string {
  const payload = message.payload;
  if (typeof payload === 'string') return payload;
  if (payload && typeof payload === 'object') {
    const rec = payload as Record<string, unknown>;
    for (const key of ['text', 'content', 'message']) {
      const value = rec[key];
      if (typeof value === 'string' && value.trim()) return value;
    }
    // Membership events carry a structured summary rather than a text field.
    if (typeof rec.event === 'string') {
      const who = typeof rec.principal_id === 'string' ? rec.principal_id : '';
      return who ? `${rec.event} · ${who}` : String(rec.event);
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

/** Parse a seq that may arrive as a string or a number. */
export function toSeq(value: unknown): number {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  const parsed = Number.parseInt(String(value ?? ''), 10);
  return Number.isFinite(parsed) ? parsed : 0;
}

function normalizeMessage(raw: unknown): GroupMessage | null {
  if (!raw || typeof raw !== 'object') return null;
  const rec = raw as Record<string, unknown>;
  const seq = toSeq(rec.seq);
  if (seq <= 0) return null;
  return {
    seq,
    sender_id: typeof rec.sender_id === 'string' ? rec.sender_id : undefined,
    kind: typeof rec.kind === 'string' ? rec.kind : undefined,
    payload: rec.payload,
    mentions: Array.isArray(rec.mentions) ? rec.mentions.map(String) : undefined,
    reply_to: typeof rec.reply_to === 'number' ? rec.reply_to : null,
    created_at: typeof rec.created_at === 'number' ? rec.created_at : undefined,
    deleted_at: typeof rec.deleted_at === 'number' ? rec.deleted_at : null,
  };
}

/**
 * Merge incoming messages into an ordered list.
 *
 * Dedupe is by `seq` because the relay's reconnect overlap is expected to
 * re-send entries the client already has — that is by design (duplicates are
 * harmless, gaps are not), so merging must be idempotent. Later entries win so
 * a deleted/edited entry updates in place.
 */
export function mergeMessages(
  existing: readonly GroupMessage[],
  incoming: readonly unknown[],
): GroupMessage[] {
  const bySeq = new Map<number, GroupMessage>();
  for (const message of existing) {
    bySeq.set(message.seq, message);
  }
  for (const raw of incoming) {
    const message = normalizeMessage(raw);
    if (message) bySeq.set(message.seq, message);
  }
  return Array.from(bySeq.values()).sort((a, b) => a.seq - b.seq);
}

/** Highest seq in a list; the resume cursor for the next subscribe. */
export function latestSeq(messages: readonly GroupMessage[]): number {
  let best = 0;
  for (const message of messages) {
    if (message.seq > best) best = message.seq;
  }
  return best;
}

export function toGroupMessages(raw: unknown): GroupMessage[] {
  return Array.isArray(raw) ? mergeMessages([], raw) : [];
}

export function toGroupMembers(raw: unknown): GroupMember[] {
  if (!Array.isArray(raw)) return [];
  const out: GroupMember[] = [];
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue;
    const rec = item as Record<string, unknown>;
    const principalId = typeof rec.principal_id === 'string' ? rec.principal_id.trim() : '';
    if (!principalId) continue;
    out.push({
      principal_id: principalId,
      role: typeof rec.role === 'string' ? rec.role : undefined,
      status: typeof rec.status === 'string' ? rec.status : undefined,
      muted: rec.muted === true,
      joined_at: typeof rec.joined_at === 'number' ? rec.joined_at : null,
      is_agent: rec.is_agent === true,
      display_name:
        typeof rec.display_name === 'string' && rec.display_name.trim()
          ? rec.display_name
          : undefined,
      invited_by:
        typeof rec.invited_by === 'string' && rec.invited_by.trim()
          ? rec.invited_by
          : undefined,
    });
  }
  return out.sort((a, b) => a.principal_id.localeCompare(b.principal_id));
}

/**
 * A `team.group.state` frame reveals whether the group is observable.
 *
 * Only the states the relay subscription can actually report are accepted;
 * anything else is ignored so a newer backend cannot silently blank the panel.
 */
export function parseSubscriptionState(raw: unknown): GroupSubscriptionState | null {
  const state = typeof raw === 'string' ? raw.trim() : '';
  switch (state) {
    case 'idle':
    case 'connecting':
    case 'live':
    case 'reconnecting':
    case 'unavailable':
    case 'deleted':
      return state;
    default:
      return null;
  }
}

/** Whether a member may be kicked; the owner is immutable on the relay. */
export function canRemoveMember(member: GroupMember): boolean {
  return member.role !== 'owner' && member.status !== 'banned';
}

/** Roles a member may be promoted to. `owner` transfer is not supported. */
export const ASSIGNABLE_ROLES = ['admin', 'member', 'readonly'] as const;

/**
 * Whether a message belongs on this side of the transcript.
 *
 * This instance speaks with two credentials — the person's and its agent's —
 * so a message is "mine" when it came from either. An agent is not a peer: it
 * belongs to the person it was invited by, and its replies in the group are
 * that person's side of the conversation.
 *
 * An unresolved identity (empty `senderId`, or no local identity yet) is
 * never "mine": leaving a message on the left is the safe failure, since
 * nothing is then misattributed to the reader.
 */
export function isMine(
  senderId: unknown,
  myPrincipalId: string,
  myAgentPrincipalId = '',
): boolean {
  const id = typeof senderId === 'string' ? senderId : '';
  if (!id) return false;
  if (myPrincipalId && id === myPrincipalId) return true;
  return Boolean(myAgentPrincipalId) && id === myAgentPrincipalId;
}

/** How one message's sender should be labelled in the transcript. */
export interface SenderDisplay {
  /** The name to render; empty when the sender cannot be resolved at all. */
  name: string;
  /** Whether the sender is an agent, so the caller can render the badge. */
  isAgent: boolean;
}

/**
 * Resolve a message's sender to something readable.
 *
 * The relay stamps messages with a principal id, which on its own is an opaque
 * credential string. The roster translates it: an agent is named after its
 * owner, so a message from `root`'s agent reads as `root` plus the agent badge.
 * Without a roster entry — a sender who has since left, or a roster that has
 * not loaded yet — the raw id is all there is, and the caller decides whether
 * to show it or its own "unknown member" wording.
 *
 * The local person's own messages use `youLabel` rather than their name: the
 * roster names an agent after its owner, so both rows would otherwise read
 * identically and the reader could not tell their own words from their agent's.
 * Which side a message belongs on is a separate question, answered by
 * ``isMine`` — an agent's messages are named from the roster like any other.
 */
export function senderDisplay(
  senderId: unknown,
  members: readonly GroupMember[],
  myPrincipalId: string,
  youLabel: string,
): SenderDisplay {
  const id = typeof senderId === 'string' ? senderId : '';
  if (!id) return { name: '', isAgent: false };
  if (myPrincipalId && id === myPrincipalId) return { name: youLabel, isAgent: false };
  const member = members.find((candidate) => candidate.principal_id === id);
  if (!member) return { name: id, isAgent: false };
  return { name: memberLabel(member), isAgent: member.is_agent === true };
}

/**
 * Whether a message is one person instructing *their own* agent.
 *
 * Written as "sender is the only one addressed": every mentioned principal is
 * either the sender or an agent that sender owns. A broadcast mention (`@all`,
 * or anyone else's agent) is not this, and stays a group message for everyone.
 *
 * The relay has no notion of a private message — an @-mention only decides
 * *who is woken*, and the entry itself lands in the shared log for every member.
 * So this is deliberately a presentation rule, not a security boundary: it keeps
 * a "run this for me" instruction out of the way of the rest of the room, and
 * nothing more.
 */
export function isSelfDirected(
  message: GroupMessage,
  members: readonly GroupMember[],
): boolean {
  const sender = typeof message.sender_id === 'string' ? message.sender_id : '';
  const mentions = message.mentions ?? [];
  if (!sender || mentions.length === 0) return false;
  return mentions.every((id) => {
    if (id === sender) return true;
    const member = members.find((candidate) => candidate.principal_id === id);
    return member?.is_agent === true && member.invited_by === sender;
  });
}

/**
 * The messages worth rendering for this viewer.
 *
 * A self-directed instruction is dropped for everyone except the pair it
 * concerns: the sender keeps seeing their own message (otherwise it looks lost),
 * their own agent never reads this list at all, and the rest of the group — who
 * it was never addressed to — does not have it in the way.
 *
 * Membership has to be known before this can decide anything, so an unloaded
 * roster yields the full transcript: showing a message that could have been
 * filtered is a milder failure than hiding a real conversation.
 */
export function visibleMessages(
  messages: readonly GroupMessage[],
  members: readonly GroupMember[],
  myPrincipalId: string,
): GroupMessage[] {
  if (members.length === 0) return [...messages];
  return messages.filter((message) => {
    if (!isSelfDirected(message, members)) return true;
    return Boolean(myPrincipalId) && message.sender_id === myPrincipalId;
  });
}

/** Display name for a member row: its name, falling back to the raw id. */
export function memberLabel(member: GroupMember): string {
  return member.display_name || member.principal_id;
}

/**
 * The `@` query being typed at the end of a draft, or `null` when there is none.
 *
 * Mentions are a structured field on the relay, not text syntax, so the typed
 * `@…` is only ever a *picker trigger*: it is stripped when a member is chosen
 * and never sent. Anchoring to the end of the draft is what keeps an `@` in the
 * middle of a sentence (an email address, say) from opening the picker.
 */
export function trailingMentionQuery(draft: string): string | null {
  const match = /(?:^|\s)@([^\s@]*)$/.exec(draft);
  return match ? match[1] : null;
}

/**
 * Drop the trailing `@query` a choice just resolved.
 *
 * The leading whitespace is preserved (a sentence keeps its word break), and a
 * draft with no trailing mention is returned untouched — the caller may be
 * resolving a mention chosen from the toolbar while the cursor sits elsewhere.
 */
export function stripMentionToken(draft: string): string {
  const match = /(^|\s)@([^\s@]*)$/.exec(draft);
  if (!match) return draft;
  return draft.slice(0, match.index) + match[1];
}

/**
 * Members matching a typed `@` query.
 *
 * An empty query lists everyone — the picker is also the "who is in this group"
 * view. Matching is on the displayed label (so `@root` finds `root` and its
 * agent alike), with the principal id as a fallback for rows whose name is not
 * useful for typing.
 */
export function filterMentionCandidates(
  members: readonly GroupMember[],
  query: string,
): GroupMember[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return [...members];
  return members.filter(
    (member) =>
      memberLabel(member).toLowerCase().includes(needle) ||
      member.principal_id.toLowerCase().includes(needle),
  );
}

/** The outcome of reading `@name` written out by hand in a draft. */
export interface TextMentions {
  /** Principals the text named, in first-appearance order, without repeats. */
  principalIds: string[];
  /** The `@…` tokens that named nobody, so the caller can say so. */
  unresolved: string[];
}

/**
 * Resolve hand-typed `@name` tokens into structured mentions.
 *
 * Typing is the primary way people address someone, and the picker only opens
 * for an `@` at the end of the draft — so `@root 看一下` written out by hand
 * would otherwise ship as inert text, silently addressing nobody. Resolving it
 * here is what makes the obvious thing work: the relay takes mentions as a list
 * of principals, never as text syntax, so the names have to be translated before
 * the send.
 *
 * Only an `@` at the start or after whitespace counts, which keeps an email
 * address in the middle of a sentence from being read as an address to someone.
 * Matching is on the displayed label and then the principal id.
 *
 * A label can be ambiguous: an agent is named after the principal that invited
 * it, so `root` and `root`'s agent both answer to "root". The person wins, since
 * that is who the name literally is — addressing the agent is done by picking
 * the badged row from the picker, or by writing its principal id.
 */
export function resolveTextMentions(
  draft: string,
  members: readonly GroupMember[],
): TextMentions {
  const text = typeof draft === 'string' ? draft : '';
  const principalIds: string[] = [];
  const unresolved: string[] = [];
  const pattern = /(?:^|\s)@([^\s@]+)/g;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(text)) !== null) {
    const token = match[1];
    const needle = token.toLowerCase();
    const matches = members.filter(
      (candidate) =>
        memberLabel(candidate).toLowerCase() === needle ||
        candidate.principal_id.toLowerCase() === needle,
    );
    const member = matches.find((candidate) => candidate.is_agent !== true) ?? matches[0];
    if (member) {
      if (!principalIds.includes(member.principal_id)) {
        principalIds.push(member.principal_id);
      }
    } else if (!unresolved.includes(token)) {
      unresolved.push(token);
    }
  }
  return { principalIds, unresolved };
}

// ── agent turn progress ──────────────────────────────────────────────────────

/**
 * Caps mirrored from the backend tracker (`progress.py`): both sides stay
 * bounded no matter how long a turn runs, and a replayed snapshot can never
 * inflate the card beyond what a live turn would have produced.
 */
export const PROGRESS_MAX_STEPS = 100;
export const PROGRESS_MAX_REASONING_CHARS = 16 * 1024;

export type ProgressPhase =
  | 'started'
  | 'tool_call'
  | 'tool_result'
  | 'reasoning'
  | 'answering'
  | 'busy'
  | 'done'
  | 'timeout'
  | 'error';

const TERMINAL_PHASES: ReadonlySet<string> = new Set(['done', 'timeout', 'error']);

export interface ProgressStep {
  seq: number;
  phase: ProgressPhase;
  tool?: string;
  detail?: string;
  status?: string;
  content?: string;
  ts?: number;
}

/**
 * The live "what is the agent doing" card's state for one turn.
 *
 * Absent (`null`) while no turn runs — a terminal event collapses the card
 * rather than flipping a flag, so there is no "finished" state to go stale.
 * `state` from the wire is dropped for the same reason: only `running` turns
 * are represented at all.
 */
export interface TurnProgressState {
  groupId: number;
  requestId: string;
  triggerSeq: number;
  senderId: string;
  startedAt: number;
  steps: ProgressStep[];
  reasoning: string;
}

function normalizeStep(raw: unknown): ProgressStep | null {
  if (!raw || typeof raw !== 'object') return null;
  const rec = raw as Record<string, unknown>;
  const phase = typeof rec.phase === 'string' ? rec.phase : '';
  if (!phase) return null;
  const seq = toSeq(rec.seq);
  // Like a group message without a seq, a step without one is malformed: the
  // backend numbers every step from 1, so anything else is a bad frame.
  if (seq <= 0) return null;
  const step: ProgressStep = {
    seq,
    phase: phase as ProgressPhase,
  };
  if (typeof rec.tool === 'string' && rec.tool) step.tool = rec.tool;
  if (typeof rec.detail === 'string' && rec.detail) step.detail = rec.detail;
  if (typeof rec.status === 'string' && rec.status) step.status = rec.status;
  if (typeof rec.content === 'string' && rec.content) step.content = rec.content;
  if (typeof rec.ts === 'number' && Number.isFinite(rec.ts)) step.ts = rec.ts;
  return step;
}

interface ProgressHeader {
  groupId: number;
  requestId: string;
  triggerSeq: number;
  senderId: string;
  startedAt: number;
}

function progressHeader(rec: Record<string, unknown>): ProgressHeader | null {
  const requestId = typeof rec.request_id === 'string' ? rec.request_id : '';
  const groupId = toSeq(rec.group_id);
  if (!requestId || !groupId) return null;
  return {
    groupId,
    requestId,
    triggerSeq: toSeq(rec.trigger_seq),
    senderId: typeof rec.sender_id === 'string' ? rec.sender_id : '',
    startedAt: typeof rec.started_at === 'number' && Number.isFinite(rec.started_at)
      ? rec.started_at
      : 0,
  };
}

function capReasoning(text: string): string {
  return text.length > PROGRESS_MAX_REASONING_CHARS
    ? text.slice(-PROGRESS_MAX_REASONING_CHARS)
    : text;
}

function freshProgress(
  header: ProgressHeader,
  step: ProgressStep | null,
): TurnProgressState {
  // A reasoning step seeds the text block instead of the step list; every other
  // step (including none — a bare header) starts the list empty or with it.
  const isReasoning = step?.phase === 'reasoning';
  return {
    ...header,
    steps: step && !isReasoning ? [step] : [],
    reasoning: isReasoning && step?.content ? capReasoning(step.content) : '',
  };
}

/**
 * Fold one `team.group.progress` frame (or a `team.group.progress.get`
 * snapshot) into the card's state.
 *
 * Two shapes arrive on the same event type: a snapshot carries `steps` and is a
 * full replacement (a non-`running` snapshot collapses the card — the panel
 * re-seeds after a refresh, when the turn may already be over); an incremental
 * frame carries one `step`.
 *
 * Incremental rules, relying on the bridge's guarantee that a group runs at
 * most one turn at a time and its events arrive in order:
 *
 * - a terminal step collapses the card, but only for the turn it belongs to —
 *   a stale terminal for an older turn must not hide a newer one;
 * - a step for a *different* turn than the one shown replaces the card: the
 *   only way that happens is a missed terminal, and clinging to the dead turn
 *   would leave the card stuck forever;
 * - reasoning steps accumulate into the text block (tail-capped), never the
 *   step list — the card shows them as one growing paragraph, not N rows;
 * - everything else appends, with the step list capped like the backend caps
 *   it, so a replay cannot grow the card unbounded.
 */
export function applyProgressEvent(
  current: TurnProgressState | null,
  payload: unknown,
): TurnProgressState | null {
  if (!payload || typeof payload !== 'object') return current;
  const rec = payload as Record<string, unknown>;
  const header = progressHeader(rec);
  if (!header) return current;

  // Snapshot: full replacement. A finished or missing turn means no card.
  if (Array.isArray(rec.steps)) {
    if (rec.state !== 'running') return null;
    const steps = rec.steps
      .map(normalizeStep)
      .filter((step): step is ProgressStep => step !== null)
      .filter((step) => step.phase !== 'reasoning')
      .slice(-PROGRESS_MAX_STEPS);
    const reasoning =
      typeof rec.reasoning === 'string' ? capReasoning(rec.reasoning) : '';
    return { ...header, steps, reasoning };
  }

  const step = normalizeStep(rec.step);
  if (!step) return current;

  if (TERMINAL_PHASES.has(step.phase)) {
    return current && current.requestId === header.requestId ? null : current;
  }
  if (!current || current.requestId !== header.requestId) {
    return freshProgress(header, step);
  }
  if (step.phase === 'reasoning') {
    return {
      ...current,
      reasoning: capReasoning(current.reasoning + (step.content ?? '')),
    };
  }
  const steps = [...current.steps, step].slice(-PROGRESS_MAX_STEPS);
  return { ...current, steps };
}

/**
 * One step's label for the timeline. Reasoning and terminal phases have no row
 * of their own (reasoning feeds the text block; terminals collapse the card),
 * so they render nothing rather than a placeholder.
 */
export function progressStepText(
  step: ProgressStep,
  t: (key: string, options?: Record<string, unknown>) => string,
): string {
  switch (step.phase) {
    case 'started':
      return t('projects.progress.started');
    case 'tool_call':
      return t('projects.progress.toolCall', { tool: step.tool || '?' });
    case 'tool_result':
      return step.status === 'error'
        ? t('projects.progress.toolFailed', { tool: step.tool || '?' })
        : t('projects.progress.toolDone', { tool: step.tool || '?' });
    case 'answering':
      return t('projects.progress.answering');
    case 'busy':
      return t('projects.progress.busy');
    default:
      return '';
  }
}

