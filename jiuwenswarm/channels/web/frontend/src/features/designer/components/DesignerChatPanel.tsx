import { Paperclip, SendHorizontal, X } from 'lucide-react';
import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { MessageList } from '../../../components/ChatPanel/MessageList';
import { InlineQuestionCard } from '../../../components/ChatPanel/InlineQuestionCard';
import { InteractionSlot } from '../../../components/InteractionSlot';
import { useChatStore } from '../../../stores/chatStore';
import { useSessionStore } from '../../../stores/sessionStore';
import type { Message, UserAnswer } from '../../../types';
import { webClient } from '../../../services/webClient';
import { useDesignerStore } from '../designerStore';
import {
  DESIGNER_REF_LIMITS,
  designerReferenceKindFromMime,
  designerReferencePreviewUrl,
  filesToBootstrapReferences,
  type DesignerReferenceKind,
  type DesignerStoredReference,
} from '../designerReferences';
import { DesignerAssetsPanel } from './DesignerAssetsPanel';

type SidebarTab = 'assistant' | 'assets';

type ComposerDraft = {
  id: string;
  file: File;
  kind: DesignerReferenceKind;
  filename: string;
  previewUrl: string | null;
};

export function DesignerEmptyState({
  variant,
  errorMessage,
}: {
  variant: 'empty' | 'error';
  errorMessage?: string | null;
}) {
  const { t } = useTranslation();

  return (
    <div className="designer-page__state" data-testid="designer-empty-state" data-variant={variant}>
      <div className="designer-page__state-card">
        <h2 className="designer-page__state-title">
          {variant === 'error' ? t('designer.loadErrorTitle') : t('designer.emptyTitle')}
        </h2>
        <p className="designer-page__state-desc">
          {variant === 'error' ? errorMessage || t('designer.loadErrorFallback') : t('designer.emptyDescription')}
        </p>
      </div>
    </div>
  );
}

function useDesignerSessionId(): string | null {
  return useDesignerStore((state) =>
    String(state.domainGraph?.metadata?.session_id ?? '').trim() || null,
  );
}

const EMPTY_MESSAGES: Message[] = [];

function useChatMessages(sessionId: string | null): Message[] {
  return useChatStore((state) => {
    if (!sessionId) return EMPTY_MESSAGES;
    return state.runtimes[sessionId]?.messages ?? EMPTY_MESSAGES;
  });
}

function ReferenceChips({
  items,
  onRemove,
  removeLabel,
}: {
  items: Array<DesignerStoredReference & { previewUrl?: string | null; id?: string }>;
  onRemove?: (id: string) => void;
  removeLabel?: string;
}) {
  if (items.length === 0) return null;
  return (
    <ul className="designer-chat-panel__refs" data-testid="designer-chat-panel-refs">
      {items.map((item, index) => {
        const key = item.id || `${item.kind}-${item.filename}-${index}`;
        const preview = item.previewUrl || designerReferencePreviewUrl(item);
        return (
          <li key={key} className="designer-chat-panel__ref" data-kind={item.kind}>
            {preview ? <img src={preview} alt="" /> : <span>{item.kind}</span>}
            <em title={item.filename}>{item.filename}</em>
            {onRemove && item.id ? (
              <button
                type="button"
                className="designer-chat-panel__ref-remove"
                aria-label={removeLabel || 'remove'}
                onClick={() => onRemove(item.id as string)}
              >
                <X size={12} aria-hidden />
              </button>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}

export function DesignerChatPanel() {
  const { t } = useTranslation();
  const sessionId = useDesignerSessionId();
  const chatMessages = useChatMessages(sessionId);
  const isProcessing = useChatStore((state) =>
    sessionId ? state.runtimes[sessionId]?.isProcessing ?? false : false,
  );
  const bodyRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [draft, setDraft] = useState('');
  const [tab, setTab] = useState<SidebarTab>('assistant');
  const [attachments, setAttachments] = useState<ComposerDraft[]>([]);
  const [attachError, setAttachError] = useState('');
  const [sending, setSending] = useState(false);

  useEffect(() => {
    if (!sessionId) return;
    useChatStore.getState().ensureRuntime(sessionId);
    // The design session runs on the team chain (team.design): the shared
    // MessageList picks its rendering mode from this runtime value and the
    // useWebSocket delta handler coalesces bubbles only for team sessions.
    if (useSessionStore.getState().getRuntime(sessionId)?.mode !== 'team') {
      useSessionStore.getState().setMode(sessionId, 'team');
    }
    useChatStore.getState().setActiveSessionId(sessionId);
  }, [sessionId]);

  const chatBusy = isProcessing || sending;
  const canSend = Boolean(draft.trim() || attachments.length > 0);

  const limitError = useCallback(
    (kind: DesignerReferenceKind) => {
      if (kind === 'image') return t('designer.chat.limitImages');
      if (kind === 'video') return t('designer.chat.limitVideo');
      return t('designer.chat.limitAudio');
    },
    [t],
  );

  const addFiles = useCallback(
    (fileList: FileList | File[]) => {
      const next = Array.from(fileList);
      if (next.length === 0) return;
      setAttachError('');
      setAttachments((current) => {
        const counts = { image: 0, video: 0, audio: 0 };
        for (const item of current) counts[item.kind] += 1;
        const accepted: ComposerDraft[] = [];
        for (const file of next) {
          const kind = designerReferenceKindFromMime(file.type, file.name);
          if (!kind) {
            setAttachError(t('designer.chat.unsupported'));
            continue;
          }
          if (counts[kind] >= DESIGNER_REF_LIMITS[kind]) {
            setAttachError(limitError(kind));
            continue;
          }
          counts[kind] += 1;
          accepted.push({
            id: `${file.name}-${file.size}-${file.lastModified}-${Math.random().toString(36).slice(2, 8)}`,
            file,
            kind,
            filename: file.name,
            previewUrl: kind === 'image' ? URL.createObjectURL(file) : null,
          });
        }
        return accepted.length > 0 ? [...current, ...accepted] : current;
      });
    },
    [limitError, t],
  );

  const removeAttachment = useCallback((id: string) => {
    setAttachments((current) => {
      const target = current.find((item) => item.id === id);
      if (target?.previewUrl?.startsWith('blob:')) URL.revokeObjectURL(target.previewUrl);
      return current.filter((item) => item.id !== id);
    });
  }, []);

  const handleSend = useCallback(() => {
    const content = draft.trim();
    if ((!content && attachments.length === 0) || chatBusy) return;
    setSending(true);
    setTab('assistant');
    const files = attachments.map((item) => item.file);
    void filesToBootstrapReferences(files)
      .then(async (converted) => {
        if (converted.error) {
          const map: Record<string, string> = {
            image_limit: t('designer.chat.limitImages'),
            video_limit: t('designer.chat.limitVideo'),
            audio_limit: t('designer.chat.limitAudio'),
            too_large: t('designer.chat.tooLarge'),
            unsupported: t('designer.chat.unsupported'),
          };
          setAttachError(map[converted.error] || t('designer.chat.unsupported'));
          return;
        }
        setDraft('');
        attachments.forEach((item) => {
          if (item.previewUrl?.startsWith('blob:')) URL.revokeObjectURL(item.previewUrl);
        });
        setAttachments([]);
        if (!sessionId) {
          setAttachError(t('designer.loadErrorFallback'));
          return;
        }
        useChatStore.getState().addMessage(sessionId, {
          id: `designer-user-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
          role: 'user',
          content,
          timestamp: new Date().toISOString(),
        });
        useChatStore.getState().setProcessing(sessionId, true);
        useChatStore.getState().setThinking(sessionId, true);
        // Standard chat pipeline (F01 §8): same request shape as the work/
        // code panels — streaming, interact and history persistence are all
        // the standard team machinery from here on.
        try {
          // Flush pending canvas edits so the leader round reads the latest graph.
          await useDesignerStore.getState().flushSave();
          await webClient.request('chat.send', {
            session_id: sessionId,
            query: content,
            mode: 'team.design',
          });
        } catch (error) {
          const message = error instanceof Error ? error.message : String(error);
          useChatStore.getState().setProcessing(sessionId, false);
          useChatStore.getState().setThinking(sessionId, false);
          useChatStore.getState().addMessage(sessionId, {
            id: `designer-error-${Date.now()}`,
            role: 'assistant',
            content: message || t('designer.chat.updateError'),
            timestamp: new Date().toISOString(),
            isError: true,
          } as Message);
        }
      })
      .finally(() => {
        setSending(false);
      });
  }, [attachments, chatBusy, draft, sessionId, t]);

  const onKeyDown = useCallback(
    (event: KeyboardEvent<HTMLTextAreaElement>) => {
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        handleSend();
      }
    },
    [handleSend],
  );

  const handleUserAnswer = useCallback(
    async (requestId: string, answers: UserAnswer[], source?: string): Promise<boolean> => {
      if (!sessionId) return false;
      const pendingQuestion = useChatStore.getState().getRuntime(sessionId)?.pendingQuestions[0];
      try {
        await webClient.request('chat.send', {
          session_id: sessionId,
          query: '',
          mode: 'team.design',
          request_id: requestId,
          answers,
          ...(source ? { source } : {}),
        });
        if (pendingQuestion) {
          useChatStore.getState().consumePendingQuestion(sessionId, pendingQuestion);
        }
        return true;
      } catch {
        return false;
      }
    },
    [sessionId],
  );

  return (
    <aside
      className="designer-chat-panel designer-chat-panel--expanded"
      aria-label={t('designer.chat.title')}
      data-testid="designer-chat-panel"
      data-tab={tab}
    >
      <div className="designer-chat-panel__header" role="tablist" aria-label={t('designer.sidebar.tabsLabel')}>
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'assistant'}
          className={`designer-chat-panel__tab${tab === 'assistant' ? ' is-active' : ''}`}
          data-testid="designer-sidebar-tab-assistant"
          onClick={() => setTab('assistant')}
        >
          {t('designer.sidebar.assistant')}
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'assets'}
          className={`designer-chat-panel__tab${tab === 'assets' ? ' is-active' : ''}`}
          data-testid="designer-sidebar-tab-assets"
          onClick={() => setTab('assets')}
        >
          {t('designer.sidebar.assets')}
        </button>
      </div>

      {tab === 'assistant' ? (
        <>
          <div
            className="designer-chat-panel__body"
            ref={bodyRef}
            data-testid="designer-chat-panel-body"
            data-timeline-scroll-root
          >
            <InlineQuestionCard onSubmit={handleUserAnswer} />
            <MessageList messages={chatMessages} />
          </div>
          <div className="designer-chat-panel__composer">
            <InteractionSlot onSubmit={handleUserAnswer} />
            {attachments.length > 0 ? (
              <ReferenceChips
                items={attachments.map((item) => ({
                  id: item.id,
                  kind: item.kind,
                  filename: item.filename,
                  previewUrl: item.previewUrl,
                }))}
                onRemove={removeAttachment}
                removeLabel={t('designer.chat.removeAttachment')}
              />
            ) : null}
            {attachError ? (
              <p className="designer-chat-panel__attach-error" data-testid="designer-chat-panel-attach-error">
                {attachError}
              </p>
            ) : null}
            <div className="designer-chat-panel__composer-row">
              <input
                ref={fileInputRef}
                type="file"
                accept="image/*,video/*,audio/*"
                multiple
                hidden
                data-testid="designer-chat-panel-file"
                onChange={(event) => {
                  if (event.target.files) addFiles(event.target.files);
                  event.target.value = '';
                }}
              />
              <button
                type="button"
                className="designer-chat-panel__attach"
                disabled={chatBusy}
                onClick={() => fileInputRef.current?.click()}
                aria-label={t('designer.chat.attach')}
                title={t('designer.chat.attachHint')}
                data-testid="designer-chat-panel-attach"
              >
                <Paperclip size={16} aria-hidden />
              </button>
              <textarea
                className="designer-chat-panel__input"
                placeholder={t('designer.chat.inputPlaceholder')}
                value={draft}
                data-testid="designer-chat-panel-input"
                onChange={(event) => setDraft(event.target.value)}
                onKeyDown={onKeyDown}
              />
              <button
                type="button"
                className="designer-chat-panel__send"
                disabled={chatBusy || !canSend}
                onClick={handleSend}
                aria-label={t('designer.chat.send')}
                data-testid="designer-chat-panel-send"
              >
                <SendHorizontal size={16} aria-hidden />
              </button>
            </div>
          </div>
        </>
      ) : (
        <DesignerAssetsPanel />
      )}
    </aside>
  );
}
