import { Loader2 } from 'lucide-react';
import { useEffect, useMemo, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import {
  collectDesignerMaterials,
  collectPendingRevisions,
} from '../designerMaterials';
import { useDesignerChatStore } from '../designerChatStore';
import { bindDesignerRuntime, useDesignerRunStore } from '../designerRunStore';
import { isDesignerPreviewGraph } from '../designerBootstrapGraph';
import { useDesignerStore } from '../designerStore';
import { useDesignerUiStore } from '../designerUiStore';
import { DesignerMaterialViewer } from '../DesignerMaterialViewer';
import { DesignerRevisionChooser } from '../DesignerRevisionChooser';
import { DesignerCanvas } from './DesignerCanvas';
import { DesignerChatPanel, DesignerEmptyState } from './DesignerChatPanel';
import { DesignerRunControl } from './DesignerRunControl';
import { designerWorkspaceClient } from '../designerGraphClient';
import { useChatStore } from '../../../stores/chatStore';
import type { DesignerChatMessage } from '../designerChatStore';
import './DesignerPage.css';

function seedChatHistory(sessionId: string, messages: DesignerChatMessage[]): void {
  // Restore persisted task history into the shared chat store on page load so
  // the MessageList (work-mode rendering) shows it after refresh. A full
  // replace mirrors the work-mode history restore: a stream that raced ahead
  // of this load simply keeps appending on top of the restored list.
  useChatStore.getState().ensureRuntime(sessionId);
  useChatStore.getState().replaceHistoryMessages(
    sessionId,
    messages.map((item) => ({
      id: item.id,
      role: item.role === 'user' ? ('user' as const) : ('assistant' as const),
      content: item.content,
      timestamp: new Date(item.createdAt).toISOString(),
    })),
  );
}

type DesignerPageProps = {
  sessionId?: string;
};

export function DesignerPage({ sessionId }: DesignerPageProps) {
  const { t } = useTranslation();
  const effectiveSessionId = sessionId;

  const loadStatus = useDesignerStore((state) => state.loadStatus);
  const loadError = useDesignerStore((state) => state.loadError);
  const domainGraph = useDesignerStore((state) => state.domainGraph);
  const graphId = useDesignerStore((state) => state.graphId);
  const bootstrapInProgress = useDesignerStore((state) => state.bootstrapInProgress);
  const resetForGraph = useDesignerRunStore((state) => state.resetForGraph);
  const boundGraphId = useDesignerRunStore((state) => state.boundGraphId);
  const run = useDesignerRunStore((state) => state.run);
  const runError = useDesignerRunStore((state) => state.runError);
  const runWarning = useDesignerRunStore((state) => state.runWarning);
  const chooseOutput = useDesignerRunStore((state) => state.chooseOutput);
  const selectedMaterialId = useDesignerUiStore((state) => state.selectedMaterialId);
  const viewerOpen = useDesignerUiStore((state) => state.viewerOpen);
  const chooserNodeId = useDesignerUiStore((state) => state.chooserNodeId);
  const editRequestKey = useDesignerUiStore((state) => state.editRequestKey);
  const setSelectedMaterialId = useDesignerUiStore((state) => state.setSelectedMaterialId);
  const closeViewer = useDesignerUiStore((state) => state.closeViewer);
  const closeRevision = useDesignerUiStore((state) => state.closeRevision);
  const resetUi = useDesignerUiStore((state) => state.reset);
  const workspaceLoadSeqRef = useRef(0);
  useEffect(() => bindDesignerRuntime(), []);

  useEffect(() => {
    const loadSeq = ++workspaceLoadSeqRef.current;
    if (!effectiveSessionId || bootstrapInProgress) return;
    const current = useDesignerStore.getState().domainGraph;
    if (current?.graph_id && String(current.metadata?.session_id ?? '') === effectiveSessionId) {
      // Background refresh (no reset → no blank flash, no dropped in-flight
      // events): heals the case where the first leader round finished before
      // this page mounted and its designer.graph.updated push went unheard
      // (e.g. the landing navigating after the round). Apply only snapshots
      // at least as new as the held one so a slow response can't roll the
      // canvas back under live pushes.
      void designerWorkspaceClient.get(effectiveSessionId)
        .then((workspace) => {
          if (workspaceLoadSeqRef.current !== loadSeq) return;
          const held = useDesignerStore.getState().domainGraph;
          const sameGraph = held?.graph_id === workspace.graph.graph_id;
          const incomingAt = Number(workspace.graph.updated_at ?? 0);
          const heldAt = Number(held?.updated_at ?? 0);
          if (!sameGraph || incomingAt >= heldAt) {
            useDesignerStore.getState().applyGraph(workspace.graph);
          }
        })
        .catch(() => {
          // Keep the held graph; a background refresh failure is not fatal.
        });
      return;
    }
    useDesignerStore.getState().reset();
    void designerWorkspaceClient.get(effectiveSessionId)
      .then((workspace) => {
        if (workspaceLoadSeqRef.current !== loadSeq) return;
        useDesignerStore.getState().applyGraph(workspace.graph);
        useDesignerChatStore.getState().replaceMessages(
          workspace.graph.graph_id,
          workspace.messages,
        );
        const historySessionId = String(
          workspace.graph.metadata?.session_id ?? effectiveSessionId,
        );
        useChatStore.getState().ensureRuntime(historySessionId);
        seedChatHistory(historySessionId, workspace.messages);
      })
      .catch((reason) => {
        if (workspaceLoadSeqRef.current !== loadSeq) return;
        const message = reason instanceof Error ? reason.message : String(reason);
        useDesignerStore.getState().failBootstrapEntry(message);
      });
    return () => {
      if (workspaceLoadSeqRef.current === loadSeq) {
        workspaceLoadSeqRef.current += 1;
      }
    };
  }, [
    bootstrapInProgress,
    effectiveSessionId,
  ]);

  useEffect(() => {
    const nextId = domainGraph?.graph_id ?? null;
    if (nextId !== boundGraphId) {
      resetUi();
      resetForGraph(domainGraph);
      useDesignerChatStore.getState().bindGraph(nextId);
    }
    if (domainGraph && !isDesignerPreviewGraph(domainGraph)) {
      useDesignerChatStore.getState().ensureGraphPrompt(domainGraph, {
        doneText: t('designer.chat.bootstrapDone'),
      });
    }
  }, [boundGraphId, domainGraph, resetForGraph, resetUi, t]);

  const materials = useMemo(
    () => collectDesignerMaterials(domainGraph, run),
    [domainGraph, run],
  );
  const pendingRevisions = useMemo(
    () => collectPendingRevisions(domainGraph, run),
    [domainGraph, run],
  );
  const autoPromotedRef = useRef(new Set<string>());
  useEffect(() => {
    const runId = run?.run_id;
    if (!runId || run?.status === 'running') return;
    for (const item of pendingRevisions) {
      const key = `${runId}:${item.nodeId}`;
      if (item.requiresSelection || autoPromotedRef.current.has(key)) continue;
      // Brief/storyboard versions remain pending until the user chooses one.
      autoPromotedRef.current.add(key);
      void chooseOutput(item.nodeId, 'new').then(() => {
        if (chooserNodeId === item.nodeId) closeRevision();
      });
    }
  }, [chooseOutput, chooserNodeId, closeRevision, pendingRevisions, run?.run_id, run?.status]);
  const activeRevision =
    pendingRevisions.find((item) => item.nodeId === chooserNodeId) ?? pendingRevisions[0];

  const graphReady = Boolean(domainGraph) && (!graphId || domainGraph?.graph_id === graphId);
  const showCanvas = graphReady;
  const showEmpty = !showCanvas && loadStatus === 'empty';
  const showError = !showCanvas && loadStatus === 'error';
  const showLoading =
    !showCanvas &&
    (loadStatus === 'loading' || loadStatus === 'idle' || loadStatus === 'bootstrapping');

  const projectTitle =
    domainGraph?.title?.trim() ||
    (showLoading || showEmpty || showError ? '' : t('designer.subtitle'));

  return (
    <div className="designer-page app-section" data-testid="designer-page">
      <header className="designer-page__toolbar" data-testid="designer-page-toolbar">
        <div className="designer-page__heading">
          <h1 className="designer-page__title" data-testid="designer-page-rail-title">
            {t('nav.design')}
          </h1>
          <p className="designer-page__subtitle" data-testid="designer-page-title">
            {projectTitle || t('designer.subtitle')}
          </p>
        </div>
        <div className="designer-page__toolbar-actions">
          <DesignerRunControl
            graph={showCanvas ? domainGraph : null}
            disabled={
              !showCanvas ||
              bootstrapInProgress ||
              isDesignerPreviewGraph(domainGraph)
            }
          />
        </div>
      </header>
      {runError ? (
        <div
          className="app-toast-wrapper app-toast-wrapper--top-center"
          data-testid="designer-run-error-toast"
        >
          <div className="app-connection-toast" role="alert" data-testid="designer-error">
            {runError}
          </div>
        </div>
      ) : null}
      {runWarning ? (
        <p className="designer-page__warning" data-testid="designer-warning">
          {runWarning}
        </p>
      ) : null}

      <div className="designer-page__workspace">
        <DesignerChatPanel />

        {showCanvas && domainGraph ? <DesignerCanvas graph={domainGraph} /> : null}

        {showLoading ? (
          <div className="designer-page__state" data-testid="designer-loading-state">
            <div className="designer-page__state-card">
              <Loader2 className="mx-auto mb-3 animate-spin" size={24} aria-hidden />
              <p className="designer-page__state-desc">
                {loadStatus === 'bootstrapping' ? t('designer.chat.thinking') : t('designer.loading')}
              </p>
            </div>
          </div>
        ) : null}

        {showEmpty ? <DesignerEmptyState variant="empty" /> : null}
        {showError ? <DesignerEmptyState variant="error" errorMessage={loadError} /> : null}
      </div>

      {viewerOpen ? (
        <DesignerMaterialViewer
          materials={materials}
          selectedId={selectedMaterialId}
          startEditKey={editRequestKey}
          onSelect={setSelectedMaterialId}
          onClose={closeViewer}
        />
      ) : null}
      {activeRevision && chooserNodeId ? (
        <DesignerRevisionChooser
          revision={activeRevision}
          busy={Boolean(run?.status === 'running')}
          onChoose={(choice) => {
            void chooseOutput(activeRevision.nodeId, choice).then(() => closeRevision());
          }}
          onClose={closeRevision}
        />
      ) : null}
    </div>
  );
}
