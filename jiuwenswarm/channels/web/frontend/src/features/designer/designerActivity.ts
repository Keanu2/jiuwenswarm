import type { DesignerNodeActivity, DesignerNodeState } from './executionGraphTypes';
import { DESIGNER_LEADER_NODE_ID } from './executionGraphTypes';

export function isDesignerLeaderNodeId(nodeId: string | null | undefined): boolean {
  return String(nodeId || '').trim() === DESIGNER_LEADER_NODE_ID;
}

export function designerActivityLines(
  state: Pick<DesignerNodeState, 'activity' | 'activity_tail'> | null | undefined,
): string[] {
  const tail = (state?.activity_tail || []).map((item) => String(item || '').trim()).filter(Boolean);
  if (tail.length > 0) return tail.slice(-8);
  const latest = designerActivityText(state?.activity);
  return latest ? [latest] : [];
}

export function designerActivityText(activity: DesignerNodeActivity | null | undefined): string {
  if (!activity) return '';
  const tool = String(activity.tool || '').trim();
  const text = String(activity.text || '').trim();
  if (tool && text && text !== tool) return `${tool} · ${text}`;
  return text || tool;
}
