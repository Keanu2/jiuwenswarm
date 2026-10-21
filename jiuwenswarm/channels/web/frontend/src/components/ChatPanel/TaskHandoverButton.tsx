/**
 * TaskHandoverButton — 会话内「交接为跨设备任务」按钮。
 *
 * 把当前原生会话的历史（user/assistant 轮次）导出成 relay 上的一个任务，
 * 服务端原子完成（create → 裸 acquire → 逐条 append → release
 * input_required），随后任何设备——包括手机——都能在「跨设备任务」里接管
 * 继续。
 *
 * 处理中也可以交接：导出是 history.jsonl 的静止快照，直接在轮次运行中导
 * 会撕裂历史，所以点击时若本轮还在跑，先复用停止按钮的 onCancel 停掉它，
 * 有界等待 isProcessing/isThinking 落地后再导出。等不到本轮停止就按失败
 * 处理，绝不带着半份历史强行导出。与 TaskContinuationToggle 同款的安全
 * 模式（mountedRef + 5s 提示）。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { webRequest } from '../../services/webClient';
import { useChatStore } from '../../stores';

const HANDOVER_HINT_MS = 5000;
/** 点击后等待本轮停止的上限：超时视为失败，不带着撕裂的历史导出。 */
const TURN_STOP_TIMEOUT_MS = 15_000;

interface TaskHandoverButtonProps {
  /** 要交接的会话 id。 */
  sessionId: string;
  /** 会话标题；为空时服务端回退到 session_id。 */
  sessionTitle: string;
  /** 停止当前轮：与输入区停止按钮同一个 onCancel，模式差异（team 走 pause 等）由 App 层处理。 */
  onCancel: () => void;
  /** 交接成功（含 taskId）后回调：App 层刷新列表并切到该任务的聊天视图。 */
  onHandedOver?: (taskId: number) => void;
}

function isTurnRunning(sessionId: string): boolean {
  const runtime = useChatStore.getState().getRuntime(sessionId);
  return Boolean(runtime?.isProcessing || runtime?.isThinking);
}

/**
 * 等待本轮真正结束（chat.processing_status 等事件把 isProcessing /
 * isThinking 落为 false）。订阅的是「运行中」这个布尔值的变化，先订阅再
 * 查当前值，两者顺序保证不漏事件；超时后按当前值收尾。
 */
function waitForTurnStopped(sessionId: string): Promise<boolean> {
  return new Promise(resolve => {
    let settled = false;
    const finish = (stopped: boolean) => {
      if (settled) return;
      settled = true;
      unsubscribe();
      window.clearTimeout(timer);
      resolve(stopped);
    };
    const unsubscribe = useChatStore.subscribe(
      state => Boolean(state.runtimes[sessionId]?.isProcessing || state.runtimes[sessionId]?.isThinking),
      running => {
        if (!running) finish(true);
      },
    );
    const timer = window.setTimeout(() => finish(!isTurnRunning(sessionId)), TURN_STOP_TIMEOUT_MS);
    if (!isTurnRunning(sessionId)) finish(true);
  });
}

export function TaskHandoverButton({ sessionId, sessionTitle, onCancel, onHandedOver }: TaskHandoverButtonProps) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);
  const [hint, setHint] = useState<{ ok: boolean; text: string } | null>(null);
  const mountedRef = useRef(true);
  const hintTimerRef = useRef<number | null>(null);

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

  const showHint = useCallback((hint: { ok: boolean; text: string } | null) => {
    if (!mountedRef.current) return;
    setHint(hint);
    if (hintTimerRef.current !== null) {
      window.clearTimeout(hintTimerRef.current);
    }
    hintTimerRef.current = window.setTimeout(() => {
      hintTimerRef.current = null;
      if (mountedRef.current) setHint(null);
    }, HANDOVER_HINT_MS);
  }, []);

  const handover = useCallback(async () => {
    if (busy || !sessionId) return;
    setBusy(true);
    showHint(null);
    try {
      // 本轮还在跑：先停（历史正在写，直接导出会撕裂），等它落地再导出。
      if (isTurnRunning(sessionId)) {
        onCancel();
        const stopped = await waitForTurnStopped(sessionId);
        if (!stopped) {
          showHint({ ok: false, text: t('chat.taskHandover.turnStopFailed') });
          return;
        }
      }
      const payload = await webRequest<{ task?: Record<string, unknown> }>('task.handover', {
        session_id: sessionId,
        title: sessionTitle,
      });
      const taskId = Number(payload?.task?.task_id ?? 0);
      showHint({
        ok: true,
        text: taskId > 0 ? t('chat.taskHandover.success', { id: taskId }) : t('chat.taskHandover.success', { id: '' }),
      });
      if (taskId > 0) onHandedOver?.(taskId);
    } catch {
      showHint({ ok: false, text: t('chat.taskHandover.failed') });
    } finally {
      if (mountedRef.current) setBusy(false);
    }
  }, [busy, sessionId, sessionTitle, onCancel, onHandedOver, showHint, t]);

  return (
    <div className="chat-task-continuation" data-testid="chat-task-handover">
      <div className="chat-task-continuation__main">
        <button
          type="button"
          onClick={() => {
            void handover();
          }}
          disabled={busy || !sessionId}
          className="btn !px-3 !py-1 text-xs disabled:opacity-50 disabled:cursor-not-allowed"
          data-testid="chat-task-handover-button"
        >
          {busy ? t('chat.taskHandover.busy') : t('chat.taskHandover.button')}
        </button>
        {hint ? (
          <span className={`chat-task-continuation__hint${hint.ok ? '' : ' chat-task-continuation__hint--warn'}`} data-testid="chat-task-handover-hint">
            {hint.text}
          </span>
        ) : null}
      </div>
    </div>
  );
}
