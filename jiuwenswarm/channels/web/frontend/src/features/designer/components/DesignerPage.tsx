import { Loader2 } from 'lucide-react';
import { useEffect, useMemo, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { useWorkspaceStore } from '../../../stores';
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
import { DesignerRecentGraphs } from './DesignerRecentGraphs';
import { DesignerRunControl } from './DesignerRunControl';
import './DesignerPage.css';

type DesignerPageProps = {
  projectId?: string;
};

export function DesignerPage({ projectId }: DesignerPageProps) {
  const { t } = useTranslation();
  const selectedProject = useWorkspaceStore((state) => state.selectedProject);
  const pendingDesignerGraphId = useWorkspaceStore((state) => state.pendingDesignerGraphId);
  const setPendingDesignerGraphId = useWorkspaceStore((state) => state.setPendingDesignerGraphId);
  const designerGraphs = useWorkspaceStore((state) => state.designerGraphs);
  const effectiveProjectId = projectId || selectedProject?.project_id;

  const loadStatus = useDesignerStore((state) => state.loadStatus);
  const loadError = useDesignerStore((state) => state.loadError);
  const domainGraph = useDesignerStore((state) => state.domainGraph);
  const graphId = useDesignerStore((state) => state.graphId);
  const bootstrapInProgress = useDesignerStore((state) => state.bootstrapInProgress);
  const loadForProject = useDesignerStore((state) => state.loadForProject);
  const loadGraph = useDesignerStore((state) => state.loadGraph);
  const resetForGraph = useDesignerRunStore((state) => state.resetForGraph);
  const boundGraphId = useDesignerRunStore((state) => state.boundGraphId);
  const run = useDesignerRunStore((state) => state.run);
  const runError = useDesignerRunStore((state) => state.runError);
  const chooseOutput = useDesignerRunStore((state) => state.chooseOutput);
  const selectedMaterialId = useDesignerUiStore((state) => state.selectedMaterialId);
  const viewerOpen = useDesignerUiStore((state) => state.viewerOpen);
  const chooserNodeId = useDesignerUiStore((state) => state.chooserNodeId);
  const editRequestKey = useDesignerUiStore((state) => state.editRequestKey);
  const setSelectedMaterialId = useDesignerUiStore((state) => state.setSelectedMaterialId);
  const closeViewer = useDesignerUiStore((state) => state.closeViewer);
  const closeRevision = useDesignerUiStore((state) => state.closeRevision);
  const resetUi = useDesignerUiStore((state) => state.reset);
  // Tasks→Design bootstrap 结束后 bootstrapInProgress 会变 false，若立刻 list/get
  //（尤其 projectId 为空或与新建 project 不一致），会把刚 apply 的图刷成 empty。
  const skipLoadAfterBootstrapRef = useRef(false);

  useEffect(() => bindDesignerRuntime(), []);

  useEffect(() => {
    if (pendingDesignerGraphId) {
      const nextId = pendingDesignerGraphId;
      setPendingDesignerGraphId(null);
      void loadGraph(nextId);
      return;
    }
    if (bootstrapInProgress) {
      skipLoadAfterBootstrapRef.current = true;
      return;
    }
    if (skipLoadAfterBootstrapRef.current) {
      skipLoadAfterBootstrapRef.current = false;
      return;
    }
    void loadForProject(effectiveProjectId);
  }, [
    bootstrapInProgress,
    effectiveProjectId,
    loadForProject,
    loadGraph,
    pendingDesignerGraphId,
    setPendingDesignerGraphId,
  ]);

  useEffect(() => {
    if (bootstrapInProgress || pendingDesignerGraphId) return;
    if (domainGraph || loadStatus === 'loading' || loadStatus === 'bootstrapping') return;
    const fallbackId = designerGraphs[0]?.graph_id;
    if (!fallbackId) return;
    if (loadStatus === 'empty' || loadStatus === 'error' || loadStatus === 'idle') {
      void loadGraph(fallbackId);
    }
  }, [
    bootstrapInProgress,
    designerGraphs,
    domainGraph,
    loadGraph,
    loadStatus,
    pendingDesignerGraphId,
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
      if (autoPromotedRef.current.has(key)) continue;
      // Always auto-accept new assets — never require one-by-one Continue/approval.
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

  const selectedGraphTitle =
    designerGraphs.find((item) => item.graph_id === graphId)?.title?.trim() || '';
  const projectTitle =
    domainGraph?.title?.trim() ||
    selectedGraphTitle ||
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
        {designerGraphs.length > 0 ? (
          <DesignerRecentGraphs
            graphId={isDesignerPreviewGraph(domainGraph) ? null : graphId}
            currentTitle={projectTitle}
          />
        ) : null}
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
        <p className="designer-page__error" data-testid="designer-error">
          {runError}
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
