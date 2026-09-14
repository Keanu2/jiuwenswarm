import {
  Headphones,
  Image as ImageIcon,
  Video,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';
import {
  DESIGNER_ADD_TEMPLATES,
  type DesignerAddTemplate,
} from '../designerCanvasNodes';

function TypeIcon({ type }: { type: string }) {
  if (type === 'video') return <Video size={14} aria-hidden />;
  if (type === 'audio') return <Headphones size={14} aria-hidden />;
  return <ImageIcon size={14} aria-hidden />;
}

type DesignerAddNodeMenuProps = {
  title?: string;
  testIdPrefix: string;
  onPick: (template: DesignerAddTemplate) => void;
};

export function DesignerAddNodeMenu({ title, testIdPrefix, onPick }: DesignerAddNodeMenuProps) {
  const { t } = useTranslation();

  return (
    <>
      {title ? <p className="designer-canvas-dock__panel-title">{title}</p> : null}
      <div className="designer-canvas-dock__choices">
        {DESIGNER_ADD_TEMPLATES.map((item) => (
          <button
            key={item.id}
            type="button"
            className="designer-canvas-dock__choice"
            data-testid={`${testIdPrefix}-${item.id}`}
            onClick={() => onPick(item)}
          >
            <TypeIcon type={item.type} />
            {t(`designer.dock.node.${item.id}`)}
          </button>
        ))}
      </div>
    </>
  );
}
