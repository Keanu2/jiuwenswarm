/**
 * useTaskMessages — 一个 relay 任务的消息日志：初载 + 实时帧。
 *
 * 从 TaskRelayPanel 的 TaskDetail 抽出，供面板详情与聊天视图
 * （TaskChatView）共用：
 *  - ``task.message.list`` 初载（limit 条）；
 *  - ``task.message.subscribe`` / ``unsubscribe`` 订阅该任务的实时帧；
 *  - ``webClient`` 的 ``task.message`` / ``task.state`` 推送按 task_id 过滤
 *    （后端广播到所有 web 连接），``backlog`` / ``message`` 帧合并进列表；
 *  - ``task.state`` 帧（lease_lost / lease_preempt 等）转交 ``onStateEvent``，
 *    调用方据此刷新任务列表与本机持有状态。
 *
 * ``error`` 存的是服务端错误码（不是文案）——i18n 由调用方完成。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { webRequest, webClient } from '../services/webClient';
import {
  mergeTaskMessages,
  toSeq,
  toTaskMessages,
  type TaskMessage,
} from '../features/taskRelayPanelState';
import { errorCode } from '../components/TaskRelayPanel/taskDisplay';

const MESSAGE_PAGE_LIMIT = 50;

export interface UseTaskMessagesResult {
  messages: TaskMessage[];
  loading: boolean;
  /** 最近一次失败的错误码（空串为未知错误）；成功后清空。 */
  error: string | null;
  reload: () => Promise<void>;
}

export function useTaskMessages(
  taskId: number,
  options?: { onStateEvent?: () => void },
): UseTaskMessagesResult {
  const [messages, setMessages] = useState<TaskMessage[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const mountedRef = useRef(true);
  const onStateEventRef = useRef(options?.onStateEvent);
  onStateEventRef.current = options?.onStateEvent;

  const reload = useCallback(async () => {
    try {
      const response = await webRequest<{ messages?: unknown[] }>('task.message.list', {
        task_id: taskId,
        limit: MESSAGE_PAGE_LIMIT,
      });
      if (!mountedRef.current) return;
      setMessages((current) => mergeTaskMessages(current, toTaskMessages(response?.messages)));
      setError(null);
    } catch (err) {
      if (!mountedRef.current) return;
      setError(errorCode(err));
    }
  }, [taskId]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // Reset on selection change, then subscribe to the task's live frames.
  useEffect(() => {
    let cancelled = false;
    setMessages([]);
    setError(null);
    setLoading(true);
    void reload().finally(() => {
      if (mountedRef.current) setLoading(false);
    });

    void (async () => {
      try {
        await webRequest('task.message.subscribe', { task_id: taskId });
      } catch (err) {
        if (!cancelled) setError(errorCode(err));
      }
    })();

    return () => {
      cancelled = true;
      void webRequest('task.message.unsubscribe', { task_id: taskId }).catch(() => {
        /* best effort: the view is going away */
      });
    };
  }, [taskId, reload]);

  // Live frames for this task only — the backend broadcasts to every client.
  useEffect(() => {
    const offMessage = webClient.on<Record<string, unknown>>('task.message', ({ payload }) => {
      if (toSeq(payload?.task_id) !== taskId) return;
      const frame = String(payload?.frame ?? '');
      if (frame === 'backlog') {
        setMessages((current) => mergeTaskMessages(current, toTaskMessages(payload.messages)));
        return;
      }
      if (frame === 'message') {
        // The subscription hub normalizes the relay's flat frame into `message`.
        setMessages((current) => mergeTaskMessages(current, [payload.message]));
      }
    });
    // lease_lost / lease_preempt（对端接管）：lease 帧兼任状态信号——本机
    // bridge 已被击落，输入框必须立刻关上，列表里的持有方也要刷新。
    const offState = webClient.on<Record<string, unknown>>('task.state', ({ payload }) => {
      if (toSeq(payload?.task_id) !== taskId) return;
      onStateEventRef.current?.();
    });
    return () => {
      offMessage();
      offState();
    };
  }, [taskId]);

  return { messages, loading, error, reload };
}
