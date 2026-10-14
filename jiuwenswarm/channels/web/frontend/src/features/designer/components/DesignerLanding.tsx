import { Loader2, Plus, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react';
import { useTranslation } from 'react-i18next';
import beeStaticIcon from '../../../assets/bee-static.png';
import AttachmentIcon from '../../../assets/agent-management/attachment.svg?react';
import sendActiveIcon from '../../../assets/send_active.svg';
import sendIcon from '../../../assets/send.svg';
import ChatModelSelector from '../../../components/ChatPanel/ChatModelSelector';
import { ProjectContextSelector } from '../../../components/ChatPanel/ProjectContextSelector';
import { generateUuidV4 } from '../../../utils/uuid';
import { useWorkspaceStore } from '../../../stores';
import { useChatStore } from '../../../stores/chatStore';
import type { Message } from '../../../types';
import {
  resolveChatModelSelection,
  useSessionStore,
} from '../../../stores/sessionStore';
import { registerCreatedConversation } from '../../../multi-session/state/newConversationLifecycle';
import { webClient } from '../../../services/webClient';
import { useDesignerChatStore } from '../designerChatStore';
import { designerWorkspaceClient } from '../designerGraphClient';
import { useDesignerStore } from '../designerStore';
import { filesToBootstrapReferences } from '../designerReferences';
import './DesignerErrorToast.css';
import './DesignerLanding.css';

type DesignerLandingProps = {
  onCreated: (projectId: string, sessionId: string) => void;
};

export function DesignerLanding({ onCreated }: DesignerLandingProps) {
  const { t, i18n } = useTranslation();
  const isZh = i18n.language.startsWith('zh');
  const [prompt, setPrompt] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const [files, setFiles] = useState<File[]>([]);
  const [attachMenuOpen, setAttachMenuOpen] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const attachMenuRef = useRef<HTMLDivElement>(null);
  const createAttemptRef = useRef<{ signature: string; token: string } | null>(null);

  const { projects, selectedProject, setSelectedProject, createProject, loadProjects } = useWorkspaceStore(
    (state) => ({
      projects: state.projects,
      selectedProject: state.selectedProject,
      setSelectedProject: state.setSelectedProject,
      createProject: state.createProject,
      loadProjects: state.loadProjects,
    }),
  );

  const designProjects = useMemo(() => {
    const list = projects.filter((project) => project.work_mode === 'design');
    return list.sort((a, b) => (b.last_message_at ?? 0) - (a.last_message_at ?? 0));
  }, [projects]);

  const displayedDesignProject = useMemo(
    () => (selectedProject?.work_mode === 'design' ? selectedProject : null),
    [selectedProject],
  );

  useEffect(() => {
    void loadProjects();
  }, [loadProjects]);

  useEffect(() => {
    if (!attachMenuOpen) return;
    const closeMenu = (event: PointerEvent) => {
      if (!attachMenuRef.current?.contains(event.target as Node)) {
        setAttachMenuOpen(false);
      }
    };
    window.addEventListener('pointerdown', closeMenu);
    return () => window.removeEventListener('pointerdown', closeMenu);
  }, [attachMenuOpen]);

  const submit = useCallback(async () => {
    const content = prompt.trim();
    if (!content || submitting) return;
    setSubmitting(true);
    setError('');
    useDesignerStore.getState().beginBootstrapEntry(content);
    try {
      const converted = await filesToBootstrapReferences(files);
      if (converted.error) throw new Error(t('designer.chat.unsupported'));
      const activeSessionId = useChatStore.getState().activeSessionId;
      const sessionState = useSessionStore.getState();
      const selectedModel = resolveChatModelSelection(
        sessionState.chatAvailableModels,
        sessionState.runtimes[activeSessionId ?? '']?.selectedModelName ?? null,
        sessionState.defaultModelName,
      );
      const modelName = selectedModel?.model_name;
      const signature = JSON.stringify({
        prompt: content,
        modelName,
        files: files.map((file) => [
          file.name,
          file.size,
          file.type,
          file.lastModified,
        ]),
      });
      let attempt = createAttemptRef.current;
      if (attempt?.signature !== signature) {
        attempt = {
          signature,
          token: generateUuidV4(),
        };
        createAttemptRef.current = attempt;
      }
      const workspace = await designerWorkspaceClient.create({
        prompt: content,
        createToken: attempt.token,
        modelName,
        projectId: displayedDesignProject?.project_id,
        references: converted.refs,
      });
      createAttemptRef.current = null;
      useDesignerStore.getState().applyGraph(workspace.graph);
      useDesignerChatStore.getState().replaceMessages(
        workspace.graph.graph_id,
        workspace.messages,
      );
      const createdSessionId = workspace.session.session_id;
      // Register the design workspace session in the global session store so the
      // sidebar and chat lifecycle treat it like any other newly created session.
      const createdSession = registerCreatedConversation(
        createdSessionId,
        {
          mode: 'team',
          selectedModelName: modelName ?? null,
          projectDir: workspace.session.project_dir || undefined,
          persistSession: true,
        },
        Date.now(),
        content,
        {
          project_id: workspace.project.project_id || '',
          project_dir: workspace.project.project_dir || '',
          work_mode: 'design',
          persist_session: true,
        },
      );
      // Work-mode send semantics: the user bubble is client-side and the first
      // leader turn rides the standard chat.send(team.design) pipeline, which
      // owns streaming, history persistence and the processing lifecycle.
      useChatStore.getState().ensureRuntime(createdSessionId);
      useChatStore.getState().addMessage(createdSessionId, {
        id: `designer-user-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        role: 'user',
        content,
        timestamp: new Date().toISOString(),
      });
      useChatStore.getState().setProcessing(createdSessionId, true);
      useChatStore.getState().setThinking(createdSessionId, true);
      // Navigate before the round starts: designer.push handlers
      // (designer.graph.updated / leader.activity) only bind once DesignerPage
      // mounts, and the chat.send response resolves only at stream end —
      // awaiting it here would silently drop the whole first-round canvas
      // event stream.
      useWorkspaceStore.getState().upsertSession(createdSession, { isNew: true });
      // Refresh the project / task lists in the background; navigation should
      // not wait for them.
      void useWorkspaceStore.getState().loadProjects();
      onCreated(workspace.project.project_id, createdSessionId);
      webClient
        .request('chat.send', {
          session_id: createdSessionId,
          query: content,
          mode: 'team.design',
          ...(modelName ? { model_name: modelName } : {}),
        })
        .catch((reason) => {
          // Delivery failed before the page took over the stream.
          const message = reason instanceof Error ? reason.message : String(reason);
          useChatStore.getState().setProcessing(createdSessionId, false);
          useChatStore.getState().setThinking(createdSessionId, false);
          useChatStore.getState().addMessage(createdSessionId, {
            id: `designer-error-${Date.now()}`,
            role: 'assistant',
            content: message || t('designer.chat.updateError'),
            timestamp: new Date().toISOString(),
            isError: true,
          } as Message);
        });
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : String(reason);
      useDesignerStore.getState().failBootstrapEntry(message);
      setError(message || t('designer.chat.bootstrapError'));
    } finally {
      setSubmitting(false);
    }
  }, [displayedDesignProject, files, onCreated, prompt, submitting, t]);

  const onKeyDown = useCallback((event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      void submit();
    }
  }, [submit]);

  return (
    <section className="designer-landing" data-testid="designer-landing">
      <div className="designer-landing__content">
        <h1 className="designer-landing__heading">
          <span className="chat-welcome__heading-highlight">WorkSwarm</span>
          <span>{isZh ? "轻松提供设计创意！" : "makes design easier!"}</span>
        </h1>
        <div className="designer-landing__composer-stage">
          <img
            className="designer-landing__bee"
            src={beeStaticIcon}
            alt={t('chat.welcomeLogoAlt')}
          />
          <div className="designer-landing__composer">
            <div className="designer-landing__input">
              {files.length > 0 ? (
                <div className="designer-landing__files">
                  {files.map((file, index) => (
                    <span key={`${file.name}-${file.size}-${index}`}>
                      {file.name}
                      <button
                        type="button"
                        onClick={() => setFiles((current) => current.filter((_, itemIndex) => itemIndex !== index))}
                        aria-label={t('designer.chat.removeAttachment')}
                      >
                        <X size={12} />
                      </button>
                    </span>
                  ))}
                </div>
              ) : null}
              <textarea
                className='chat-input-editor'
                value={prompt}
                onChange={(event) => setPrompt(event.target.value)}
                onKeyDown={onKeyDown}
                disabled={submitting}
                autoFocus
                placeholder={t('designer.chat.emptyHint')}
                data-testid="designer-landing-input"
              />
              <div className="designer-landing__toolbar">
                <div ref={attachMenuRef} className="chat-input-attach-menu-anchor">
                  <button
                    type="button"
                    className={`chat-input-btn chat-input-btn--add-file${attachMenuOpen ? ' chat-input-btn--menu-open' : ''}${submitting ? ' chat-input-btn--disabled' : ''}`}
                    onClick={() => setAttachMenuOpen((open) => !open)}
                    disabled={submitting}
                    aria-label={t('chat.addFile')}
                    aria-haspopup="menu"
                    aria-expanded={attachMenuOpen}
                    data-testid="designer-landing-attach"
                  >
                    <Plus size={17} strokeWidth={1.8} aria-hidden />
                  </button>
                  {attachMenuOpen ? (
                    <div className="designer-landing__attach-menu chat-mode-select__menu chat-input-attach-menu" role="menu">
                      <button
                        type="button"
                        className="chat-mode-select__option"
                        role="menuitem"
                        onClick={() => {
                          setAttachMenuOpen(false);
                          fileInputRef.current?.click();
                        }}
                        data-testid="designer-landing-attach-file"
                      >
                        <span className="chat-mode-select__option-main">
                          <span className="chat-mode-select__icon chat-mode-select__icon--asset" aria-hidden>
                            <AttachmentIcon />
                          </span>
                          <span className="chat-mode-select__label">{t('chat.addFile')}</span>
                        </span>
                      </button>
                    </div>
                  ) : null}
                </div>
                <div className="designer-landing__actions">
                  <ChatModelSelector disabled={submitting} />
                  <button
                    type="button"
                    className={prompt.trim() && !submitting ? 'designer-landing__send designer-landing__send--active' : 'designer-landing__send'}
                    onClick={() => void submit()}
                    disabled={!prompt.trim() || submitting}
                    aria-label={t('chat.send')}
                    data-testid="designer-landing-submit"
                  >
                    {submitting
                      ? <Loader2 className="designer-landing__spinner" size={18} aria-hidden />
                      : (
                        <img
                          src={prompt.trim() ? sendActiveIcon : sendIcon}
                          alt=""
                          aria-hidden
                        />
                      )}
                  </button>
                </div>
              </div>
            </div>
          </div>
          <input
            ref={fileInputRef}
            type="file"
            multiple
            accept="image/*,video/*,audio/*"
            hidden
            onChange={(event) => {
              const selected = Array.from(event.target.files ?? []);
              setFiles((current) => [...current, ...selected]);
              event.target.value = '';
            }}
          />
        </div>
        <div className="designer-landing__context-row" data-testid="designer-landing-context-row">
          <ProjectContextSelector
            projects={designProjects}
            displayedProject={displayedDesignProject}
            placeholder={t('designer.selectProject')}
            disabled={submitting}
            onSelect={setSelectedProject}
            onCreateProject={createProject}
            testIdPrefix="designer-landing-work-select"
          />
        </div>
      </div>
      <div className="chat-ai-disclaimer designer-landing__disclaimer" data-testid="designer-landing-ai-disclaimer">
        {t('share.aiNotice')}
      </div>
      {error ? (
        <div className="app-toast-wrapper app-toast-wrapper--top-center" data-testid="designer-landing-error-toast">
          <div className="designer-error-toast" role="alert">{error}</div>
        </div>
      ) : null}
    </section>
  );
}
