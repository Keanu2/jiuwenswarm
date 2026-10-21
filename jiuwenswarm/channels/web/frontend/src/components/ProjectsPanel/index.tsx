import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { webRequest, webClient } from '../../services/webClient';
import {
  ASSIGNABLE_ROLES,
  applyProgressEvent,
  canRemoveMember,
  filterMentionCandidates,
  isMine,
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
  type GroupMember,
  type GroupMessage,
  type GroupSubscriptionState,
  type TurnProgressState,
} from '../../features/projectsPanelState';

/**
 * A "project" in this page is a group on the relay's group chat API, reached
 * through the backend's `team.*` RPCs.
 *
 * Live messages arrive over the normal event channel as `team.group.message`
 * frames (broadcast to every web connection, filtered here by `group_id`); the
 * relay subscription behind them is started and stopped by
 * `team.message.subscribe` / `team.message.unsubscribe`. Creation and member
 * management are deliberately *not* gated on team/cluster mode.
 */

interface ProjectInfo {
  group_id: number;
  name: string;
  owner_dataset?: string;
  max_members?: number;
}

interface ProjectsListResponse {
  projects?: unknown[];
}

const MAX_NAME_CHARS = 120;
const MESSAGE_PAGE_LIMIT = 50;

function toProjectInfos(raw: unknown[]): ProjectInfo[] {
  const out: ProjectInfo[] = [];
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue;
    const rec = item as Record<string, unknown>;
    const groupId = typeof rec.group_id === 'number' ? rec.group_id : Number(rec.group_id);
    const name = typeof rec.name === 'string' ? rec.name : '';
    if (!name || !Number.isFinite(groupId)) continue;
    out.push({
      group_id: groupId,
      name,
      owner_dataset: typeof rec.owner_dataset === 'string' ? rec.owner_dataset : undefined,
      max_members: typeof rec.max_members === 'number' ? rec.max_members : undefined,
    });
  }
  return out;
}

/** Read the server's error code off a rejected webRequest, for i18n lookup. */
function errorCode(error: unknown): string {
  if (error && typeof error === 'object' && 'code' in error) {
    return String((error as { code?: unknown }).code ?? '');
  }
  return '';
}

function useServerErrorText(scope: string, fallbackKey: string) {
  const { t } = useTranslation();
  return useCallback(
    (error: unknown) =>
      t(`${scope}.errors.${errorCode(error)}`, { defaultValue: t(fallbackKey) }),
    [t, scope, fallbackKey],
  );
}

// ── Create / invite dialogs ──────────────────────────────────────────────────

interface ModalShellProps {
  testId: string;
  title: string;
  subtitle?: string;
  onClose: () => void;
  children: React.ReactNode;
  footer: React.ReactNode;
}

function ModalShell({ testId, title, subtitle, onClose, children, footer }: ModalShellProps) {
  const { t } = useTranslation();
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [onClose]);

  return (
    <div data-testid={`${testId}-overlay`} className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <button
        type="button"
        data-testid={`${testId}-backdrop`}
        className="absolute inset-0 bg-black/60"
        onClick={onClose}
        aria-label={t('common.close')}
      />
      <div
        data-testid={`${testId}-dialog`}
        className="relative w-full max-w-md rounded-[8px] border border-border bg-card shadow-2xl animate-rise"
      >
        <div className="px-5 py-4 border-b border-border">
          <h3 className="text-base font-semibold" data-testid={`${testId}-title`}>{title}</h3>
          {subtitle ? <p className="text-xs text-text-muted mt-1">{subtitle}</p> : null}
        </div>
        <div className="px-5 py-4 space-y-3">{children}</div>
        <div className="px-5 py-4 border-t border-border flex justify-end gap-2">{footer}</div>
      </div>
    </div>
  );
}

interface JoinProjectDialogProps {
  open: boolean;
  onClose: () => void;
  onJoined: () => void | Promise<void>;
}

/**
 * Redeem an invitation token someone sent you.
 *
 * The relay's tokens are single-use-per-redeemer and deliberately opaque (only
 * a hash is stored server-side), so the only thing the user can supply is the
 * plaintext string. On success the group appears in the list like any other.
 */
function JoinProjectDialog({ open, onClose, onJoined }: JoinProjectDialogProps) {
  const { t } = useTranslation();
  const errorText = useServerErrorText('projects', 'projects.errors.joinProject');
  const [token, setToken] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [joinedName, setJoinedName] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setToken('');
    setError(null);
    setSaving(false);
    setJoinedName(null);
  }, [open]);

  if (!open) return null;

  const canSubmit = !saving && Boolean(token.trim());

  const submit = async () => {
    if (!canSubmit) return;
    setSaving(true);
    setError(null);
    try {
      const response = await webRequest<{ group?: { name?: string } }>('team.invite.accept', {
        token: token.trim(),
      });
      setJoinedName(response?.group?.name ?? '');
      await onJoined();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <ModalShell
      testId="projects-join-modal"
      title={t('projects.joinTitle')}
      subtitle={t('projects.joinSubtitle')}
      onClose={onClose}
      footer={
        <>
          <button type="button" onClick={onClose} className="btn !px-3 !py-1.5" data-testid="projects-join-cancel-button">
            {joinedName !== null ? t('common.close') : t('common.cancel')}
          </button>
          {joinedName !== null ? null : (
            <button
              type="button"
              onClick={() => void submit()}
              disabled={!canSubmit}
              className="btn primary !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
              data-testid="projects-join-confirm-button"
            >
              {saving ? t('projects.joining') : t('common.confirm')}
            </button>
          )}
        </>
      }
    >
      {joinedName !== null ? (
        <div
          className="rounded-md border border-accent/40 bg-accent-subtle px-3 py-2 text-sm"
          data-testid="projects-join-success"
        >
          {joinedName
            ? t('projects.joinSuccess', { project: joinedName })
            : t('projects.joinSuccessNoName')}
        </div>
      ) : (
        <>
          <div>
            <label className="block text-xs text-text-muted mb-1" htmlFor="projects-join-token">
              {t('projects.joinTokenLabel')}
            </label>
            <textarea
              id="projects-join-token"
              value={token}
              autoFocus
              rows={3}
              onChange={(event) => setToken(event.target.value)}
              placeholder={t('projects.joinTokenPlaceholder')}
              className="w-full resize-none rounded-md border border-border bg-secondary/30 px-3 py-2 text-xs font-mono"
              data-testid="projects-join-token-input"
            />
            <p className="text-xs text-text-muted mt-1">{t('projects.joinTokenHint')}</p>
          </div>

          {error ? (
            <div className="rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger" data-testid="projects-join-modal-error">
              {error}
            </div>
          ) : null}
        </>
      )}
    </ModalShell>
  );
}

interface CreateProjectDialogProps {
  open: boolean;
  onClose: () => void;
  onCreated: () => void | Promise<void>;
}

function CreateProjectDialog({ open, onClose, onCreated }: CreateProjectDialogProps) {
  const { t } = useTranslation();
  const errorText = useServerErrorText('projects', 'projects.errors.createProject');
  const [name, setName] = useState('');
  const [maxMembers, setMaxMembers] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setName('');
    setMaxMembers('');
    setError(null);
    setSaving(false);
  }, [open]);

  if (!open) return null;

  const trimmed = name.trim();
  const tooLong = trimmed.length > MAX_NAME_CHARS;
  const canSubmit = Boolean(trimmed) && !tooLong && !saving;

  const submit = async () => {
    if (!canSubmit) return;
    setSaving(true);
    setError(null);
    try {
      const params: Record<string, unknown> = { name: trimmed };
      if (maxMembers.trim()) params.max_members = Number(maxMembers.trim());
      await webRequest('team.create', params);
      await onCreated();
      onClose();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <ModalShell
      testId="projects-create-modal"
      title={t('projects.createTitle')}
      subtitle={t('projects.createSubtitle')}
      onClose={onClose}
      footer={
        <>
          <button type="button" onClick={onClose} className="btn !px-3 !py-1.5" data-testid="projects-create-cancel-button">
            {t('common.cancel')}
          </button>
          <button
            type="button"
            onClick={() => void submit()}
            disabled={!canSubmit}
            className="btn primary !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
            data-testid="projects-create-confirm-button"
          >
            {saving ? t('projects.creating') : t('common.confirm')}
          </button>
        </>
      }
    >
      <div>
        <label className="block text-xs text-text-muted mb-1" htmlFor="projects-create-name">
          {t('projects.nameLabel')}
        </label>
        <input
          id="projects-create-name"
          type="text"
          value={name}
          autoFocus
          maxLength={MAX_NAME_CHARS + 1}
          onChange={(event) => setName(event.target.value)}
          placeholder={t('projects.namePlaceholder')}
          className="w-full rounded-md border border-border bg-secondary/30 px-3 py-2 text-sm"
          data-testid="projects-create-name-input"
        />
        {tooLong ? (
          <p className="text-xs text-danger mt-1" data-testid="projects-create-name-error">
            {t('projects.errors.nameTooLong', { max: MAX_NAME_CHARS })}
          </p>
        ) : null}
      </div>
      <div>
        <label className="block text-xs text-text-muted mb-1" htmlFor="projects-create-max-members">
          {t('projects.maxMembersLabel')}
        </label>
        <input
          id="projects-create-max-members"
          type="number"
          min={1}
          value={maxMembers}
          onChange={(event) => setMaxMembers(event.target.value)}
          placeholder={t('projects.maxMembersPlaceholder')}
          className="w-full rounded-md border border-border bg-secondary/30 px-3 py-2 text-sm"
          data-testid="projects-create-max-members-input"
        />
      </div>
      {error ? (
        <div className="rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger" data-testid="projects-create-modal-error">
          {error}
        </div>
      ) : null}
    </ModalShell>
  );
}

interface InviteMemberDialogProps {
  open: boolean;
  groupId: number;
  onClose: () => void;
}

function InviteMemberDialog({ open, groupId, onClose }: InviteMemberDialogProps) {
  const { t } = useTranslation();
  const errorText = useServerErrorText('projects', 'projects.errors.inviteMember');
  const [mode, setMode] = useState<'targeted' | 'link'>('targeted');
  const [principalId, setPrincipalId] = useState('');
  const [maxUses, setMaxUses] = useState('1');
  const [ttlHours, setTtlHours] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [linkToken, setLinkToken] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setMode('targeted');
    setPrincipalId('');
    setMaxUses('1');
    setTtlHours('');
    setError(null);
    setSaving(false);
    setLinkToken(null);
  }, [open]);

  if (!open) return null;

  const canSubmit = !saving && (mode === 'targeted' ? Boolean(principalId.trim()) : true);

  const submit = async () => {
    if (!canSubmit) return;
    setSaving(true);
    setError(null);
    try {
      const params: Record<string, unknown> = { group_id: groupId };
      if (mode === 'targeted') {
        params.invitee_id = principalId.trim();
      } else {
        params.max_uses = Number(maxUses.trim() || '1');
        if (ttlHours.trim()) params.ttl_seconds = Math.round(Number(ttlHours.trim()) * 3600);
      }
      const response = await webRequest<{ invite?: { token?: string; mode?: string } }>(
        'team.invite.create',
        params,
      );
      const token = response?.invite?.token ?? null;
      if (!token) {
        throw new Error('missing invite token');
      }
      // Always show the token: it is returned exactly once and only its hash is
      // stored, so closing here would lose it. A targeted invite must be
      // redeemed *by the invitee* — the relay refuses anyone else — so the
      // member is not added until they accept.
      setLinkToken(token);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <ModalShell
      testId="projects-invite-modal"
      title={t('projects.inviteTitle')}
      subtitle={t('projects.inviteSubtitle')}
      onClose={onClose}
      footer={
        <>
          <button type="button" onClick={onClose} className="btn !px-3 !py-1.5" data-testid="projects-invite-cancel-button">
            {linkToken ? t('common.close') : t('common.cancel')}
          </button>
          {linkToken ? null : (
            <button
              type="button"
              onClick={() => void submit()}
              disabled={!canSubmit}
              className="btn primary !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
              data-testid="projects-invite-confirm-button"
            >
              {saving ? t('projects.inviting') : t('common.confirm')}
            </button>
          )}
        </>
      }
    >
      {linkToken ? (
        <div className="space-y-2">
          <p className="text-xs text-text-muted">
            {mode === 'targeted' ? t('projects.inviteTargetedHint') : t('projects.inviteLinkHint')}
          </p>
          <code
            className="block break-all rounded-md border border-border bg-secondary/30 px-3 py-2 text-xs"
            data-testid="projects-invite-token"
          >
            {linkToken}
          </code>
        </div>
      ) : (
        <>
          <div className="flex gap-2" data-testid="projects-invite-mode">
            {(['targeted', 'link'] as const).map((value) => (
              <button
                key={value}
                type="button"
                onClick={() => setMode(value)}
                className={`btn !px-3 !py-1.5 text-xs ${mode === value ? 'primary' : ''}`}
                data-testid={`projects-invite-mode-${value}`}
              >
                {value === 'targeted' ? t('projects.inviteModeTargeted') : t('projects.inviteModeLink')}
              </button>
            ))}
          </div>

          {mode === 'targeted' ? (
            <div>
              <label className="block text-xs text-text-muted mb-1" htmlFor="projects-invite-principal">
                {t('projects.inviteeLabel')}
              </label>
              <input
                id="projects-invite-principal"
                type="text"
                value={principalId}
                autoFocus
                onChange={(event) => setPrincipalId(event.target.value)}
                placeholder={t('projects.inviteePlaceholder')}
                className="w-full rounded-md border border-border bg-secondary/30 px-3 py-2 text-sm"
                data-testid="projects-invite-principal-input"
              />
              <p className="text-xs text-text-muted mt-1">{t('projects.inviteeHint')}</p>
            </div>
          ) : (
            <div className="flex gap-3">
              <div className="flex-1">
                <label className="block text-xs text-text-muted mb-1" htmlFor="projects-invite-max-uses">
                  {t('projects.maxUsesLabel')}
                </label>
                <input
                  id="projects-invite-max-uses"
                  type="number"
                  min={1}
                  value={maxUses}
                  onChange={(event) => setMaxUses(event.target.value)}
                  className="w-full rounded-md border border-border bg-secondary/30 px-3 py-2 text-sm"
                  data-testid="projects-invite-max-uses-input"
                />
              </div>
              <div className="flex-1">
                <label className="block text-xs text-text-muted mb-1" htmlFor="projects-invite-ttl">
                  {t('projects.ttlLabel')}
                </label>
                <input
                  id="projects-invite-ttl"
                  type="number"
                  min={0}
                  value={ttlHours}
                  onChange={(event) => setTtlHours(event.target.value)}
                  placeholder={t('projects.ttlPlaceholder')}
                  className="w-full rounded-md border border-border bg-secondary/30 px-3 py-2 text-sm"
                  data-testid="projects-invite-ttl-input"
                />
              </div>
            </div>
          )}

          {error ? (
            <div className="rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger" data-testid="projects-invite-modal-error">
              {error}
            </div>
          ) : null}
        </>
      )}
    </ModalShell>
  );
}

// ── Group detail ─────────────────────────────────────────────────────────────

interface TurnProgressCardProps {
  progress: TurnProgressState;
  members: readonly GroupMember[];
  myPrincipalId: string;
}

/**
 * The live "what is the agent doing" card for a running @-mention turn.
 *
 * Rendered at the bottom of the transcript while a turn runs, and removed the
 * moment it ends — the answer (or its outcome notice) arrives as a normal group
 * message right after, so the card never shows a finished state that could go
 * stale. Progress frames are instance-local: they reach the browsers of this
 * deployment only, which is why this lives in the panel rather than in the
 * relay's group log.
 */
function TurnProgressCard({ progress, members, myPrincipalId }: TurnProgressCardProps) {
  const { t } = useTranslation();
  const [stepsOpen, setStepsOpen] = useState(false);
  const [reasoningOpen, setReasoningOpen] = useState(false);
  const [nowSec, setNowSec] = useState(() => Math.floor(Date.now() / 1000));

  // One tick per second keeps the elapsed label honest; the card is short-lived
  // (one turn), so the interval's cost is bounded by the turn itself.
  useEffect(() => {
    const timer = window.setInterval(() => setNowSec(Math.floor(Date.now() / 1000)), 1000);
    return () => window.clearInterval(timer);
  }, []);

  // The sender is the agent's principal; the roster names it after its owner,
  // exactly like a message row would.
  const agent = senderDisplay(progress.senderId, members, myPrincipalId, t('projects.you'));
  const currentStep = progress.steps[progress.steps.length - 1];
  // Server timestamps drive the elapsed label; a small clock skew just makes it
  // round to 0, never negative.
  const elapsed =
    progress.startedAt > 0 ? Math.max(0, nowSec - Math.floor(progress.startedAt)) : 0;

  return (
    <div
      className="rounded-lg border border-[var(--color-border-accent)] bg-accent-subtle px-3 py-2"
      data-testid="projects-detail-progress"
    >
      <div className="flex items-center gap-2 text-sm">
        <div className="w-4 h-4 shrink-0 rounded-full border-2 border-border border-t-accent animate-spin" />
        <span className="font-medium truncate">
          {t('projects.progress.working', { name: agent.name || progress.senderId })}
        </span>
        <span className="ml-auto shrink-0 text-[11px] text-text-muted" data-testid="projects-detail-progress-elapsed">
          {t('projects.progress.elapsed', { seconds: elapsed })}
        </span>
      </div>
      {currentStep ? (
        <p className="mt-1 truncate text-xs text-text-muted" data-testid="projects-detail-progress-current">
          {progressStepText(currentStep, t)}
        </p>
      ) : null}
      {progress.steps.length > 1 ? (
        <button
          type="button"
          onClick={() => setStepsOpen((open) => !open)}
          className="mt-1 text-[11px] text-text-link"
          data-testid="projects-detail-progress-steps-toggle"
        >
          {t('projects.progress.stepsCount', { count: progress.steps.length })}
        </button>
      ) : null}
      {stepsOpen ? (
        <ol className="mt-1 max-h-40 space-y-0.5 overflow-auto" data-testid="projects-detail-progress-steps">
          {progress.steps.map((step) => (
            <li key={step.seq} className="flex gap-2 text-[11px] text-text-muted">
              <span className="shrink-0 tabular-nums">{step.seq}.</span>
              <span className="min-w-0">
                {progressStepText(step, t)}
                {step.detail ? <span className="break-all"> · {step.detail}</span> : null}
              </span>
            </li>
          ))}
        </ol>
      ) : null}
      {progress.reasoning ? (
        <div className="mt-1">
          <button
            type="button"
            onClick={() => setReasoningOpen((open) => !open)}
            className="text-[11px] text-text-link"
            data-testid="projects-detail-progress-reasoning-toggle"
          >
            {t('projects.progress.reasoningToggle')}
          </button>
          {reasoningOpen ? (
            <pre
              className="mt-1 max-h-32 overflow-auto whitespace-pre-wrap break-words text-[11px] text-text-muted"
              data-testid="projects-detail-progress-reasoning"
            >
              {progress.reasoning}
            </pre>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

interface GroupDetailProps {
  project: ProjectInfo;
  myPrincipalId: string;
  /**
   * The principal this instance's agent speaks as, when it has one of its own.
   *
   * Empty in a shared-credential setup, where the person already is the agent.
   * Resolved by the backend because only it holds the agent's credential.
   */
  myAgentPrincipalId: string;
  onProjectGone: (groupId: number) => void;
}

function GroupDetail({ project, myPrincipalId, myAgentPrincipalId, onProjectGone }: GroupDetailProps) {
  const { t } = useTranslation();
  const membersErrorText = useServerErrorText('projects', 'projects.errors.loadMembers');
  const [tab, setTab] = useState<'messages' | 'members'>('messages');
  const [messages, setMessages] = useState<GroupMessage[]>([]);
  const [members, setMembers] = useState<GroupMember[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const [mentions, setMentions] = useState<string[]>([]);
  const [mentionPickerOpen, setMentionPickerOpen] = useState(false);
  /**
   * The `@…` being typed, or `null` when the picker was opened by the toolbar.
   *
   * Typing is the primary way in; the `@` button remains for discoverability and
   * opens the same list unfiltered. The typed token is a picker trigger only —
   * it is stripped on selection and never reaches the relay, which takes mentions
   * as a structured list of principal ids.
   */
  const [mentionQuery, setMentionQuery] = useState<string | null>(null);
  /**
   * A non-blocking note about the last send, e.g. a typed `@name` that matched
   * nobody. Not an error: the message went out, it just addressed fewer people
   * than the writer probably intended.
   */
  const [mentionNotice, setMentionNotice] = useState('');
  const [subState, setSubState] = useState<GroupSubscriptionState>('idle');
  const [inviteOpen, setInviteOpen] = useState(false);
  const [busyMember, setBusyMember] = useState('');
  /**
   * The running agent turn's live progress, or `null` when none is. Seeded from
   * `team.group.progress.get` on open (a refresh mid-turn must not lose the
   * card) and then driven by `team.group.progress` frames.
   */
  const [progress, setProgress] = useState<TurnProgressState | null>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const groupId = project.group_id;

  const loadMessages = useCallback(async () => {
    try {
      const response = await webRequest<{ messages?: unknown[] }>('team.message.list', {
        group_id: groupId,
        limit: MESSAGE_PAGE_LIMIT,
      });
      setMessages((current) => mergeMessages(current, toGroupMessages(response?.messages)));
      setError(null);
    } catch (err) {
      setError(membersErrorText(err));
    }
  }, [groupId, membersErrorText]);

  const loadMembers = useCallback(async () => {
    try {
      const response = await webRequest<{ members?: unknown[] }>('team.members.list', {
        group_id: groupId,
      });
      setMembers(toGroupMembers(response?.members));
      setError(null);
    } catch (err) {
      setError(membersErrorText(err));
    }
  }, [groupId, membersErrorText]);

  const refresh = useCallback(async () => {
    setLoading(true);
    await Promise.all([loadMessages(), loadMembers()]);
    setLoading(false);
  }, [loadMessages, loadMembers]);

  // Reset everything when the selection changes, then subscribe to the new group.
  useEffect(() => {
    let cancelled = false;
    setMessages([]);
    setMembers([]);
    setError(null);
    setProgress(null);
    void refresh();

    // A turn may already be running when the panel opens (or after a refresh),
    // so ask for its snapshot before the live frames take over. Best effort: a
    // failure here only means the card appears on the next step instead.
    void (async () => {
      try {
        const response = await webRequest<{ progress?: unknown }>(
          'team.group.progress.get',
          { group_id: groupId },
        );
        if (!cancelled) setProgress((current) => applyProgressEvent(current, response?.progress));
      } catch {
        /* best effort: live frames will drive the card */
      }
    })();

    void (async () => {
      try {
        await webRequest('team.message.subscribe', { group_id: groupId });
        if (!cancelled) setSubState('connecting');
      } catch (err) {
        if (!cancelled) setError(membersErrorText(err));
      }
    })();

    return () => {
      cancelled = true;
      void webRequest('team.message.unsubscribe', { group_id: groupId }).catch(() => {
        /* best effort: the panel is going away */
      });
    };
  }, [groupId, refresh, membersErrorText]);

  // Live frames for this group only — the backend broadcasts to every client.
  useEffect(() => {
    const offMessage = webClient.on<Record<string, unknown>>('team.group.message', ({ payload }) => {
      if (toSeq(payload?.group_id) !== groupId) return;
      const frame = String(payload?.frame ?? '');
      if (frame === 'backlog') {
        setMessages((current) => mergeMessages(current, toGroupMessages(payload.messages)));
        return;
      }
      if (frame === 'message' || frame === 'event') {
        // The hub normalizes the relay's flat frame into a `message` object.
        setMessages((current) => mergeMessages(current, [payload.message]));
      }
    });
    const offState = webClient.on<Record<string, unknown>>('team.group.state', ({ payload }) => {
      if (toSeq(payload?.group_id) !== groupId) return;
      const next = parseSubscriptionState(payload?.state);
      if (next) setSubState(next);
      if (next === 'deleted') {
        // The relay tells subscribers before it drops the rows, so this is the
        // only reliable signal that the project no longer exists.
        onProjectGone(groupId);
        return;
      }
      // A reconnect may have missed membership changes, so re-pull the roster.
      if (next === 'live') void loadMembers();
    });
    const offProgress = webClient.on<Record<string, unknown>>('team.group.progress', ({ payload }) => {
      if (toSeq(payload?.group_id) !== groupId) return;
      setProgress((current) => applyProgressEvent(current, payload));
    });
    return () => {
      offMessage();
      offState();
      offProgress();
    };
  }, [groupId, loadMembers, onProjectGone]);

  // Keep the newest message in view.
  useEffect(() => {
    const node = listRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [messages, progress]);

  const send = async () => {
    const text = draft.trim();
    if (!text || sending) return;
    setSending(true);
    setError(null);
    try {
      // Mentions are a structured field on the relay, not text syntax, so a
      // hand-typed `@name` has to be translated before it means anything.
      // Picked chips win; the text is only read for what the picker missed.
      const typed = resolveTextMentions(text, members);
      const structured = mentions.length ? mentions : typed.principalIds;
      const params: Record<string, unknown> = {
        group_id: groupId,
        text,
        // Idempotency key: a retry after a flaky connection returns the original
        // entry instead of posting a duplicate.
        client_msg_id: `web-${groupId}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      };
      // This is what wakes an agent, so it must be sent explicitly.
      if (structured.length) params.mentions = structured;
      await webRequest('team.message.send', params);
      setDraft('');
      setMentions([]);
      setMentionPickerOpen(false);
      // Say so rather than let a typo'd name fail silently.
      setMentionNotice(
        typed.unresolved.length
          ? t('projects.mentionUnresolved', { names: typed.unresolved.join('、') })
          : '',
      );
      await loadMessages();
    } catch (err) {
      setError(membersErrorText(err));
    } finally {
      setSending(false);
    }
  };

  const toggleMention = (principalId: string) => {
    setMentions((current) =>
      current.includes(principalId)
        ? current.filter((id) => id !== principalId)
        : [...current, principalId],
    );
    // The typed `@…` was only a trigger: drop it now that it has resolved, so
    // the draft does not carry text the relay would treat as ordinary content.
    if (mentionQuery !== null) {
      setDraft((current) => stripMentionToken(current));
      setMentionQuery(null);
      setMentionPickerOpen(false);
    }
  };

  /** Open the picker for a typed `@`, or close it when the token was undone. */
  const onDraftChange = (value: string) => {
    setDraft(value);
    // `null` means no trailing token: an `@` mid-sentence is not a trigger, and
    // a token that just got deleted must close a picker it had opened.
    setMentionQuery(trailingMentionQuery(value));
    setMentionPickerOpen(trailingMentionQuery(value) !== null);
  };

  const mentionCandidates = useMemo(
    () => filterMentionCandidates(members, mentionQuery ?? ''),
    [members, mentionQuery],
  );

  const removeMember = async (member: GroupMember) => {
    if (busyMember) return;
    const confirmed = window.confirm(t('projects.removeMemberConfirm', { member: member.principal_id }));
    if (!confirmed) return;
    setBusyMember(member.principal_id);
    try {
      await webRequest('team.member.remove', { group_id: groupId, principal_id: member.principal_id });
      await loadMembers();
    } catch (err) {
      setError(membersErrorText(err));
    } finally {
      setBusyMember('');
    }
  };

  const changeRole = async (member: GroupMember, role: string) => {
    if (busyMember) return;
    setBusyMember(member.principal_id);
    try {
      await webRequest('team.member.role', {
        group_id: groupId,
        principal_id: member.principal_id,
        role,
      });
      await loadMembers();
    } catch (err) {
      setError(membersErrorText(err));
    } finally {
      setBusyMember('');
    }
  };

  const [archiving, setArchiving] = useState(false);

  const archiveProject = async () => {
    if (archiving) return;
    const confirmed = window.confirm(t('projects.archiveConfirm', { project: project.name }));
    if (!confirmed) return;
    setArchiving(true);
    setError(null);
    try {
      await webRequest('team.project.archive', { group_id: groupId, archived: true });
      // An archived group leaves every listing, so it disappears from here too.
      onProjectGone(groupId);
    } catch (err) {
      setError(membersErrorText(err));
    } finally {
      setArchiving(false);
    }
  };

  const liveLabel = useMemo(() => {
    switch (subState) {
      case 'live':
        return t('projects.subStateLive');
      case 'connecting':
        return t('projects.subStateConnecting');
      case 'reconnecting':
        return t('projects.subStateReconnecting');
      case 'unavailable':
        return t('projects.subStateUnavailable');
      case 'deleted':
        return t('projects.subStateDeleted');
      default:
        return t('projects.subStateIdle');
    }
  }, [subState, t]);

  /**
   * What this viewer should see.
   *
   * An instruction addressed only to the sender's own agent is the sender's
   * business: it stays visible to them, and is kept out of everyone else's
   * transcript. The relay has no private messages, so this is presentation —
   * see ``isSelfDirected``.
   */
  const renderedMessages = useMemo(
    () => visibleMessages(messages, members, myPrincipalId),
    [messages, members, myPrincipalId],
  );

  return (
    <div className="flex flex-col h-full min-h-0" data-testid="projects-detail">
      <div className="flex items-center justify-between gap-3 px-1 pb-2">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold truncate" data-testid="projects-detail-name">{project.name}</h3>
          <p className="text-[11px] text-text-muted" data-testid="projects-detail-meta">
            {t('projects.groupId', { id: groupId })} · {liveLabel}
          </p>
        </div>
        <div className="flex gap-2 shrink-0">
          <button type="button" onClick={() => setInviteOpen(true)} className="btn !px-3 !py-1.5 text-xs" data-testid="projects-detail-invite-button">
            {t('projects.inviteMember')}
          </button>
          <button type="button" onClick={() => void refresh()} disabled={loading} className="btn !px-3 !py-1.5 text-xs disabled:opacity-50" data-testid="projects-detail-refresh-button">
            {loading ? t('common.refreshing') : t('common.refresh')}
          </button>
          <button
            type="button"
            onClick={() => void archiveProject()}
            disabled={archiving}
            className="btn !px-3 !py-1.5 text-xs disabled:opacity-50 disabled:cursor-not-allowed"
            data-testid="projects-detail-archive-button"
          >
            {archiving ? t('projects.archiving') : t('projects.archiveProject')}
          </button>
        </div>
      </div>

      <div className="flex gap-2 pb-2" data-testid="projects-detail-tabs">
        {(['messages', 'members'] as const).map((value) => (
          <button
            key={value}
            type="button"
            onClick={() => setTab(value)}
            className={`btn !px-3 !py-1 text-xs ${tab === value ? 'primary' : ''}`}
            data-testid={`projects-detail-tab-${value}`}
          >
            {value === 'messages'
              ? t('projects.tabMessages', { count: messages.length })
              : t('projects.tabMembers', { count: members.length })}
          </button>
        ))}
      </div>

      {error ? (
        <div className="mb-2 rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger" data-testid="projects-detail-error">
          {error}
        </div>
      ) : null}

      {tab === 'messages' ? (
        <>
          <div ref={listRef} className="flex-1 min-h-0 overflow-auto rounded-lg border border-border bg-secondary/10 p-3 space-y-2" data-testid="projects-detail-messages">
            {loading && renderedMessages.length === 0 ? (
              <div className="h-full flex items-center justify-center" data-testid="projects-detail-messages-state" data-variant="loading">
                <div className="w-6 h-6 rounded-full border-4 border-border border-t-accent animate-spin" />
              </div>
            ) : renderedMessages.length === 0 ? (
              <div className="h-full flex items-center justify-center text-sm text-text-muted" data-testid="projects-detail-messages-state" data-variant="empty">
                {t('projects.noMessages')}
              </div>
            ) : (
              renderedMessages.map((message) => {
                const isEvent = message.kind === 'event' || message.kind === 'system';
                // Membership transitions are not "someone talking", so they stay
                // centred rather than being attributed to a side.
                if (isEvent) {
                  return (
                    <div
                      key={message.seq}
                      className="text-center text-[11px] text-text-muted italic"
                      data-testid="projects-detail-message"
                      data-variant="event"
                    >
                      {messageText(message)}
                    </div>
                  );
                }
                // The agent belongs to the person it was invited by, so its
                // messages sit on that person's side. Without a resolved
                // identity every message falls to the left, which is the safe
                // default: nothing is misattributed to me.
                const isMineMessage = isMine(
                  message.sender_id,
                  myPrincipalId,
                  myAgentPrincipalId,
                );
                const sender = senderDisplay(
                  message.sender_id,
                  members,
                  myPrincipalId,
                  t('projects.you'),
                );
                return (
                  <div
                    key={message.seq}
                    className={`flex flex-col ${isMineMessage ? 'items-end' : 'items-start'}`}
                    data-testid="projects-detail-message"
                    data-variant={isMineMessage ? 'mine' : 'theirs'}
                  >
                    <div className="flex items-baseline gap-2 max-w-[80%]">
                      <span className="text-[11px] text-text-muted truncate" data-testid="projects-detail-message-sender">
                        {sender.name || t('projects.unknownSender')}
                        {sender.isAgent ? ` · ${t('projects.agentBadge')}` : ''}
                      </span>
                      <span className="text-[10px] text-text-muted">#{message.seq}</span>
                    </div>
                    <p
                      className={`max-w-[80%] whitespace-pre-wrap break-words rounded-lg px-3 py-2 text-sm ${
                        isMineMessage
                          ? 'bg-accent-subtle border border-[var(--color-border-accent)]'
                          : 'bg-secondary/40 border border-border'
                      }`}
                      data-testid="projects-detail-message-text"
                    >
                      {messageText(message)}
                    </p>
                    {/*
                      Who a message was addressed to is part of reading it: a
                      mention is what makes an agent act, so a row that carried
                      one must not look like ordinary chatter. The text itself
                      cannot show this — a picked mention is stripped from the
                      draft, so the bubble would otherwise hide it entirely.
                    */}
                    {message.mentions?.length ? (
                      <div
                        className="flex flex-wrap justify-end gap-1 mt-1 max-w-[80%]"
                        data-testid="projects-detail-message-mentions"
                      >
                        {message.mentions.map((id) => {
                          const mentioned = members.find((m) => m.principal_id === id);
                          return (
                            <span
                              key={id}
                              className="text-[10px] px-1.5 py-0.5 rounded-full border border-[var(--color-border-accent)] bg-accent-subtle text-text-muted"
                              data-testid="projects-detail-message-mention"
                              data-variant={id}
                              title={t('projects.mentionedBadge')}
                            >
                              @{mentioned ? memberLabel(mentioned) : id}
                              {mentioned?.is_agent ? ` · ${t('projects.agentBadge')}` : ''}
                            </span>
                          );
                        })}
                      </div>
                    ) : null}
                  </div>
                );
              })
            )}
            {/*
              The live card sits at the transcript's tail — where the reply will
              land — so a reader watching the room sees the turn's progress in
              the same place the answer will appear. See TurnProgressCard for
              why it renders nothing once the turn ends.
            */}
            {progress ? (
              <TurnProgressCard
                progress={progress}
                members={members}
                myPrincipalId={myPrincipalId}
              />
            ) : null}
          </div>
          <div className="flex flex-col gap-2 pt-2">
            {mentionPickerOpen ? (
              <div
                className="rounded-md border border-border bg-secondary/20 p-2 max-h-40 overflow-auto"
                data-testid="projects-detail-mention-picker"
              >
                <p className="text-[11px] text-text-muted px-1 pb-1">{t('projects.mentionHint')}</p>
                {mentionCandidates.length === 0 ? (
                  <p className="text-xs text-text-muted px-1 py-1">{t('projects.noMembers')}</p>
                ) : (
                  mentionCandidates.map((member) => {
                    const chosen = mentions.includes(member.principal_id);
                    return (
                      <button
                        key={member.principal_id}
                        type="button"
                        onClick={() => toggleMention(member.principal_id)}
                        className={`w-full text-left rounded px-2 py-1 text-xs ${
                          chosen ? 'bg-accent-subtle text-text' : 'hover:bg-secondary/50 text-text-muted'
                        }`}
                        data-testid="projects-detail-mention-option"
                        data-variant={member.principal_id}
                        data-selected={chosen ? 'true' : 'false'}
                      >
                        {chosen ? '✓ ' : ''}
                        {member.display_name || member.principal_id}
                        {member.is_agent ? ` · ${t('projects.agentBadge')}` : ''}
                        {member.role ? ` (${member.role})` : ''}
                      </button>
                    );
                  })
                )}
              </div>
            ) : null}

            {mentions.length ? (
              <div className="flex flex-wrap gap-1" data-testid="projects-detail-mention-chips">
                {mentions.map((id) => {
                  const member = members.find((m) => m.principal_id === id);
                  return (
                    <button
                      key={id}
                      type="button"
                      onClick={() => toggleMention(id)}
                      title={t('projects.removeMention')}
                      className="text-[11px] px-2 py-0.5 rounded-full border border-[var(--color-border-accent)] bg-accent-subtle"
                      data-testid="projects-detail-mention-chip"
                    >
                      @{member ? memberLabel(member) : id} ×
                    </button>
                  );
                })}
              </div>
            ) : null}

            {mentionNotice ? (
              <p className="text-[11px] text-warn" data-testid="projects-detail-mention-notice">
                {mentionNotice}
              </p>
            ) : null}

            <div className="flex gap-2">
              <textarea
                value={draft}
                rows={2}
                onChange={(event) => onDraftChange(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' && !event.shiftKey) {
                    event.preventDefault();
                    void send();
                    return;
                  }
                  // Escape dismisses the picker without touching the draft: the
                  // `@` stays as typed text, which is the way out of a picker
                  // opened by accident.
                  if (event.key === 'Escape') {
                    setMentionPickerOpen(false);
                    setMentionQuery(null);
                  }
                }}
                placeholder={t('projects.messagePlaceholder')}
                className="flex-1 resize-none rounded-md border border-border bg-secondary/30 px-3 py-2 text-sm"
                data-testid="projects-detail-composer"
              />
              <button type="button" onClick={() => void send()} disabled={!draft.trim() || sending} className="btn primary !px-3 !py-1.5 self-end disabled:opacity-50 disabled:cursor-not-allowed" data-testid="projects-detail-send-button">
                {sending ? t('projects.sending') : t('projects.send')}
              </button>
            </div>
          </div>
        </>
      ) : (
        <div className="flex-1 min-h-0 overflow-auto rounded-lg border border-border bg-secondary/10 p-2 space-y-1" data-testid="projects-detail-member-list">
          {loading && members.length === 0 ? (
            <div className="h-full flex items-center justify-center" data-testid="projects-detail-members-state" data-variant="loading">
              <div className="w-6 h-6 rounded-full border-4 border-border border-t-accent animate-spin" />
            </div>
          ) : members.length === 0 ? (
            <div className="h-full flex items-center justify-center text-sm text-text-muted" data-testid="projects-detail-members-state" data-variant="empty">
              {t('projects.noMembers')}
            </div>
          ) : (
            members.map((member) => (
              <div key={member.principal_id} className="flex items-center gap-3 rounded-lg px-3 py-2 text-sm hover:bg-secondary/40" data-testid="projects-detail-member" data-variant={member.principal_id}>
                <div className="min-w-0 flex-1">
                  <span className="block truncate font-medium" data-testid="projects-detail-member-name">
                    {member.display_name || member.principal_id}
                    {member.is_agent ? (
                      <span
                        className="ml-1.5 align-middle text-[10px] px-1.5 py-0.5 rounded-full border border-[var(--color-border-accent)] bg-accent-subtle text-text-muted"
                        data-testid="projects-detail-member-agent-badge"
                        title={member.principal_id}
                      >
                        {t('projects.agentBadge')}
                      </span>
                    ) : null}
                  </span>
                  <span className="block text-[11px] text-text-muted" data-testid="projects-detail-member-role">{member.role || 'member'}</span>
                </div>
                {member.role !== 'owner' ? (
                  <select
                    value={member.role || 'member'}
                    onChange={(event) => void changeRole(member, event.target.value)}
                    disabled={Boolean(busyMember)}
                    className="shrink-0 rounded-md border border-border bg-secondary/30 px-2 py-1 text-xs"
                    data-testid="projects-detail-member-role-select"
                  >
                    {ASSIGNABLE_ROLES.map((role) => (
                      <option key={role} value={role}>{role}</option>
                    ))}
                  </select>
                ) : null}
                {canRemoveMember(member) ? (
                  <button
                    type="button"
                    onClick={() => void removeMember(member)}
                    disabled={Boolean(busyMember)}
                    className="shrink-0 text-xs px-2 py-1 rounded-md border border-danger/30 text-danger hover:bg-danger-subtle disabled:opacity-50 disabled:cursor-not-allowed"
                    data-testid="projects-detail-member-remove-button"
                  >
                    {busyMember === member.principal_id ? t('projects.removing') : t('common.delete')}
                  </button>
                ) : null}
              </div>
            ))
          )}
        </div>
      )}

      <InviteMemberDialog
        open={inviteOpen}
        groupId={groupId}
        onClose={() => {
          setInviteOpen(false);
          // An accepted invite changes the roster, so refresh on close —
          // the panel has no other reliable signal that it happened.
          void loadMembers();
        }}
      />
    </div>
  );
}

// ── Panel ────────────────────────────────────────────────────────────────────

export function ProjectsPanel() {
  const { t } = useTranslation();
  const errorText = useServerErrorText('projects', 'projects.errors.loadProjects');
  const [projects, setProjects] = useState<ProjectInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [joinOpen, setJoinOpen] = useState(false);
  const [myPrincipalId, setMyPrincipalId] = useState('');
  const [myAgentPrincipalId, setMyAgentPrincipalId] = useState('');
  const [busyProjectId, setBusyProjectId] = useState<number | null>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);

  const loadProjects = useCallback(async () => {
    setLoading(true);
    try {
      const payload = await webRequest<ProjectsListResponse>('team.projects.list', {});
      const next = Array.isArray(payload?.projects) ? toProjectInfos(payload.projects) : [];
      setProjects(next);
      setError(null);
      setSelectedId((current) =>
        current !== null && next.some((project) => project.group_id === current)
          ? current
          : next[0]?.group_id ?? null,
      );
    } catch (err) {
      console.error('Failed to load projects:', err);
      setProjects([]);
      setSelectedId(null);
      setError(errorText(err));
    } finally {
      setLoading(false);
    }
  }, [errorText]);

  useEffect(() => {
    void loadProjects();
  }, [loadProjects]);

  /**
   * Learn which principals this instance speaks as.
   *
   * The relay stamps every message with its *sender's* id, so the only way to
   * tell "mine" from "theirs" is to ask who the configured credentials are.
   * Both come back together: the person's, and this instance's agent's — the
   * agent's messages belong on the same side as the person it belongs to.
   * Failure is non-fatal: without them every message renders on the left, which
   * is the safe default (nothing is misattributed to the local user).
   */
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const me = await webRequest<{ principal_id?: string; agent_principal_id?: string }>(
          'team.whoami',
          {},
        );
        if (cancelled) return;
        setMyPrincipalId(String(me?.principal_id ?? ''));
        setMyAgentPrincipalId(String(me?.agent_principal_id ?? ''));
      } catch (err) {
        console.warn('Failed to resolve the local groupchat identity:', err);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  /**
   * Drop a project from the list without a round trip.
   *
   * Used both for a local delete and for a `deleted` frame pushed by the relay
   * when someone else removes the group — in the latter case a refetch would
   * also work, but this keeps the panel responsive and avoids a flash of the
   * stale row.
   */
  const handleProjectGone = useCallback((groupId: number) => {
    setProjects((current) => {
      const next = current.filter((project) => project.group_id !== groupId);
      setSelectedId((selected) =>
        selected === groupId ? next[0]?.group_id ?? null : selected,
      );
      return next;
    });
  }, []);

  /**
   * Delete a project from the list row.
   *
   * Lives here rather than in GroupDetail because the entry point is the list
   * row, which must work whether or not that project is the selected one.
   */
  const deleteProject = async (groupId: number, projectName: string) => {
    if (busyProjectId !== null) return;
    // Deletion is irreversible on the relay (members, invites and messages all
    // go), and owner-only — so the confirmation carries the consequences.
    const confirmed = window.confirm(t('projects.deleteConfirm', { project: projectName }));
    if (!confirmed) return;
    setBusyProjectId(groupId);
    setError(null);
    try {
      await webRequest('team.project.delete', { group_id: groupId });
      handleProjectGone(groupId);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusyProjectId(null);
    }
  };

  const selected = projects.find((project) => project.group_id === selectedId) ?? null;

  return (
    <div className="flex-1 min-h-0" data-testid="projects-panel-root">
      <div className="card w-full h-full flex flex-col" data-testid="projects-panel-card">
        <div className="flex items-center justify-between gap-4 mb-4">
          <div>
            <h2 className="text-lg font-semibold" data-testid="projects-panel-title">{t('projects.title')}</h2>
            <p className="text-sm text-text-muted mt-1" data-testid="projects-panel-subtitle">{t('projects.subtitle')}</p>
          </div>
          <div className="flex items-center gap-2">
            <button type="button" onClick={() => setDialogOpen(true)} className="btn primary !px-3 !py-1.5" data-testid="projects-panel-create-button">
              {t('projects.createProject')}
            </button>
            <button type="button" onClick={() => setJoinOpen(true)} className="btn !px-3 !py-1.5" data-testid="projects-panel-join-button">
              {t('projects.joinProject')}
            </button>
            <button type="button" onClick={() => void loadProjects()} disabled={loading} className="btn !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed" data-testid="projects-panel-refresh-button">
              {loading ? t('common.refreshing') : t('common.refresh')}
            </button>
          </div>
        </div>

        {error ? (
          <div className="mb-4 rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger" data-testid="projects-panel-error">
            {error}
          </div>
        ) : null}

        <div className="flex-1 min-h-0 grid grid-cols-[minmax(200px,1fr)_minmax(0,3fr)] gap-4">
          <div className="rounded-xl border border-border bg-card/70 backdrop-blur-sm overflow-hidden shadow-sm flex flex-col min-h-0">
            <div className="px-4 py-3 bg-secondary/30 border-b border-border">
              <h3 className="text-sm font-medium text-text" data-testid="projects-panel-list-heading">{t('projects.listHeading')}</h3>
              <p className="text-xs text-text-muted mt-1" data-testid="projects-panel-list-count">
                {t('projects.count', { count: projects.length })}
              </p>
            </div>
            <div className="flex-1 overflow-auto p-2 space-y-1" data-testid="projects-panel-list">
              {loading ? (
                <div className="h-full flex items-center justify-center" data-testid="projects-panel-list-state" data-variant="loading">
                  <div className="w-7 h-7 rounded-full border-4 border-border border-t-accent animate-spin" />
                </div>
              ) : projects.length === 0 ? (
                <div className="h-full flex items-center justify-center text-sm text-text-muted" data-testid="projects-panel-list-state" data-variant="empty">
                  {t('projects.empty')}
                </div>
              ) : (
                projects.map((project) => (
                  <div
                    key={project.group_id}
                    className={`w-full min-w-0 flex items-center gap-1 rounded-lg border px-3 py-2 text-sm ${
                      selectedId === project.group_id
                        ? 'border-[var(--color-border-accent)] bg-accent-subtle text-text'
                        : 'border-transparent hover:bg-secondary/40 text-text-muted hover:text-text'
                    }`}
                    data-testid="projects-panel-item"
                    data-variant={String(project.group_id)}
                  >
                    <button
                      type="button"
                      onClick={() => setSelectedId(project.group_id)}
                      className="min-w-0 flex-1 text-left"
                      title={project.name}
                      data-testid="projects-panel-item-select"
                    >
                      <span className="truncate block font-medium" data-testid="projects-panel-item-name">{project.name}</span>
                      <span className="mt-1 block text-[11px] text-text-muted" data-testid="projects-panel-item-id">
                        {t('projects.groupId', { id: project.group_id })}
                      </span>
                    </button>
                    <button
                      type="button"
                      onClick={(event) => {
                        event.stopPropagation();
                        void deleteProject(project.group_id, project.name);
                      }}
                      disabled={busyProjectId !== null}
                      className="shrink-0 text-xs px-2 py-1 rounded-md border border-danger/30 text-danger hover:bg-danger-subtle disabled:opacity-50 disabled:cursor-not-allowed"
                      title={t('projects.deleteProject')}
                      data-testid="projects-panel-item-delete"
                    >
                      {busyProjectId === project.group_id ? t('projects.deleting') : t('common.delete')}
                    </button>
                  </div>
                ))
              )}
            </div>
          </div>

          <div className="rounded-xl border border-border bg-card/70 backdrop-blur-sm overflow-hidden shadow-sm p-3 min-h-0">
            {selected ? (
              <GroupDetail
                project={selected}
                myPrincipalId={myPrincipalId}
                myAgentPrincipalId={myAgentPrincipalId}
                onProjectGone={handleProjectGone}
              />
            ) : (
              <div className="h-full flex items-center justify-center text-sm text-text-muted" data-testid="projects-panel-detail-empty">
                {t('projects.selectFirst')}
              </div>
            )}
          </div>
        </div>
      </div>

      <CreateProjectDialog open={dialogOpen} onClose={() => setDialogOpen(false)} onCreated={loadProjects} />
      <JoinProjectDialog
        open={joinOpen}
        onClose={() => setJoinOpen(false)}
        onJoined={loadProjects}
      />
    </div>
  );
}
