import {
  FileText,
  Headphones,
  Image as ImageIcon,
  Loader2,
  Sheet,
  Video,
  type LucideIcon,
} from 'lucide-react';
import { useEffect, useState, type ReactNode } from 'react';
import { Handle, NodeToolbar, Position, type Node, type NodeProps } from '@xyflow/react';
import { designerAssetPreviewUrl, designerAssetTextUrl } from '../../designerAssetUrl';
import {
  DESIGNER_MATERIAL_SAVED_EVENT,
  preferredDesignerPreviewRef,
} from '../../designerMaterials';
import { parseMarkdownTable, EMPTY_STORYBOARD_TABLE } from '../../designerNodePreview';
import {
  DESIGNER_NODE_STATUS_COMPLETED,
  DESIGNER_NODE_STATUS_FAILED,
  DESIGNER_NODE_STATUS_RUNNING,
  DESIGNER_NODE_TYPE_AUDIO,
  DESIGNER_NODE_TYPE_IMAGE,
  DESIGNER_NODE_TYPE_TABLE,
  DESIGNER_NODE_TYPE_TEXT,
  DESIGNER_NODE_TYPE_VIDEO,
} from '../../executionGraphTypes';
import type { DesignerReactFlowNode } from '../../designerGraphAdapter';
import { useDesignerRunStore } from '../../designerRunStore';
import { useDesignerStore } from '../../designerStore';
import { isMediaNodeType, supportsNodeToolbar } from '../../mediaNodeConfig';
import { DesignerNodeToolbar } from '../controls/DesignerNodeToolbar';

type DesignerNodeData = DesignerReactFlowNode['data'];
type DesignerFlowNode = Node<DesignerNodeData>;

function modalityIcon(nodeType: string): LucideIcon {
  switch (nodeType) {
    case DESIGNER_NODE_TYPE_TABLE:
      return Sheet;
    case DESIGNER_NODE_TYPE_IMAGE:
      return ImageIcon;
    case DESIGNER_NODE_TYPE_VIDEO:
      return Video;
    case DESIGNER_NODE_TYPE_AUDIO:
      return Headphones;
    case DESIGNER_NODE_TYPE_TEXT:
    default:
      return FileText;
  }
}

function PlaceholderBody({ nodeType }: { nodeType: string }) {
  const Icon = modalityIcon(nodeType);
  return (
    <span className="designer-node__placeholder" data-testid="designer-node-placeholder" data-node-type={nodeType}>
      <Icon className="designer-node__placeholder-icon" size={40} strokeWidth={1.5} aria-hidden />
    </span>
  );
}

function NodeOutputFrame({ running, children }: { running: boolean; children: ReactNode }) {
  return (
    <span className="designer-node__output-frame">
      {children}
      {running ? (
        <span className="designer-node__running-overlay" data-testid="designer-node-running">
          <Loader2 className="designer-node__running-icon" size={22} aria-hidden />
        </span>
      ) : null}
    </span>
  );
}

function DesignerNodeShell({
  nodeId,
  label,
  nodeType,
  body,
  media = false,
  mediaFilled = false,
  selected = false,
  toolbar = null,
}: {
  nodeId: string;
  label: string;
  nodeType: string;
  body: ReactNode;
  media?: boolean;
  mediaFilled?: boolean;
  selected?: boolean;
  toolbar?: ReactNode;
}) {
  const status = useDesignerRunStore(
    (state) => state.nodeStates[nodeId]?.status ?? 'pending',
  );
  const statusClass =
    status === DESIGNER_NODE_STATUS_RUNNING
      ? ' is-running'
      : status === DESIGNER_NODE_STATUS_COMPLETED
        ? ' is-completed'
        : status === DESIGNER_NODE_STATUS_FAILED
          ? ' is-failed'
          : '';
  const TypeIcon = modalityIcon(nodeType);
  const showMediaFill = media && (mediaFilled || status === DESIGNER_NODE_STATUS_COMPLETED);

  return (
    <div
      className={`designer-node${media ? ' designer-node--media' : ''}${showMediaFill ? ' designer-node--media-filled' : ''}${selected ? ' is-selected' : ''}${statusClass} group`}
      data-testid="designer-node"
      data-selected={selected ? 'true' : 'false'}
      data-status={status}
    >
      <Handle type="target" position={Position.Left} className='size-2 bg-gray-500 transition-all ease-out group-hover:size-3' />
      <div className="designer-node__header">
        <span className="designer-node__type-icon" aria-hidden data-testid="designer-node-type-icon" data-node-type={nodeType}>
          <TypeIcon size={14} strokeWidth={1.75} />
        </span>
        <span className="designer-node__label">{label}</span>
      </div>
      <div className="designer-node__body">{body}</div>
      <Handle type="source" position={Position.Right} className='size-2 bg-gray-500 transition-all ease-out group-hover:size-3' />
      {toolbar}
    </div>
  );
}

function useDesignerAssetText(uri: string | null | undefined): string | null {
  const url = designerAssetTextUrl(uri);
  const [text, setText] = useState<string | null>(null);
  const [reloadAt, setReloadAt] = useState(0);

  useEffect(() => {
    const onSaved = (event: Event) => {
      const savedUri = (event as CustomEvent<{ uri?: string }>).detail?.uri;
      if (savedUri && uri && savedUri === uri) {
        setReloadAt(Date.now());
      }
    };
    window.addEventListener(DESIGNER_MATERIAL_SAVED_EVENT, onSaved);
    return () => window.removeEventListener(DESIGNER_MATERIAL_SAVED_EVENT, onSaved);
  }, [uri]);

  useEffect(() => {
    if (!url) {
      setText(null);
      return;
    }
    let cancelled = false;
    setText(null);
    const fetchUrl = `${url}${url.includes('?') ? '&' : '?'}t=${reloadAt}`;
    void fetch(fetchUrl)
      .then((response) => {
        if (!response.ok) throw new Error(String(response.status));
        return response.text();
      })
      .then((value) => {
        if (!cancelled) setText(value);
      })
      .catch(() => {
        if (!cancelled) setText(null);
      });
    return () => {
      cancelled = true;
    };
  }, [url, reloadAt]);

  return text;
}

function useNodePreviewUri(nodeId: string): string | null {
  const outputUri = useDesignerRunStore((state) => {
    const nodeState = state.nodeStates[nodeId];
    return preferredDesignerPreviewRef(
      nodeState?.output_ref,
      nodeState?.candidate_output_ref,
    )?.uri ?? null;
  });
  const domainOutputUri = useDesignerStore(
    (state) => state.domainGraph?.nodes.find((node) => node.id === nodeId)?.output_ref?.uri ?? null,
  );
  return outputUri || domainOutputUri;
}

function TextPreviewBody({ nodeId, nodeType }: { nodeId: string; nodeType: string }) {
  const uri = useNodePreviewUri(nodeId);
  const text = useDesignerAssetText(uri);
  if (!text) return <PlaceholderBody nodeType={nodeType} />;
  return (
    <p className="designer-node__text-preview" data-testid="designer-node-text-preview">
      {text}
    </p>
  );
}

function TableFrame({
  table,
  empty = false,
}: {
  table: { headers: string[]; rows: string[][] };
  empty?: boolean;
}) {
  return (
    <div className="designer-node__table-wrap">
      <table
        className="designer-node__table"
        data-testid="designer-node-table-preview"
        data-empty={empty ? 'true' : 'false'}
      >
        <thead>
          <tr>
            {table.headers.map((header, index) => (
              <th key={`${header}-${index}`}>{header || '\u00a0'}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {table.rows.map((row, rowIndex) => (
            <tr key={row.join('|') || String(rowIndex)}>
              {row.map((cell, cellIndex) => (
                <td key={`${rowIndex}-${cellIndex}`}>{cell || '\u00a0'}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function TablePreviewBody({ nodeId }: { nodeId: string }) {
  const uri = useNodePreviewUri(nodeId);
  const text = useDesignerAssetText(uri);
  const table = text ? parseMarkdownTable(text) : null;
  if (table) {
    return <TableFrame table={table} />;
  }
  if (text) {
    return (
      <p className="designer-node__text-preview" data-testid="designer-node-text-preview">
        {text}
      </p>
    );
  }
  return <TableFrame table={EMPTY_STORYBOARD_TABLE} empty />;
}

export function DesignerTextNode({ id, data, selected }: NodeProps<DesignerFlowNode>) {
  const nodeData = data as DesignerNodeData;
  const status = useDesignerRunStore((state) => state.nodeStates[id]?.status ?? 'pending');
  const toolbar = (
    <NodeToolbar
      isVisible={selected}
      position={Position.Bottom}
      offset={16}
      align="center"
      className="designer-node-toolbar-portal"
    >
      <DesignerNodeToolbar nodeId={id} nodeType={DESIGNER_NODE_TYPE_TEXT} />
    </NodeToolbar>
  );

  return (
    <DesignerNodeShell
      nodeId={id}
      label={nodeData.label}
      nodeType={DESIGNER_NODE_TYPE_TEXT}
      selected={selected}
      body={
        <NodeOutputFrame running={status === DESIGNER_NODE_STATUS_RUNNING}>
          <TextPreviewBody nodeId={id} nodeType={DESIGNER_NODE_TYPE_TEXT} />
        </NodeOutputFrame>
      }
      toolbar={toolbar}
    />
  );
}

export function DesignerTableNode({ id, data, selected }: NodeProps<DesignerFlowNode>) {
  const nodeData = data as DesignerNodeData;
  const status = useDesignerRunStore((state) => state.nodeStates[id]?.status ?? 'pending');
  const toolbar = (
    <NodeToolbar
      isVisible={selected}
      position={Position.Bottom}
      offset={16}
      align="center"
      className="designer-node-toolbar-portal"
    >
      <DesignerNodeToolbar nodeId={id} nodeType={DESIGNER_NODE_TYPE_TABLE} />
    </NodeToolbar>
  );

  return (
    <DesignerNodeShell
      nodeId={id}
      label={nodeData.label}
      nodeType={DESIGNER_NODE_TYPE_TABLE}
      selected={selected}
      body={
        <NodeOutputFrame running={status === DESIGNER_NODE_STATUS_RUNNING}>
          <TablePreviewBody nodeId={id} />
        </NodeOutputFrame>
      }
      toolbar={toolbar}
    />
  );
}

export function DesignerMediaNode({ id, data, selected }: NodeProps<DesignerFlowNode>) {
  const nodeData = data as DesignerNodeData;
  const nodeType = nodeData.nodeType;
  const status = useDesignerRunStore((state) => state.nodeStates[id]?.status ?? 'pending');
  const previewUri = useNodePreviewUri(id);
  const previewSrc = designerAssetPreviewUrl(previewUri) || previewUri;
  const hasPreview = Boolean(previewSrc);

  let inner: ReactNode = <PlaceholderBody nodeType={nodeType} />;
  if (hasPreview && nodeType === DESIGNER_NODE_TYPE_IMAGE) {
    inner = (
      <img
        className="designer-node__media-preview"
        src={previewSrc || ''}
        alt={nodeData.label}
        data-testid="designer-node-image-preview"
      />
    );
  } else if (hasPreview && nodeType === DESIGNER_NODE_TYPE_VIDEO) {
    inner = (
      <video
        className="designer-node__media-preview"
        src={previewSrc || ''}
        playsInline
        controls={true}
        autoPlay={false}
        data-testid="designer-node-video-preview"
      />
    );
  } else if (hasPreview && nodeType === DESIGNER_NODE_TYPE_AUDIO) {
    inner = (
      <audio
        className="designer-node__audio-preview"
        src={previewSrc || ''}
        controls
        data-testid="designer-node-uploaded-audio"
      />
    );
  }

  const toolbar = supportsNodeToolbar(nodeType) ? (
    <NodeToolbar
      isVisible={selected}
      position={Position.Bottom}
      offset={16}
      align="center"
      className="designer-node-toolbar-portal"
    >
      <DesignerNodeToolbar nodeId={id} nodeType={nodeType} />
    </NodeToolbar>
  ) : null;

  return (
    <DesignerNodeShell
      nodeId={id}
      label={nodeData.label}
      nodeType={nodeType}
      media={isMediaNodeType(nodeType)}
      mediaFilled={hasPreview}
      selected={selected}
      body={
        <NodeOutputFrame running={status === DESIGNER_NODE_STATUS_RUNNING}>
          {inner}
        </NodeOutputFrame>
      }
      toolbar={toolbar}
    />
  );
}

export const designerNodeTypes = {
  [DESIGNER_NODE_TYPE_TEXT]: DesignerTextNode,
  [DESIGNER_NODE_TYPE_TABLE]: DesignerTableNode,
  [DESIGNER_NODE_TYPE_IMAGE]: DesignerMediaNode,
  [DESIGNER_NODE_TYPE_VIDEO]: DesignerMediaNode,
  [DESIGNER_NODE_TYPE_AUDIO]: DesignerMediaNode,
};
