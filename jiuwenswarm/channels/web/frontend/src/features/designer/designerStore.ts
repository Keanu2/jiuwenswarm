import { create } from 'zustand';
import {
  graphForBootstrapThinking,
  isDesignerPreviewGraph,
} from './designerBootstrapGraph';
import { designerGraphClient } from './designerGraphClient';
import {
  rememberDesignerGraphId,
  resolveDesignerGraphToLoad,
  uniqueDesignerGraphIds,
} from './designerGraphLoad';
import type { DesignerReactFlowGraph } from './designerGraphAdapter';
import {
  autoLayoutDesignerGraph,
  connectNodeToGraph,
  removeNodesFromGraph,
} from './designerCanvasNodes';
import { useDesignerUiStore } from './designerUiStore';
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
const deletedNodeIds = new Set<string>();

function isShotPipelineNodeId(nodeId: string): boolean {
  return (
    nodeId === 'n_frame' ||
    nodeId === 'n_clip' ||
    nodeId.startsWith('n_frame_') ||
    nodeId.startsWith('n_clip_')
  );
}

function rememberDeletedNodeIds(nodeIds: Iterable<string>): void {
  for (const id of nodeIds) {
    const trimmed = String(id || '').trim();
    if (trimmed) deletedNodeIds.add(trimmed);
  }
}

function stripRememberedDeletedNodes(graph: DesignerExecutionGraph): DesignerExecutionGraph {
  if (deletedNodeIds.size === 0) return graph;
  const nodes = graph.nodes.filter((node) => !deletedNodeIds.has(node.id));
  if (nodes.length === graph.nodes.length) {
    for (const id of [...deletedNodeIds]) {
      if (!graph.nodes.some((node) => node.id === id)) deletedNodeIds.delete(id);
    }
    return graph;
  }
  const keep = new Set(nodes.map((node) => node.id));
  return {
    ...graph,
    nodes,
    edges: graph.edges.filter((edge) => keep.has(edge.source) && keep.has(edge.target)),
  };
}

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
  addNode: (node: DesignerGraphNode) => void;
  addConnectedNode: (sourceId: string, node: DesignerGraphNode) => void;
  addEdge: (connection: { source: string; target: string; id?: string; label?: string }) => void;
  removeEdges: (edgeIds: string[]) => void;
  removeNodes: (nodeIds: string[]) => void;
  persistReactFlowLayout: (reactFlow: DesignerReactFlowGraph) => void;
  autoLayout: () => void;
  updateNodeLayoutSize: (
    nodeId: string,
    size: { width: number; height: number },
    options?: { userResized?: boolean },
  ) => void;
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
    loadSeq += 1;
    saveSeq += 1;
    const preview = graphForBootstrapThinking(prompt);
    useDesignerUiStore.getState().reset();
    set({
      graphId: preview.graph_id,
      domainGraph: preview,
      loadStatus: 'ready',
      loadError: null,
      bootstrapInProgress: true,
      selectedNodeId: null,
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
      domainGraph: stripRememberedDeletedNodes(graph),
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
        domainGraph: stripRememberedDeletedNodes(graph),
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

  addNode: (node) => {
    const graph = get().domainGraph;
    if (!graph) return;
    const id = String(node.id || '').trim();
    if (!id || graph.nodes.some((item) => item.id === id)) return;
    set({
      domainGraph: {
        ...graph,
        nodes: [...graph.nodes, node],
        updated_at: Date.now(),
      },
      selectedNodeId: id,
    });
    get().scheduleSave();
  },

  addConnectedNode: (sourceId, node) => {
    const graph = get().domainGraph;
    if (!graph) return;
    const next = connectNodeToGraph(graph, sourceId, node);
    if (!next) return;
    set({
      domainGraph: {
        ...graph,
        nodes: next.nodes,
        edges: next.edges,
        updated_at: Date.now(),
      },
      selectedNodeId: node.id,
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

  removeNodes: (nodeIds) => {
    const graph = get().domainGraph;
    if (!graph || nodeIds.length === 0) return;
    const next = removeNodesFromGraph(graph, nodeIds);
    if (next.nodes.length === graph.nodes.length && next.edges.length === graph.edges.length) {
      return;
    }
    const removed = new Set(nodeIds.map((id) => String(id || '').trim()).filter(Boolean));
    rememberDeletedNodeIds(removed);
    const selectedNodeId = get().selectedNodeId;
    const removedShot = [...removed].some(isShotPipelineNodeId);
    set({
      domainGraph: {
        ...graph,
        nodes: next.nodes,
        edges: next.edges,
        updated_at: Date.now(),
        metadata: {
          ...(graph.metadata ?? {}),
          ...(removedShot
            ? { freeze_shot_topology: true, user_topology_edit: true }
            : {}),
        },
      },
      selectedNodeId:
        selectedNodeId && removed.has(selectedNodeId) ? null : selectedNodeId,
    });
    const ui = useDesignerUiStore.getState();
    const materialId = ui.selectedMaterialId;
    if (
      [...removed].some(
        (id) => materialId === id || materialId.startsWith(`${id}:`),
      )
    ) {
      ui.closeViewer();
    }
    if (removed.has(ui.chooserNodeId)) {
      ui.closeRevision();
    }
    get().scheduleSave();
  },

  persistReactFlowLayout: (reactFlow) => {
    const graph = get().domainGraph;
    if (!graph || get().bootstrapInProgress || isDesignerPreviewGraph(graph)) return;
    const rfById = new Map(reactFlow.nodes.map((node) => [node.id, node]));
    const nodes = graph.nodes.map((node) => {
      const rfNode = rfById.get(node.id);
      if (!rfNode) return node;
      // Drag persist only writes position. Media nodes own width/height via
      // updateNodeLayoutSize so a stale RF style cannot flatten portrait cards.
      return {
        ...node,
        layout: {
          x: rfNode.position.x,
          y: rfNode.position.y,
          ...(typeof node.layout?.width === 'number' ? { width: node.layout.width } : {}),
          ...(typeof node.layout?.height === 'number' ? { height: node.layout.height } : {}),
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

  autoLayout: () => {
    const graph = get().domainGraph;
    if (!graph || get().bootstrapInProgress || isDesignerPreviewGraph(graph)) return;
    if (graph.nodes.length === 0) return;
    const next = autoLayoutDesignerGraph(graph);
    if (next === graph) return;
    set({
      domainGraph: {
        ...next,
        updated_at: Date.now(),
      },
    });
    get().scheduleSave();
  },

  updateNodeLayoutSize: (nodeId, size, options) => {
    const graph = get().domainGraph;
    if (!graph) return;
    const width = Math.round(size.width);
    const height = Math.round(size.height);
    if (width < 80 || height < 80) return;
    const lockSize = Boolean(options?.userResized);
    let changed = false;
    const nodes = graph.nodes.map((node) => {
      if (node.id !== nodeId) return node;
      const currentWidth = node.layout?.width;
      const currentHeight = node.layout?.height;
      const alreadyLocked = node.config?.user_resized === true;
      const sameSize =
        typeof currentWidth === 'number' &&
        typeof currentHeight === 'number' &&
        Math.abs(currentWidth - width) < 4 &&
        Math.abs(currentHeight - height) < 4;
      if (sameSize && (!lockSize || alreadyLocked)) {
        return node;
      }
      changed = true;
      return {
        ...node,
        layout: {
          x: node.layout?.x ?? 0,
          y: node.layout?.y ?? 0,
          width,
          height,
        },
        config: lockSize ? { ...(node.config ?? {}), user_resized: true } : node.config,
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
        domainGraph: stripRememberedDeletedNodes(saved),
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
        domainGraph: stripRememberedDeletedNodes(graph),
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
