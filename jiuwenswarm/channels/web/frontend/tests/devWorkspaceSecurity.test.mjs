import assert from 'node:assert/strict';
import { test } from 'node:test';
import { isLocalDevelopmentRequest } from '../node_modules/.cache/dev-workspace/devWorkspaceSecurity.js';

test('default local HTTP, WebSocket and HMR clients remain compatible', () => {
  for (const peer of ['127.0.0.1', '::1', '::ffff:127.0.0.1']) {
    assert.equal(isLocalDevelopmentRequest(peer), true);
    assert.equal(isLocalDevelopmentRequest(peer, 'http://localhost:5173'), true);
    assert.equal(isLocalDevelopmentRequest(peer, 'http://127.0.0.1:5173'), true);
  }
});

test('remote peers and browser origins cannot bypass authentication via Vite', () => {
  for (const peer of ['10.0.0.1', '::ffff:10.0.0.1', undefined]) {
    assert.equal(isLocalDevelopmentRequest(peer, 'http://localhost:5173'), false);
  }
  for (const origin of ['https://evil.example', 'null', 'http://127.0.0.1.evil.example']) {
    assert.equal(isLocalDevelopmentRequest('127.0.0.1', origin), false);
  }
});
