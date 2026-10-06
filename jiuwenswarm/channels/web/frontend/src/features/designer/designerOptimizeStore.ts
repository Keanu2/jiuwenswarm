import { create } from 'zustand';

export type DesignerOptimizeMode = 'cost' | 'quality';

type DesignerOptimizeState = {
  optimizeFor: DesignerOptimizeMode;
  setOptimizeFor: (mode: DesignerOptimizeMode) => void;
};

export const useDesignerOptimizeStore = create<DesignerOptimizeState>((set) => ({
  optimizeFor: 'quality',
  setOptimizeFor: (mode) => set({ optimizeFor: mode }),
}));
