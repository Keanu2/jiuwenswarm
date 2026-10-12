import { Headphones, Image as ImageIcon, Video, Workflow } from 'lucide-react';
import { useRef, type ChangeEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { DESIGNER_ADD_TEMPLATES, type DesignerAddTemplate } from '../designerCanvasNodes';

function TypeIcon({ type }: { type: string }) {
  if (type === 'video') return <Video size={14} aria-hidden />;
  if (type === 'audio') return <Headphones size={14} aria-hidden />;
  return <ImageIcon size={14} aria-hidden />;
}

type DesignerAddNodeMenuProps = {
  title?: string;
  testIdPrefix: string;
  onPick: (template: DesignerAddTemplate) => void;
  /** Chosen ComfyUI workflow JSON files. */
  onImportComfyui: (files: File[]) => void;
};

export function DesignerAddNodeMenu({ title, testIdPrefix, onPick, onImportComfyui }: DesignerAddNodeMenuProps) {
  const { t } = useTranslation();
  const fileInputRef = useRef<HTMLInputElement>(null);

  const onFileChange = (event: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files ?? []);
    event.target.value = '';
    if (files.length) onImportComfyui(files);
  };

  return (
    <>
      {title ? (
        <p className="designer-node__port-menu-title" data-testid={`${testIdPrefix}-menu-title`}>
          {title}
        </p>
      ) : null}
      {DESIGNER_ADD_TEMPLATES.map((item) => (
        <button
          key={item.id}
          type="button"
          className="chat-mode-select__option"
          role="menuitem"
          data-testid={`${testIdPrefix}-${item.id}`}
          onClick={() => onPick(item)}
        >
          <span className="chat-mode-select__option-main">
            <span className="chat-mode-select__icon chat-mode-select__icon--asset" aria-hidden>
              <TypeIcon type={item.type} />
            </span>
            <span className="chat-mode-select__label">{t(`designer.dock.node.${item.id}`)}</span>
          </span>
        </button>
      ))}
      <button
        type="button"
        className="chat-mode-select__option"
        role="menuitem"
        title={t('designer.dock.comfyuiHint')}
        data-testid={`${testIdPrefix}-comfyui`}
        onClick={() => fileInputRef.current?.click()}
      >
        <span className="chat-mode-select__option-main">
          <span className="chat-mode-select__icon chat-mode-select__icon--asset" aria-hidden>
            <Workflow size={14} />
          </span>
          <span className="chat-mode-select__label">{t('designer.dock.node.comfyui')}</span>
        </span>
      </button>
      <input
        ref={fileInputRef}
        type="file"
        accept=".json,application/json"
        multiple
        hidden
        data-testid={`${testIdPrefix}-comfyui-input`}
        onChange={onFileChange}
      />
    </>
  );
}
