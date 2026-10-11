import { designerAssetTextUrl } from './designerAssetUrl';
import {
  DESIGNER_NODE_ROLE_STORYBOARD,
  type DesignerExecutionGraph,
  type DesignerExecutionRun,
} from './executionGraphTypes';

export type StoryboardClip = {
  clip_no: string;
  timeline: string;
  camera: string;
  move: string;
  character_action: string;
  scene_change: string;
  comment: string;
};

const FIELD_ALIASES: Record<keyof StoryboardClip, string[]> = {
  clip_no: ['Clip', 'Shot', '镜号'],
  timeline: ['Timeline', '时间轴'],
  camera: ['Camera', '镜头视角', '景别'],
  move: ['Move', '运镜'],
  character_action: ['Character action', 'Character', '人物变化'],
  scene_change: ['Scene change', 'Scene', '场景变化'],
  comment: ['Comment', 'Notes', '注释', '备注', '画面描述', '提示词'],
};

const POSITIONAL_FIELDS: Array<keyof StoryboardClip> = [
  'clip_no',
  'timeline',
  'camera',
  'move',
  'character_action',
  'scene_change',
  'comment',
];

function emptyClip(): StoryboardClip {
  return {
    clip_no: '',
    timeline: '',
    camera: '',
    move: '',
    character_action: '',
    scene_change: '',
    comment: '',
  };
}

function splitMarkdownRow(line: string): string[] {
  let text = line.trim();
  if (text.startsWith('|')) text = text.slice(1);
  if (text.endsWith('|')) text = text.slice(0, -1);
  return text.split('|').map((cell) => cell.trim());
}

function isSeparator(cells: string[]): boolean {
  return cells.every((cell) => !cell || /^:?-{3,}:?$/.test(cell));
}

function headerFieldMap(cells: string[]): Map<number, keyof StoryboardClip> | null {
  const mapping = new Map<number, keyof StoryboardClip>();
  cells.forEach((cell, index) => {
    const name = cell.trim();
    if (!name) return;
    (Object.keys(FIELD_ALIASES) as Array<keyof StoryboardClip>).some((field) => {
      const matched = FIELD_ALIASES[field].some((alias) => name === alias || name.includes(alias));
      if (matched) mapping.set(index, field);
      return matched;
    });
  });
  const values = [...mapping.values()];
  if (values.includes('clip_no') || values.includes('timeline')) return mapping;
  return null;
}

export function parseStoryboardClips(text: string): StoryboardClip[] {
  const clips: StoryboardClip[] = [];
  let headerSeen = false;
  let fieldMap: Map<number, keyof StoryboardClip> | null = null;
  for (const line of (text || '').split(/\r?\n/)) {
    if (!line.includes('|')) continue;
    const cells = splitMarkdownRow(line);
    if (!cells.some(Boolean)) continue;
    if (isSeparator(cells)) continue;
    const joined = cells.join('');
    if (!headerSeen && /clip|shot|timeline|镜号|时间轴/i.test(joined)) {
      headerSeen = true;
      fieldMap = headerFieldMap(cells);
      continue;
    }
    if (!headerSeen) continue;
    const clip = emptyClip();
    if (fieldMap) {
      fieldMap.forEach((field, index) => {
        if (index < cells.length) clip[field] = cells[index];
      });
    } else {
      POSITIONAL_FIELDS.forEach((field, index) => {
        if (index < cells.length) clip[field] = cells[index];
      });
    }
    if (!clip.clip_no) clip.clip_no = String(clips.length + 1);
    if (!/^\d/.test(clip.clip_no) && cells.length < 4) continue;
    clips.push(clip);
    if (clips.length >= 6) break;
  }
  return clips;
}

export function clipGeneratePrompt(clip: StoryboardClip): string {
  const comment = (clip.comment || '').trim();
  if (comment) return comment;
  const parts: string[] = [];
  const timeline = (clip.timeline || '').trim();
  if (timeline) parts.push(`Timeline ${timeline}`);
  const fields: Array<[string, keyof StoryboardClip]> = [
    ['Camera', 'camera'],
    ['Camera move', 'move'],
    ['Character action', 'character_action'],
    ['Scene change', 'scene_change'],
  ];
  fields.forEach(([label, key]) => {
    const value = (clip[key] || '').trim();
    if (value) parts.push(`${label} ${value}`);
  });
  return parts.join('; ');
}

export function storyboardTextUrl(
  graph: DesignerExecutionGraph | null | undefined,
  run: DesignerExecutionRun | null | undefined,
): string | null {
  const node = graph?.nodes.find((item) => item.config?.role === DESIGNER_NODE_ROLE_STORYBOARD);
  if (!node) return null;
  const state = run?.node_states?.[node.id];
  const ref = state?.output_ref || node.output_ref;
  return designerAssetTextUrl(ref?.uri);
}

export async function fetchStoryboardText(url: string): Promise<string> {
  const response = await fetch(url);
  if (!response.ok) throw new Error(String(response.status));
  return response.text();
}
