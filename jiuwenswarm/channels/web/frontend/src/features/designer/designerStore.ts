import { create } from 'zustand';
import {
  buildDesignerBootstrapPreviewGraph,
  isDesignerPreviewGraph,
} from './designerBootstrapGraph';
import { designerGraphClient } from './designerGraphClient';
import {
  rememberDesignerGraphId,
  resolveDesignerGraphToLoad,
  uniqueDesignerGraphIds,
} from './designerGraphLoad';
import type { DesignerReactFlowGraph } from './designerGraphAdapter';
import type { AssetRef, DesignerExecutionGraph, DesignerGraphNode } from './executionGraphTypes';

export type DesignerLoadStatus =
  | 'idle'
  | 'loading'
  | 'bootstrapping'
  | 'ready'
  | 'empty'
  | 'error';

const SAVE_DEBOUNCE_MS = 500;

let saveTimer: ReturnType<typeof setTimeout> | null = null;
let saveSeq = 0;
let loadSeq = 0;

type DesignerStore = {
  graphId: string | null;
  domainGraph: DesignerExecutionGraph | null;
  loadStatus: DesignerLoadStatus;
  loadError: string | null;
  /** True while Tasks→Design bootstrap owns the page load (blocks list/get race). */
  bootstrapInProgress: boolean;
  selectedNodeId: string | null;
  saveStatus: 'idle' | 'saving' | 'saved' | 'error';
  setSelectedNodeId: (nodeId: string | null) => void;
  loadForProject: (projectId: string | undefined) => Promise<void>;
  loadGraph: (graphId: string) => Promise<void>;
  beginBootstrapEntry: (prompt?: string) => void;
  failBootstrapEntry: (message: string) => void;
  applyGraph: (graph: DesignerExecutionGraph) => void;
  updateNodeConfig: (
    nodeId: string,
    updater: (config: Record<string, unknown>) => Record<string, unknown>,
  ) => void;
  setNodeOutputRef: (nodeId: string, outputRef: AssetRef | null) => void;
  clearAssetReferences: (assetId: string) => void;
  addEdge: (connection: { source: string; target: string; id?: string; label?: string }) => void;
  removeEdges: (edgeIds: string[]) => void;
  persistReactFlowLayout: (reactFlow: DesignerReactFlowGraph) => void;
  scheduleSave: () => void;
  flushSave: () => Promise<void>;
  reset: () => void;
};

const initialState = {
  graphId: null as string | null,
  domainGraph: null as DesignerExecutionGraph | null,
  loadStatus: 'idle' as DesignerLoadStatus,
  loadError: null as string | null,
  bootstrapInProgress: false,
  selectedNodeId: null as string | null,
  saveStatus: 'idle' as DesignerStore['saveStatus'],
};

function clearSaveTimer() {
  if (saveTimer) {
    clearTimeout(saveTimer);
    saveTimer = null;
  }
}

export const useDesignerStore = create<DesignerStore>((set, get) => ({
  ...initialState,

  setSelectedNodeId: (nodeId) => set({ selectedNodeId: nodeId }),

  beginBootstrapEntry: (prompt) => {
    clearSaveTimer();
    const existing = get().domainGraph;
    const keepExisting = Boolean(existing) && !isDesignerPreviewGraph(existing);
    const preview = keepExisting ? existing : buildDesignerBootstrapPreviewGraph(prompt);
    set({
      graphId: preview?.graph_id ?? null,
      domainGraph: preview,
      loadStatus: preview ? 'ready' : 'bootstrapping',
      loadError: null,
      bootstrapInProgress: true,
      selectedNodeId: keepExisting ? get().selectedNodeId : null,
      saveStatus: 'idle',
    });
  },

  failBootstrapEntry: (message) => {
    const graph = get().domainGraph;
    const keep = Boolean(graph) && !isDesignerPreviewGraph(graph);
    set({
      loadStatus: keep ? 'ready' : 'error',
      loadError: message,
      bootstrapInProgress: false,
      ...(keep
        ? {}
        : {
            graphId: null,
            domainGraph: null,
            selectedNodeId: null,
          }),
    });
  },

  applyGraph: (graph) => {
    saveSeq += 1;
    rememberDesignerGraphId(graph.graph_id);
    set({
      graphId: graph.graph_id,
      domainGraph: graph,
      loadStatus: 'ready',
      loadError: null,
      bootstrapInProgress: false,
    });
  },

  loadGraph: async (graphId) => {
    const id = String(graphId ?? '').trim();
    if (!id) return;
    const gen = ++loadSeq;
    const keepGraph = get().domainGraph;
    const sameGraph = keepGraph?.graph_id === id;
    rememberDesignerGraphId(id);
    set({
      graphId: id,
      domainGraph: keepGraph,
      loadStatus: sameGraph ? 'ready' : 'loading',
      loadError: null,
    });
    try {
      const { graph } = await designerGraphClient.get(id);
      if (gen !== loadSeq) return;
      rememberDesignerGraphId(graph.graph_id);
      set({
        graphId: graph.graph_id,
        domainGraph: graph,
        loadStatus: 'ready',
        loadError: null,
        bootstrapInProgress: false,
      });
    } catch (error) {
      if (gen !== loadSeq) return;
      const message = error instanceof Error ? error.message : String(error);
      if (keepGraph && !isDesignerPreviewGraph(keepGraph)) {
        rememberDesignerGraphId(keepGraph.graph_id);
        set({
          graphId: keepGraph.graph_id,
          domainGraph: keepGraph,
          loadStatus: 'ready',
          loadError: message,
        });
        return;
      }
      set({
        loadStatus: 'error',
        loadError: message,
      });
    }
  },

  updateNodeConfig: (nodeId, updater) => {
    const graph = get().domainGraph;
    if (!graph) return;
    const nodes = graph.nodes.map((node) => {
      if (node.id !== nodeId) return node;
      const nextConfig = updater({ ...(node.config ?? {}) } as Record<string, unknown>);
      return { ...node, config: nextConfig as DesignerGraphNode['config'] };
    });
    set({
      domainGraph: {
        ...graph,
        nodes,
        updated_at: Date.now(),
      },
    });
    get().scheduleSave();
  },

  setNodeOutputRef: (nodeId, outputRef) => {
    const graph = get().domainGraph;
    if (!graph) return;
    let changed = false;
    const nodes = graph.nodes.map((node) => {
      if (node.id !== nodeId) return node;
      changed = true;
      return { ...node, output_ref: outputRef };
    });
    if (!changed) return;
    set({
      domainGraph: {
        ...graph,
        nodes,
        updated_at: Date.now(),
      },
    });
    get().scheduleSave();
  },

  clearAssetReferences: (assetId) => {
    const graph = get().domainGraph;
    if (!graph || !assetId) return;
    let changed = false;
    const nodes = graph.nodes.map((node) => {
      const config = { ...(node.config ?? {}) };
      const upload = (config.upload ?? null) as
        | { asset_id?: string; filename?: string; mime_type?: string }
        | null;
      const uploadMatched = upload?.asset_id === assetId;
      let touched = false;

      if (uploadMatched && upload) {
        config.upload = {
          ...upload,
          asset_id: '',
          filename: '',
          mime_type: '',
        };
        touched = true;
      }

      if (Array.isArray(config.materials)) {
        const filtered = config.materials.filter((item) => {
          if (!item || typeof item !== 'object') return true;
          return (item as { asset_id?: string }).asset_id !== assetId;
        });
        if (filtered.length !== config.materials.length) {
          config.materials = filtered;
          touched = true;
        }
      }

      if (!touched) return node;
      changed = true;
      return {
        ...node,
        config,
        ...(uploadMatched ? { output_ref: null } : {}),
      };
    });
    if (!changed) return;
    set({
      domainGraph: {
        ...graph,
        nodes,
        updated_at: Date.now(),
      },
    });
    get().scheduleSave();
  },

  addEdge: (connection) => {
    const graph = get().domainGraph;
    if (!graph) return;
    const source = String(connection.source ?? '').trim();
    const target = String(connection.target ?? '').trim();
    if (!source || !target) return;
    const nodeIds = new Set(graph.nodes.map((node) => node.id));
    if (!nodeIds.has(source) || !nodeIds.has(target)) return;
    const duplicate = graph.edges.some(
      (edge) => edge.source === source && edge.target === target,
    );
    if (duplicate) return;
    const id =
      String(connection.id ?? '').trim() ||
      `e_${source}_${target}_${Date.now().toString(36)}`;
    const nextEdge = {
      id,
      source,
      target,
      ...(connection.label ? { label: connection.label } : {}),
    };
    set({
      domainGraph: {
        ...graph,
        edges: [...graph.edges, nextEdge],
        updated_at: Date.now(),
      },
    });
    get().scheduleSave();
  },

  removeEdges: (edgeIds) => {
    const graph = get().domainGraph;
    if (!graph || edgeIds.length === 0) return;
    const removeSet = new Set(edgeIds);
    const edges = graph.edges.filter((edge) => !removeSet.has(edge.id));
    if (edges.length === graph.edges.length) return;
    set({
      domainGraph: {
        ...graph,
        edges,
        updated_at: Date.now(),
      },
    });
    get().scheduleSave();
  },

  persistReactFlowLayout: (reactFlow) => {
    const graph = get().domainGraph;
    if (!graph) return;
    const rfById = new Map(reactFlow.nodes.map((node) => [node.id, node]));
    const nodes = graph.nodes.map((node) => {
      const rfNode = rfById.get(node.id);
      if (!rfNode) return node;
      const width =
        typeof rfNode.style?.width === 'number'
          ? rfNode.style.width
          : node.layout?.width;
      const height =
        typeof rfNode.style?.height === 'number'
          ? rfNode.style.height
          : node.layout?.height;
      return {
        ...node,
        layout: {
          x: rfNode.position.x,
          y: rfNode.position.y,
          ...(typeof width === 'number' ? { width } : {}),
          ...(typeof height === 'number' ? { height } : {}),
        },
      };
    });
    set({
      domainGraph: {
        ...graph,
        nodes,
        updated_at: Date.now(),
      },
    });
    get().scheduleSave();
  },

  scheduleSave: () => {
    clearSaveTimer();
    saveTimer = setTimeout(() => {
      void get().flushSave();
    }, SAVE_DEBOUNCE_MS);
  },

  flushSave: async () => {
    clearSaveTimer();
    const graph = get().domainGraph;
    if (!graph || get().bootstrapInProgress || isDesignerPreviewGraph(graph)) return;
    const seq = ++saveSeq;
    const sentCount = graph.nodes.length;
    const savedGraphId = graph.graph_id;
    set({ saveStatus: 'saving' });
    try {
      const { graph: saved } = await designerGraphClient.save(graph);
      if (seq !== saveSeq) return;
      const live = get().domainGraph;
      if (!live || live.graph_id !== savedGraphId) {
        return;
      }
      if (live.nodes.length > sentCount) {
        set({ saveStatus: 'saved' });
        return;
      }
      rememberDesignerGraphId(saved.graph_id);
      set({
        domainGraph: saved,
        graphId: saved.graph_id,
        saveStatus: 'saved',
      });
    } catch {
      if (seq !== saveSeq) return;
      set({ saveStatus: 'error' });
    }
  },

  reset: () => {
    clearSaveTimer();
    set({ ...initialState });
  },

  loadForProject: async (projectId) => {
    if (get().bootstrapInProgress) {
      return;
    }

    const normalizedProjectId = String(projectId ?? '').trim();
    const graphProjectId = String(get().domainGraph?.project_id ?? '').trim();
    const effectiveProjectId = normalizedProjectId || graphProjectId;
    const previousGraph = get().domainGraph;
    const previousStatus = get().loadStatus;
    const previousGraphId = get().graphId;
    const gen = ++loadSeq;

    // 刷新后内存是空的：没有 project 也不能直接 empty，先按全量列表 / 上次打开的图恢复。
    if (previousStatus === 'ready' && previousGraph && !effectiveProjectId) {
      return;
    }

    set({
      loadStatus: previousGraph ? 'ready' : 'loading',
      loadError: null,
    });

    const collectListedIds = (listed: {
      graphs?: Array<{ graph_id?: string }>;
      summaries?: Array<{ graph_id?: string }>;
    }) =>
      uniqueDesignerGraphIds([
        ...(listed.summaries || []).map((item) => item.graph_id),
        ...(listed.graphs || []).map((item) => item.graph_id),
      ]);

    try {
      let listed = await designerGraphClient.list(effectiveProjectId || undefined);
      if (gen !== loadSeq || get().bootstrapInProgress) {
        return;
      }
      let listedIds = collectListedIds(listed);
      if (effectiveProjectId && listedIds.length === 0) {
        listed = await designerGraphClient.list();
        if (gen !== loadSeq || get().bootstrapInProgress) {
          return;
        }
        listedIds = collectListedIds(listed);
      }
      const targetId = resolveDesignerGraphToLoad({
        currentId: get().graphId,
        isPreview: isDesignerPreviewGraph(get().domainGraph),
        listedIds,
      });
      if (previousGraphId && get().graphId && get().graphId !== previousGraphId && get().graphId !== targetId) {
        return;
      }
      const candidateIds = uniqueDesignerGraphIds([targetId, ...listedIds]);
      if (candidateIds.length === 0) {
        if (previousStatus === 'ready' && previousGraph) {
          set({
            graphId: previousGraphId,
            domainGraph: previousGraph,
            loadStatus: 'ready',
            loadError: null,
          });
          return;
        }
        set({
          graphId: null,
          domainGraph: null,
          loadStatus: 'empty',
          loadError: null,
          selectedNodeId: null,
        });
        return;
      }

      if (get().domainGraph?.graph_id === candidateIds[0] && get().loadStatus === 'ready') {
        rememberDesignerGraphId(candidateIds[0]);
        return;
      }

      let graph: DesignerExecutionGraph | null = null;
      let lastError: unknown;
      for (const id of candidateIds) {
        try {
          const loaded = await designerGraphClient.get(id);
          graph = loaded.graph;
          break;
        } catch (error) {
          lastError = error;
        }
      }
      if (gen !== loadSeq || get().bootstrapInProgress) {
        return;
      }
      if (!graph) {
        throw lastError instanceof Error ? lastError : new Error('graph not found');
      }
      rememberDesignerGraphId(graph.graph_id);
      set({
        graphId: graph.graph_id,
        domainGraph: graph,
        loadStatus: 'ready',
        loadError: null,
      });
    } catch (error) {
      if (gen !== loadSeq || get().bootstrapInProgress) {
        return;
      }
      if (previousStatus === 'ready' && previousGraph) {
        set({
          graphId: previousGraphId,
          domainGraph: previousGraph,
          loadStatus: 'ready',
          loadError: null,
        });
        return;
      }
      set({
        graphId: null,
        domainGraph: null,
        loadStatus: 'error',
        loadError: error instanceof Error ? error.message : String(error),
        selectedNodeId: null,
      });
    }
  },
}));
