import assert from 'node:assert/strict';
import { test } from 'node:test';
import { establishWebAuthSession, getWebAuthToken, sendWebSocketAuth, setWebAuthToken } from '../node_modules/.cache/web-auth/services/webAuth.js';

test('credentials stay in the auth frame and session storage, separate from model keys', () => {
  const storage = new Map();
  globalThis.window = { sessionStorage: { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value) } };
  setWebAuthToken(' proxy-access-token ');
  assert.equal(getWebAuthToken(), 'proxy-access-token');
  const frames = [];
  sendWebSocketAuth({ send: data => frames.push(JSON.parse(data)) });
  assert.deepEqual(frames, [{ type: 'auth', token: 'proxy-access-token' }]);
  storage.clear();
  sendWebSocketAuth({ send: data => frames.push(data) });
  assert.equal(frames.length, 1);
});

test('a browser without session storage still supports the default local connection', () => {
  globalThis.window = {
    get sessionStorage() {
      throw new Error('blocked');
    },
  };
  assert.equal(getWebAuthToken(), '');
  sendWebSocketAuth({
    send() {
      assert.fail('should not send an empty credential');
    },
  });
});

test('HTTP session exchange authenticates by header and stores only successful credentials', async () => {
  const storage = new Map();
  globalThis.window = { sessionStorage: { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value) } };
  globalThis.fetch = async (url, options) => {
    assert.equal(url, '/web-auth/session');
    assert.equal(options.headers.Authorization, 'Bearer valid-token');
    assert.equal(options.credentials, 'same-origin');
    return { ok: true, status: 200 };
  };
  await establishWebAuthSession('valid-token');
  assert.equal(getWebAuthToken(), 'valid-token');
  globalThis.fetch = async () => ({ ok: false, status: 403 });
  await assert.rejects(establishWebAuthSession('wrong-token'));
  assert.equal(getWebAuthToken(), 'valid-token');
});
