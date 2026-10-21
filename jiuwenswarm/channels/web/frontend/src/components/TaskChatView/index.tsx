/**
 * TaskChatView — 在聊天主视图内打开一个 relay 跨设备任务。
 *
 * 侧边栏「任务」列表里的跨设备条目点击后路由到这里（替换 ChatPanel 的
 * 位置）：像普通会话一样查看任务日志、继续对话——数据与动作全部走
 * relay 协议（``task.message.list/subscribe/send``、``task.acquire``、
 * ``task.release``），本视图不持有任何本地任务状态。
 *
 * 视觉与 ChatPanel 共用同一套类系统（``chat-panel-header`` 头部、
 * ``chat-scroll`` 消息流、``chat-input-*`` 输入框），让跨设备任务呈现为一
 * 个「普通会话」。输入门槛与 relay 协议一致：只有本机 bridge 持有租约
 * （``task.local_status.held_task_ids``）才能发送；接管是显式动作
 * （force acquire 会击落对端设备的执行）。对端接管时本视图收到
 * ``task.state``（lease_lost / lease_preempt）→ 刷新 → 输入立即关上、
 * 接管按钮出现。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ArrowUp, ChevronLeft, LoaderCircle, MonitorSmartphone, RefreshCw } from 'lucide-react';
import { webRequest } from '../../services/webClient';
import { awaitingReplyCleared, canSend, isTerminalState, taskStateKey, toHeldTaskIds, type TaskItem } from '../../features/taskRelayPanelState';
import { errorCode, taskErrorText } from '../TaskRelayPanel/taskDisplay';
import { TaskMessageList } from '../TaskRelayPanel/TaskMessageList';
import { useTaskMessages } from '../../hooks/useTaskMessages';
import { tasksAvailable, useTaskRelayStore } from '../../stores/taskRelayStore';

const TOGGLE_HINT_MS = 3000;
/** chat-input-editor 的 max-height（与 ChatPanel.css 保持一致）。 */
const INPUT_MAX_HEIGHT = 200;

interface TaskChatViewProps {
  taskId: number;
  /** 退出任务视图（清掉 activeTaskId，回到普通会话视图）。 */
  onClose: () => void;
}

export function TaskChatView({ taskId, onClose }: TaskChatViewProps) {
  const { t } = useTranslation();
  const tasks = useTaskRelayStore(state => state.tasks);
  const status = useTaskRelayStore(state => state.status);
  const myPrincipalId = useTaskRelayStore(state => state.myPrincipalId);
  const storeLoading = useTaskRelayStore(state => state.loading);
  const refresh = useTaskRelayStore(state => state.refresh);
  const startPolling = useTaskRelayStore(state => state.startPolling);
  const stopPolling = useTaskRelayStore(state => state.stopPolling);

  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const [awaitingReply, setAwaitingReply] = useState(false);
  const [busy, setBusy] = useState(false);
  const [actionHint, setActionHint] = useState('');
  /** 动作类失败（发送/接管/挂起）的文案；日志/订阅失败在 useTaskMessages 里。 */
  const [actionError, setActionError] = useState<string | null>(null);
  const hintTimerRef = useRef<number | null>(null);
  const mountedRef = useRef(true);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const task: TaskItem | undefined = useMemo(() => tasks.find(item => item.task_id === taskId), [tasks, taskId]);
  const available = tasksAvailable(status);
  const heldTaskIds = useMemo(() => toHeldTaskIds(status?.held_task_ids), [status]);
  const heldHere = canSend(taskId, heldTaskIds);
  const terminal = isTerminalState(task?.state ?? '');

  const { messages, loading, error, reload } = useTaskMessages(taskId, {
    onStateEvent: () => {
      void refresh();
    },
  });

  const errorText = useCallback((err: unknown) => taskErrorText(t, errorCode(err)), [t]);

  const showHint = useCallback((text: string) => {
    setActionHint(text);
    if (hintTimerRef.current !== null) window.clearTimeout(hintTimerRef.current);
    hintTimerRef.current = window.setTimeout(() => {
      hintTimerRef.current = null;
      if (mountedRef.current) setActionHint('');
    }, TOGGLE_HINT_MS);
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (hintTimerRef.current !== null) {
        window.clearTimeout(hintTimerRef.current);
        hintTimerRef.current = null;
      }
    };
  }, []);

  // 任务视图也是 store 的消费者：打开期间保持轮询（列表侧边栏可能已卸载）。
  useEffect(() => {
    startPolling();
    return () => stopPolling();
  }, [startPolling, stopPolling]);

  // Clear "answering…" once the bridge's answer (a reply_to message) has
  // landed in the log.
  useEffect(() => {
    if (awaitingReply && awaitingReplyCleared(messages)) setAwaitingReply(false);
  }, [messages, awaitingReply]);

  // 输入框随内容自增高（封顶 ChatPanel 的 200px），与 contenteditable 行为一致。
  useEffect(() => {
    const node = textareaRef.current;
    if (!node) return;
    node.style.height = 'auto';
    node.style.height = `${Math.min(node.scrollHeight, INPUT_MAX_HEIGHT)}px`;
  }, [draft]);

  const send = async () => {
    const text = draft.trim();
    if (!text || sending) return;
    setSending(true);
    setActionError(null);
    try {
      await webRequest('task.message.send', {
        task_id: taskId,
        kind: 'text',
        payload: { text },
        // 幂等键：连接抖动后重发不会落两条。刻意不带 fence——服务端在本机
        // bridge 持有该任务时用它的 fence 兜底。
        client_msg_id: `web-task-${taskId}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      });
      setDraft('');
      setAwaitingReply(true);
      await reload();
    } catch (err) {
      setActionError(errorText(err));
    } finally {
      setSending(false);
    }
  };

  const takeOver = async () => {
    if (busy) return;
    setBusy(true);
    setActionError(null);
    try {
      await webRequest('task.acquire', { task_id: taskId, mode: 'force' });
      await refresh();
      showHint(t('taskRelay.takeOverDone', { id: taskId }));
    } catch (err) {
      setActionError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const releaseTask = async (nextState: 'input_required' | 'completed') => {
    if (busy) return;
    setBusy(true);
    setActionError(null);
    try {
      await webRequest('task.release', { task_id: taskId, next_state: nextState });
      setAwaitingReply(false);
      await refresh();
    } catch (err) {
      setActionError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const holderLabel = useMemo(() => {
    if (!task?.lease_holder) return t('taskRelay.holderNone');
    if (heldHere) return t('taskRelay.heldHere');
    // 同一 principal 的另一台设备持有（两实例配了同一把 key）。
    if (myPrincipalId && task.lease_holder === myPrincipalId) {
      return t('taskRelay.heldByMyDevice');
    }
    return t('taskRelay.heldElsewhere');
  }, [task?.lease_holder, heldHere, myPrincipalId, t]);

  const title = task?.title || t('taskRelay.untitled', { id: taskId });
  const stateLabel = t(`taskRelay.states.${taskStateKey(task?.state ?? '')}`);
  // 副标题：跨设备身份 + 任务号 + 状态 + 持有方（title 悬浮可见全文）。
  const subtitle = task ? `${t('multiSession.taskEntry.crossDevice')} · ${t('taskRelay.taskId', { id: taskId })} · ${stateLabel} · ${holderLabel}` : '';

  const backButtonText = t('multiSession.taskEntry.backToList');

  // 任务已不在列表里（被 retire）：空态 + 返回，不再渲染日志与输入。
  if (!task) {
    return (
      <div className="chat-panel-shell flex flex-col h-full" data-testid="task-chat-view">
        <div className="chat-panel-header" data-testid="task-chat-view-header">
          <div className="chat-panel-header__meta">
            <button
              type="button"
              onClick={onClose}
              className="chat-header-icon-btn"
              data-testid="task-chat-view-back-button"
              title={backButtonText}
              aria-label={backButtonText}
            >
              <ChevronLeft size={16} strokeWidth={2} aria-hidden="true" />
            </button>
          </div>
        </div>
        <div className="flex-1 min-h-0 flex flex-col items-center justify-center gap-4" data-testid="task-chat-view-missing">
          {storeLoading || !available ? (
            <LoaderCircle className="animate-spin text-text-muted" size={24} strokeWidth={2} aria-hidden="true" data-testid="task-chat-view-missing-loading" />
          ) : (
            <>
              <p className="text-sm text-text-muted">{t('taskRelay.errors.task_not_found')}</p>
              <button
                type="button"
                onClick={() => {
                  void refresh();
                }}
                className="btn !px-3 !py-1.5 text-xs"
                data-testid="task-chat-view-missing-refresh"
              >
                {t('common.refresh')}
              </button>
            </>
          )}
        </div>
      </div>
    );
  }

  const canSubmit = !sending && Boolean(draft.trim());

  return (
    <div className="chat-panel-shell flex flex-col h-full" data-testid="task-chat-view">
      <div className="chat-panel-header" data-testid="task-chat-view-header">
        <div className="chat-panel-header__meta" data-testid="task-chat-view-header-meta">
          <button
            type="button"
            onClick={onClose}
            className="chat-header-icon-btn shrink-0"
            data-testid="task-chat-view-back-button"
            title={backButtonText}
            aria-label={backButtonText}
          >
            <ChevronLeft size={16} strokeWidth={2} aria-hidden="true" />
          </button>
          <div className="chat-panel-header__title" title={title} data-testid="task-chat-view-title">
            {title}
          </div>
          <div className="chat-panel-header__project" title={subtitle} data-testid="task-chat-view-subtitle">
            <MonitorSmartphone size={14} strokeWidth={1.8} aria-hidden="true" />
            <span>{t('multiSession.taskEntry.crossDevice')}</span>
            <span>{`${stateLabel} · ${holderLabel}`}</span>
          </div>
        </div>
        <div className="chat-panel-header__actions" data-testid="task-chat-view-actions">
          {heldHere && !terminal ? (
            <>
              <button
                type="button"
                onClick={() => void releaseTask('input_required')}
                disabled={busy}
                className="btn !px-2.5 !py-1 text-xs disabled:cursor-not-allowed disabled:opacity-50"
                data-testid="task-chat-view-suspend-button"
              >
                {t('taskRelay.suspend')}
              </button>
              <button
                type="button"
                onClick={() => void releaseTask('completed')}
                disabled={busy}
                className="btn !px-2.5 !py-1 text-xs disabled:cursor-not-allowed disabled:opacity-50"
                data-testid="task-chat-view-complete-button"
              >
                {t('taskRelay.complete')}
              </button>
            </>
          ) : null}
          <button
            type="button"
            onClick={() => void reload()}
            disabled={loading}
            className="chat-header-icon-btn"
            data-testid="task-chat-view-refresh-button"
            title={t('common.refresh')}
            aria-label={t('common.refresh')}
          >
            <RefreshCw className={loading ? 'animate-spin' : undefined} size={16} strokeWidth={2} aria-hidden="true" />
          </button>
        </div>
      </div>

      {actionError || error ? (
        <div className="mx-5 mb-2 rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger" data-testid="task-chat-view-error">
          {actionError ?? taskErrorText(t, error, 'taskRelay.errors.load')}
        </div>
      ) : null}
      {actionHint ? (
        <div className="mx-5 mb-2 rounded-md border border-[var(--color-border-accent)] bg-accent-subtle px-3 py-2 text-sm" data-testid="task-chat-view-hint">
          {actionHint}
        </div>
      ) : null}

      <TaskMessageList messages={messages} loading={loading} awaitingReply={awaitingReply} />

      <div className="chat-compose" data-testid="task-chat-view-composer">
        {terminal ? (
          <div
            className="rounded-2xl border border-[var(--color-chat-composer-border)] bg-[var(--color-chat-composer-surface)] px-4 py-3 text-center text-xs text-text-muted"
            data-testid="task-chat-view-terminated"
          >
            {t('taskRelay.terminated')}
          </div>
        ) : heldHere ? (
          <div className="chat-input-frame">
            <div className="chat-input-container">
              <textarea
                ref={textareaRef}
                value={draft}
                onChange={event => setDraft(event.target.value)}
                onKeyDown={event => {
                  if (event.key === 'Enter' && !event.shiftKey) {
                    event.preventDefault();
                    void send();
                  }
                }}
                placeholder={t('taskRelay.messagePlaceholder')}
                className="chat-input-editor"
                disabled={sending}
                data-testid="task-chat-view-input"
              />
              <div className="chat-input-toolbar">
                <div className="chat-input-toolbar-left" />
                <div className="chat-input-actions">
                  <button
                    type="button"
                    onClick={() => void send()}
                    disabled={!canSubmit}
                    className={`chat-input-btn chat-input-btn--send ${canSubmit ? 'chat-input-btn--send-active' : 'chat-input-btn--disabled'}`}
                    title={t('taskRelay.send')}
                    aria-label={t('taskRelay.send')}
                    data-testid="task-chat-view-send-button"
                  >
                    {sending ? (
                      <LoaderCircle className="animate-spin" size={16} strokeWidth={2} aria-hidden="true" />
                    ) : (
                      <ArrowUp size={16} strokeWidth={2} aria-hidden="true" />
                    )}
                  </button>
                </div>
              </div>
            </div>
          </div>
        ) : (
          <div
            className="flex items-center justify-between gap-3 rounded-2xl border border-[var(--color-chat-composer-border)] bg-[var(--color-chat-composer-surface)] px-4 py-2.5"
            data-testid="task-chat-view-not-held"
          >
            <p className="min-w-0 truncate text-xs text-text-muted">{t('taskRelay.notHeldHere')}</p>
            <button
              type="button"
              onClick={() => void takeOver()}
              disabled={busy}
              className="btn primary shrink-0 !px-3 !py-1.5 text-xs disabled:cursor-not-allowed disabled:opacity-50"
              data-testid="task-chat-view-takeover-button"
            >
              {t('taskRelay.takeOver')}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
