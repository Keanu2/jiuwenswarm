import { isEnterprise } from '../edition';
import { hasManagerSessionCredentials, managerAuthenticatedFetch } from './managerSession';

/** Refresh platform credentials before native downloads/media use the cookie. */
export function fileDownloadHead(url: string): Promise<Response> {
  const target = new URL(url, window.location.href);
  const enterpriseFile =
    isEnterprise() && target.origin === window.location.origin && target.pathname === '/file-api/download';
  return (enterpriseFile ? managerAuthenticatedFetch : fetch)(url, { method: 'HEAD' });
}

/** A transient HEAD failure must not prevent an independently authorized GET. */
export async function prepareFileDownload(url: string): Promise<boolean> {
  try {
    const response = await fileDownloadHead(url);
    return response.ok || (hasManagerSessionCredentials() && (response.status >= 500 || response.status === 405));
  } catch {
    // A rejected Identity refresh clears credentials; a network failure does not.
    return hasManagerSessionCredentials();
  }
}
