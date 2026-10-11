import { LayoutGrid, Loader2, Map as MapIcon, MessageSquare } from 'lucide-react';
import { useEffect, useMemo, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import PanelCollapseIcon from '../../../assets/panel-collapse.svg?react';
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
import { DesignerAssetsSidebar } from './DesignerAssetsSidebar';
import { DesignerCanvas } from './DesignerCanvas';
import { DesignerChatPanel, DesignerEmptyState } from './DesignerChatPanel';
import { DesignerRunControl } from './DesignerRunControl';
import { designerWorkspaceClient } from '../designerGraphClient';
import './DesignerPage.css';

type DesignerPageProps = {
  projectId?: string;
  sidebarCollapsed?: boolean;
  onToggleSidebarCollapse?: () => void;
};

function startOfLocalDay(date: Date): Date {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate());
}

function formatModifiedAt(
  timestamp: number | undefined,
  locale: string,
  yesterdayLabel: string,
): string {
  if (!timestamp || !Number.isFinite(timestamp)) return '';
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return '';

  const time = new Intl.DateTimeFormat(locale, {
    hour: '2-digit',
    minute: '2-digit',
  }).format(date);
  const now = new Date();
  const dayDiff = Math.round(
    (startOfLocalDay(now).getTime() - startOfLocalDay(date).getTime()) / 86_400_000,
  );
  if (dayDiff === 0) return time;
  if (dayDiff === 1) return `${yesterdayLabel} ${time}`;

  const isZh = locale.toLowerCase().startsWith('zh');
  if (date.getFullYear() === now.getFullYear()) {
    if (isZh) return `${date.getMonth() + 1}月${date.getDate()}日 ${time}`;
    return `${new Intl.DateTimeFormat(locale, { month: 'short', day: 'numeric' }).format(date)} ${time}`;
  }
  if (isZh) {
    return `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日 ${time}`;
  }
  return `${new Intl.DateTimeFormat(locale, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  }).format(date)} ${time}`;
}

export function DesignerPage({
  projectId,
  sidebarCollapsed = false,
  onToggleSidebarCollapse,
}: DesignerPageProps) {
  const { t, i18n } = useTranslation();
  const effectiveProjectId = projectId;

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
  const chatSidebarOpen = useDesignerUiStore((state) => state.chatSidebarOpen);
  const assetsSidebarOpen = useDesignerUiStore((state) => state.assetsSidebarOpen);
  const minimapVisible = useDesignerUiStore((state) => state.minimapVisible);
  const zoomPercent = useDesignerUiStore((state) => state.zoomPercent);
  const toggleChatSidebar = useDesignerUiStore((state) => state.toggleChatSidebar);
  const toggleMinimap = useDesignerUiStore((state) => state.toggleMinimap);
  const requestAutoLayout = useDesignerUiStore((state) => state.requestAutoLayout);
  const workspaceLoadSeqRef = useRef(0);
  useEffect(() => bindDesignerRuntime(), []);

  useEffect(() => {
    const loadSeq = ++workspaceLoadSeqRef.current;
    if (!effectiveProjectId || bootstrapInProgress) return;
    useDesignerStore.getState().reset();
    void designerWorkspaceClient.get(effectiveProjectId)
      .then((workspace) => {
        if (workspaceLoadSeqRef.current !== loadSeq) return;
        useDesignerStore.getState().applyGraph(workspace.graph);
        useDesignerChatStore.getState().replaceMessages(
          workspace.graph.graph_id,
          workspace.messages,
        );
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
    effectiveProjectId,
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
  const modifiedLabel = formatModifiedAt(
    domainGraph?.updated_at,
    i18n.language,
    t('designer.toolbar.yesterday'),
  );
  const canvasInteractive =
    showCanvas && !bootstrapInProgress && !isDesignerPreviewGraph(domainGraph);

  return (
    <div className="designer-page app-section" data-testid="designer-page">
      <div className="designer-page__main">
        <header className="designer-page__toolbar" data-testid="designer-page-toolbar">
          {onToggleSidebarCollapse ? (
            <button
              type="button"
              className="designer-page__sidebar-collapse"
              onClick={onToggleSidebarCollapse}
              aria-label={sidebarCollapsed ? t('common.expand') : t('common.collapse')}
              aria-pressed={!sidebarCollapsed}
              title={sidebarCollapsed ? t('common.expand') : t('common.collapse')}
              data-testid="designer-page-sidebar-collapse"
              data-variant={sidebarCollapsed ? 'expand' : 'collapse'}
            >
              <PanelCollapseIcon aria-hidden />
            </button>
          ) : null}
          <div className="designer-page__heading">
            <h1 className="designer-page__title" data-testid="designer-page-title">
              {projectTitle || t('designer.subtitle')}
            </h1>
            {modifiedLabel ? (
              <span className="designer-page__modified" data-testid="designer-page-modified">
                {modifiedLabel}
              </span>
            ) : null}
          </div>
          <div className="designer-page__toolbar-actions">
            <span className="designer-page__zoom" data-testid="designer-page-zoom">
              {zoomPercent}%
            </span>
            <span className="designer-page__toolbar-split" aria-hidden />
            <button
              type="button"
              className="designer-page__toolbar-icon-btn"
              aria-label={t('designer.dock.layout')}
              title={t('designer.dock.layoutHint')}
              data-testid="designer-page-layout"
              disabled={!canvasInteractive || !domainGraph?.nodes.length}
              onClick={requestAutoLayout}
            >
              <LayoutGrid size={18} aria-hidden />
            </button>
            <button
              type="button"
              className={`designer-page__toolbar-text-btn${minimapVisible ? ' is-active' : ''}`}
              aria-label={t('designer.toolbar.minimap')}
              aria-pressed={minimapVisible}
              title={t('designer.toolbar.minimap')}
              data-testid="designer-page-minimap-toggle"
              disabled={!canvasInteractive}
              onClick={toggleMinimap}
            >
              <MapIcon size={16} aria-hidden />
              <span>{t('designer.toolbar.minimap')}</span>
            </button>
            <button
              type="button"
              className={`designer-page__toolbar-outline-btn${chatSidebarOpen ? ' is-active' : ''}`}
              aria-label={t('designer.toolbar.openChat')}
              aria-pressed={chatSidebarOpen}
              title={t('designer.toolbar.openChat')}
              data-testid="designer-page-chat-toggle"
              onClick={toggleChatSidebar}
            >
              <MessageSquare size={16} aria-hidden />
              <span>{t('designer.toolbar.openChat')}</span>
            </button>
            <DesignerRunControl
              graph={showCanvas ? domainGraph : null}
              disabled={!canvasInteractive}
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

        <div className="designer-page__canvas-area">
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
      </div>

      {assetsSidebarOpen ? <DesignerAssetsSidebar /> : null}
      {chatSidebarOpen ? <DesignerChatPanel /> : null}

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
