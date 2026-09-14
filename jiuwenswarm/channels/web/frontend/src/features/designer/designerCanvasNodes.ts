import { generateUuidV4 } from '../../utils/uuid';
import type { DesignerLibraryAsset } from './designerAssetLibraryStore';
import {
  DESIGNER_CONFIG_DELEGATE_HANDLER,
  DESIGNER_NODE_ROLE_AUDIO,
  DESIGNER_NODE_ROLE_IMAGE,
  DESIGNER_NODE_ROLE_VIDEO,
  DESIGNER_NODE_TYPE_AUDIO,
  DESIGNER_NODE_TYPE_IMAGE,
  DESIGNER_NODE_TYPE_TEXT,
  DESIGNER_NODE_TYPE_TABLE,
  DESIGNER_NODE_TYPE_VIDEO,
  type DesignerGraphEdge,
  type DesignerGraphNode,
  type DesignerNodeRole,
  type DesignerNodeType,
} from './executionGraphTypes';

export const DESIGNER_CANVAS_NODE_WIDTH = 280;
export const DESIGNER_CANVAS_NODE_HEIGHT = 160;
export const DESIGNER_SUCCESSOR_GAP_X = 88;
export const DESIGNER_SUCCESSOR_GAP_Y = 36;
export const DESIGNER_ASSET_DRAG_MIME = 'application/x-designer-asset-id';
export const DESIGNER_ADD_GROUP_ORDER: DesignerAddGroup[] = ['image', 'video', 'audio'];

export type DesignerCanvasTool = 'select' | 'hand';
export type DesignerDockPanel = 'add' | 'assets' | null;
export type DesignerAddGroup = 'image' | 'video' | 'audio';

export type DesignerAddTemplate = {
  id: string;
  type: DesignerNodeType;
  role: DesignerNodeRole;
  label: string;
  group: DesignerAddGroup;
};

export const DESIGNER_ADD_TEMPLATES: DesignerAddTemplate[] = [
  {
    id: 'image',
    type: DESIGNER_NODE_TYPE_IMAGE,
    role: DESIGNER_NODE_ROLE_IMAGE,
    label: 'Image',
    group: 'image',
  },
  {
    id: 'video',
    type: DESIGNER_NODE_TYPE_VIDEO,
    role: DESIGNER_NODE_ROLE_VIDEO,
    label: 'Video',
    group: 'video',
  },
  {
    id: 'audio',
    type: DESIGNER_NODE_TYPE_AUDIO,
    role: DESIGNER_NODE_ROLE_AUDIO,
    label: 'Audio',
    group: 'audio',
  },
];

export function groupedDesignerAddTemplates(): Array<{
  group: DesignerAddGroup;
  items: DesignerAddTemplate[];
}> {
  return DESIGNER_ADD_GROUP_ORDER.map((group) => ({
    group,
    items: DESIGNER_ADD_TEMPLATES.filter((item) => item.group === group),
  }));
}

export function nextTypeIndex(
  nodes: Array<{ type?: string }>,
  type: string,
): number {
  return nodes.filter((node) => String(node.type || '') === type).length + 1;
}

export function uniqueDesignerNodeId(existingIds: Iterable<string>, role: string): string {
  const used = new Set(existingIds);
  const slug = String(role || 'node').replace(/[^a-z0-9]+/gi, '_').replace(/^_|_$/g, '') || 'node';
  for (let attempt = 0; attempt < 8; attempt += 1) {
    const id = `n_${slug}_${generateUuidV4().replace(/-/g, '').slice(0, 8)}`;
    if (!used.has(id)) return id;
  }
  return `n_${slug}_${Date.now().toString(36)}`;
}

export function offsetCanvasPosition(
  origin: { x: number; y: number },
  existingCount: number,
): { x: number; y: number } {
  const shift = existingCount * 28;
  return {
    x: origin.x - DESIGNER_CANVAS_NODE_WIDTH / 2 + shift,
    y: origin.y - DESIGNER_CANVAS_NODE_HEIGHT / 2 + shift,
  };
}

export function positionRightOfNode(
  source: { id?: string; layout?: { x?: number; y?: number; width?: number; height?: number } },
  existing: Array<{ id: string; layout?: { x?: number; y?: number } }>,
): { x: number; y: number } {
  const width = source.layout?.width ?? DESIGNER_CANVAS_NODE_WIDTH;
  const x = (source.layout?.x ?? 0) + width + DESIGNER_SUCCESSOR_GAP_X;
  const baseY = source.layout?.y ?? 0;
  const occupiedYs = existing
    .filter((node) => node.id !== source.id)
    .filter((node) => Math.abs((node.layout?.x ?? 0) - x) < DESIGNER_CANVAS_NODE_WIDTH * 0.6)
    .map((node) => node.layout?.y ?? 0)
    .sort((left, right) => left - right);
  let y = baseY;
  for (const used of occupiedYs) {
    if (Math.abs(used - y) < DESIGNER_CANVAS_NODE_HEIGHT * 0.8) {
      y = used + DESIGNER_CANVAS_NODE_HEIGHT + DESIGNER_SUCCESSOR_GAP_Y;
    }
  }
  return { x, y };
}

export function buildSuccessorEdge(sourceId: string, targetId: string): DesignerGraphEdge {
  return {
    id: `e_${sourceId}_${targetId}_${generateUuidV4().replace(/-/g, '').slice(0, 8)}`,
    source: sourceId,
    target: targetId,
    kind: 'data',
  };
}

export function withPredecessorInput(
  node: DesignerGraphNode,
  sourceId: string,
): DesignerGraphNode {
  const current = Array.isArray(node.config?.inputs) ? node.config.inputs.map(String) : [];
  if (current.includes(sourceId)) return node;
  return {
    ...node,
    config: {
      ...(node.config ?? {}),
      inputs: [...current, sourceId],
    } as DesignerGraphNode['config'],
  };
}

export function connectNodeToGraph(
  graph: { nodes: DesignerGraphNode[]; edges: DesignerGraphEdge[] },
  sourceId: string,
  node: DesignerGraphNode,
): { nodes: DesignerGraphNode[]; edges: DesignerGraphEdge[] } | null {
  const source = String(sourceId || '').trim();
  const target = String(node.id || '').trim();
  if (!source || !target) return null;
  if (!graph.nodes.some((item) => item.id === source)) return null;
  if (graph.nodes.some((item) => item.id === target)) return null;
  const nextNode = withPredecessorInput(node, source);
  const duplicateEdge = graph.edges.some(
    (edge) => edge.source === source && edge.target === target,
  );
  return {
    nodes: [...graph.nodes, nextNode],
    edges: duplicateEdge ? graph.edges : [...graph.edges, buildSuccessorEdge(source, target)],
  };
}

export function removeNodesFromGraph(
  graph: { nodes: DesignerGraphNode[]; edges: DesignerGraphEdge[] },
  nodeIds: Iterable<string>,
): { nodes: DesignerGraphNode[]; edges: DesignerGraphEdge[] } {
  const removeSet = new Set(
    [...nodeIds].map((id) => String(id || '').trim()).filter(Boolean),
  );
  if (removeSet.size === 0) {
    return { nodes: graph.nodes, edges: graph.edges };
  }
  const nodes = graph.nodes
    .filter((node) => !removeSet.has(node.id))
    .map((node) => {
      const inputs = Array.isArray(node.config?.inputs) ? node.config.inputs.map(String) : null;
      if (!inputs) return node;
      const nextInputs = inputs.filter((id) => !removeSet.has(id));
      if (nextInputs.length === inputs.length) return node;
      return {
        ...node,
        config: {
          ...(node.config ?? {}),
          inputs: nextInputs,
        } as DesignerGraphNode['config'],
      };
    });
  const edges = graph.edges.filter(
    (edge) => !removeSet.has(edge.source) && !removeSet.has(edge.target),
  );
  return { nodes, edges };
}

export function buildManualDesignerNode(params: {
  template: DesignerAddTemplate;
  existing: DesignerGraphNode[];
  position: { x: number; y: number };
  upload?: { filename: string; asset_id?: string; mime_type?: string };
  outputRef?: DesignerGraphNode['output_ref'];
}): DesignerGraphNode {
  const index = nextTypeIndex(params.existing, params.template.type);
  const label = `${params.template.label} ${index}`;
  const interactionMode = params.upload
    ? 'upload'
    : params.template.type === DESIGNER_NODE_TYPE_TEXT || params.template.type === DESIGNER_NODE_TYPE_TABLE
      ? 'edit'
      : 'generate';
  const node: DesignerGraphNode = {
    id: uniqueDesignerNodeId(params.existing.map((item) => item.id), params.template.role),
    type: params.template.type,
    label,
    config: {
      role: params.template.role,
      delegate: DESIGNER_CONFIG_DELEGATE_HANDLER,
      interaction_mode: interactionMode,
      ...(params.upload ? { upload: params.upload } : {}),
    },
    layout: {
      x: params.position.x,
      y: params.position.y,
      width: DESIGNER_CANVAS_NODE_WIDTH,
      height: DESIGNER_CANVAS_NODE_HEIGHT,
    },
  };
  if (params.outputRef) {
    node.output_ref = params.outputRef;
  }
  return node;
}

export function templateForAssetKind(kind: DesignerLibraryAsset['kind']): DesignerAddTemplate {
  if (kind === 'video') {
    return DESIGNER_ADD_TEMPLATES.find((item) => item.id === 'video') as DesignerAddTemplate;
  }
  if (kind === 'audio') {
    return DESIGNER_ADD_TEMPLATES.find((item) => item.id === 'audio') as DesignerAddTemplate;
  }
  return DESIGNER_ADD_TEMPLATES.find((item) => item.id === 'image') as DesignerAddTemplate;
}

export function buildNodeFromLibraryAsset(params: {
  asset: DesignerLibraryAsset;
  existing: DesignerGraphNode[];
  position: { x: number; y: number };
}): DesignerGraphNode {
  const template = templateForAssetKind(params.asset.kind);
  return buildManualDesignerNode({
    template,
    existing: params.existing,
    position: params.position,
    upload: {
      filename: params.asset.filename,
      asset_id: params.asset.id,
      mime_type: params.asset.mime_type,
    },
    outputRef: {
      kind: template.type,
      uri: params.asset.objectUrl,
      mime_type: params.asset.mime_type,
      label: params.asset.filename,
    },
  });
}
