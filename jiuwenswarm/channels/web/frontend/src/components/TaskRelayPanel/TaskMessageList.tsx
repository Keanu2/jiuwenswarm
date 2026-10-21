/**
 * TaskMessageList — relay 任务消息日志的渲染（跨设备任务聊天视图使用）。
 *
 * 视觉与 ChatPanel 的消息流保持同一套语言：``chat-scroll``/``chat-content``
 * 滚动容器；``text`` 按 ``isAssistantSide`` 分侧——用户气泡 ``chat-bubble
 * user`` 右对齐，助手回复走 ``assistant-row`` 头像 + ``chat-bubble
 * assistant``（MarkdownRenderer 渲染 markdown）。``tool_result`` 是交接
 * 导入/回合执行的工具事件（紧凑折叠条目，chat-tool-card 的 token 组合），
 * ``event``/``system`` 居中弱化。列表自管滚动容器并在新消息到达时贴底。
 */
import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronDown, CircleAlert, LoaderCircle, Paperclip, Wrench } from 'lucide-react';
import { MarkdownRenderer } from '../MarkdownRenderer';
import { TeamMemberAvatar } from '../TeamMemberAvatar';
import { isAssistantSide, payloadText, type TaskMessage } from '../../features/taskRelayPanelState';

// ── Tool messages (imported handover history) ───────────────────────────────

/** 交接时经 relay artifact-relay 存档的文件引用（payload.artifacts 项）。 */
interface ArtifactRef {
  name?: unknown;
  size?: unknown;
  download_url?: unknown;
}

function isArtifactRef(value: unknown): value is ArtifactRef {
  if (!value || typeof value !== 'object') return false;
  const ref = value as ArtifactRef;
  // 只认 http(s) 的签名 URL：链接直接可点，也顺带挡掉异常 payload。
  return typeof ref.download_url === 'string' && /^https?:\/\//i.test(ref.download_url);
}

function formatBytes(size: number): string {
  if (size >= 1024 * 1024) return `${(size / (1024 * 1024)).toFixed(1)} MB`;
  if (size >= 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${size} B`;
}

/**
 * 会话交接导入的工具事件（kind=tool_result）：一条 = 一次调用 + 它的结果。
 * 收起时只占一行摘要（payload.text 预渲染），点开看思考/参数/结果——接手
 * 设备靠它还原「上个设备查了什么、拿到了什么」，不必重跑。
 *
 * 该工具产出的文件在交接时已上传到 relay（artifact-relay），签名下载链接
 * 常驻展示（不收进折叠区）：文件就是接续的硬通货，任何设备点开即得。
 */
function ToolResultMessage({ message }: { message: TaskMessage }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const payload: Record<string, unknown> = message.payload && typeof message.payload === 'object' ? (message.payload as Record<string, unknown>) : {};
  const toolName = typeof payload.tool_name === 'string' ? payload.tool_name : '';
  const argumentsText = typeof payload.arguments === 'string' ? payload.arguments : '';
  const resultText = typeof payload.result === 'string' ? payload.result : '';
  const thinking = typeof payload.thinking === 'string' ? payload.thinking : '';
  const failed = payload.status === 'error';
  const artifacts = Array.isArray(payload.artifacts) ? payload.artifacts.filter(isArtifactRef) : [];

  return (
    <div className="flex animate-rise justify-start assistant-row" data-testid="task-relay-detail-message" data-variant="tool">
      {/* 与助手回复同列对齐（空头像占位），工具事件属于 agent 侧的活动。 */}
      <div className="assistant-row__avatar" aria-hidden="true" />
      <div className="chat-bubble-wrapper max-w-[82%] min-w-0 flex-1">
        <div
          className={`rounded-md border px-3 py-2 ${failed ? 'border-danger/40 bg-danger/10' : 'border-border bg-secondary/40'}`}
          data-testid="task-relay-detail-tool-card"
        >
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => setOpen(current => !current)}
              className="inline-flex min-w-0 flex-1 items-center gap-1.5 text-left text-xs text-text-secondary transition-colors hover:text-text"
              data-testid="task-relay-detail-tool-toggle"
              title={open ? t('taskRelay.toolCollapse') : t('taskRelay.toolExpand')}
            >
              {failed ? (
                <CircleAlert className="shrink-0 text-danger" size={14} strokeWidth={1.8} aria-hidden="true" />
              ) : (
                <Wrench className="shrink-0" size={14} strokeWidth={1.8} aria-hidden="true" />
              )}
              <span className="truncate font-medium">
                {t('taskRelay.toolCall')}
                {toolName ? ` · ${toolName}` : ''}
                {failed ? ' · ✕' : ''}
              </span>
              <ChevronDown className={`shrink-0 transition-transform ${open ? '' : '-rotate-90'}`} size={14} strokeWidth={1.8} aria-hidden="true" />
            </button>
          </div>
          {open ? (
            <div className="mt-2 space-y-2 text-xs" data-testid="task-relay-detail-tool-body">
              {thinking ? (
                <div>
                  <p className="text-text-muted">{t('taskRelay.toolThinking')}</p>
                  <p className="whitespace-pre-wrap break-words text-text-muted">{thinking}</p>
                </div>
              ) : null}
              {argumentsText ? (
                <div>
                  <p className="text-text-muted">{t('taskRelay.toolArguments')}</p>
                  <p className="whitespace-pre-wrap break-words">{argumentsText}</p>
                </div>
              ) : null}
              {resultText ? (
                <div>
                  <p className="text-text-muted">{t('taskRelay.toolResult')}</p>
                  <p className="whitespace-pre-wrap break-words">{resultText}</p>
                </div>
              ) : null}
            </div>
          ) : (
            <p className="mt-1 truncate text-xs text-text-muted" data-testid="task-relay-detail-tool-summary">
              {payloadText(message)}
            </p>
          )}
        </div>
        {artifacts.length > 0 ? (
          <div className="mt-1 flex flex-col items-start gap-0.5" data-testid="task-relay-detail-tool-artifacts">
            {artifacts.map(ref => {
              const url = ref.download_url as string;
              const name = typeof ref.name === 'string' && ref.name ? ref.name : 'artifact';
              const size = typeof ref.size === 'number' && ref.size > 0 ? ` (${formatBytes(ref.size)})` : '';
              return (
                <a
                  key={url}
                  href={url}
                  download={name}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex min-w-0 items-center gap-1 text-xs text-accent transition-colors hover:text-accent/80"
                  title={t('taskRelay.toolArtifactHint')}
                >
                  <Paperclip className="shrink-0" size={12} strokeWidth={1.8} aria-hidden="true" />
                  <span className="truncate underline">{name}</span>
                  <span className="shrink-0 text-text-muted">{size}</span>
                </a>
              );
            })}
          </div>
        ) : null}
      </div>
    </div>
  );
}

// ── Message list ────────────────────────────────────────────────────────────

export interface TaskMessageListProps {
  messages: readonly TaskMessage[];
  loading: boolean;
  /** 发送后等待 bridge 答复（一条带 reply_to 的消息落地即清除，由调用方管理）。 */
  awaitingReply: boolean;
}

export function TaskMessageList({ messages, loading, awaitingReply }: TaskMessageListProps) {
  const { t } = useTranslation();
  const listRef = useRef<HTMLDivElement>(null);

  // Keep the newest message in view.
  useEffect(() => {
    const node = listRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [messages]);

  return (
    <div ref={listRef} className="chat-scroll flex-1 overflow-y-auto" data-testid="task-relay-detail-messages">
      <div className="chat-content flex flex-col gap-3">
        {loading && messages.length === 0 ? (
          <div className="flex h-full min-h-40 items-center justify-center" data-testid="task-relay-detail-messages-state" data-variant="loading">
            <LoaderCircle className="animate-spin text-text-muted" size={20} strokeWidth={2} aria-hidden="true" />
          </div>
        ) : messages.length === 0 ? (
          <div
            className="flex h-full min-h-40 items-center justify-center text-sm text-text-muted"
            data-testid="task-relay-detail-messages-state"
            data-variant="empty"
          >
            {t('taskRelay.noMessages')}
          </div>
        ) : (
          messages.map(message => {
            // 交接导入的工具事件：紧凑条目，点开看参数/结果。
            if (message.kind === 'tool_result') {
              return <ToolResultMessage key={message.seq} message={message} />;
            }
            if (message.kind === 'event' || message.kind === 'system') {
              return (
                <div key={message.seq} className="text-center text-xs text-text-muted" data-testid="task-relay-detail-message" data-variant="event">
                  {payloadText(message)}
                </div>
              );
            }
            const assistant = isAssistantSide(message);
            if (assistant) {
              return (
                <div
                  key={message.seq}
                  className="flex animate-rise justify-start assistant-row"
                  data-testid="task-relay-detail-message"
                  data-variant="assistant"
                >
                  <div className="assistant-row__avatar" data-testid="task-relay-detail-message-avatar">
                    <TeamMemberAvatar member="team_leader" />
                  </div>
                  <div className="chat-bubble-wrapper max-w-[82%] min-w-0">
                    <div className="chat-bubble assistant markdown" data-testid="task-relay-detail-message-text">
                      <MarkdownRenderer content={payloadText(message)} className="chat-text chat-markdown" />
                    </div>
                  </div>
                </div>
              );
            }
            return (
              <div key={message.seq} className="flex animate-rise justify-end" data-testid="task-relay-detail-message" data-variant="user">
                <div className="chat-bubble-wrapper max-w-[82%] min-w-0">
                  <div className="chat-bubble user" data-testid="task-relay-detail-message-text">
                    <div className="chat-text">
                      <span className="whitespace-pre-wrap">{payloadText(message)}</span>
                    </div>
                  </div>
                </div>
              </div>
            );
          })
        )}
        {awaitingReply ? (
          <div className="flex animate-rise justify-start assistant-row" data-testid="task-relay-detail-awaiting">
            <div className="assistant-row__avatar" aria-hidden="true" />
            <div className="chat-bubble-wrapper">
              <div className="flex items-center gap-2 py-1 text-sm text-text-muted">
                <LoaderCircle className="animate-spin" size={14} strokeWidth={2} aria-hidden="true" />
                {t('taskRelay.awaitingReply')}
              </div>
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}
