import { useEffect, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { establishWebAuthSession, WEB_AUTH_REQUIRED_EVENT } from '../services/webAuth';

export function WebAuthentication() {
  const { t } = useTranslation();
  const [required, setRequired] = useState(false);
  const [token, setToken] = useState('');
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const requireAuthentication = () => setRequired(true);
    window.addEventListener(WEB_AUTH_REQUIRED_EVENT, requireAuthentication);
    return () => window.removeEventListener(WEB_AUTH_REQUIRED_EVENT, requireAuthentication);
  }, []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!token.trim()) return;
    try {
      await establishWebAuthSession(token);
      // Reload clears in-memory messages and reconnects both RPC and Git sockets.
      window.location.reload();
    } catch {
      setFailed(true);
    }
  }

  if (!required) return null;
  return (
    <div className="app-restart-modal" role="dialog" aria-modal="true" aria-labelledby="web-auth-title">
      <div className="app-restart-modal__backdrop" />
      <form className="app-restart-modal__panel web-auth-form" onSubmit={submit}>
        <h2 id="web-auth-title">{t('network.authenticationRequired')}</h2>
        {failed ? <p role="alert">{t('network.authenticationFailed')}</p> : null}
        <label htmlFor="web-auth-token">{t('network.accessToken')}</label>
        <input id="web-auth-token" type="password" autoComplete="off" autoFocus value={token} onChange={event => setToken(event.target.value)} required />
        <button type="submit">{t('network.authenticate')}</button>
      </form>
    </div>
  );
}
