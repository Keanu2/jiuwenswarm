/**
 * Pure state logic for merging relay cross-device tasks into the chat
 * sidebar's 「任务」 list.
 *
 * Mirrors ``taskRelayPanelState``: kept free of React and of the WebSocket
 * client (type-only imports) so the merge rules — where the bugs live — can
 * be tested directly with the tsc + node --test harness.
 *
 * The sidebar's own ordering lives in ``multi-session/sidebar/sidebarModel``
 * (``getSessionActivityAt``); task entries must interleave with sessions by
 * the same activity semantics, so the seconds-vs-milliseconds normalization
 * is mirrored here rather than imported — a runtime import would leave this
 * module's compiled output with an extensionless specifier Node cannot
 * resolve. Keep the two rules in sync.
 */
import type { TaskItem } from './taskRelayPanelState';

/** 侧边栏会话的最小结构投影（结构化兼容 stores 的 Session）。 */
export interface SidebarSessionLike {
  session_id: string;
  title?: string;
  last_user_message_at?: number;
  last_message_at?: number;
  updated_at?: string;
  created_at?: string;
  /** task.handover 成功后由后端写入的交接标记；未交接为空。 */
  handover_task_id?: number | null;
}

/** 侧边栏混排条目：本地会话或 relay 跨设备任务，附统一归一后的活跃时间。 */
export type SidebarEntry<S extends SidebarSessionLike = SidebarSessionLike> =
  { kind: 'session'; session: S; activityAt: number } | { kind: 'task'; task: TaskItem; activityAt: number };

export interface MergeSidebarEntriesOptions {
  /**
   * relay 任务是否可混排（``task.local_status`` 的 ``service_ready``——
   * 即 relay 已配置）。false 时完全降级为纯会话列表：不显示任务条目、
   * 不隐藏任何会话。
   */
  tasksAvailable: boolean;
}

function normalizeActivityTime(value: number | string | undefined): number | null {
  if (typeof value === 'number') {
    return value < 1e11 ? value * 1000 : value;
  }
  if (typeof value === 'string') {
    const parsed = Date.parse(value);
    return Number.isNaN(parsed) ? null : parsed;
  }
  return null;
}

/** 会话活跃时间：与 sidebarModel.getSessionActivityAt 同一回退链，仅供混排排序。 */
function sessionActivityAt(session: SidebarSessionLike): number {
  return (
    normalizeActivityTime(session.last_user_message_at) ??
    normalizeActivityTime(session.last_message_at) ??
    normalizeActivityTime(session.updated_at) ??
    normalizeActivityTime(session.created_at) ??
    Date.now()
  );
}

/**
 * 任务活跃时间：``updated_at`` 回退 ``created_at``，秒/毫秒与会话同一规则
 * 归一。两个时间都缺失时返回 0（沉底），不用「现在」——排序不应被缺数据
 * 的行目顶到最前。
 */
export function getTaskActivityAt(task: Pick<TaskItem, 'updated_at' | 'created_at'>): number {
  return normalizeActivityTime(task.updated_at) ?? normalizeActivityTime(task.created_at) ?? 0;
}

/**
 * 交接后的会话是否应从侧边栏隐藏（被跨设备任务条目替换）。
 *
 * 隐藏 ⟺ tasks 可用 ∧ 该任务仍在当前可见列表里（未 archived）：任务被
 * retire/归档或 relay 未配置时原会话条目自动回归，永远不会出现
 * 「会话和任务两边都看不到」。
 */
export function shouldHideHandedOverSession(
  session: Pick<SidebarSessionLike, 'handover_task_id'>,
  visibleTaskIds: ReadonlySet<number>,
  tasksAvailable: boolean,
): boolean {
  const taskId = session.handover_task_id;
  if (!tasksAvailable || !taskId || taskId <= 0) return false;
  return visibleTaskIds.has(taskId);
}

/**
 * 把 relay 跨设备任务混排进侧边栏「任务」分组（默认项目会话列表）。
 *
 * - 已交接且任务仍可见的会话被隐藏（条目替换为任务，见
 *   ``shouldHideHandedOverSession``）；
 * - archived 任务不参与混排；
 * - 会话与任务统一按活跃时间倒序交错。
 */
export function mergeSidebarEntries<S extends SidebarSessionLike>(
  sessions: readonly S[],
  tasks: readonly TaskItem[],
  options: MergeSidebarEntriesOptions,
): SidebarEntry<S>[] {
  const visibleTasks = options.tasksAvailable ? tasks.filter(task => !task.archived) : [];
  const visibleTaskIds = new Set(visibleTasks.map(task => task.task_id));
  const entries: SidebarEntry<S>[] = [];
  for (const session of sessions) {
    if (shouldHideHandedOverSession(session, visibleTaskIds, options.tasksAvailable)) continue;
    entries.push({ kind: 'session', session, activityAt: sessionActivityAt(session) });
  }
  for (const task of visibleTasks) {
    entries.push({ kind: 'task', task, activityAt: getTaskActivityAt(task) });
  }
  entries.sort((left, right) => right.activityAt - left.activityAt);
  return entries;
}
