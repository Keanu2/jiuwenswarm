import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';

await build({
  entryPoints: ['src/services/managerSession.ts'],
  bundle: true,
  format: 'esm',
  platform: 'node',
  outfile: 'node_modules/.cache/enterprise/managerSession.mjs',
});
const session = await import('../node_modules/.cache/enterprise/managerSession.mjs');
const storage = () => {
  const values = new Map();
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  };
};
const access = 'openjiuwen_access_token';
const refresh = 'openjiuwen_refresh_token';
const json = (status, body) => new Response(JSON.stringify(body), { status });

function setup() {
  const redirects = [];
  globalThis.localStorage = storage();
  globalThis.sessionStorage = storage();
  globalThis.document = { cookie: '' };
  globalThis.window = {
    location: { protocol: 'http:', pathname: '/chat/new', replace: (value) => redirects.push(value) },
  };
  Object.defineProperty(globalThis, 'navigator', {
    configurable: true,
    value: { locks: { request: (_key, action) => action() } },
  });
  localStorage.setItem(access, 'old');
  localStorage.setItem(refresh, 'refresh-old');
  return redirects;
}

test('concurrent HTTP/SSE callers share one refresh and retry with current credentials', async () => {
  setup();
  let rotations = 0;
  let retries = 0;
  globalThis.fetch = async (url, init) => {
    if (url === '/idp/v1/auth/refresh') {
      rotations++;
      return json(200, { access_token: 'new', refresh_token: 'refresh-new' });
    }
    if (new Headers(init.headers).get('Authorization') === 'Bearer new') {
      retries++;
      return json(200, {});
    }
    return json(401, {});
  };
  const responses = await Promise.all(
    Array.from({ length: 5 }, () => session.managerAuthenticatedFetch('/gateway-api/v1/sessions')),
  );
  assert.ok(responses.every((response) => response.ok));
  assert.equal(rotations, 1);
  assert.equal(retries, 5);
  assert.match(document.cookie, /openjiuwen_access_token=new/);
});

test('Identity transport failures retain credentials; rejected refreshes clear them', async () => {
  const redirects = setup();
  globalThis.fetch = async (url) => {
    if (url === '/idp/v1/auth/refresh') throw new Error('network unavailable');
    return json(401, {});
  };
  await assert.rejects(session.managerAuthenticatedFetch('/gateway-api/v1/sessions'), /network unavailable/);
  assert.equal(localStorage.getItem(refresh), 'refresh-old');
  assert.equal(redirects.length, 0);
  globalThis.fetch = async () => json(401, {});
  await assert.rejects(session.managerAuthenticatedFetch('/gateway-api/v1/sessions'), /Not authenticated/);
  assert.equal(localStorage.getItem(access), null);
  assert.equal(redirects[0], '/auth');
});

test('logout wins over an in-flight refresh and revokes Identity refresh token', async () => {
  setup();
  let finish;
  let started;
  const refreshing = new Promise((resolve) => {
    started = resolve;
  });
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push([url, init]);
    if (url === '/idp/v1/auth/refresh') {
      started();
      return new Promise((resolve) => {
        finish = resolve;
      });
    }
    if (url.includes('logout') || url.includes('active-cluster')) return json(200, {});
    return json(401, {});
  };
  const request = session.managerAuthenticatedFetch('/gateway-api/v1/sessions');
  await refreshing;
  await session.logoutManagerSession();
  finish(json(200, { access_token: 'resurrected', refresh_token: 'resurrected' }));
  await request;
  assert.equal(localStorage.getItem(access), null);
  assert.ok(
    calls.some(([url, init]) => url === '/idp/v1/auth/logout' && JSON.parse(init.body).refresh_token === 'refresh-old'),
  );
});

test('cross-tab lock notices a rotation already completed by another tab', async () => {
  setup();
  navigator.locks.request = async (_key, action) => {
    localStorage.setItem(access, 'other-tab');
    localStorage.setItem(refresh, 'other-refresh');
    await action();
  };
  let refreshCalls = 0;
  globalThis.fetch = async (url, init) => {
    if (url.includes('refresh')) refreshCalls++;
    return json(new Headers(init.headers).get('Authorization') === 'Bearer other-tab' ? 200 : 401, {});
  };
  assert.equal((await session.managerAuthenticatedFetch('/gateway-api/v1/sessions')).status, 200);
  assert.equal(refreshCalls, 0);
});
