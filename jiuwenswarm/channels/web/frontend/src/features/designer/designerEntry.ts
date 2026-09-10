import { useWorkspaceStore } from '../../stores';
import { useDesignerStore } from './designerStore';
import { useDesignerChatStore } from './designerChatStore';
import { designerGraphClient } from './designerGraphClient';
import { useDesignerOptimizeStore } from './designerOptimizeStore';

export const DESIGNER_BOOTSTRAP_THINKING_MS = 1200;

export type LaunchDesignerFromTaskParams = {
  prompt: string;
  projectId?: string;
  projectDir?: string;
  workMode?: 'work' | 'code';
  optimizeFor?: 'cost' | 'quality';
  scenario?: string;
  /** Navigate to Design nav before/while bootstrap runs. */
  onNavigateToDesign: () => void;
  thinkingMs?: number;
  thinkingText?: string;
  doneText?: string;
  errorText?: string;
};

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms);
  });
}

/**
 * Design canvas Assistant send: already on Design page, no nav jump; bootstrap onto canvas.
 * Shares the same ``designer.graph.bootstrap`` path as the Tasks entry.
 */
export async function bootstrapDesignerFromChat(params: {
  prompt: string;
  projectId?: string;
  projectDir?: string;
  workMode?: 'work' | 'code';
  optimizeFor?: 'cost' | 'quality';
  scenario?: string;
  thinkingText?: string;
  doneText?: string;
  errorText?: string;
}): Promise<void> {
  await launchDesignerFromTask({
    ...params,
    onNavigateToDesign: () => undefined,
    thinkingMs: 400,
  });
}

/**
 * Tasks page Design arm → Design tab: compose agentic graph via bootstrap RPC.
 */
export async function launchDesignerFromTask(params: LaunchDesignerFromTaskParams): Promise<void> {
  const prompt = params.prompt.trim();
  if (!prompt) return;

  const optimizeFor =
    params.optimizeFor ?? useDesignerOptimizeStore.getState().optimizeFor ?? 'quality';
  const thinkingMs = params.thinkingMs ?? DESIGNER_BOOTSTRAP_THINKING_MS;
  const thinkingText =
    params.thinkingText ??
    `Decomposing your request into an agentic ${optimizeFor}-optimized design graph…`;
  const doneText =
    params.doneText ??
    'Workflow composed. Tweak parameters and rerun nodes as needed.';
  const errorText = params.errorText ?? 'Failed to compose the design workflow. Please retry.';

  const designerStore = useDesignerStore.getState();
  const chatStore = useDesignerChatStore.getState();

  chatStore.reset();
  designerStore.beginBootstrapEntry(prompt);
  chatStore.appendMessage({
    role: 'user',
    content: prompt,
    kind: 'user',
  });
  params.onNavigateToDesign();

  chatStore.setBootstrapPhase('thinking');
  const thinkingId = chatStore.appendMessage({
    role: 'assistant',
    content: thinkingText,
    kind: 'thinking',
  });

  await sleep(thinkingMs);

  chatStore.removeMessage(thinkingId);
  chatStore.setBootstrapPhase('bootstrapping');

  try {
    const result = await designerGraphClient.bootstrap({
      prompt,
      projectId: params.projectId,
      projectDir: params.projectDir,
      workMode: params.workMode,
      optimizeFor,
      scenario: params.scenario,
    });
    const graph = result?.graph;
    if (!graph?.graph_id || !Array.isArray(graph.nodes)) {
      throw new Error('bootstrap response missing graph');
    }
    useDesignerStore.getState().applyGraph(graph);
    useDesignerChatStore.getState().bindGraph(graph.graph_id);
    void useWorkspaceStore.getState().loadDesignerGraphs();
    const scenario = String(graph.metadata?.scenario || 'auto');
    const nodeCount = graph.nodes.length;
    useDesignerChatStore.getState().appendMessage({
      role: 'assistant',
      content: `${doneText}\n\nScenario: ${scenario} · Nodes: ${nodeCount} · Optimize: ${optimizeFor}`,
      kind: 'bootstrap_done',
    });
    useDesignerChatStore.getState().setBootstrapPhase('done');
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    useDesignerStore.getState().failBootstrapEntry(message);
    useDesignerChatStore.getState().appendMessage({
      role: 'assistant',
      content: `${errorText}${message ? ` (${message})` : ''}`,
      kind: 'bootstrap_error',
    });
    useDesignerChatStore.getState().setBootstrapPhase('error');
  }
}
