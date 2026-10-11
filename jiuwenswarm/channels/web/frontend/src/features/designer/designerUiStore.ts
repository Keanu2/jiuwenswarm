import { create } from 'zustand';
import type { DesignerCanvasTool, DesignerDockPanel } from './designerCanvasNodes';

type DesignerUiStore = {
  selectedMaterialId: string;
  viewerOpen: boolean;
  chooserNodeId: string;
  editRequestKey: number;
  canvasTool: DesignerCanvasTool;
  dockPanel: DesignerDockPanel;
  successorMenuNodeId: string | null;
  chatSidebarOpen: boolean;
  assetsSidebarOpen: boolean;
  minimapVisible: boolean;
  zoomPercent: number;
  layoutRequestId: number;
  setCanvasTool: (tool: DesignerCanvasTool) => void;
  setDockPanel: (panel: DesignerDockPanel) => void;
  toggleSuccessorMenu: (nodeId: string) => void;
  closeDock: () => void;
  toggleChatSidebar: () => void;
  setChatSidebarOpen: (open: boolean) => void;
  toggleAssetsSidebar: () => void;
  setAssetsSidebarOpen: (open: boolean) => void;
  toggleMinimap: () => void;
  setZoomPercent: (percent: number) => void;
  requestAutoLayout: () => void;
  inspectNode: (nodeId: string, materialIndex?: number) => void;
  startEdit: (materialId: string) => void;
  openViewer: (id: string) => void;
  closeViewer: () => void;
  openRevision: (nodeId: string) => void;
  closeRevision: () => void;
  setSelectedMaterialId: (id: string) => void;
  reset: () => void;
};

const initialState = {
  selectedMaterialId: '',
  viewerOpen: false,
  chooserNodeId: '',
  editRequestKey: 0,
  canvasTool: 'select' as DesignerCanvasTool,
  dockPanel: null as DesignerDockPanel,
  successorMenuNodeId: null as string | null,
  chatSidebarOpen: true,
  assetsSidebarOpen: false,
  minimapVisible: true,
  zoomPercent: 100,
  layoutRequestId: 0,
};

export const useDesignerUiStore = create<DesignerUiStore>((set) => ({
  ...initialState,

  inspectNode: (nodeId, materialIndex) =>
    set({
      selectedMaterialId:
        typeof materialIndex === 'number' ? `${nodeId}:${materialIndex}` : nodeId,
      viewerOpen: true,
    }),

  startEdit: (materialId) =>
    set((state) => ({
      selectedMaterialId: materialId,
      viewerOpen: true,
      editRequestKey: state.editRequestKey + 1,
    })),

  openViewer: (id) => set({ selectedMaterialId: id, viewerOpen: true }),

  closeViewer: () => set({ viewerOpen: false }),

  openRevision: (nodeId) => set({ chooserNodeId: nodeId }),

  closeRevision: () => set({ chooserNodeId: '' }),

  setSelectedMaterialId: (id) => set({ selectedMaterialId: id }),

  setCanvasTool: (tool) => set({ canvasTool: tool, dockPanel: null, successorMenuNodeId: null }),

  setDockPanel: (panel) =>
    set((state) => ({
      dockPanel: state.dockPanel === panel ? null : panel,
      successorMenuNodeId: null,
    })),

  toggleSuccessorMenu: (nodeId) =>
    set((state) => ({
      successorMenuNodeId: state.successorMenuNodeId === nodeId ? null : nodeId,
      dockPanel: null,
    })),

  closeDock: () => set({ dockPanel: null, successorMenuNodeId: null }),

  toggleChatSidebar: () => set((state) => ({ chatSidebarOpen: !state.chatSidebarOpen })),

  setChatSidebarOpen: (open) => set({ chatSidebarOpen: open }),

  toggleAssetsSidebar: () => set((state) => ({ assetsSidebarOpen: !state.assetsSidebarOpen })),

  setAssetsSidebarOpen: (open) => set({ assetsSidebarOpen: open }),

  toggleMinimap: () => set((state) => ({ minimapVisible: !state.minimapVisible })),

  setZoomPercent: (percent) => set({ zoomPercent: percent }),

  requestAutoLayout: () => set((state) => ({ layoutRequestId: state.layoutRequestId + 1 })),

  reset: () => set({ ...initialState }),
}));
