import { CirclePlus } from 'lucide-react';
import { useCallback, useRef, type MouseEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { Handle, Position } from '@xyflow/react';
import {
  buildManualDesignerNode,
  positionLeftOfNode,
  positionRightOfNode,
  type DesignerAddTemplate,
  type DesignerNodePortSide,
} from '../../designerCanvasNodes';
import { useDesignerStore } from '../../designerStore';
import { useDesignerUiStore } from '../../designerUiStore';
import { useComfyuiImport } from '../../useComfyuiImport';
import { DesignerAddNodeMenu } from '../DesignerAddNodeMenu';

/** Pointer travel (px) above which a press counts as edge-drag, not a menu click. */
const PORT_CLICK_MOVE_PX = 6;

type DesignerNodePortControlProps = {
  nodeId: string;
  side: DesignerNodePortSide;
};

export function DesignerNodePortControl({ nodeId, side }: DesignerNodePortControlProps) {
  const { t } = useTranslation();
  const domainGraph = useDesignerStore((state) => state.domainGraph);
  const addNode = useDesignerStore((state) => state.addNode);
  const nodePortMenu = useDesignerUiStore((state) => state.nodePortMenu);
  const toggleNodePortMenu = useDesignerUiStore((state) => state.toggleNodePortMenu);
  const closeDock = useDesignerUiStore((state) => state.closeDock);
  const menuOpen = nodePortMenu?.nodeId === nodeId && nodePortMenu.side === side;
  const importComfyui = useComfyuiImport();
  const suppressClickRef = useRef(false);
  const isSuccessor = side === 'successor';
  const testId = isSuccessor ? 'designer-node-add' : 'designer-node-add-predecessor';
  const menuTestId = isSuccessor ? 'designer-node-add-menu' : 'designer-node-add-predecessor-menu';
  const testIdPrefix = isSuccessor ? 'designer-node-add' : 'designer-node-add-predecessor';
  const ariaLabel = isSuccessor
    ? t('designer.nodeActions.add')
    : t('designer.nodeActions.addPredecessor');

  const placeNear = useCallback(
    (source: { id?: string; layout?: { x?: number; y?: number; width?: number; height?: number } }) => {
      const peers = domainGraph?.nodes ?? [];
      return isSuccessor ? positionRightOfNode(source, peers) : positionLeftOfNode(source, peers);
    },
    [domainGraph?.nodes, isSuccessor],
  );

  const onMouseDown = useCallback(
    (event: MouseEvent) => {
      const startX = event.clientX;
      const startY = event.clientY;
      suppressClickRef.current = false;
      const onMove = (moveEvent: PointerEvent) => {
        const dx = moveEvent.clientX - startX;
        const dy = moveEvent.clientY - startY;
        if (dx * dx + dy * dy > PORT_CLICK_MOVE_PX * PORT_CLICK_MOVE_PX) {
          suppressClickRef.current = true;
          if (useDesignerUiStore.getState().nodePortMenu) {
            closeDock();
          }
        }
      };
      const onUp = () => {
        window.removeEventListener('pointermove', onMove);
        window.removeEventListener('pointerup', onUp);
      };
      window.addEventListener('pointermove', onMove);
      window.addEventListener('pointerup', onUp);
    },
    [closeDock],
  );

  const onClick = useCallback(
    (event: MouseEvent) => {
      event.stopPropagation();
      event.preventDefault();
      if (suppressClickRef.current) return;
      toggleNodePortMenu(nodeId, side);
    },
    [nodeId, side, toggleNodePortMenu],
  );

  const onPick = useCallback(
    (template: DesignerAddTemplate) => {
      const source = domainGraph?.nodes.find((node) => node.id === nodeId);
      if (!domainGraph || !source) return;
      const node = buildManualDesignerNode({
        template,
        existing: domainGraph.nodes,
        position: placeNear(source),
      });
      addNode(node);
      closeDock();
    },
    [addNode, closeDock, domainGraph, nodeId, placeNear],
  );

  const onImportComfyui = useCallback(
    (files: File[]) => {
      const source = domainGraph?.nodes.find((node) => node.id === nodeId);
      if (!domainGraph || !source) return;
      const origin = placeNear(source);
      closeDock();
      void importComfyui(files, origin);
    },
    [closeDock, domainGraph, importComfyui, nodeId, placeNear],
  );

  return (
    <>
      <Handle
        type={isSuccessor ? 'source' : 'target'}
        position={isSuccessor ? Position.Right : Position.Left}
        className={`designer-node__port designer-node__port--${side}${menuOpen ? ' is-open' : ''}`}
        aria-label={ariaLabel}
        title={ariaLabel}
        aria-expanded={menuOpen}
        data-testid={testId}
        onClick={onClick}
        onMouseDown={onMouseDown}
      >
        {/* Icon is offset from the handle box so edges still dock to the node border. */}
        <span className="designer-node__port-icon" aria-hidden>
          <CirclePlus size={22} strokeWidth={1.75} />
        </span>
      </Handle>
      {menuOpen ? (
        <div
          className={`designer-node__port-menu designer-node__port-menu--${side} chat-mode-select__menu chat-input-attach-menu nodrag nopan nowheel`}
          role="menu"
          data-testid={menuTestId}
          onClick={(event) => event.stopPropagation()}
          onMouseDown={(event) => event.stopPropagation()}
          onPointerDown={(event) => event.stopPropagation()}
        >
          <DesignerAddNodeMenu
            title={t(
              isSuccessor
                ? 'designer.nodeActions.successorMenuTitle'
                : 'designer.nodeActions.predecessorMenuTitle',
            )}
            testIdPrefix={testIdPrefix}
            onPick={onPick}
            onImportComfyui={onImportComfyui}
          />
        </div>
      ) : null}
    </>
  );
}
