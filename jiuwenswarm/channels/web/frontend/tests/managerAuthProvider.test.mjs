import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';

for (const prefix of ['', '/portal/']) {
  const { outputFiles } = await build({
    entryPoints: ['src/auth/manager/ManagerAuthProvider.ts'],
    bundle: true,
    format: 'esm',
    platform: 'node',
    write: false,
    define: { 'import.meta.env': JSON.stringify({ VITE_API_PREFIX: prefix }) },
  });
  const { managerAuthProvider } = await import(
    `data:text/javascript;base64,${Buffer.from(outputFiles[0].contents).toString('base64')}`
  );

  test(`Manager access uses the API prefix ${JSON.stringify(prefix)} and preserves permission results`, async () => {
    const originalFetch = globalThis.fetch;
    const originalStorage = globalThis.localStorage;
    globalThis.localStorage = {
      getItem: (key) => (key === 'openjiuwen_access_token' ? 'access-token' : null),
    };
    try {
      for (const [data, expected] of [
        [{ manager_access: true }, true],
        [{ is_platform_admin: true }, true],
        [{ manager_access: false, is_platform_admin: false }, false],
      ]) {
        const calls = [];
        globalThis.fetch = async (url, init) => {
          calls.push([url, new Headers(init.headers).get('Authorization')]);
          return Response.json({ code: 200, data });
        };

        assert.equal(await managerAuthProvider.getManagerAccess(), expected);
        assert.deepEqual(calls, [[`${prefix.replace(/\/+$/, '')}/manager-api/v1/authz/me`, 'Bearer access-token']]);
      }
    } finally {
      globalThis.fetch = originalFetch;
      globalThis.localStorage = originalStorage;
    }
  });
}
