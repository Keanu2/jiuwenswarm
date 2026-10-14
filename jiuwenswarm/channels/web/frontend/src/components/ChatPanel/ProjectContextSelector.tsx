import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import { ProjectCreateMenu, type ProjectCreateMode } from '../../multi-session/sidebar/ProjectCreateMenu';
import { projectCreateErrorKey } from '../../multi-session/sidebar/projectCreateErrors';
import {
  isLikelyAbsolutePath,
  isProjectDirectoryPickerSupported,
  selectProjectDirectory,
} from '../../features/workspace/projectDirectoryPicker';
import { useAdaptiveTooltip } from '../../hooks/useAdaptiveTooltip';
import { getInputProjectOptions } from './projectSelection';
import type { ProjectInfo } from '../../types';

export type WorkIconName = 'add' | 'arrow' | 'check' | 'close' | 'collapse' | 'expand' | 'folder' | 'search';

export function WorkIcon({ name, className }: { name: WorkIconName; className?: string }) {
  return <span className={clsx('chat-work-icon', `chat-work-icon--${name}`, className)} aria-hidden="true" />;
}

export interface ProjectContextSelectorProps {
  /** Candidate projects (work-mode scoped by the caller's workspace store). */
  projects: ProjectInfo[];
  /** Project shown on the trigger (session-resolved by the caller). */
  displayedProject: ProjectInfo | null;
  /** Project id that gets the active check mark; defaults to displayedProject. */
  selectedProjectId?: string | null;
  /** Select a project; null clears the selection. */
  onSelect: (project: ProjectInfo | null) => void;
  /** Create a project (same signature as workspaceStore.createProject); throws on failure. */
  onCreateProject: (name: string, projectDir: string) => Promise<ProjectInfo>;
  /** Trigger label when nothing is selected. */
  placeholder: string;
  disabled?: boolean;
  onMenuOpenChange?: (open: boolean) => void;
  testIdPrefix?: string;
}

function ProjectAddSubmenu({ onCreate }: { onCreate: (mode: ProjectCreateMode) => void }) {
  const { t } = useTranslation();
  return (
    <div className="chat-work-select__add" role="none" data-testid="chat-panel-work-select-add">
      <button
        type="button"
        className="chat-work-select__option chat-work-select__option--compact"
        role="menuitem"
        data-testid="chat-panel-work-select-add-trigger"
        aria-haspopup="menu"
      >
        <WorkIcon name="add" />
        <span>{t('multiSession.project.addNewProject')}</span>
        <WorkIcon name="arrow" className="chat-work-select__arrow" />
      </button>
      <div className="chat-work-select__submenu" role="menu">
        <ProjectCreateMenu
          onCreate={onCreate}
          itemClassName="chat-work-select__option chat-work-select__option--compact"
          blankIcon={<WorkIcon name="add" />}
          existingIcon={<WorkIcon name="folder" />}
        />
      </div>
    </div>
  );
}

/**
 * Work-panel project selector (chat-work-select): trigger + dropdown + create dialog.
 * Shared by the chat InputArea (work/code) and the DesignerLanding composer (design).
 */
export function ProjectContextSelector({
  projects,
  displayedProject,
  selectedProjectId,
  onSelect,
  onCreateProject,
  placeholder,
  disabled = false,
  onMenuOpenChange,
  testIdPrefix = 'chat-panel-work-select',
}: ProjectContextSelectorProps) {
  const { t } = useTranslation();
  const [menuOpen, setMenuOpen] = useState(false);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [createMode, setCreateMode] = useState<ProjectCreateMode>('blank');
  const [nameDraft, setNameDraft] = useState('');
  const [dirDraft, setDirDraft] = useState('');
  const [dirError, setDirError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const menuRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const { tooltip: clearTooltipNode, handlers: clearTooltipHandlers } = useAdaptiveTooltip({ placement: 'top' });
  const { tooltip: triggerTooltipNode, handlers: triggerTooltipHandlers } = useAdaptiveTooltip({ placement: 'top' });

  const options = useMemo(() => getInputProjectOptions(projects, search), [projects, search]);
  const hasOptions = useMemo(() => getInputProjectOptions(projects).length > 0, [projects]);
  const activeId = selectedProjectId ?? displayedProject?.project_id ?? null;

  const closeMenu = useCallback(() => setMenuOpen(false), []);

  useEffect(() => {
    onMenuOpenChange?.(menuOpen);
  }, [menuOpen, onMenuOpenChange]);

  useEffect(() => {
    if (!dirError || dialogOpen) return;
    const timeoutId = window.setTimeout(() => setDirError(null), 3000);
    return () => window.clearTimeout(timeoutId);
  }, [dirError, dialogOpen]);

  useEffect(() => {
    if (!menuOpen) return;
    const handlePointerDown = (event: PointerEvent) => {
      if (!menuRef.current?.contains(event.target as Node)) closeMenu();
    };
    const handleKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') closeMenu();
    };
    document.addEventListener('pointerdown', handlePointerDown);
    document.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('pointerdown', handlePointerDown);
      document.removeEventListener('keydown', handleKeyDown);
    };
  }, [menuOpen, closeMenu]);

  const openCreateDialog = useCallback(
    async (mode: ProjectCreateMode) => {
      setDirError(null);
      setCreateMode(mode);
      closeMenu();

      if (mode === 'blank') {
        setNameDraft('');
        setDirDraft('');
        setDialogOpen(true);
        return;
      }

      if (!isProjectDirectoryPickerSupported()) {
        setNameDraft('');
        setDirDraft('');
        setDialogOpen(true);
        return;
      }

      const result = await selectProjectDirectory();
      if (!result.ok) {
        if (result.reason !== 'cancelled') {
          setNameDraft('');
          setDirDraft('');
          setDialogOpen(true);
          setDirError(
            result.reason === 'unsupported'
              ? t('multiSession.project.directoryPickerUnsupported')
              : result.message || t('multiSession.project.directoryPickerFailed'),
          );
        }
        return;
      }

      try {
        await onCreateProject(result.name, result.path);
      } catch (error) {
        const errorKey = projectCreateErrorKey(error);
        setDirError(errorKey ? t(errorKey) : error instanceof Error ? error.message : String(error));
      }
    },
    [closeMenu, onCreateProject, t],
  );

  const handleConfirmCreate = useCallback(async () => {
    const name = nameDraft.trim();
    const projectDir = createMode === 'blank' ? '' : dirDraft.trim();
    if (!name || (createMode === 'existing' && !projectDir)) return;
    setDirError(null);
    if (projectDir && (!isLikelyAbsolutePath(projectDir) || projectDir.startsWith('~/'))) {
      setDirError(t('multiSession.project.absolutePathError'));
      return;
    }
    try {
      await onCreateProject(name, projectDir);
      setNameDraft('');
      setDirDraft('');
      setDialogOpen(false);
    } catch (error) {
      const errorKey = projectCreateErrorKey(error);
      setDirError(errorKey ? t(errorKey) : error instanceof Error ? error.message : String(error));
    }
  }, [createMode, dirDraft, nameDraft, onCreateProject, t]);

  return (
    <>
      <div
        ref={menuRef}
        className={clsx('chat-work-select', menuOpen && 'chat-work-select--open')}
        data-testid={testIdPrefix}
      >
        <button
          ref={triggerRef}
          type="button"
          className={clsx('chat-work-select__trigger', displayedProject && 'chat-work-select__trigger--selected')}
          data-testid={`${testIdPrefix}-trigger`}
          onClick={() => !disabled && setMenuOpen((open) => !open)}
          disabled={disabled}
          data-tooltip={
            displayedProject?.project_dir
            || (disabled ? t('multiSession.project.lockedProjectTitle') : placeholder)
          }
          {...triggerTooltipHandlers}
        >
          <WorkIcon name="folder" className="chat-work-select__root-icon" />
          <span>{displayedProject ? displayedProject.name : placeholder}</span>
          {displayedProject && !disabled ? (
            <span className="chat-work-select__trigger-action">
              <svg
                className="chat-work-select__chevron"
                width="12"
                height="12"
                viewBox="0 0 20 20"
                fill="none"
                stroke="currentColor"
                strokeWidth={1.8}
                aria-hidden="true"
              >
                <path strokeLinecap="round" strokeLinejoin="round" d="M6 8l4 4 4-4" />
              </svg>
              <button
                type="button"
                className="chat-work-select__clear"
                data-testid={`${testIdPrefix}-clear`}
                aria-label={t('multiSession.project.clearProject')}
                data-tooltip={t('multiSession.project.clearProject')}
                {...clearTooltipHandlers}
                onMouseEnter={(event) => {
                  triggerTooltipHandlers.onMouseLeave();
                  clearTooltipHandlers.onMouseEnter(event);
                }}
                onMouseLeave={() => {
                  clearTooltipHandlers.onMouseLeave();
                  if (triggerRef.current) {
                    triggerTooltipHandlers.onMouseEnter({ currentTarget: triggerRef.current });
                  }
                }}
                onClick={(e) => {
                  e.stopPropagation();
                  onSelect(null);
                  closeMenu();
                }}
              >
                <WorkIcon name="close" />
              </button>
              {clearTooltipNode}
            </span>
          ) : (
            <svg
              className="chat-work-select__chevron"
              width="12"
              height="12"
              viewBox="0 0 20 20"
              fill="none"
              stroke="currentColor"
              strokeWidth={1.8}
              aria-hidden="true"
            >
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 8l4 4 4-4" />
            </svg>
          )}
        </button>
        {triggerTooltipNode}
        {menuOpen && !disabled ? (
          <div
            className={clsx('chat-work-select__menu', hasOptions && 'chat-work-select__menu--projects')}
            role="menu"
            data-testid={`${testIdPrefix}-menu`}
          >
            {!hasOptions ? (
              <ProjectCreateMenu
                onCreate={(mode) => {
                  void openCreateDialog(mode);
                }}
                itemClassName="chat-work-select__option chat-work-select__option--compact"
                blankIcon={<WorkIcon name="add" />}
                existingIcon={<WorkIcon name="folder" />}
              />
            ) : (
              <>
                <label className="chat-work-select__search-wrap">
                  <WorkIcon name="search" />
                  <input
                    className="chat-work-select__search"
                    data-testid={`${testIdPrefix}-search`}
                    value={search}
                    onChange={(event) => setSearch(event.target.value)}
                    placeholder={t('multiSession.project.searchProject')}
                  />
                </label>
                <div className="chat-work-select__options" data-testid={`${testIdPrefix}-options`}>
                  {options.map((project) => {
                    const active = activeId === project.project_id;
                    return (
                      <button
                        type="button"
                        key={project.project_id}
                        className={clsx('chat-work-select__option', active && 'is-active')}
                        data-testid={`${testIdPrefix}-option`}
                        data-variant={project.project_id}
                        onClick={() => {
                          onSelect(project);
                          closeMenu();
                        }}
                        role="menuitemradio"
                        aria-checked={active}
                        title={project.project_dir}
                      >
                        <WorkIcon name="folder" />
                        <span>{project.name}</span>
                        {active ? <WorkIcon name="check" className="chat-work-select__check" /> : null}
                      </button>
                    );
                  })}
                  {options.length === 0 ? (
                    <div className="chat-work-select__empty" data-testid={`${testIdPrefix}-empty`}>
                      {t('multiSession.project.noProjectMatches')}
                    </div>
                  ) : null}
                </div>
                <ProjectAddSubmenu
                  onCreate={(mode) => {
                    void openCreateDialog(mode);
                  }}
                />
              </>
            )}
          </div>
        ) : null}
      </div>
      {dirError && !dialogOpen ? (
        <div className="app-toast-wrapper app-toast-wrapper--top-center">
          <div
            className="app-session-toast"
            role="status"
            aria-live="polite"
            data-testid={`${testIdPrefix}-error-toast`}
          >
            {dirError}
          </div>
        </div>
      ) : null}
      {dialogOpen ? (
        <div className="chat-work-dialog-backdrop" role="presentation" data-testid="chat-panel-work-dialog">
          <form
            className="chat-work-dialog"
            data-testid="chat-panel-work-dialog-form"
            onSubmit={(event) => {
              event.preventDefault();
              void handleConfirmCreate();
            }}
          >
            <button
              type="button"
              className="chat-work-dialog__close"
              data-testid="chat-panel-work-dialog-close"
              aria-label={t('common.close')}
              onClick={() => {
                setDirDraft('');
                setNameDraft('');
                setDirError(null);
                setDialogOpen(false);
              }}
            >
              <WorkIcon name="close" />
            </button>
            <div className="chat-work-dialog__title" data-testid="chat-panel-work-dialog-title">
              {createMode === 'existing'
                ? t('multiSession.project.selectExisting')
                : t('multiSession.project.createBlank')}
            </div>
            <input
              className="chat-work-dialog__input"
              data-testid="chat-panel-work-dialog-name-input"
              value={nameDraft}
              onChange={(event) => setNameDraft(event.target.value)}
              placeholder={t('multiSession.project.namePlaceholder')}
              autoFocus
            />
            {createMode === 'existing' ? (
              <input
                className="chat-work-dialog__input"
                data-testid="chat-panel-work-dialog-path-input"
                data-variant="existing"
                value={dirDraft}
                onChange={(event) => setDirDraft(event.target.value)}
                placeholder={t('multiSession.project.pathPlaceholder')}
              />
            ) : null}
            {dirError ? (
              <div className="chat-work-dialog__error" data-testid="chat-panel-work-dialog-error">
                {dirError}
              </div>
            ) : null}
            <div className="chat-work-dialog__actions" data-testid="chat-panel-work-dialog-actions">
              <button
                type="button"
                data-testid="chat-panel-work-dialog-cancel"
                onClick={() => {
                  setDirDraft('');
                  setNameDraft('');
                  setDirError(null);
                  setDialogOpen(false);
                }}
              >
                {t('multiSession.project.cancel')}
              </button>
              <button
                type="submit"
                data-testid="chat-panel-work-dialog-confirm"
                disabled={!nameDraft.trim() || (createMode === 'existing' && !dirDraft.trim())}
              >
                {t('multiSession.project.confirm')}
              </button>
            </div>
          </form>
        </div>
      ) : null}
    </>
  );
}
