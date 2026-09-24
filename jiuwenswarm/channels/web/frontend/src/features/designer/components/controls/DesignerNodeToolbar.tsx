import { useCallback, useEffect, useRef, useState, type ChangeEvent, type DragEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { DesignerTextEditor } from '../../DesignerTextEditor';
import { useDesignerAssetLibraryStore } from '../../designerAssetLibraryStore';
import {
  collectDesignerMaterials,
  hasPendingDesignerRevision,
} from '../../designerMaterials';
import { useDesignerRunStore } from '../../designerRunStore';
import { useDesignerStore } from '../../designerStore';
import { useDesignerUiStore } from '../../designerUiStore';
import {
  isMediaNodeType,
  isTextLikeNodeType,
  readMediaConfig,
  writeMediaGeneratePatch,
  writeMediaUploadPatch,
} from '../../mediaNodeConfig';
import { DesignerMaterialStrip } from './DesignerMaterialStrip';

type DesignerNodeToolbarProps = {
  nodeId: string;
  nodeType: string;
};

type ExpandedPanel = 'generate' | 'upload' | 'edit' | null;

export function DesignerNodeToolbar({ nodeId, nodeType }: DesignerNodeToolbarProps) {
  const { t } = useTranslation();
  const isTextLike = isTextLikeNodeType(nodeType);
  const isMedia = isMediaNodeType(nodeType);
  const updateNodeConfig = useDesignerStore((state) => state.updateNodeConfig);
  const setNodeOutputRef = useDesignerStore((state) => state.setNodeOutputRef);
  const applyUploadedOutput = useDesignerRunStore((state) => state.applyUploadedOutput);
  const rerunNode = useDesignerRunStore((state) => state.rerunNode);
  const isRunning = useDesignerRunStore((state) => state.isRunning);
  const run = useDesignerRunStore((state) => state.run);
  const nodeState = useDesignerRunStore((state) => state.nodeStates[nodeId]);
  const domainGraph = useDesignerStore((state) => state.domainGraph);
  const addFromFile = useDesignerAssetLibraryStore((state) => state.addFromFile);
  const getAsset = useDesignerAssetLibraryStore((state) => state.getById);
  const inspectNode = useDesignerUiStore((state) => state.inspectNode);
  const startEdit = useDesignerUiStore((state) => state.startEdit);
  const openRevision = useDesignerUiStore((state) => state.openRevision);
  const config = useDesignerStore(
    (state) => state.domainGraph?.nodes.find((node) => node.id === nodeId)?.config ?? {},
  );
  const media = readMediaConfig(config, nodeType);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const [expanded, setExpanded] = useState<ExpandedPanel>(null);

  useEffect(() => {
    setExpanded(null);
  }, [nodeId]);

  const materials = collectDesignerMaterials(domainGraph, run);
  const material =
    materials.find((item) => item.id.startsWith(`${nodeId}:`)) ??
    materials.find((item) => item.nodeId === nodeId);
  const hasOutput = Boolean(
    material && !material.placeholder && (material.previewUrl || material.textUrl),
  );
  const pendingRevision = hasPendingDesignerRevision(nodeState);

  const patchGenerate = useCallback(
    (patch: Parameters<typeof writeMediaGeneratePatch>[1]) => {
      updateNodeConfig(nodeId, (current) => writeMediaGeneratePatch(current, patch));
    },
    [nodeId, updateNodeConfig],
  );

  const applyUploadFile = useCallback(
    (file: File | undefined) => {
      if (!file) return;
      const asset = addFromFile(file);
      if (!asset) return;
      updateNodeConfig(nodeId, (current) =>
        writeMediaUploadPatch(current, {
          filename: asset.filename,
          asset_id: asset.id,
          mime_type: asset.mime_type,
        }),
      );
    },
    [addFromFile, nodeId, updateNodeConfig],
  );

  const confirmUpload = useCallback(() => {
    const assetId = media.upload?.asset_id?.trim();
    if (!assetId) return;
    const asset = getAsset(assetId);
    if (!asset) return;
    const outputRef = {
      kind: nodeType,
      uri: asset.objectUrl,
      mime_type: asset.mime_type,
      label: asset.filename,
    };
    setNodeOutputRef(nodeId, outputRef);
    applyUploadedOutput(nodeId, outputRef);
  }, [
    applyUploadedOutput,
    getAsset,
    media.upload?.asset_id,
    nodeId,
    nodeType,
    setNodeOutputRef,
  ]);

  const onFileChange = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      applyUploadFile(event.target.files?.[0]);
      event.target.value = '';
    },
    [applyUploadFile],
  );

  const onDrop = useCallback(
    (event: DragEvent<HTMLDivElement>) => {
      event.preventDefault();
      event.stopPropagation();
      setDragging(false);
      applyUploadFile(event.dataTransfer.files?.[0]);
    },
    [applyUploadFile],
  );

  const canConfirmUpload = Boolean(media.upload?.asset_id?.trim());

  const onGenerateNode = useCallback(() => {
    if (!domainGraph || isRunning) return;
    void rerunNode(domainGraph, nodeId);
  }, [domainGraph, isRunning, nodeId, rerunNode]);

  const secondaryPanel: ExpandedPanel = isTextLike ? 'edit' : 'upload';

  return (
    <div
      className="designer-node-toolbar"
      data-testid="designer-node-toolbar"
      data-node-type={nodeType}
      data-expanded={expanded ?? 'idle'}
      onClick={(event) => event.stopPropagation()}
      onMouseDown={(event) => event.stopPropagation()}
    >
      <div className="designer-node-toolbar__tabs" role="tablist" aria-label={t('designer.toolbar.modeLabel')}>
        <button
          type="button"
          role="tab"
          aria-selected={false}
          className="designer-node-toolbar__tab"
          data-testid="designer-node-toolbar-tab-inspect"
          disabled={!hasOutput}
          onClick={() => inspectNode(nodeId)}
        >
          {t('designer.toolbar.inspect')}
        </button>
        <button
          type="button"
          role="tab"
          data-testid="designer-node-toolbar-tab-generate"
          disabled={isRunning || !domainGraph}
          title={t('designer.toolbar.rerunHint')}
          aria-selected={isMedia && expanded === 'generate'}
          className={`designer-node-toolbar__tab${isMedia && expanded === 'generate' ? ' is-active' : ''}`}
          onClick={() => {
            if (isMedia) {
              setExpanded((prev) => (prev === 'generate' ? null : 'generate'));
              return;
            }
            onGenerateNode();
          }}
        >
          {t('designer.toolbar.regenerate')}
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={expanded === secondaryPanel}
          className={`designer-node-toolbar__tab${expanded === secondaryPanel ? ' is-active' : ''}`}
          data-testid={
            isTextLike ? 'designer-node-toolbar-tab-edit' : 'designer-node-toolbar-tab-upload'
          }
          onClick={() => {
            if (isTextLike) {
              if (hasOutput) {
                startEdit(material?.id || nodeId);
                return;
              }
              setExpanded((prev) => (prev === 'edit' ? null : 'edit'));
              return;
            }
            setExpanded((prev) => (prev === secondaryPanel ? null : secondaryPanel));
          }}
        >
          {isTextLike ? t('designer.toolbar.edit') : t('designer.toolbar.upload')}
        </button>
        {pendingRevision ? (
          <button
            type="button"
            className="designer-node-toolbar__tab"
            data-testid="designer-node-toolbar-tab-compare"
            onClick={() => openRevision(nodeId)}
          >
            {t('designer.revision.compare')}
          </button>
        ) : null}
      </div>

      {expanded === 'generate' && isMedia ? (
        <div
          className="designer-node-toolbar__panel"
          role="tabpanel"
          data-testid="designer-node-toolbar-panel-generate"
        >
          <DesignerMaterialStrip nodeId={nodeId} nodeType={nodeType} />
          <p className="designer-node-toolbar__prompt-hint" data-testid="designer-node-toolbar-prompt-hint">
            {t('designer.toolbar.finalPromptHint', {
              defaultValue:
                'Shows the last prompt sent to the image/video tool. Edit before regenerate; the leaf agent may still lightly refine locks.',
            })}
          </p>
          <textarea
            className="designer-node-toolbar__prompt"
            value={media.generate?.prompt ?? ''}
            placeholder={t('designer.toolbar.promptPlaceholder')}
            rows={4}
            data-testid="designer-node-toolbar-prompt"
            onChange={(event) => patchGenerate({ prompt: event.target.value })}
          />
          <button
            type="button"
            className="designer-node-toolbar__action"
            data-testid="designer-node-toolbar-generate-action"
            disabled={isRunning || !domainGraph}
            title={t('designer.toolbar.rerunHint')}
            onClick={onGenerateNode}
          >
            {t('designer.toolbar.generateAction')}
          </button>
        </div>
      ) : null}

      {expanded === 'edit' ? (
        <div
          className="designer-node-toolbar__panel"
          role="tabpanel"
          data-testid="designer-node-toolbar-panel-edit"
        >
          {material?.textUrl ? (
            <DesignerTextEditor material={material} compact showStartButton={false} startEditKey={1} />
          ) : (
            <p>{t('designer.materials.placeholderHint')}</p>
          )}
        </div>
      ) : null}

      {expanded === 'upload' ? (
        <div
          className={`designer-node-toolbar__panel designer-node-toolbar__panel--upload${dragging ? ' is-dragging' : ''}`}
          role="tabpanel"
          data-testid="designer-node-toolbar-panel-upload"
          onDragEnter={(event) => {
            event.preventDefault();
            setDragging(true);
          }}
          onDragOver={(event) => {
            event.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
        >
          <input
            ref={fileInputRef}
            type="file"
            className="designer-node-toolbar__file-input"
            accept={
              nodeType === 'audio'
                ? 'audio/*'
                : nodeType === 'video'
                  ? 'video/*'
                  : 'image/*'
            }
            data-testid="designer-node-toolbar-file-input"
            onChange={onFileChange}
          />
          <button
            type="button"
            className="designer-node-toolbar__browse"
            data-testid="designer-node-toolbar-browse"
            onClick={() => fileInputRef.current?.click()}
          >
            {t('designer.toolbar.browseFiles')}
          </button>
          <p className="designer-node-toolbar__drop-hint">{t('designer.toolbar.dropHint')}</p>
          {media.upload?.filename ? (
            <p className="designer-node-toolbar__filename" data-testid="designer-node-toolbar-filename">
              {media.upload.filename}
            </p>
          ) : null}
          <button
            type="button"
            className="designer-node-toolbar__action"
            data-testid="designer-node-toolbar-upload-action"
            disabled={!canConfirmUpload}
            onClick={confirmUpload}
          >
            {t('designer.toolbar.uploadAction')}
          </button>
        </div>
      ) : null}
    </div>
  );
}
