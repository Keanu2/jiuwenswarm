import { isEnterprise } from '../edition';
import { managerAuthenticatedFetch } from './managerSession';

/** Refresh platform credentials before native downloads/media use the cookie. */
export function fileDownloadHead(url: string): Promise<Response> {
  const target = new URL(url, window.location.href);
  const enterpriseFile =
    isEnterprise() && target.origin === window.location.origin && target.pathname === '/file-api/download';
  return (enterpriseFile ? managerAuthenticatedFetch : fetch)(url, { method: 'HEAD' });
}
