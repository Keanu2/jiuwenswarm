/**
 * TaskContinuationToggle — 「任务」界面的跨设备任务接续开关。
 *
 * 与手机端同一套两层门控：服务可用性（config.yaml tasks 段的 base_url +
 * api_key，界面只读提示）∩ 用户开关（tasks.enabled，本开关读写）。
 * 开关状态持久化在 config.yaml，进程内由 task.set_enabled RPC 即时启停
 * bridge（持有租约 / 抢占让位），无需重启服务。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Switch } from '../Switch';
import { webRequest } from '../../services/webClient';

interface TaskLocalStatusPayload {
  enabled?: boolean;
  service_ready?: boolean;
  bridge_running?: boolean;
}

const TOGGLE_ERROR_HINT_MS = 3000;

/** 开关行出现的两处（欢迎页输入区 / 会话输入区）共用同一份后端状态。 */
export function TaskContinuationToggle() {
  const { t } = useTranslation();
  const [status, setStatus] = useState<TaskLocalStatusPayload | null>(null);
  const [busy, setBusy] = useState(false);
  const [toggleFailed, setToggleFailed] = useState(false);
  const mountedRef = useRef(true);
  const failTimerRef = useRef<number | null>(null);

  useEffect(() => {
    mountedRef.current = true;
    webRequest<TaskLocalStatusPayload>('task.local_status')
      .then((payload) => {
        if (mountedRef.current) setStatus(payload ?? {});
      })
      .catch(() => {
        // 拿不到状态（服务未连上等）也照常渲染，开关默认关闭。
        if (mountedRef.current) setStatus({});
      });
    return () => {
      mountedRef.current = false;
      if (failTimerRef.current !== null) {
        window.clearTimeout(failTimerRef.current);
        failTimerRef.current = null;
      }
    };
  }, []);

  const handleChange = useCallback(
    (checked: boolean) => {
      if (busy) return;
      setBusy(true);
      setToggleFailed(false);
      webRequest<TaskLocalStatusPayload>('task.set_enabled', { enabled: checked })
        .then((payload) => {
          if (mountedRef.current) setStatus(payload ?? {});
        })
        .catch(() => {
          if (!mountedRef.current) return;
          // 失败提示通过 hint 行短暂展示，之后回落到常态文案。
          setToggleFailed(true);
          if (failTimerRef.current !== null) {
            window.clearTimeout(failTimerRef.current);
          }
          failTimerRef.current = window.setTimeout(() => {
            failTimerRef.current = null;
            if (mountedRef.current) setToggleFailed(false);
          }, TOGGLE_ERROR_HINT_MS);
        })
        .finally(() => {
          if (mountedRef.current) setBusy(false);
        });
    },
    [busy]
  );

  const enabled = status?.enabled ?? false;
  const serviceReady = status?.service_ready ?? false;

  const hint = toggleFailed
    ? t('chat.taskContinuation.toggleFailed')
    : serviceReady
      ? t('chat.taskContinuation.hint')
      : t('chat.taskContinuation.unconfigured');

  return (
    <div
      className={`chat-task-continuation${toggleFailed ? ' chat-task-continuation--error' : ''}`}
      data-testid="chat-task-continuation"
    >
      <div className="chat-task-continuation__main">
        <span className="chat-task-continuation__title">
          {t('chat.taskContinuation.title')}
        </span>
        <span
          className={`chat-task-continuation__hint${serviceReady && !toggleFailed ? '' : ' chat-task-continuation__hint--warn'}`}
        >
          {hint}
        </span>
      </div>
      <Switch
        checked={enabled}
        onChange={handleChange}
        disabled={busy}
        title={t('chat.taskContinuation.title')}
      />
    </div>
  );
}
