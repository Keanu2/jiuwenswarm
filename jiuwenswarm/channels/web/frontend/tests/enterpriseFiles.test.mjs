import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';

await build({
  stdin: {
    contents:
      "export { persistEnterpriseAttachments } from './src/services/obsUpload'; export { setRuntimeScope } from './src/services/runtimeScope'; export { fileDownloadHead } from './src/services/fileDownload';",
    resolveDir: process.cwd(),
  },
  bundle: true,
  format: 'esm',
  platform: 'node',
  define: { 'import.meta.env': '{}' },
  outfile: 'node_modules/.cache/enterprise/files.mjs',
});
const { persistEnterpriseAttachments, setRuntimeScope, fileDownloadHead } =
  await import('../node_modules/.cache/enterprise/files.mjs');
const item = { filename: '中文.txt', base64Data: 'aGVsbG8=' };
function setup() {
  const values = new Map([['openjiuwen_access_token', 'token']]);
  globalThis.localStorage = { getItem: (key) => values.get(key) ?? null };
  globalThis.document = { cookie: '' };
  globalThis.window = { location: { search: '', protocol: 'http:' }, localStorage };
  setRuntimeScope({ userId: 'u', groupId: 'g', botId: 'b' });
  globalThis.fetch = async () => new Response(JSON.stringify({ ok: true, url: 'http://obs/signed', name: '中文.txt' }));
}

test('signed URL stays in import; persist/chat receive only AS paths', async () => {
  setup();
  const calls = [];
  const request = async (method, params) => {
    calls.push([method, params]);
    if (method === 'file.import_url') return { files: [{ path: '/tenant/uploads/file.txt', filename: 'file.txt' }] };
    return { media_items: params.documents };
  };
  const result = await persistEnterpriseAttachments(request, 'hello', 's', [item], 'document');
  assert.equal(calls[0][1].files[0].url, 'http://obs/signed');
  assert.ok(!JSON.stringify(calls[1]).includes('http://obs'));
  assert.equal(result.media_items[0].path, '/tenant/uploads/file.txt');
});

test('persist failure cleans imported paths without sending chat', async () => {
  setup();
  const calls = [];
  const request = async (method, params) => {
    calls.push(method);
    if (method === 'file.import_url') return { files: [{ path: '/tenant/a', filename: 'a' }] };
    if (method === 'document.persist') throw new Error('parse failed');
  };
  await assert.rejects(persistEnterpriseAttachments(request, '', 's', [item], 'document'), /parse failed/);
  assert.deepEqual(calls, ['file.import_url', 'document.persist', 'media.discard']);
});

test('context switch aborts before import and never cleans a different tenant', async () => {
  setup();
  globalThis.fetch = async () => {
    setRuntimeScope({ userId: 'other', groupId: 'g', botId: 'b' });
    return new Response(JSON.stringify({ ok: true, url: 'http://obs/signed', name: 'a' }));
  };
  let calls = 0;
  await assert.rejects(
    persistEnterpriseAttachments(
      async () => {
        calls++;
      },
      '',
      's',
      [item],
      'document',
    ),
    /context changed/,
  );
  assert.equal(calls, 0);
});

test('download authentication stays on the same-origin file endpoint', async () => {
  setup();
  window.__JIUWENSWARM_EDITION__ = 'enterprise';
  Object.assign(window.location, { href: 'http://localhost/chat/', origin: 'http://localhost' });
  const authorizations = [];
  globalThis.fetch = async (url, init) => {
    authorizations.push(new Headers(init.headers).get('Authorization'));
    return new Response('', { status: 200 });
  };
  await fileDownloadHead('/file-api/download?token=signed');
  await fileDownloadHead('http://untrusted.example/file-api/download');
  assert.deepEqual(authorizations, ['Bearer token', null]);
});
