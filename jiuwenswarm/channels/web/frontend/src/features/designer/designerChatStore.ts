import { create } from 'zustand';
import { generateUuidV4 } from '../../utils/uuid';
import type { DesignerExecutionGraph } from './executionGraphTypes';
import {
  extractDesignerGraphPrompt,
  hasDesignerUserPrompt,
  persistDesignerChat,
  readPersistedDesignerChat,
  resolveBoundDesignerMessages,
} from './designerChatHistory';

export type DesignerChatRole = 'user' | 'assistant' | 'system';

export type DesignerChatMessageKind =
  | 'user'
  | 'thinking'
  | 'bootstrap_done'
  | 'bootstrap_error'
  | 'not_implemented';

export type DesignerChatMessage = {
  id: string;
  role: DesignerChatRole;
  content: string;
  kind: DesignerChatMessageKind;
  createdAt: number;
};

export type DesignerBootstrapPhase = 'idle' | 'thinking' | 'bootstrapping' | 'done' | 'error';

type DesignerChatStore = {
  activeGraphId: string | null;
  messages: DesignerChatMessage[];
  messagesByGraphId: Record<string, DesignerChatMessage[]>;
  bootstrapPhase: DesignerBootstrapPhase;
  reset: () => void;
  bindGraph: (graphId: string | null) => void;
  appendMessage: (message: Omit<DesignerChatMessage, 'id' | 'createdAt'> & {
    id?: string;
    createdAt?: number;
  }) => string;
  removeMessage: (id: string) => void;
  setBootstrapPhase: (phase: DesignerBootstrapPhase) => void;
  ensureGraphPrompt: (graph: DesignerExecutionGraph | null | undefined, options?: {
    doneText?: string;
  }) => void;
};

function archiveCurrent(
  activeGraphId: string | null,
  messages: DesignerChatMessage[],
  messagesByGraphId: Record<string, DesignerChatMessage[]>,
): Record<string, DesignerChatMessage[]> {
  if (!activeGraphId) return messagesByGraphId;
  return { ...messagesByGraphId, [activeGraphId]: messages };
}

function commitArchive(messagesByGraphId: Record<string, DesignerChatMessage[]>) {
  persistDesignerChat(messagesByGraphId);
  return messagesByGraphId;
}

export const useDesignerChatStore = create<DesignerChatStore>((set, get) => ({
  activeGraphId: null,
  messages: [],
  messagesByGraphId: readPersistedDesignerChat(),
  bootstrapPhase: 'idle',

  reset: () => {
    const { activeGraphId, messages, messagesByGraphId } = get();
    const archived = commitArchive(archiveCurrent(activeGraphId, messages, messagesByGraphId));
    set({
      messages: [],
      bootstrapPhase: 'idle',
      activeGraphId: null,
      messagesByGraphId: archived,
    });
  },

  bindGraph: (graphId) => {
    const id = String(graphId ?? '').trim() || null;
    const { activeGraphId, messages, messagesByGraphId } = get();
    if (id === activeGraphId) return;
    const archived = archiveCurrent(activeGraphId, messages, messagesByGraphId);
    const pending = !activeGraphId && messages.length > 0 ? messages : [];
    const nextMessages = resolveBoundDesignerMessages({
      stored: id ? archived[id] : null,
      pending,
    });
    const nextArchive = id
      ? { ...archived, [id]: nextMessages }
      : archived;
    set({
      activeGraphId: id,
      messages: nextMessages,
      messagesByGraphId: commitArchive(nextArchive),
    });
  },

  appendMessage: (message) => {
    const id = message.id ?? generateUuidV4();
    const createdAt = message.createdAt ?? Date.now();
    set((state) => {
      const next = [
        ...state.messages,
        {
          id,
          role: message.role,
          content: message.content,
          kind: message.kind,
          createdAt,
        },
      ];
      const messagesByGraphId = state.activeGraphId
        ? commitArchive({ ...state.messagesByGraphId, [state.activeGraphId]: next })
        : state.messagesByGraphId;
      return { messages: next, messagesByGraphId };
    });
    return id;
  },

  removeMessage: (id) =>
    set((state) => {
      const next = state.messages.filter((item) => item.id !== id);
      const messagesByGraphId = state.activeGraphId
        ? commitArchive({ ...state.messagesByGraphId, [state.activeGraphId]: next })
        : state.messagesByGraphId;
      return { messages: next, messagesByGraphId };
    }),

  setBootstrapPhase: (phase) => set({ bootstrapPhase: phase }),

  ensureGraphPrompt: (graph, options) => {
    const graphId = String(graph?.graph_id ?? '').trim();
    if (!graphId || !graph) return;
    if (get().activeGraphId !== graphId) {
      get().bindGraph(graphId);
    }
    if (hasDesignerUserPrompt(get().messages)) return;
    const prompt = extractDesignerGraphPrompt(graph);
    if (!prompt) return;
    get().appendMessage({
      role: 'user',
      content: prompt,
      kind: 'user',
    });
    const doneText = String(options?.doneText ?? '').trim();
    if (!doneText) return;
    get().appendMessage({
      role: 'assistant',
      content: doneText,
      kind: 'bootstrap_done',
    });
    if (get().bootstrapPhase === 'idle') {
      get().setBootstrapPhase('done');
    }
  },
}));
