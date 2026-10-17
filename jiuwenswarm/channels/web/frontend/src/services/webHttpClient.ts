/**
 * 北向 A2 HTTP/SSE 客户端（`getWebTransport()==='http'`：显式 transport 或企业默认）。
 * 对外口与 WebClient 相同：connect / request / on。映射与泵流只发生在这里。
 */
import { WsEvent } from '../types';
import { isEnterprise } from '../edition';

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function pickHeaderString(value: unknown): string | undefined {
  if (typeof value !== 'string') {
    return undefined;
  }
  const normalized = value.trim();
  return normalized || undefined;
}

type HttpVerb = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
type RestKind = 'unary' | 'sse' | 'history-stream';

export class RestAssemblyError extends Error {
  readonly missing: string[];

  constructor(message: string, missing: string[] = []) {
    super(message);
    this.name = 'RestAssemblyError';
    this.missing = missing;
  }
}

interface AssembledWebRest {
  method: string;
  verb: HttpVerb;
  url: string;
  query: Record<string, string> | null;
  jsonBody: Record<string, unknown> | null;
  kind: RestKind;
}

interface RouteRow {
  verb: HttpVerb;
  path: string;
  kind: RestKind;
}

const ROUTES: Record<string, RouteRow> = {
  'connection.status': { verb: 'GET', path: '/connection/status', kind: 'unary' },
  'session.list': { verb: 'GET', path: '/sessions', kind: 'unary' },
  'session.create': { verb: 'POST', path: '/sessions', kind: 'unary' },
  'session.get_metadata': { verb: 'GET', path: '/sessions/{session_id}', kind: 'unary' },
  'session.rename': { verb: 'PATCH', path: '/sessions/{session_id}', kind: 'unary' },
  'session.pin': { verb: 'PATCH', path: '/sessions/{session_id}', kind: 'unary' },
  'session.delete': { verb: 'DELETE', path: '/sessions/{session_id}', kind: 'unary' },
  'history.get': { verb: 'GET', path: '/sessions/{session_id}/history', kind: 'history-stream' },
  'chat.send': { verb: 'POST', path: '/chat/completions', kind: 'sse' },
  'chat.interrupt': { verb: 'POST', path: '/chat/{session_id}/actions/interrupt', kind: 'unary' },
  'chat.user_answer': { verb: 'POST', path: '/chat/{session_id}/actions/answer', kind: 'unary' },
  'config.get': { verb: 'GET', path: '/config', kind: 'unary' },
  'models.list': { verb: 'GET', path: '/models', kind: 'unary' },
  'a2a.outbound.list': { verb: 'GET', path: '/a2a/outbound/agents', kind: 'unary' },
  'a2a.outbound.enabled.update': { verb: 'PATCH', path: '/a2a/outbound/agents/{agent_id}/enabled', kind: 'unary' },
  'a2a.outbound.dispatch.list': { verb: 'GET', path: '/a2a/outbound/dispatches', kind: 'unary' },
  'locale.get_conf': { verb: 'GET', path: '/locale', kind: 'unary' },
  'locale.set_conf': { verb: 'PUT', path: '/locale', kind: 'unary' },
  'cron.job.list': { verb: 'GET', path: '/cron/jobs', kind: 'unary' },
  'cron.job.create': { verb: 'POST', path: '/cron/jobs', kind: 'unary' },
  'cron.job.meta': { verb: 'GET', path: '/cron/jobs/meta', kind: 'unary' },
  'cron.job.get': { verb: 'GET', path: '/cron/jobs/{id}', kind: 'unary' },
  'cron.job.update': { verb: 'PATCH', path: '/cron/jobs/{id}', kind: 'unary' },
  'cron.job.delete': { verb: 'DELETE', path: '/cron/jobs/{id}', kind: 'unary' },
  'cron.job.toggle': { verb: 'POST', path: '/cron/jobs/{id}/actions/toggle', kind: 'unary' },
  'cron.job.preview': { verb: 'POST', path: '/cron/jobs/{id}/actions/preview', kind: 'unary' },
  'cron.job.run_now': { verb: 'POST', path: '/cron/jobs/{id}/actions/run-now', kind: 'unary' },
  'skills.list': { verb: 'GET', path: '/skills', kind: 'unary' },
  'skills.installed': { verb: 'GET', path: '/skills/installed', kind: 'unary' },
  'skills.get': { verb: 'GET', path: '/skills/{name}', kind: 'unary' },
  'skills.install': { verb: 'POST', path: '/skills/actions/install', kind: 'unary' },
  'skills.uninstall': { verb: 'POST', path: '/skills/actions/uninstall', kind: 'unary' },
  'skills.toggle': { verb: 'POST', path: '/skills/actions/toggle', kind: 'unary' },
  'skills.import_local': { verb: 'POST', path: '/skills/actions/import-local', kind: 'unary' },
  'skills.source.providers': { verb: 'GET', path: '/skills/sources', kind: 'unary' },
  'skills.source.search': { verb: 'GET', path: '/skills/sources/search', kind: 'unary' },
  'skills.source.install': { verb: 'POST', path: '/skills/sources/actions/install', kind: 'unary' },
  'skills.updates.check': { verb: 'GET', path: '/skills/updates', kind: 'unary' },
  'skills.update': { verb: 'POST', path: '/skills/actions/update', kind: 'unary' },
  'skills.teamskillshub.info': { verb: 'GET', path: '/skills/teamskillshub', kind: 'unary' },
  'skills.retrieval.status': { verb: 'GET', path: '/skills/retrieval/status', kind: 'unary' },
  'skills.retrieval.tree': { verb: 'GET', path: '/skills/retrieval/tree', kind: 'unary' },
  'skills.retrieval.index_build': { verb: 'POST', path: '/skills/retrieval/actions/index-build', kind: 'unary' },
  'skills.retrieval.index_cancel': { verb: 'POST', path: '/skills/retrieval/actions/index-cancel', kind: 'unary' },
  'skills.enterprise.list': { verb: 'GET', path: '/skills/enterprise', kind: 'unary' },
  'skills.enterprise.install': { verb: 'POST', path: '/skills/enterprise/actions/install', kind: 'unary' },
  'skills.enterprise.uninstall': { verb: 'POST', path: '/skills/enterprise/actions/uninstall', kind: 'unary' },
  'skills.marketplace.list': { verb: 'GET', path: '/skills/marketplace', kind: 'unary' },
  'skills.marketplace.add': { verb: 'POST', path: '/skills/marketplace', kind: 'unary' },
  'skills.marketplace.remove': { verb: 'POST', path: '/skills/marketplace/actions/remove', kind: 'unary' },
  'skills.marketplace.toggle': { verb: 'POST', path: '/skills/marketplace/actions/toggle', kind: 'unary' },
  'skills.clawhub.get_token': { verb: 'GET', path: '/skills/clawhub/token', kind: 'unary' },
  'skills.clawhub.set_token': { verb: 'PUT', path: '/skills/clawhub/token', kind: 'unary' },
  'skills.clawhub.search': { verb: 'GET', path: '/skills/clawhub/search', kind: 'unary' },
  'skills.clawhub.download': { verb: 'POST', path: '/skills/clawhub/actions/download', kind: 'unary' },
  'skills.skillnet.search': { verb: 'POST', path: '/skills/skillnet/actions/search', kind: 'unary' },
  'skills.skillnet.install': { verb: 'POST', path: '/skills/skillnet/actions/install', kind: 'unary' },
  'skills.skillnet.install_status': { verb: 'GET', path: '/skills/skillnet/install-status', kind: 'unary' },
  'skills.skillnet.evaluate': { verb: 'POST', path: '/skills/skillnet/actions/evaluate', kind: 'unary' },
  'skills.evolution.get': { verb: 'GET', path: '/skills/evolution', kind: 'unary' },
  'skills.evolution.save': { verb: 'PUT', path: '/skills/evolution', kind: 'unary' },
  'project.list': { verb: 'GET', path: '/projects', kind: 'unary' },
  'project.info': { verb: 'GET', path: '/projects/{project_id}', kind: 'unary' },
  'project.get_sessions': { verb: 'GET', path: '/projects/{project_id}/sessions', kind: 'unary' },
  'project.get_cron_sessions': { verb: 'GET', path: '/projects/{project_id}/cron-sessions', kind: 'unary' },
  'project.create': { verb: 'POST', path: '/projects', kind: 'unary' },
  'project.rename': { verb: 'PATCH', path: '/projects/{project_id}', kind: 'unary' },
  'project.pin': { verb: 'POST', path: '/projects/{project_id}/actions/pin', kind: 'unary' },
  'project.remove': { verb: 'DELETE', path: '/projects/{project_id}', kind: 'unary' },
  'project.restore': { verb: 'POST', path: '/projects/actions/restore', kind: 'unary' },
  'project.pinned_sessions': { verb: 'GET', path: '/projects/pinned-sessions', kind: 'unary' },
  'workspace.tree': { verb: 'GET', path: '/workspace/tree', kind: 'unary' },
  'workspace.usage': { verb: 'GET', path: '/workspace/usage', kind: 'unary' },
  'workspace.entries.delete': { verb: 'DELETE', path: '/workspace/entries', kind: 'unary' },
  'workspace.preview': { verb: 'GET', path: '/workspace/preview', kind: 'unary' },
  'im.hosting.status': { verb: 'GET', path: '/im/hosting/status', kind: 'unary' },
  'im.hosting.discover': { verb: 'GET', path: '/im/hosting/discover', kind: 'unary' },
  'im.hosting.targets.list': { verb: 'GET', path: '/im/hosting/targets', kind: 'unary' },
  'im.hosting.targets.add': { verb: 'POST', path: '/im/hosting/targets', kind: 'unary' },
  'im.hosting.targets.patch': { verb: 'PATCH', path: '/im/hosting/targets/{id}', kind: 'unary' },
  'im.hosting.targets.delete': { verb: 'DELETE', path: '/im/hosting/targets/{id}', kind: 'unary' },
  'im.hosting.policy.get': { verb: 'GET', path: '/im/hosting/policy', kind: 'unary' },
  'im.hosting.policy.patch': { verb: 'PATCH', path: '/im/hosting/policy', kind: 'unary' },
  'im.hosting.poll_now': { verb: 'POST', path: '/im/hosting/actions/poll-now', kind: 'unary' },
  'im.hosting.history': { verb: 'GET', path: '/im/hosting/targets/{id}/history', kind: 'unary' },
  'im.hosting.login.start': { verb: 'POST', path: '/im/hosting/login/start', kind: 'unary' },
  'im.hosting.login.status': { verb: 'GET', path: '/im/hosting/login/status', kind: 'unary' },
};

const PATH_PLACEHOLDER = /\{([A-Za-z_][A-Za-z0-9_]*)\}/g;

export function lookupWebRestRoute(method: string): RouteRow | null {
  return ROUTES[method] ?? null;
}

function joinGatewayUrl(baseUrl: string, path: string): string {
  const base = baseUrl.replace(/\/+$/, '');
  const rel = path.startsWith('/') ? path : `/${path}`;
  return `${base}${rel}`;
}

function fillPath(
  template: string,
  params: Record<string, unknown>
): { path: string; used: Set<string> } {
  const used = new Set<string>();
  const missing: string[] = [];
  const path = template.replace(PATH_PLACEHOLDER, (_all, key: string) => {
    const value = params[key];
    if (value === undefined || value === null || String(value) === '') {
      missing.push(key);
      return _all;
    }
    used.add(key);
    return encodeURIComponent(String(value));
  });
  if (missing.length > 0) {
    throw new RestAssemblyError(`REST 路径缺占位符 ${missing.join(',')}: ${template}`, missing);
  }
  return { path, used };
}

function remainingParams(
  params: Record<string, unknown>,
  used: Set<string>
): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(params)) {
    if (used.has(key) || value === undefined) {
      continue;
    }
    out[key] = value;
  }
  return out;
}

function queryValues(remaining: Record<string, unknown>): Record<string, string> | null {
  const query: Record<string, string> = {};
  for (const [key, value] of Object.entries(remaining)) {
    if (value === null) {
      continue;
    }
    if (typeof value === 'object') {
      query[key] = JSON.stringify(value);
    } else {
      query[key] = String(value);
    }
  }
  return Object.keys(query).length > 0 ? query : null;
}

function applyMethodBody(
  method: string,
  remaining: Record<string, unknown>
): Record<string, unknown> {
  if (method === 'chat.send') {
    const body: Record<string, unknown> = { ...remaining, enable_streaming: true };
    if (body.query == null && body.content != null) {
      body.query = body.content;
    } else if (body.content == null && body.query != null) {
      body.content = body.query;
    }
    return body;
  }
  return remaining;
}

export function assembleWebRest(
  method: string,
  params: Record<string, unknown> | undefined,
  baseUrl: string
): AssembledWebRest | null {
  const name = method.trim();
  if (!name) {
    throw new RestAssemblyError('method 为空，无法组装 REST');
  }
  const row = lookupWebRestRoute(name);
  if (!row) {
    return null;
  }
  const values = { ...(params ?? {}) };
  const { path, used } = fillPath(row.path, values);
  const remaining = applyMethodBody(name, remainingParams(values, used));
  const useQuery = row.verb === 'GET' || row.kind === 'history-stream';
  return {
    method: name,
    verb: row.verb,
    url: joinGatewayUrl(baseUrl, path),
    query: useQuery ? queryValues(remaining) : null,
    jsonBody: useQuery ? null : remaining,
    kind: row.kind,
  };
}

function appendQuery(url: string, query: Record<string, string> | null): string {
  if (!query) {
    return url;
  }
  const encoded = Object.entries(query)
    .map(([key, value]) => `${encodeURIComponent(key)}=${encodeURIComponent(value)}`)
    .join('&');
  return encoded ? `${url}?${encoded}` : url;
}

interface UnaryOk {
  ok: true;
  payload: unknown;
  requestId?: string;
}

interface UnaryFail {
  ok: false;
  message: string;
  code?: string;
  requestId?: string;
}

type UnaryResult = UnaryOk | UnaryFail;

interface SseFrame {
  event?: string;
  data?: string;
  id?: string;
}

function errorFields(error: unknown): { message: string; code?: string } {
  if (typeof error === 'string' && error.trim()) {
    return { message: error };
  }
  if (isRecord(error)) {
    const message =
      typeof error.message === 'string' && error.message.trim()
        ? error.message
        : typeof error.error === 'string'
          ? error.error
          : 'request failed';
    const code = typeof error.code === 'string' ? error.code : undefined;
    return { message, code };
  }
  return { message: 'request failed' };
}

export function unwrapHttpUnary(input: unknown, status?: number): UnaryResult {
  if (!isRecord(input)) {
    return { ok: false, message: 'empty http response', code: 'HTTP_ERROR' };
  }
  const requestId = typeof input.request_id === 'string' ? input.request_id : undefined;
  if (typeof input.ok === 'boolean') {
    if (input.ok) {
      const payload = input.data !== undefined ? input.data : input.payload;
      return { ok: true, payload: payload ?? {}, requestId };
    }
    const { message, code } = errorFields(input.error);
    return { ok: false, message, code, requestId };
  }
  if (input.agent_ready !== undefined) {
    return { ok: true, payload: input, requestId };
  }
  // 鉴权代理层（manager-web 同源反代等）返回 FastAPI/Starlette 的 {"detail": ...}
  // 错误形状——不是标准信封，但要给出真实原因而非笼统的 "invalid http envelope"。
  if (input.detail !== undefined) {
    const detail = input.detail;
    const message =
      typeof detail === 'string' && detail.trim()
        ? detail
        : 'request rejected';
    const code =
      status === 401 ? 'UNAUTHORIZED' : status === 403 ? 'FORBIDDEN' : 'HTTP_ERROR';
    return { ok: false, message, code, requestId };
  }
  return { ok: false, message: 'invalid http envelope', code: 'HTTP_ERROR', requestId };
}

function parseSseBlock(block: string): SseFrame | null {
  const trimmed = block.replace(/^\uFEFF/, '').trimEnd();
  if (!trimmed) {
    return null;
  }
  const frame: SseFrame = {};
  for (const rawLine of trimmed.split(/\r?\n/)) {
    if (!rawLine || rawLine.startsWith(':')) {
      continue;
    }
    const idx = rawLine.indexOf(':');
    const field = idx === -1 ? rawLine : rawLine.slice(0, idx);
    let value = idx === -1 ? '' : rawLine.slice(idx + 1);
    if (value.startsWith(' ')) {
      value = value.slice(1);
    }
    if (field === 'event') {
      frame.event = value;
    } else if (field === 'data') {
      frame.data = frame.data === undefined ? value : `${frame.data}\n${value}`;
    } else if (field === 'id') {
      frame.id = value;
    }
  }
  if (frame.event === undefined && frame.data === undefined && frame.id === undefined) {
    return null;
  }
  return frame;
}

export function consumeSseBuffer(buffer: string): { frames: SseFrame[]; rest: string } {
  const parts = buffer.split(/\r?\n\r?\n/);
  const rest = parts.pop() ?? '';
  const frames: SseFrame[] = [];
  for (const part of parts) {
    const frame = parseSseBlock(part);
    if (frame) {
      frames.push(frame);
    }
  }
  return { frames, rest };
}

function payloadFromData(
  data: string | undefined
): { payload: Record<string, unknown>; raw: Record<string, unknown> | null } {
  if (!data) {
    return { payload: {}, raw: null };
  }
  try {
    const parsed: unknown = JSON.parse(data);
    if (isRecord(parsed)) {
      if (isRecord(parsed.payload)) {
        return { payload: parsed.payload, raw: parsed };
      }
      return { payload: parsed, raw: parsed };
    }
    return { payload: { value: parsed }, raw: null };
  } catch {
    return { payload: { raw: data }, raw: null };
  }
}

function extractSseRequestId(
  raw: Record<string, unknown> | null,
  payload: Record<string, unknown>,
  frameId?: string
): string | undefined {
  if (raw) {
    const top = typeof raw.request_id === 'string' ? raw.request_id : undefined;
    if (top) return top;
  }
  const inner = typeof payload.request_id === 'string' ? payload.request_id : undefined;
  if (inner) return inner;
  const rid = typeof payload.rid === 'string' ? payload.rid : undefined;
  if (rid) return rid;
  const id = typeof frameId === 'string' ? frameId.trim() : '';
  return id || undefined;
}

export function sseFrameToWsEvent(frame: SseFrame): WsEvent | null {
  let eventName = frame.event?.trim() ?? '';
  const { payload, raw } = payloadFromData(frame.data);
  if (!eventName && typeof payload.event === 'string') {
    eventName = payload.event;
  }
  if (!eventName) {
    return null;
  }
  // SSE 标准 id 字段承载 Gateway 生成的 request_id；HTTP 模式下 data
  // 往往不重复携带该字段，必须保留它才能恢复 WS 帧的请求关联语义。
  const requestId = extractSseRequestId(
    raw,
    payload,
    isEnterprise() ? frame.id : undefined
  );
  return {
    type: 'event',
    event: eventName,
    payload,
    ...(requestId ? { request_id: requestId } : {}),
  };
}

export function historyPageToEvents(page: unknown, sessionId?: string): WsEvent[] {
  if (!isRecord(page)) {
    return [];
  }
  const sid =
    (typeof page.session_id === 'string' && page.session_id) || sessionId || '';
  const events: WsEvent[] = [];
  const messages = Array.isArray(page.messages) ? page.messages : [];
  for (const message of messages) {
    events.push({
      type: 'event',
      event: 'history.message',
      payload: {
        session_id: sid,
        page_idx: page.page_idx,
        total_pages: page.total_pages,
        message,
      },
    });
  }
  events.push({
    type: 'event',
    event: 'history.message',
    payload: {
      session_id: sid,
      page_idx: page.page_idx,
      total_pages: page.total_pages,
      status: 'done',
    },
  });
  return events;
}

function isSseContentType(contentType: string | null | undefined): boolean {
  return Boolean(contentType && contentType.toLowerCase().includes('text/event-stream'));
}

function isChatSseTerminal(event: WsEvent): boolean {
  // 仅企业版使用任务级 SSE 生命周期：chat.final 只是回复段结束，
  // 必须继续读到 processing_status(false)，避免工具状态停在 pending。
  // 个人版保持原有 chat.final 即结束的协议，避免改变个人版行为。
  if (!isEnterprise()) {
    return event.event === 'chat.final' || event.event === 'chat.error';
  }
  return (
    event.event === 'chat.processing_status' &&
    isRecord(event.payload) &&
    event.payload.is_processing === false
  );
}

/**
 * HTTP unary ``chat.interrupt`` 把 ``accepted`` 与 ``interrupt_result`` 合在同一 JSON body。
 * 只在 Gateway 明确给出 ``event_type`` 时映射为 WS 事件，前端不伪造 success。
 */
export function interruptUnaryToEvents(payload: unknown, requestId?: string): WsEvent | null {
  if (!isRecord(payload) || payload.event_type !== 'chat.interrupt_result') {
    return null;
  }
  return {
    type: 'event',
    event: 'chat.interrupt_result',
    // HTTP 合成事件补上 interrupt 请求 id：interrupt_result 处理器按
    // event.request_id 清理 pendingInterruptRequestIdsRef，缺失会缓慢泄漏。
    ...(requestId ? { request_id: requestId } : {}),
    payload: { ...payload },
  };
}
