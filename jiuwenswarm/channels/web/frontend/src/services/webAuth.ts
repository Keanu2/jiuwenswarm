const TOKEN_KEY = 'jiuwenswarm.web.auth-token';
export const WEB_AUTH_REQUIRED_EVENT = 'jiuwen:web-auth-required';

export function getWebAuthToken(): string {
  try {
    return window.sessionStorage.getItem(TOKEN_KEY) ?? '';
  } catch {
    return '';
  }
}

export function setWebAuthToken(token: string): void {
  window.sessionStorage.setItem(TOKEN_KEY, token.trim());
}

export async function establishWebAuthSession(token: string): Promise<void> {
  const response = await fetch('/web-auth/session', {
    method: 'POST',
    headers: { Authorization: `Bearer ${token.trim()}` },
    credentials: 'same-origin',
  });
  // A custom host may expose only WebSocket. Production app_web also creates
  // an HttpOnly cookie for its workspace HTTP routes.
  if (!response.ok && response.status !== 404) throw new Error('AUTH_FAILED');
  setWebAuthToken(token);
}

export function sendWebSocketAuth(socket: Pick<WebSocket, 'send'>, token = getWebAuthToken()): void {
  if (token) {
    // Credentials are sent before RPC frames, never in URLs or traffic logs.
    socket.send(JSON.stringify({ type: 'auth', token }));
  }
}

export function requestWebAuthentication(): void {
  window.dispatchEvent(new Event(WEB_AUTH_REQUIRED_EVENT));
}
