import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { Archive, X } from 'lucide-react';
import { webRequest } from '../../services/webClient';

type SoftDelete = {
  enabled: boolean;
  archive_dir: string;
  batch_delete_threshold: number;
};

type FileBackup = {
  enabled: boolean;
  backup_dir: string;
  max_total_mb: number;
};

type FormState = {
  softDelete: boolean;
  archiveDir: string;
  threshold: string;
  backup: boolean;
  backupDir: string;
  quota: string;
};

function toForm(soft: SoftDelete, backup: FileBackup): FormState {
  return {
    softDelete: soft.enabled,
    archiveDir: soft.archive_dir,
    threshold: String(soft.batch_delete_threshold),
    backup: backup.enabled,
    backupDir: backup.backup_dir,
    quota: String(backup.max_total_mb),
  };
}

function wholeNumber(value: string): number | null {
  const text = value.trim();
  if (!/^\d+$/.test(text)) return null;
  return Number(text);
}

export function DataSafetyEditor({ isConnected }: { isConnected: boolean }) {
  const { t } = useTranslation();
  const [form, setForm] = useState<FormState | null>(null);
  const [saved, setSaved] = useState<FormState | null>(null);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [reload, setReload] = useState(0);
  const lock = useRef(false);
  const generation = useRef(0);
  const dirty = JSON.stringify(form) !== JSON.stringify(saved);
  const disabled = !isConnected || loading || busy;

  useEffect(() => {
    if (!message) return;
    const timer = window.setTimeout(() => setMessage(''), 5000);
    return () => window.clearTimeout(timer);
  }, [message]);

  useEffect(() => {
    const current = ++generation.current;
    if (!isConnected) return;
    setLoading(true);
    setError('');
    void Promise.all([
      webRequest<{ soft_delete: SoftDelete }>('permissions.soft_delete.get', {}),
      webRequest<{ file_backup: FileBackup }>('file_backup.get', {}),
    ])
      .then(([soft, backup]) => {
        if (generation.current !== current) return;
        const next = toForm(soft.soft_delete, backup.file_backup);
        setForm(next);
        setSaved(next);
      })
      .catch(e => {
        if (generation.current !== current) return;
        setForm(null);
        setSaved(null);
        setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (generation.current === current) setLoading(false);
      });
    return () => {
      generation.current++;
    };
  }, [isConnected, reload]);

  function update(patch: Partial<FormState>) {
    setForm(current => (current ? { ...current, ...patch } : current));
    setMessage('');
  }

  async function save() {
    if (disabled || lock.current || !form || !dirty) return;
    const threshold = wholeNumber(form.threshold);
    const quota = wholeNumber(form.quota);
    if (threshold === null || quota === null) {
      setError(t('dataSafety.invalidNumber'));
      return;
    }
    const current = generation.current;
    lock.current = true;
    setBusy(true);
    setError('');
    setMessage('');
    try {
      const soft = await webRequest<{ soft_delete: SoftDelete }>('permissions.soft_delete.set', {
        patch: {
          enabled: form.softDelete,
          archive_dir: form.archiveDir,
          batch_delete_threshold: threshold,
        },
      });
      const backup = await webRequest<{ file_backup: FileBackup; applied?: boolean }>('file_backup.set', {
        patch: {
          enabled: form.backup,
          backup_dir: form.backupDir,
          max_total_mb: quota,
        },
      });
      if (generation.current !== current) return;
      const next = toForm(soft.soft_delete, backup.file_backup);
      setForm(next);
      setSaved(next);
      setMessage(backup.applied === false ? t('dataSafety.savedPending') : t('dataSafety.saved'));
    } catch (e) {
      if (generation.current === current) setError(e instanceof Error ? e.message : String(e));
    } finally {
      lock.current = false;
      setBusy(false);
    }
  }

  return (
    <section className="rounded-xl border border-border bg-card/70 backdrop-blur-sm overflow-hidden shadow-sm" aria-labelledby="data-safety-title">
      <div className="flex items-center justify-between gap-3 px-4 py-3 bg-secondary/30 border-b border-border">
        <div className="flex items-center gap-3 min-w-0">
          <span className="inline-flex items-center justify-center rounded-md border w-7 h-7 shrink-0 text-danger bg-danger-subtle border-danger/20">
            <Archive className="w-4 h-4" />
          </span>
          <div>
            <h3 id="data-safety-title" className="text-sm font-medium text-text-strong">
              {t('dataSafety.title')}
            </h3>
            <p className="text-xs text-text-muted">{t('dataSafety.hint')}</p>
          </div>
        </div>
        <button
          type="button"
          className="btn text-xs disabled:opacity-50"
          disabled={disabled || dirty}
          onClick={() => {
            setMessage('');
            setReload(n => n + 1);
          }}
        >
          {t('fileSecurity.refresh')}
        </button>
      </div>
      {loading && (
        <p role="status" className="px-4 py-2 text-xs text-text-muted">
          {t('fileSecurity.loading')}
        </p>
      )}
      {form && (
        <div className="divide-y divide-border">
          <label className="flex items-center px-4 py-2.5">
            <span className="w-[32%] shrink-0 text-xs text-text-muted">{t('dataSafety.softDelete')}</span>
            <button
              type="button"
              role="switch"
              aria-label={t('dataSafety.softDelete')}
              aria-checked={form.softDelete}
              disabled={disabled}
              onClick={() => update({ softDelete: !form.softDelete })}
              className={`relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-60 ${form.softDelete ? 'bg-[var(--color-toggle-enabled)]' : 'bg-[var(--color-toggle-disabled)]'}`}
            >
              <span className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-[var(--color-control-thumb)] shadow ${form.softDelete ? 'translate-x-4' : 'translate-x-0'}`} />
            </button>
          </label>
          <label className="flex items-center px-4 py-2.5">
            <span className="w-[32%] shrink-0 text-xs text-text-muted">{t('dataSafety.archiveDir')}</span>
            <input
              value={form.archiveDir}
              disabled={disabled}
              placeholder={t('dataSafety.archivePlaceholder')}
              onChange={e => update({ archiveDir: e.target.value })}
              className="min-w-0 flex-1 rounded border border-border bg-bg px-2 py-1 text-xs text-text"
            />
          </label>
          <label className="flex items-center px-4 py-2.5">
            <span className="w-[32%] shrink-0 text-xs text-text-muted">{t('dataSafety.threshold')}</span>
            <input
              value={form.threshold}
              disabled={disabled}
              inputMode="numeric"
              onChange={e => update({ threshold: e.target.value })}
              className="w-28 rounded border border-border bg-bg px-2 py-1 text-xs text-text"
            />
          </label>
          <label className="flex items-center px-4 py-2.5">
            <span className="w-[32%] shrink-0 text-xs text-text-muted">{t('dataSafety.backup')}</span>
            <button
              type="button"
              role="switch"
              aria-label={t('dataSafety.backup')}
              aria-checked={form.backup}
              disabled={disabled}
              onClick={() => update({ backup: !form.backup })}
              className={`relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-60 ${form.backup ? 'bg-[var(--color-toggle-enabled)]' : 'bg-[var(--color-toggle-disabled)]'}`}
            >
              <span className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-[var(--color-control-thumb)] shadow ${form.backup ? 'translate-x-4' : 'translate-x-0'}`} />
            </button>
          </label>
          <label className="flex items-center px-4 py-2.5">
            <span className="w-[32%] shrink-0 text-xs text-text-muted">{t('dataSafety.backupDir')}</span>
            <input
              value={form.backupDir}
              disabled={disabled}
              placeholder={t('dataSafety.backupPlaceholder')}
              onChange={e => update({ backupDir: e.target.value })}
              className="min-w-0 flex-1 rounded border border-border bg-bg px-2 py-1 text-xs text-text"
            />
          </label>
          <label className="flex items-center px-4 py-2.5">
            <span className="w-[32%] shrink-0 text-xs text-text-muted">{t('dataSafety.quota')}</span>
            <input
              value={form.quota}
              disabled={disabled}
              inputMode="numeric"
              onChange={e => update({ quota: e.target.value })}
              className="w-28 rounded border border-border bg-bg px-2 py-1 text-xs text-text"
            />
          </label>
        </div>
      )}
      <div className="px-4 py-3 space-y-3">
        <p className="text-xs text-text-muted">{t('dataSafety.softDeleteHint')}</p>
        <p className="text-xs text-text-muted">{t('dataSafety.thresholdHint')}</p>
        <p className="text-xs text-text-muted">{t('dataSafety.quotaHint')}</p>
        {error && (
          <p role="alert" className="text-xs text-danger">
            {error}
          </p>
        )}
        <button type="button" className="btn text-xs disabled:opacity-50" disabled={disabled || !form || !dirty} onClick={() => void save()}>
          {t('fileSecurity.save')}
        </button>
      </div>
      {message &&
        createPortal(
          <div className="app-toast-wrapper app-toast-wrapper--top-center" style={{ zIndex: 2101, pointerEvents: 'none' }}>
            <div className="app-session-toast animate-rise flex items-start gap-3 text-sm max-w-[calc(100vw-2rem)]" role="status" style={{ pointerEvents: 'auto' }}>
              <span className="whitespace-pre-wrap break-words">{message}</span>
              <button type="button" aria-label={t('common.close')} className="shrink-0 rounded p-0.5 hover:opacity-70" onClick={() => setMessage('')}>
                <X className="h-4 w-4" />
              </button>
            </div>
          </div>,
          document.body,
        )}
    </section>
  );
}
