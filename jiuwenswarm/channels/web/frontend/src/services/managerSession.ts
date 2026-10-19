/** Platform credentials shared with Manager Web, not Account Kit model login. */
const ACCESS_KEY = 'openjiuwen_access_token';
const REFRESH_KEY = 'openjiuwen_refresh_token';
let refreshInFlight: Promise<void> | null = null;

export function managerAccessToken(): string | null {
  return localStorage.getItem(ACCESS_KEY)?.trim() || null;
}

export function hasManagerSessionCredentials(): boolean {
  return Boolean(managerAccessToken() || localStorage.getItem(REFRESH_KEY));
}

function syncCookie(token: string | null): void {
  const secure = window.location.protocol === 'https:' ? '; Secure' : '';
  document.cookie = token
    ? `${ACCESS_KEY}=${encodeURIComponent(token)}; Path=/; SameSite=Strict${secure}`
    : `${ACCESS_KEY}=; Path=/; Max-Age=0; SameSite=Strict${secure}`;
}

function requireLogin(): Error {
  clearManagerSession();
  window.location.replace(managerLoginPath());
  return new Error('Not authenticated');
}

export function clearManagerSession(): void {
  localStorage.removeItem(ACCESS_KEY);
  localStorage.removeItem(REFRESH_KEY);
  sessionStorage.removeItem('jiuwenclaw_active_cluster');
  syncCookie(null);
}

export function managerLoginPath(): string {
  const index = window.location.pathname.indexOf('/chat');
  return `${index < 0 ? '' : window.location.pathname.slice(0, index)}/auth`;
}

async function refresh(): Promise<void> {
  const previous = localStorage.getItem(REFRESH_KEY);
  if (!previous) throw requireLogin();
  // Web Locks prevents refresh-token rotation races across tabs (Chrome 107+).
  const rotate = async () => {
    if (localStorage.getItem(REFRESH_KEY) !== previous) return;
    const response = await fetch('/idp/v1/auth/refresh', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: previous }),
    });
    if (response.status === 401 || response.status === 403) {
      throw requireLogin();
    }
    if (!response.ok) throw new Error(`Identity HTTP ${response.status}`);
    const tokens = (await response.json()) as { access_token?: string; refresh_token?: string };
    if (!tokens.access_token || !tokens.refresh_token) throw new Error('Invalid Identity token response');
    // A logout or account change while refresh was in flight must win.
    if (localStorage.getItem(REFRESH_KEY) !== previous) return;
    localStorage.setItem(ACCESS_KEY, tokens.access_token);
    localStorage.setItem(REFRESH_KEY, tokens.refresh_token);
    syncCookie(tokens.access_token);
  };
  if (navigator.locks) await navigator.locks.request('jiuwenswarm-manager-refresh', rotate);
  else await rotate();
}

export async function managerAuthenticatedFetch(input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> {
  const send = () => {
    const headers = new Headers(init.headers);
    const token = managerAccessToken();
    if (token) headers.set('Authorization', `Bearer ${token}`);
    else headers.delete('Authorization');
    syncCookie(token);
    return fetch(input, { ...init, headers, credentials: 'include' });
  };
  const accessUsed = managerAccessToken();
  const response = await send();
  if (response.status !== 401) return response;
  if (managerAccessToken() === accessUsed) {
    refreshInFlight ??= refresh().finally(() => {
      refreshInFlight = null;
    });
    await refreshInFlight;
  }
  const retried = await send();
  if (retried.status === 401) {
    clearManagerSession();
    window.location.replace(managerLoginPath());
  }
  return retried;
}

export async function logoutManagerSession(): Promise<void> {
  const refreshToken = localStorage.getItem(REFRESH_KEY);
  // Clear immediately: concurrent SSE reconnects must not restore this session.
  clearManagerSession();
  await Promise.allSettled([
    fetch('/idp/v1/auth/logout', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: refreshToken }),
      signal: AbortSignal.timeout(10_000),
    }),
    fetch('/manager-api/v1/user-console/active-cluster', {
      method: 'DELETE',
      credentials: 'include',
      signal: AbortSignal.timeout(10_000),
    }),
  ]);
  window.location.replace(managerLoginPath());
}
