export const DESIGNER_GRAPH_SCHEMA_VERSION = 'designer-execution-graph.v1' as const;
export const DESIGNER_RUN_SCHEMA_VERSION = 'designer-execution-run.v1' as const;

export const DESIGNER_NODE_TYPE_TEXT = 'text' as const;
export const DESIGNER_NODE_TYPE_TABLE = 'table' as const;
export const DESIGNER_NODE_TYPE_IMAGE = 'image' as const;
export const DESIGNER_NODE_TYPE_VIDEO = 'video' as const;
export const DESIGNER_NODE_TYPE_AUDIO = 'audio' as const;

export const DESIGNER_NODE_TYPES = [
  DESIGNER_NODE_TYPE_TEXT,
  DESIGNER_NODE_TYPE_TABLE,
  DESIGNER_NODE_TYPE_IMAGE,
  DESIGNER_NODE_TYPE_VIDEO,
  DESIGNER_NODE_TYPE_AUDIO,
] as const;

export type DesignerNodeType = (typeof DESIGNER_NODE_TYPES)[number];

export const DESIGNER_NODE_ROLE_BRIEF = 'brief' as const;
export const DESIGNER_NODE_ROLE_CHARACTER_DESIGN = 'character_design' as const;
export const DESIGNER_NODE_ROLE_SCENE = 'scene' as const;
export const DESIGNER_NODE_ROLE_STORYBOARD = 'storyboard' as const;
export const DESIGNER_NODE_ROLE_FRAME = 'frame' as const;
export const DESIGNER_NODE_ROLE_CLIP = 'clip' as const;
export const DESIGNER_NODE_ROLE_COMPOSE = 'compose' as const;
export const DESIGNER_NODE_ROLE_MUSIC = 'music' as const;
export const DESIGNER_NODE_ROLE_SPEECH = 'speech' as const;

export const DESIGNER_NODE_ROLES = [
  DESIGNER_NODE_ROLE_BRIEF,
  DESIGNER_NODE_ROLE_CHARACTER_DESIGN,
  DESIGNER_NODE_ROLE_SCENE,
  DESIGNER_NODE_ROLE_STORYBOARD,
  DESIGNER_NODE_ROLE_FRAME,
  DESIGNER_NODE_ROLE_CLIP,
  DESIGNER_NODE_ROLE_COMPOSE,
  DESIGNER_NODE_ROLE_MUSIC,
  DESIGNER_NODE_ROLE_SPEECH,
] as const;

export type DesignerNodeRole = (typeof DESIGNER_NODE_ROLES)[number];

export const DESIGNER_CONFIG_DELEGATE_HANDLER = 'handler' as const;
export const DESIGNER_CONFIG_DELEGATE_SUBAGENT = 'subagent' as const;
export const DESIGNER_CONFIG_DELEGATE_AGENT = 'agent' as const;

export const DESIGNER_CONFIG_DELEGATES = [
  DESIGNER_CONFIG_DELEGATE_HANDLER,
  DESIGNER_CONFIG_DELEGATE_SUBAGENT,
  DESIGNER_CONFIG_DELEGATE_AGENT,
] as const;

export type DesignerConfigDelegate = (typeof DESIGNER_CONFIG_DELEGATES)[number];

export const DESIGNER_AGENT_GROUP_NAME = 'designer' as const;

export const DESIGNER_ROLE_DEFAULT_TEMPLATES = {
  [DESIGNER_NODE_ROLE_BRIEF]: 'designer/leader',
  [DESIGNER_NODE_ROLE_CHARACTER_DESIGN]: 'designer/character',
  [DESIGNER_NODE_ROLE_SCENE]: 'designer/scene',
  [DESIGNER_NODE_ROLE_STORYBOARD]: 'designer/storyboard',
  [DESIGNER_NODE_ROLE_FRAME]: 'designer/frame',
  [DESIGNER_NODE_ROLE_CLIP]: 'designer/clip',
  [DESIGNER_NODE_ROLE_COMPOSE]: 'designer/clip',
} as const;

export const DESIGNER_NODE_CONFIG_KEYS = [
  'role',
  'prompt',
  'inputs',
  'delegate',
  'agent_template',
  'collaborate',
  'generate',
  'upload',
  'edit',
  'interaction_mode',
  'materials',
] as const;

export type DesignerMediaGenerateConfig = {
  prompt?: string;
  prompt_origin?: 'storyboard' | 'user';
  aspect_ratio?: string;
  resolution?: string;
  duration?: string;
  has_audio?: boolean;
  count?: number;
};

export type DesignerMediaUploadConfig = {
  filename?: string;
  asset_id?: string;
  mime_type?: string;
};

export type DesignerMediaEditConfig = {
  content?: string;
};

export type DesignerMediaMaterialSlot = {
  id: string;
  label?: string;
  filename?: string;
  mime_type?: string;
  asset_id?: string;
};

type DesignerRoleConfig<R extends DesignerNodeRole> = {
  role: R;
  prompt?: string;
  inputs?: string[];
  delegate?: DesignerConfigDelegate;
  agent_template?: string;
  collaborate?: boolean;
  generate?: DesignerMediaGenerateConfig;
  upload?: DesignerMediaUploadConfig;
  edit?: DesignerMediaEditConfig;
  interaction_mode?: 'generate' | 'upload' | 'edit';
  materials?: DesignerMediaMaterialSlot[];
  shot_index?: number;
};

export type DesignerNodeConfig =
  | DesignerRoleConfig<typeof DESIGNER_NODE_ROLE_BRIEF>
  | DesignerRoleConfig<typeof DESIGNER_NODE_ROLE_CHARACTER_DESIGN>
  | DesignerRoleConfig<typeof DESIGNER_NODE_ROLE_SCENE>
  | DesignerRoleConfig<typeof DESIGNER_NODE_ROLE_STORYBOARD>
  | DesignerRoleConfig<typeof DESIGNER_NODE_ROLE_FRAME>
  | DesignerRoleConfig<typeof DESIGNER_NODE_ROLE_CLIP>
  | DesignerRoleConfig<typeof DESIGNER_NODE_ROLE_COMPOSE>
  | DesignerRoleConfig<typeof DESIGNER_NODE_ROLE_MUSIC>
  | DesignerRoleConfig<typeof DESIGNER_NODE_ROLE_SPEECH>
  | {
      role?: string;
      prompt?: string;
      inputs?: string[];
      delegate?: string;
      agent_template?: string;
      collaborate?: boolean;
      generate?: DesignerMediaGenerateConfig;
      upload?: DesignerMediaUploadConfig;
      edit?: DesignerMediaEditConfig;
      interaction_mode?: string;
      materials?: DesignerMediaMaterialSlot[];
      shot_index?: number;
    };

export const DESIGNER_EDGE_KIND_DATA = 'data' as const;
export const DESIGNER_EDGE_KIND_SYNC = 'sync' as const;

export const DESIGNER_EDGE_KINDS = [DESIGNER_EDGE_KIND_DATA, DESIGNER_EDGE_KIND_SYNC] as const;

export type DesignerEdgeKind = (typeof DESIGNER_EDGE_KINDS)[number];

export const DESIGNER_GRAPH_SOURCE_PROMPT = 'prompt' as const;
export const DESIGNER_GRAPH_SOURCE_MANUAL = 'manual' as const;

export const DESIGNER_NODE_STATUS_PENDING = 'pending' as const;
export const DESIGNER_NODE_STATUS_RUNNING = 'running' as const;
export const DESIGNER_NODE_STATUS_COMPLETED = 'completed' as const;
export const DESIGNER_NODE_STATUS_FAILED = 'failed' as const;
export const DESIGNER_NODE_STATUS_CANCELLED = 'cancelled' as const;

export const DESIGNER_RUN_STATUS_DRAFT = 'draft' as const;
export const DESIGNER_RUN_STATUS_RUNNING = 'running' as const;
export const DESIGNER_RUN_STATUS_PAUSED = 'paused' as const;
export const DESIGNER_RUN_STATUS_COMPLETED = 'completed' as const;
export const DESIGNER_RUN_STATUS_FAILED = 'failed' as const;
export const DESIGNER_RUN_STATUS_CANCELLED = 'cancelled' as const;

export type AssetRef = {
  kind: string;
  uri: string;
  mime_type?: string;
  label?: string;
};

export type NodeLayout = {
  x?: number;
  y?: number;
  width?: number;
  height?: number;
};

export type DesignerGraphNode = {
  id: string;
  type: DesignerNodeType | string;
  label: string;
  config?: DesignerNodeConfig;
  layout?: NodeLayout;
  output_ref?: AssetRef | null;
};

export type DesignerGraphEdge = {
  id: string;
  source: string;
  target: string;
  kind?: DesignerEdgeKind | string;
  label?: string;
};

export type DesignerGraphPatch = {
  title?: string;
  description?: string;
  upsert_nodes?: DesignerGraphNode[];
  upsert_edges?: DesignerGraphEdge[];
  remove_node_ids?: string[];
  remove_edge_ids?: string[];
};

export type DesignerExecutionGraph = {
  schema_version: typeof DESIGNER_GRAPH_SCHEMA_VERSION | string;
  graph_id: string;
  project_id: string;
  title: string;
  description?: string;
  source?: typeof DESIGNER_GRAPH_SOURCE_PROMPT | typeof DESIGNER_GRAPH_SOURCE_MANUAL | string;
  nodes: DesignerGraphNode[];
  edges: DesignerGraphEdge[];
  metadata?: Record<string, unknown>;
  created_at?: number;
  updated_at?: number;
};

export type DesignerNodeState = {
  status: string;
  started_at?: number | null;
  completed_at?: number | null;
  output_ref?: AssetRef | null;
  output_refs?: AssetRef[] | null;
  candidate_output_ref?: AssetRef | null;
  candidate_output_refs?: AssetRef[] | null;
  error?: string | null;
  blocked_by?: string[];
};

export type DesignerExecutionRun = {
  schema_version: typeof DESIGNER_RUN_SCHEMA_VERSION | string;
  run_id: string;
  graph_id: string;
  project_id: string;
  status: string;
  node_states: Record<string, DesignerNodeState>;
  current_node_ids: string[];
  created_at?: number;
  updated_at?: number;
};

export type DesignerGraphSummary = {
  graph_id: string;
  project_id: string;
  title: string;
  updated_at?: number;
  run_id?: string | null;
  run_status?: string | null;
  has_video: boolean;
  clip_label?: string | null;
};

export type DesignerGraphBootstrapResult = {
  graph: DesignerExecutionGraph;
  project_id: string;
  project?: {
    project_id: string;
    project_dir: string;
    restored: boolean;
    work_mode: string;
  };
};
