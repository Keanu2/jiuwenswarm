import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';

await build({
  stdin: {
    contents:
      "export { persistEnterpriseAttachments } from './src/services/obsUpload'; export { setRuntimeScope } from './src/services/runtimeScope'; export { fileDownloadHead, prepareFileDownload } from './src/services/fileDownload';",
    resolveDir: process.cwd(),
  },
  bundle: true,
  format: 'esm',
  platform: 'node',
  define: { 'import.meta.env': '{}' },
  outfile: 'node_modules/.cache/enterprise/files.mjs',
});
const { persistEnterpriseAttachments, setRuntimeScope, fileDownloadHead, prepareFileDownload } =
  await import('../node_modules/.cache/enterprise/files.mjs');
const item = { filename: '中文.txt', base64Data: 'aGVsbG8=' };
function setup() {
  const values = new Map([['openjiuwen_access_token', 'token']]);
  globalThis.localStorage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  };
  globalThis.sessionStorage = { removeItem() {} };
  globalThis.document = { cookie: '' };
  globalThis.window = {
    location: {
      search: '',
      protocol: 'http:',
      href: 'http://localhost/chat/',
      origin: 'http://localhost',
      pathname: '/chat/',
      replace() {},
    },
    localStorage,
    __JIUWENSWARM_EDITION__: 'enterprise',
  };
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

for (const failure of ['import', 'persist', 'none']) {
  test(`temporary OBS object cleanup after ${failure === 'none' ? 'success' : failure + ' failure'}`, async () => {
    setup();
    const deleted = [];
    globalThis.fetch = async (url, init) => {
      if (init.method === 'DELETE') {
        deleted.push({ url: JSON.parse(init.body).url, user: new Headers(init.headers).get('X-User-Id') });
        return new Response(JSON.stringify({ ok: true }));
      }
      return new Response(JSON.stringify({ ok: true, url: 'http://obs/signed', name: 'a' }));
    };
    const methods = [];
    const operation = persistEnterpriseAttachments(
      async (method) => {
        methods.push(method);
        if (method === 'file.import_url') {
          if (failure === 'import') throw new Error('import failed');
          return { files: [{ path: '/tenant/a', filename: 'a' }] };
        }
        if (method === 'document.persist' && failure === 'persist') throw new Error('persist failed');
        return {};
      },
      '',
      's',
      [item],
      'document',
    );
    if (failure === 'none') await operation;
    else await assert.rejects(operation, new RegExp(failure + ' failed'));
    assert.deepEqual(deleted, [{ url: 'http://obs/signed', user: 'u' }]);
    assert.equal(methods.includes('media.discard'), failure === 'persist');
  });
}

test('cleanup keeps captured scope and does not replace the original import error', async () => {
  setup();
  const cleanupScopes = [];
  globalThis.fetch = async (url, init) => {
    if (init.method === 'DELETE') {
      cleanupScopes.push(new Headers(init.headers).get('X-User-Id'));
      throw new Error('cleanup unavailable');
    }
    return new Response(JSON.stringify({ ok: true, url: 'http://obs/signed', name: 'a' }));
  };
  const previousWarn = console.warn;
  const warnings = [];
  console.warn = (...args) => warnings.push(args);
  try {
    await assert.rejects(
      persistEnterpriseAttachments(
        async (method) => {
          assert.equal(method, 'file.import_url');
          setRuntimeScope({ userId: 'other', groupId: 'g', botId: 'b' });
          throw new Error('original import failure');
        },
        '',
        's',
        [item],
        'document',
      ),
      /original import failure/,
    );
  } finally {
    console.warn = previousWarn;
  }
  assert.deepEqual(cleanupScopes, ['u']);
  assert.equal(warnings.length, 1);
});

for (const status of [200, 401, 403, 404, 410, 405, 500, 502, 503]) {
  test(`download HEAD ${status} ${status === 200 || status === 405 || status >= 500 ? 'permits' : 'blocks'} GET`, async () => {
    setup();
    globalThis.fetch = async () => new Response(null, { status });
    assert.equal(
      await prepareFileDownload('/file-api/download?token=signed'),
      status === 200 || status === 405 || status >= 500,
    );
  });
}

test('HEAD network failure does not make native download depend on Identity credentials', async () => {
  setup();
  globalThis.fetch = async () => {
    throw new TypeError('network unavailable');
  };
  assert.equal(await prepareFileDownload('/file-api/download?token=signed'), true);
  localStorage.removeItem('openjiuwen_access_token');
  assert.equal(await prepareFileDownload('/file-api/download?token=signed'), true);
});

test('download HEAD does not call Identity even on 401', async () => {
  setup();
  const urls = [];
  globalThis.fetch = async (url) => {
    urls.push(url);
    return new Response(null, { status: 401 });
  };
  assert.equal(await prepareFileDownload('/file-api/download?token=signed'), false);
  assert.deepEqual(urls, ['/file-api/download?token=signed']);
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

test('download HEAD never injects platform credentials into native links', async () => {
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
  assert.deepEqual(authorizations, [null, null]);
});
