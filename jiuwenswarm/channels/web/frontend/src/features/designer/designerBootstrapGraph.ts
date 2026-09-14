import {
  DESIGNER_EDGE_KIND_DATA,
  DESIGNER_EDGE_KIND_SYNC,
  DESIGNER_GRAPH_SCHEMA_VERSION,
  DESIGNER_GRAPH_SOURCE_PROMPT,
  DESIGNER_NODE_ROLE_BRIEF,
  DESIGNER_NODE_ROLE_CHARACTER_DESIGN,
  DESIGNER_NODE_ROLE_CLIP,
  DESIGNER_NODE_ROLE_COMPOSE,
  DESIGNER_NODE_ROLE_FRAME,
  DESIGNER_NODE_ROLE_SCENE,
  DESIGNER_NODE_ROLE_STORYBOARD,
  DESIGNER_NODE_TYPE_IMAGE,
  DESIGNER_NODE_TYPE_TABLE,
  DESIGNER_NODE_TYPE_TEXT,
  DESIGNER_NODE_TYPE_VIDEO,
  type DesignerExecutionGraph,
} from './executionGraphTypes';

export const DESIGNER_PREVIEW_GRAPH_ID = 'preview_bootstrap';

export function isDesignerPreviewGraph(
  graph: Pick<DesignerExecutionGraph, 'graph_id'> | null | undefined,
): boolean {
  const id = String(graph?.graph_id || '');
  return id === DESIGNER_PREVIEW_GRAPH_ID || id.startsWith('preview_');
}

/** Local canvas skeleton shown immediately while bootstrap / generation APIs run. */
export function buildDesignerBootstrapPreviewGraph(prompt = ''): DesignerExecutionGraph {
  const now = Date.now();
  const promptText = prompt.trim();
  return {
    schema_version: DESIGNER_GRAPH_SCHEMA_VERSION,
    graph_id: DESIGNER_PREVIEW_GRAPH_ID,
    project_id: '',
    title: promptText.slice(0, 80) || 'Design',
    description: promptText,
    source: DESIGNER_GRAPH_SOURCE_PROMPT,
    nodes: [
      {
        id: 'n_brief',
        type: DESIGNER_NODE_TYPE_TEXT,
        label: 'Text 1',
        config: {
          role: DESIGNER_NODE_TYPE_TEXT,
          pipeline: DESIGNER_NODE_ROLE_BRIEF,
          prompt: promptText,
        },
        layout: { x: 40, y: 240, width: 280, height: 160 },
      },
      {
        id: 'n_character',
        type: DESIGNER_NODE_TYPE_IMAGE,
        label: 'Image 1',
        config: {
          role: DESIGNER_NODE_TYPE_IMAGE,
          pipeline: DESIGNER_NODE_ROLE_CHARACTER_DESIGN,
          inputs: ['n_brief'],
        },
        layout: { x: 400, y: 40, width: 280, height: 160 },
      },
      {
        id: 'n_scene',
        type: DESIGNER_NODE_TYPE_IMAGE,
        label: 'Image 2',
        config: {
          role: DESIGNER_NODE_TYPE_IMAGE,
          pipeline: DESIGNER_NODE_ROLE_SCENE,
          inputs: ['n_brief'],
        },
        layout: { x: 400, y: 240, width: 280, height: 160 },
      },
      {
        id: 'n_storyboard',
        type: DESIGNER_NODE_TYPE_TABLE,
        label: 'Table 1',
        config: {
          role: DESIGNER_NODE_TYPE_TABLE,
          pipeline: DESIGNER_NODE_ROLE_STORYBOARD,
          inputs: ['n_brief'],
        },
        layout: { x: 400, y: 440, width: 280, height: 160 },
      },
      {
        id: 'n_frame_1',
        type: DESIGNER_NODE_TYPE_IMAGE,
        label: 'Image 3',
        config: {
          role: DESIGNER_NODE_TYPE_IMAGE,
          pipeline: DESIGNER_NODE_ROLE_FRAME,
          shot_index: 1,
          inputs: ['n_character', 'n_scene', 'n_storyboard'],
        },
        layout: { x: 760, y: 240, width: 280, height: 160 },
      },
      {
        id: 'n_clip_1',
        type: DESIGNER_NODE_TYPE_VIDEO,
        label: 'Video 1',
        config: {
          role: DESIGNER_NODE_TYPE_VIDEO,
          pipeline: DESIGNER_NODE_ROLE_CLIP,
          shot_index: 1,
          inputs: ['n_character', 'n_scene', 'n_storyboard', 'n_frame_1'],
        },
        layout: { x: 1120, y: 240, width: 280, height: 160 },
      },
      {
        id: 'n_compose',
        type: DESIGNER_NODE_TYPE_VIDEO,
        label: 'Video 2',
        config: {
          role: DESIGNER_NODE_TYPE_VIDEO,
          pipeline: DESIGNER_NODE_ROLE_COMPOSE,
          inputs: ['n_clip_1'],
        },
        layout: { x: 1480, y: 240, width: 280, height: 160 },
      },
    ],
    edges: [
      {
        id: 'e_brief_character',
        source: 'n_brief',
        target: 'n_character',
        kind: DESIGNER_EDGE_KIND_DATA,
      },
      {
        id: 'e_brief_scene',
        source: 'n_brief',
        target: 'n_scene',
        kind: DESIGNER_EDGE_KIND_DATA,
      },
      {
        id: 'e_brief_storyboard',
        source: 'n_brief',
        target: 'n_storyboard',
        kind: DESIGNER_EDGE_KIND_DATA,
      },
      {
        id: 'e_character_storyboard',
        source: 'n_character',
        target: 'n_storyboard',
        kind: DESIGNER_EDGE_KIND_SYNC,
        label: 'Align',
      },
      {
        id: 'e_scene_storyboard',
        source: 'n_scene',
        target: 'n_storyboard',
        kind: DESIGNER_EDGE_KIND_SYNC,
        label: 'Align',
      },
      {
        id: 'e_character_n_frame_1',
        source: 'n_character',
        target: 'n_frame_1',
        kind: DESIGNER_EDGE_KIND_DATA,
      },
      {
        id: 'e_scene_n_frame_1',
        source: 'n_scene',
        target: 'n_frame_1',
        kind: DESIGNER_EDGE_KIND_DATA,
      },
      {
        id: 'e_storyboard_n_frame_1',
        source: 'n_storyboard',
        target: 'n_frame_1',
        kind: DESIGNER_EDGE_KIND_DATA,
      },
      {
        id: 'e_character_n_clip_1',
        source: 'n_character',
        target: 'n_clip_1',
        kind: DESIGNER_EDGE_KIND_DATA,
      },
      {
        id: 'e_scene_n_clip_1',
        source: 'n_scene',
        target: 'n_clip_1',
        kind: DESIGNER_EDGE_KIND_DATA,
      },
      {
        id: 'e_storyboard_n_clip_1',
        source: 'n_storyboard',
        target: 'n_clip_1',
        kind: DESIGNER_EDGE_KIND_DATA,
      },
      {
        id: 'e_n_frame_1_n_clip_1',
        source: 'n_frame_1',
        target: 'n_clip_1',
        kind: DESIGNER_EDGE_KIND_DATA,
      },
      {
        id: 'e_n_clip_1_compose',
        source: 'n_clip_1',
        target: 'n_compose',
        kind: DESIGNER_EDGE_KIND_DATA,
      },
    ],
    metadata: { bootstrap: 'designer.graph.bootstrap.preview' },
    created_at: now,
    updated_at: now,
  };
}
