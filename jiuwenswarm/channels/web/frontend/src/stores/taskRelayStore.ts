/**
 * taskRelayStore — relay 跨设备任务的共享状态分片。
 *
 * 侧边栏（chat/cron 两处挂载）与「跨设备任务」面板共一份任务列表与本机
 * bridge 状态：单一 5s 轮询（引用计数，最后一个消费者卸载才停），页面
 * 不可见时跳过、回前台立即补一拍。照 ``cronStore`` 的先例独立成片，不塞
 * workspaceStore（那是本地磁盘会话域，relay 任务是外部状态）。
 *
 * relay 是任务状态的唯一权威；本 store 只做拉取与缓存：
 *  - ``task.local_status`` 由本地 AgentServer 直答（不依赖 relay 连通），
 *    永远轮询——用户打开开关后 ≤5s 任务自动出现；
 *  - ``task.list`` 仅在 relay 已配置（``service_ready``）时拉取。注意
 *    ``enabled`` 只关本地 bridge（能不能在本机发送），不隐藏 relay 上的
 *    任务本身——侧边栏要展示的是跨设备状态，另一台设备可能正持有；
 *  - 拉取失败保留上一份快照（侧边栏条目不闪空），错误码存 ``error``，
 *    由面板/任务视图翻译展示。
 */
import { create } from 'zustand';
import { webRequest } from '../services/webClient';
import { toTaskItems, type TaskItem } from '../features/taskRelayPanelState';

export interface TaskLocalStatus {
  enabled?: boolean;
  service_ready?: boolean;
  bridge_running?: boolean;
  held_task_ids?: unknown[];
}

const LIST_LIMIT = 100;
const LIST_POLL_MS = 5000;

/** Read the server's error code off a rejected webRequest, for i18n lookup. */
function errorCode(error: unknown): string {
  if (error && typeof error === 'object' && 'code' in error) {
    return String((error as { code?: unknown }).code ?? '');
  }
  return '';
}

interface TaskRelayState {
  tasks: TaskItem[];
  status: TaskLocalStatus | null;
  /** 当前 principal（task.whoami），用于区分「我的另一台设备持有」。 */
  myPrincipalId: string;
  loading: boolean;
  /** 最近一次 task.list 失败的错误码（空串为未知）；成功后清空。 */
  error: string | null;
  /** 拉取本机状态 + （可用时）任务列表。 */
  refresh: () => Promise<void>;
  /** 引用计数轮询：挂载时调用（会立即补一拍 refresh），卸载时配对调用。 */
  startPolling: () => void;
  stopPolling: () => void;
}

// 引用计数轮询的模块级单例（timer 与监听器）。
let pollRefCount = 0;
let pollTimer: number | null = null;

function onVisibilityChange() {
  if (document.visibilityState === 'visible') void useTaskRelayStore.getState().refresh();
}

function ensurePolling() {
  pollRefCount += 1;
  if (pollTimer !== null) return;
  pollTimer = window.setInterval(() => {
    if (document.visibilityState === 'visible') void useTaskRelayStore.getState().refresh();
  }, LIST_POLL_MS);
  document.addEventListener('visibilitychange', onVisibilityChange);
}

function releasePolling() {
  pollRefCount = Math.max(0, pollRefCount - 1);
  if (pollRefCount === 0 && pollTimer !== null) {
    window.clearInterval(pollTimer);
    pollTimer = null;
    document.removeEventListener('visibilitychange', onVisibilityChange);
  }
}

export const useTaskRelayStore = create<TaskRelayState>((set, get) => ({
  tasks: [],
  status: null,
  myPrincipalId: '',
  loading: true,
  error: null,

  refresh: async () => {
    // 本地状态永远可答；拿不到（服务未连上等）时保留空态，按未持有渲染。
    try {
      const payload = await webRequest<TaskLocalStatus>('task.local_status');
      set({ status: payload ?? {} });
    } catch {
      set((state) => ({ status: state.status ?? {} }));
    }

    // 知道自己是谁，才能把 lease_holder 翻译成「我的另一台设备」。
    // principal 由 API key 决定，会话期间不变——解析一次即可，失败不致命。
    if (!get().myPrincipalId) {
      void webRequest<{ principal_id?: string }>('task.whoami')
        .then((me) => {
          if (me?.principal_id) set({ myPrincipalId: String(me.principal_id) });
        })
        .catch(() => {
          /* 身份解析失败：持有方一律显示「其他设备」。 */
        });
    }

    const status = get().status ?? {};
    if (!status.service_ready) {
      // relay 未配置：没有可拉的列表。清空任务让隐藏规则失效，
      // 侧边栏原会话条目回归，与现状一致。
      set({ tasks: [], loading: false });
      return;
    }
    try {
      const payload = await webRequest<{ tasks?: unknown[] }>('task.list', { limit: LIST_LIMIT });
      set({ tasks: toTaskItems(payload?.tasks), error: null, loading: false });
    } catch (err) {
      // relay 不可达：保留上一份快照（条目不闪空），错误码交给面板展示。
      set({ loading: false, error: errorCode(err) });
    }
  },

  startPolling: () => {
    ensurePolling();
    void get().refresh();
  },

  stopPolling: () => {
    releasePolling();
  },
}));

/** relay 是否已配置（可拉任务列表）。本机 bridge 开关（enabled）不影响可见性。 */
export function tasksAvailable(status: TaskLocalStatus | null | undefined): boolean {
  return Boolean(status?.service_ready);
}
