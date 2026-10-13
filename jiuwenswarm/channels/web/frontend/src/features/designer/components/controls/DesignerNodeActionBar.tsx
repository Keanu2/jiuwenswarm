import { Download, ScanSearch, Trash } from 'lucide-react';
import { useCallback, type MouseEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { downloadPreviewFile } from '../../../../components/ui/FilePreviewPanel/filePreviewShared';
import { collectDesignerMaterials } from '../../designerMaterials';
import { isTextLikeNodeType } from '../../mediaNodeConfig';
import { useDesignerRunStore } from '../../designerRunStore';
import { useDesignerStore } from '../../designerStore';
import { useDesignerUiStore } from '../../designerUiStore';

type DesignerNodeActionBarProps = {
  nodeId: string;
  nodeType: string;
};

export function DesignerNodeActionBar({ nodeId, nodeType }: DesignerNodeActionBarProps) {
  const { t } = useTranslation();
  const domainGraph = useDesignerStore((state) => state.domainGraph);
  const run = useDesignerRunStore((state) => state.run);
  const inspectNode = useDesignerUiStore((state) => state.inspectNode);
  const requestDeleteNodes = useDesignerUiStore((state) => state.requestDeleteNodes);
  const previewLabel = isTextLikeNodeType(nodeType)
    ? t('designer.nodeActions.previewOrEdit')
    : t('designer.nodeActions.preview');

  const materials = collectDesignerMaterials(domainGraph, run);
  const material =
    materials.find((item) => item.id.startsWith(`${nodeId}:`)) ??
    materials.find((item) => item.nodeId === nodeId);
  const hasOutput = Boolean(
    material && !material.placeholder && (material.previewUrl || material.textUrl),
  );

  const onPreview = useCallback(
    (event: MouseEvent) => {
      event.stopPropagation();
      if (!hasOutput) return;
      inspectNode(nodeId);
    },
    [hasOutput, inspectNode, nodeId],
  );

  const onDownload = useCallback(
    async (event: MouseEvent) => {
      event.stopPropagation();
      if (!material || !hasOutput) return;
      const filename =
        material.label?.trim() ||
        material.uri.split('/').pop() ||
        `designer-node-${nodeId}`;
      if (material.previewUrl) {
        await downloadPreviewFile({ filename, downloadUrl: material.previewUrl });
        return;
      }
      if (!material.textUrl) return;
      try {
        const response = await fetch(material.textUrl);
        if (!response.ok) return;
        const content = await response.text();
        await downloadPreviewFile({ filename, content });
      } catch {
        // ignore fetch/download failures
      }
    },
    [hasOutput, material, nodeId],
  );

  const onDelete = useCallback(
    (event: MouseEvent) => {
      event.stopPropagation();
      event.preventDefault();
      requestDeleteNodes([nodeId]);
    },
    [nodeId, requestDeleteNodes],
  );

  return (
    <div
      className="designer-node-action-bar nodrag nopan nowheel"
      role="toolbar"
      aria-label={t('designer.nodeActions.actionBarLabel')}
      data-testid="designer-node-action-bar"
      onClick={(event) => event.stopPropagation()}
      onMouseDown={(event) => event.stopPropagation()}
    >
      <button
        type="button"
        className="designer-node-action-bar__btn"
        data-testid="designer-node-action-preview"
        disabled={!hasOutput}
        onClick={onPreview}
      >
        <ScanSearch size={14} aria-hidden />
        <span>{previewLabel}</span>
      </button>
      <button
        type="button"
        className="designer-node-action-bar__btn"
        data-testid="designer-node-action-download"
        disabled={!hasOutput}
        onClick={(event) => {
          void onDownload(event);
        }}
      >
        <Download size={14} aria-hidden />
        <span>{t('designer.nodeActions.download')}</span>
      </button>
      <button
        type="button"
        className="designer-node-action-bar__btn"
        data-testid="designer-node-action-delete"
        aria-label={t('designer.nodeActions.delete')}
        title={t('designer.nodeActions.deleteHint')}
        onClick={onDelete}
      >
        <Trash size={14} aria-hidden />
        <span>{t('designer.nodeActions.delete')}</span>
      </button>
    </div>
  );
}
