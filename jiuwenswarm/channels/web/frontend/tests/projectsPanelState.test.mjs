import assert from 'node:assert/strict';
import test from 'node:test';

import {
  ASSIGNABLE_ROLES,
  applyProgressEvent,
  canRemoveMember,
  filterMentionCandidates,
  isMine,
  isSelfDirected,
  latestSeq,
  memberLabel,
  mergeMessages,
  messageText,
  parseSubscriptionState,
  progressStepText,
  resolveTextMentions,
  senderDisplay,
  stripMentionToken,
  toGroupMembers,
  toGroupMessages,
  toSeq,
  trailingMentionQuery,
  visibleMessages,
} from '../node_modules/.cache/projects-panel-state/features/projectsPanelState.js';

test('mergeMessages orders by seq', () => {
  const merged = mergeMessages([], [{ seq: 3 }, { seq: 1 }, { seq: 2 }]);
  assert.deepEqual(merged.map((m) => m.seq), [1, 2, 3]);
});

test('mergeMessages is idempotent, because reconnect overlaps re-send entries', () => {
  const first = mergeMessages([], [{ seq: 1 }, { seq: 2 }]);
  const second = mergeMessages(first, [{ seq: 2 }, { seq: 3 }]);
  assert.deepEqual(second.map((m) => m.seq), [1, 2, 3]);
  // Merging the same page again must not grow the list.
  const third = mergeMessages(second, [{ seq: 2 }, { seq: 3 }]);
  assert.equal(third.length, 3);
});

test('mergeMessages lets a later entry win, so edits and deletes land in place', () => {
  const merged = mergeMessages(
    [{ seq: 1, payload: 'old' }],
    [{ seq: 1, payload: 'new', deleted_at: 123 }],
  );
  assert.equal(merged.length, 1);
  assert.equal(merged[0].payload, 'new');
  assert.equal(merged[0].deleted_at, 123);
});

test('mergeMessages drops entries without a usable seq', () => {
  const merged = mergeMessages([], [{ seq: 0 }, { seq: 'nope' }, null, 'junk', { seq: 5 }]);
  assert.deepEqual(merged.map((m) => m.seq), [5]);
});

test('mergeMessages accepts a numeric string seq', () => {
  const merged = mergeMessages([], [{ seq: '7' }]);
  assert.deepEqual(merged.map((m) => m.seq), [7]);
});

test('latestSeq reports the resume cursor', () => {
  assert.equal(latestSeq([]), 0);
  assert.equal(latestSeq([{ seq: 4 }, { seq: 11 }, { seq: 2 }]), 11);
});

test('messageText reads the text field out of a JSON payload', () => {
  assert.equal(messageText({ seq: 1, payload: { text: 'hi' } }), 'hi');
  assert.equal(messageText({ seq: 1, payload: 'plain' }), 'plain');
});

test('messageText summarizes a membership event', () => {
  const text = messageText({
    seq: 1,
    kind: 'event',
    payload: { event: 'member_joined', principal_id: 'u_abc' },
  });
  assert.match(text, /member_joined/);
  assert.match(text, /u_abc/);
});

test('messageText never throws on an unstringifiable payload', () => {
  const cyclic = {};
  cyclic.self = cyclic;
  assert.equal(messageText({ seq: 1, payload: cyclic }), '');
  assert.equal(messageText({ seq: 1, payload: null }), '');
});

test('toSeq parses numbers and numeric strings, defaults to 0', () => {
  assert.equal(toSeq(5), 5);
  assert.equal(toSeq('12'), 12);
  assert.equal(toSeq(undefined), 0);
  assert.equal(toSeq('x'), 0);
});

test('toGroupMessages accepts a raw page and rejects non-arrays', () => {
  assert.deepEqual(toGroupMessages(undefined), []);
  assert.deepEqual(toGroupMessages([{ seq: 2 }, { seq: 1 }]).map((m) => m.seq), [1, 2]);
});

test('toGroupMembers keeps well-formed members and sorts them', () => {
  const members = toGroupMembers([
    { principal_id: 'u_b', role: 'member' },
    { principal_id: '' },
    { principal_id: 'u_a', role: 'owner', status: 'active' },
    'junk',
  ]);
  assert.deepEqual(members.map((m) => m.principal_id), ['u_a', 'u_b']);
  assert.equal(members[0].role, 'owner');
  assert.equal(members[0].status, 'active');
});

test('toGroupMembers marks the agent row and keeps its display name', () => {
  const members = toGroupMembers([
    { principal_id: 'u_human', role: 'owner' },
    { principal_id: 'u_agent', role: 'member', is_agent: true, display_name: '研究助手' },
  ]);
  const human = members.find((m) => m.principal_id === 'u_human');
  const agent = members.find((m) => m.principal_id === 'u_agent');
  // A row is only the agent when the backend says so; absence is not "agent".
  assert.equal(human.is_agent, false);
  assert.equal(human.display_name, undefined);
  assert.equal(agent.is_agent, true);
  assert.equal(agent.display_name, '研究助手');
});

test('toGroupMembers ignores a non-boolean agent flag and a blank display name', () => {
  const [member] = toGroupMembers([
    { principal_id: 'u_agent', is_agent: 'yes', display_name: '   ' },
  ]);
  assert.equal(member.is_agent, false);
  assert.equal(member.display_name, undefined);
});

test('parseSubscriptionState accepts only reported states', () => {
  for (const state of ['idle', 'connecting', 'live', 'reconnecting', 'unavailable']) {
    assert.equal(parseSubscriptionState(state), state);
  }
  assert.equal(parseSubscriptionState('bogus'), null);
  assert.equal(parseSubscriptionState(undefined), null);
  assert.equal(parseSubscriptionState(7), null);
});

test('canRemoveMember protects the owner and bans', () => {
  assert.equal(canRemoveMember({ principal_id: 'u_a', role: 'owner' }), false);
  assert.equal(canRemoveMember({ principal_id: 'u_b', role: 'member' }), true);
  assert.equal(canRemoveMember({ principal_id: 'u_c', role: 'member', status: 'banned' }), false);
});

test('owner is not an assignable role, because ownership transfer is unsupported', () => {
  assert.equal(ASSIGNABLE_ROLES.includes('owner'), false);
});

test('isMine counts this instance\'s agent as the same side as its person', () => {
  const me = 'u_human';
  const agent = 'u_agent';
  assert.equal(isMine(me, me, agent), true);
  assert.equal(isMine(agent, me, agent), true);
  assert.equal(isMine('u_peer', me, agent), false);
});

test('isMine keeps its promise when the agent identity is unknown', () => {
  // A shared-credential setup has no agent of its own: only the person is mine.
  assert.equal(isMine('u_human', 'u_human', ''), true);
  assert.equal(isMine('u_agent', 'u_human', ''), false);
});

test('an unresolved sender is never "mine", so nothing is misattributed', () => {
  assert.equal(isMine('', 'u_human', 'u_agent'), false);
  assert.equal(isMine(undefined, 'u_human', 'u_agent'), false);
  assert.equal(isMine('u_human', '', 'u_agent'), false);
});

test('senderDisplay names a peer after the roster, not its credential', () => {
  const members = toGroupMembers([
    { principal_id: 'u_peer', role: 'owner', display_name: 'peer-owner' },
  ]);
  const sender = senderDisplay('u_peer', members, 'u_human', '我');
  assert.equal(sender.name, 'peer-owner');
  assert.equal(sender.isAgent, false);
});

test('senderDisplay labels an agent with its owner name and the agent flag', () => {
  // The relay names an agent after its owner, so both rows read "root"; the
  // flag is what tells the two apart.
  const members = toGroupMembers([
    { principal_id: 'u_human', role: 'owner', display_name: 'root' },
    { principal_id: 'u_agent', role: 'member', is_agent: true, display_name: 'root' },
    { principal_id: 'u_peer_agent', role: 'member', is_agent: true, display_name: 'peer-owner' },
  ]);
  const own = senderDisplay('u_agent', members, 'u_human', '我');
  assert.equal(own.name, 'root');
  assert.equal(own.isAgent, true);

  const peer = senderDisplay('u_peer_agent', members, 'u_human', '我');
  assert.equal(peer.name, 'peer-owner');
  assert.equal(peer.isAgent, true);
});

test('senderDisplay keeps "you" for the local person, so it differs from their agent', () => {
  const members = toGroupMembers([
    { principal_id: 'u_human', role: 'owner', display_name: 'root' },
    { principal_id: 'u_agent', role: 'member', is_agent: true, display_name: 'root' },
  ]);
  const mine = senderDisplay('u_human', members, 'u_human', '我');
  assert.equal(mine.name, '我');
  assert.equal(mine.isAgent, false);
});

test('senderDisplay falls back to the raw id when the roster lacks the sender', () => {
  const members = toGroupMembers([{ principal_id: 'u_human', display_name: 'root' }]);
  const sender = senderDisplay('u_gone', members, 'u_human', '我');
  assert.equal(sender.name, 'u_gone');
  assert.equal(sender.isAgent, false);
  // No sender at all resolves to nothing, for the caller's "unknown" wording.
  assert.deepEqual(senderDisplay('', members, 'u_human', '我'), {
    name: '',
    isAgent: false,
  });
});

test('resolveTextMentions turns hand-typed @name into principals', () => {
  const members = toGroupMembers([
    { principal_id: 'u_human', display_name: 'root' },
    { principal_id: 'u_agent', display_name: 'root', is_agent: true },
    { principal_id: 'u_peer', display_name: 'peer' },
  ]);
  // Mid-sentence mention works, because typing is the obvious way to address someone.
  assert.deepEqual(resolveTextMentions('@root 看下这个', members).principalIds, ['u_human']);
  assert.deepEqual(resolveTextMentions('@peer 你看', members).principalIds, ['u_peer']);
  // A trailing one works too, as does the principal id.
  assert.deepEqual(resolveTextMentions('帮忙看下 @peer', members).principalIds, ['u_peer']);
  assert.deepEqual(resolveTextMentions('@u_peer 在吗', members).principalIds, ['u_peer']);
});

test('resolveTextMentions dedupes and keeps first-appearance order', () => {
  const members = toGroupMembers([
    { principal_id: 'u_a', display_name: 'a' },
    { principal_id: 'u_b', display_name: 'b' },
  ]);
  assert.deepEqual(resolveTextMentions('@b @a @b', members).principalIds, ['u_b', 'u_a']);
});

test('resolveTextMentions reports names nobody answers to', () => {
  const members = toGroupMembers([{ principal_id: 'u_a', display_name: 'a' }]);
  const out = resolveTextMentions('@ghost 在吗', members);
  assert.deepEqual(out.principalIds, []);
  assert.deepEqual(out.unresolved, ['ghost']);
});

test('resolveTextMentions leaves an email address alone', () => {
  const members = toGroupMembers([{ principal_id: 'u_a', display_name: 'a' }]);
  // Not at a word boundary, so it is not an address to anyone.
  assert.deepEqual(resolveTextMentions('write to me@a.example', members).principalIds, []);
  assert.deepEqual(resolveTextMentions('write to me@a.example', members).unresolved, []);
});

test('resolveTextMentions is safe on empty and non-string drafts', () => {
  assert.deepEqual(resolveTextMentions('', []), { principalIds: [], unresolved: [] });
  assert.deepEqual(resolveTextMentions(undefined, []), { principalIds: [], unresolved: [] });
  assert.deepEqual(resolveTextMentions('@ 空格后无内容', []).unresolved, []);
});

test('memberLabel prefers the name over the credential', () => {
  assert.equal(memberLabel({ principal_id: 'u_a', display_name: 'root' }), 'root');
  assert.equal(memberLabel({ principal_id: 'u_a' }), 'u_a');
});

// ── self-directed instructions (a person @-ing their own agent) ──────────────

function _roster() {
  return toGroupMembers([
    { principal_id: 'u_me', role: 'owner', display_name: 'me' },
    { principal_id: 'u_my_agent', role: 'member', is_agent: true, invited_by: 'u_me' },
    { principal_id: 'u_peer', role: 'member', display_name: 'peer' },
    { principal_id: 'u_peer_agent', role: 'member', is_agent: true, invited_by: 'u_peer' },
  ]);
}

test('isSelfDirected recognises an instruction to my own agent', () => {
  const members = _roster();
  assert.equal(isSelfDirected({ seq: 1, sender_id: 'u_me', mentions: ['u_my_agent'] }, members), true);
  // Mentioning yourself alone carries no task for anyone, but is still private.
  assert.equal(isSelfDirected({ seq: 1, sender_id: 'u_me', mentions: ['u_me'] }, members), true);
});

test('isSelfDirected leaves group traffic alone', () => {
  const members = _roster();
  // Someone else's agent: their business, visible to the room.
  assert.equal(isSelfDirected({ seq: 1, sender_id: 'u_me', mentions: ['u_peer_agent'] }, members), false);
  // Mixed: addressed to my agent *and* a peer, so it is not mine alone.
  assert.equal(
    isSelfDirected({ seq: 1, sender_id: 'u_me', mentions: ['u_my_agent', 'u_peer'] }, members),
    false,
  );
  // No mention at all is ordinary chatter.
  assert.equal(isSelfDirected({ seq: 1, sender_id: 'u_me', mentions: [] }, members), false);
  assert.equal(isSelfDirected({ seq: 1, sender_id: 'u_me' }, members), false);
  // An agent row with no recorded inviter cannot be attributed to anyone.
  const orphan = toGroupMembers([{ principal_id: 'u_a1', is_agent: true }]);
  assert.equal(isSelfDirected({ seq: 1, sender_id: 'u_me', mentions: ['u_a1'] }, orphan), false);
});

test('visibleMessages keeps my own instruction and hides it from the room', () => {
  const members = _roster();
  const messages = [
    { seq: 1, sender_id: 'u_me', mentions: [], payload: { text: 'hi all' } },
    { seq: 2, sender_id: 'u_me', mentions: ['u_my_agent'], payload: { text: 'do this' } },
    { seq: 3, sender_id: 'u_peer', mentions: [], payload: { text: 'ok' } },
  ];
  // The sender keeps their own message, so it does not look lost.
  const mine = visibleMessages(messages, members, 'u_me');
  assert.deepEqual(mine.map((m) => m.seq), [1, 2, 3]);
  // Everyone else sees the room, not the instruction.
  const peer = visibleMessages(messages, members, 'u_peer');
  assert.deepEqual(peer.map((m) => m.seq), [1, 3]);
});

test('visibleMessages shows a peer\'s own instruction only to that peer', () => {
  const members = _roster();
  const messages = [
    { seq: 1, sender_id: 'u_peer', mentions: ['u_peer_agent'], payload: { text: 'their task' } },
  ];
  assert.deepEqual(visibleMessages(messages, members, 'u_me'), []);
  assert.deepEqual(
    visibleMessages(messages, members, 'u_peer').map((m) => m.seq),
    [1],
  );
});

test('visibleMessages shows everything until the roster is known', () => {
  // Hiding a real conversation is worse than briefly showing a private one.
  const messages = [{ seq: 1, sender_id: 'u_me', mentions: ['u_my_agent'] }];
  assert.deepEqual(visibleMessages(messages, [], 'u_me').map((m) => m.seq), [1]);
  assert.deepEqual(visibleMessages(messages, _roster(), '').map((m) => m.seq), []);
});

test('toGroupMembers keeps the inviter, which is how an agent is attributed', () => {
  const [member] = toGroupMembers([
    { principal_id: 'u_a', is_agent: true, invited_by: 'u_owner' },
  ]);
  assert.equal(member.invited_by, 'u_owner');
  const [blank] = toGroupMembers([{ principal_id: 'u_b', invited_by: '   ' }]);
  assert.equal(blank.invited_by, undefined);
});

test('trailingMentionQuery finds an @ query only at the end of the draft', () => {
  assert.equal(trailingMentionQuery('@'), '');
  assert.equal(trailingMentionQuery('hi @ro'), 'ro');
  assert.equal(trailingMentionQuery('@root'), 'root');
  // An `@` mid-sentence is not a picker trigger.
  assert.equal(trailingMentionQuery('mail me at a@b'), null);
  assert.equal(trailingMentionQuery('hi @ro done'), null);
  assert.equal(trailingMentionQuery('nothing here'), null);
});

test('stripMentionToken removes the resolved query and keeps the word break', () => {
  assert.equal(stripMentionToken('hi @ro'), 'hi ');
  assert.equal(stripMentionToken('@ro'), '');
  assert.equal(stripMentionToken('hi @ro done'), 'hi @ro done');
});

test('filterMentionCandidates matches the label, then the id, and lists all when empty', () => {
  const members = toGroupMembers([
    { principal_id: 'u_human', display_name: 'root' },
    { principal_id: 'u_agent', display_name: 'root', is_agent: true },
    { principal_id: 'u_other', display_name: 'peer' },
  ]);
  assert.equal(filterMentionCandidates(members, '').length, 3);
  // Both of root's identities match; that is the point of naming agents after
  // owners. Order follows the roster, which is sorted by principal id.
  assert.deepEqual(
    filterMentionCandidates(members, 'root').map((m) => m.principal_id),
    ['u_agent', 'u_human'],
  );
  assert.deepEqual(
    filterMentionCandidates(members, 'peer').map((m) => m.principal_id),
    ['u_other'],
  );
  // A row with no name is still reachable by its credential.
  const unnamed = toGroupMembers([{ principal_id: 'u_zzz' }]);
  assert.equal(filterMentionCandidates(unnamed, 'u_z').length, 1);
  assert.equal(filterMentionCandidates(members, 'nobody').length, 0);
});

// ── agent turn progress ──────────────────────────────────────────────────────

/** A header shared by every frame of one turn, like the bridge emits. */
function _progressFrame(step, extra = {}) {
  return {
    event_type: 'team.group.progress',
    group_id: 3,
    request_id: 'groupchat-3-9',
    trigger_seq: 9,
    sender_id: 'u_agent',
    state: 'running',
    started_at: 1000.5,
    step,
    ...extra,
  };
}

const _STEP = (seq, phase, fields = {}) => ({ seq, phase, ts: 1000 + seq, ...fields });

test('applyProgressEvent builds a card from incremental frames', () => {
  let state = applyProgressEvent(null, _progressFrame(_STEP(1, 'started')));
  assert.equal(state.requestId, 'groupchat-3-9');
  assert.equal(state.groupId, 3);
  assert.equal(state.triggerSeq, 9);
  assert.equal(state.senderId, 'u_agent');
  assert.equal(state.startedAt, 1000.5);
  assert.deepEqual(state.steps.map((s) => s.phase), ['started']);

  state = applyProgressEvent(state, _progressFrame(_STEP(2, 'tool_call', { tool: 'web_search', detail: 'q' })));
  state = applyProgressEvent(state, _progressFrame(_STEP(3, 'tool_result', { tool: 'web_search', status: 'completed' })));
  state = applyProgressEvent(state, _progressFrame(_STEP(4, 'answering')));
  assert.deepEqual(state.steps.map((s) => s.phase), [
    'started',
    'tool_call',
    'tool_result',
    'answering',
  ]);
  assert.equal(state.steps[1].tool, 'web_search');
});

test('applyProgressEvent feeds reasoning into the text block, not the timeline', () => {
  let state = applyProgressEvent(null, _progressFrame(_STEP(1, 'started')));
  state = applyProgressEvent(state, _progressFrame(_STEP(2, 'reasoning', { content: 'think ' })));
  state = applyProgressEvent(state, _progressFrame(_STEP(3, 'reasoning', { content: 'more' })));
  assert.equal(state.reasoning, 'think more');
  assert.deepEqual(state.steps.map((s) => s.phase), ['started']);
  // A reasoning frame that arrives before anything else still makes a card.
  const seeded = applyProgressEvent(null, _progressFrame(_STEP(1, 'reasoning', { content: 'hm' })));
  assert.equal(seeded.reasoning, 'hm');
  assert.deepEqual(seeded.steps, []);
});

test('applyProgressEvent caps reasoning and the step list like the backend does', () => {
  let state = applyProgressEvent(null, _progressFrame(_STEP(1, 'started')));
  const chunk = 'x'.repeat(1000);
  for (let i = 0; i < 20; i += 1) {
    state = applyProgressEvent(state, _progressFrame(_STEP(i + 2, 'reasoning', { content: chunk })));
  }
  assert.equal(state.reasoning.length, 16 * 1024);
  assert.ok(state.reasoning.startsWith('x'));

  for (let i = 0; i < 150; i += 1) {
    state = applyProgressEvent(state, _progressFrame(_STEP(i + 100, 'busy', { detail: 'seq' })));
  }
  assert.equal(state.steps.length, 100);
});

test('applyProgressEvent collapses the card on a terminal for the shown turn', () => {
  let state = applyProgressEvent(null, _progressFrame(_STEP(1, 'started')));
  state = applyProgressEvent(state, _progressFrame(_STEP(2, 'answering')));
  for (const phase of ['done', 'timeout', 'error']) {
    const next = applyProgressEvent(state, _progressFrame(_STEP(3, phase)));
    assert.equal(next, null, phase);
  }
  // A terminal with no card at all stays nothing.
  assert.equal(applyProgressEvent(null, _progressFrame(_STEP(1, 'done'))), null);
});

test('applyProgressEvent ignores a stale terminal but adopts a newer turn', () => {
  // Card shows turn 9; a terminal for an older turn 8 must not hide it.
  let state = applyProgressEvent(null, _progressFrame(_STEP(1, 'started')));
  const oldTerminal = _progressFrame(_STEP(9, 'done'), { request_id: 'groupchat-3-8', trigger_seq: 8 });
  assert.equal(applyProgressEvent(state, oldTerminal), state);

  // A frame for a newer turn 10 replaces the card: the only way it arrives is
  // a missed terminal for turn 9, and clinging to the dead turn would freeze
  // the card forever.
  const nextTurn = _progressFrame(_STEP(1, 'started'), { request_id: 'groupchat-3-10', trigger_seq: 10 });
  state = applyProgressEvent(state, nextTurn);
  assert.equal(state.requestId, 'groupchat-3-10');
  assert.deepEqual(state.steps.map((s) => s.phase), ['started']);
});

test('applyProgressEvent replaces wholesale on a snapshot and drops finished turns', () => {
  // A running snapshot replaces whatever was there, reasoning included.
  const snapshot = _progressFrame(null, {
    step: undefined,
    state: 'running',
    steps: [_STEP(1, 'started'), _STEP(2, 'tool_call', { tool: 't' })],
    reasoning: 'carried over',
  });
  let state = applyProgressEvent({ requestId: 'stale', steps: [], reasoning: 'old' }, snapshot);
  assert.equal(state.requestId, 'groupchat-3-9');
  assert.deepEqual(state.steps.map((s) => s.phase), ['started', 'tool_call']);
  assert.equal(state.reasoning, 'carried over');

  // A finished snapshot means no card — the panel re-seeds after a refresh, by
  // which time the turn may already be over.
  assert.equal(applyProgressEvent(state, { ...snapshot, state: 'done' }), null);
  assert.equal(applyProgressEvent(null, { ...snapshot, state: 'timeout' }), null);
});

test('applyProgressEvent ignores frames without a usable header or step', () => {
  assert.equal(applyProgressEvent(null, null), null);
  assert.equal(applyProgressEvent(null, {}), null);
  assert.equal(applyProgressEvent(null, { group_id: 3, request_id: '' }), null);
  const state = applyProgressEvent(null, _progressFrame(_STEP(1, 'started')));
  // A frame with no step, or an unparseable one, leaves the card untouched.
  assert.equal(applyProgressEvent(state, _progressFrame(null)), state);
  assert.equal(applyProgressEvent(state, _progressFrame({ phase: 'answering' })), state);
});

test('progressStepText labels each timeline row', () => {
  const t = (key, options = {}) => {
    const labels = {
      'projects.progress.started': '已开始处理',
      'projects.progress.toolCall': `正在调用 ${options.tool}`,
      'projects.progress.toolDone': `${options.tool} 完成`,
      'projects.progress.toolFailed': `${options.tool} 失败`,
      'projects.progress.answering': '正在生成回答…',
      'projects.progress.busy': '处理中收到新的 @，已忽略',
    };
    return labels[key] ?? key;
  };
  assert.equal(progressStepText(_STEP(1, 'started'), t), '已开始处理');
  assert.equal(progressStepText(_STEP(2, 'tool_call', { tool: 'web_search' }), t), '正在调用 web_search');
  assert.equal(
    progressStepText(_STEP(3, 'tool_result', { tool: 'web_search', status: 'completed' }), t),
    'web_search 完成',
  );
  assert.equal(
    progressStepText(_STEP(4, 'tool_result', { tool: 'web_search', status: 'error' }), t),
    'web_search 失败',
  );
  assert.equal(progressStepText(_STEP(5, 'answering'), t), '正在生成回答…');
  assert.equal(progressStepText(_STEP(6, 'busy'), t), '处理中收到新的 @，已忽略');
  // Reasoning and terminal phases have no row of their own.
  assert.equal(progressStepText(_STEP(7, 'reasoning', { content: 'x' }), t), '');
  assert.equal(progressStepText(_STEP(8, 'done'), t), '');
});
