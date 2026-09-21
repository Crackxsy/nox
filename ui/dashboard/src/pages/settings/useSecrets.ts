/**
 * Writing and deleting credentials, including the single-PIN-in-flight rule.
 *
 * The PIN lives in component state for exactly one request and is cleared afterwards — a wrong one
 * is worth retyping, a right one has done its single job. It is never persisted, never logged, and
 * never sent with anything but the `secrets.set` / `secrets.delete` the user just triggered.
 */

import { useCallback, useState } from 'react';

import { errorText } from '../../../../shared/errors';
import type { Key, T } from '../../i18n';
import { type IpcClient, IpcError, api } from '../../ipc';
import { type SecretStatus, pinErrorKey } from '../../model';

export interface SecretsController {
  present: (name: string) => boolean;
  draft: Record<string, string>;
  setDraft: (name: string, value: string) => void;
  editing: Record<string, boolean>;
  setEditing: (name: string, editing: boolean) => void;
  busy: string | null;
  error: string;
  /** Which secret the PIN prompt currently belongs to, or null. */
  pinFor: string | null;
  pin: string;
  setPin: (name: string, value: string) => void;
  pinMessage: Key | '';
  save: (name: string) => void;
  remove: (name: string) => void;
}

export function useSecrets(
  client: IpcClient | null,
  t: T,
  secrets: SecretStatus[] | null,
  pinConfigured: boolean,
  reloadSecrets: (c: IpcClient) => Promise<void>,
): SecretsController {
  const [draft, setDraftState] = useState<Record<string, string>>({});
  const [editing, setEditingState] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState('');
  const [pinFor, setPinFor] = useState<string | null>(null);
  const [pin, setPinValue] = useState('');
  const [pinMessage, setPinMessage] = useState<Key | ''>('');

  const present = useCallback(
    (name: string) => secrets?.some((s) => s.name === name && s.present) ?? false,
    [secrets],
  );

  const setDraft = useCallback((name: string, value: string) => {
    setDraftState((d) => ({ ...d, [name]: value }));
  }, []);

  const setEditing = useCallback((name: string, next: boolean) => {
    setEditingState((d) => ({ ...d, [name]: next }));
  }, []);

  const setPin = useCallback((name: string, value: string) => {
    setPinFor(name);
    setPinValue(value);
    setPinMessage('');
  }, []);

  /**
   * A failed secret change: a refusal that names the PIN becomes the honest PIN message, anything
   * else keeps the core's own wording. The typed PIN is dropped either way.
   */
  const report = useCallback(
    (name: string, e: unknown, pinSent: boolean) => {
      const key = e instanceof IpcError ? pinErrorKey(e.code, e.message, pinSent) : null;
      if (key) {
        setPinFor(name);
        setPinMessage(key);
      } else {
        setError(errorText(t('error_prefix'), e));
      }
      setPinValue('');
    },
    [t],
  );

  const act = useCallback(
    async (name: string, run: (c: IpcClient, sentPin: string) => Promise<unknown>) => {
      if (!client || busy !== null) return;
      const sentPin = pinConfigured && pinFor === name ? pin : '';
      setBusy(name);
      setError('');
      setPinMessage('');
      try {
        await run(client, sentPin);
        setPinValue('');
        setPinFor(null);
        await reloadSecrets(client);
      } catch (e) {
        report(name, e, sentPin !== '');
      } finally {
        setBusy(null);
      }
    },
    [client, busy, pinConfigured, pinFor, pin, reloadSecrets, report],
  );

  const save = useCallback(
    (name: string) => {
      const value = (draft[name] ?? '').trim();
      if (value === '') return;
      void act(name, async (c, sentPin) => {
        await api.secretSet(c, name, value, sentPin);
        setDraftState((d) => ({ ...d, [name]: '' }));
        setEditingState((d) => ({ ...d, [name]: false }));
      });
    },
    [act, draft],
  );

  const remove = useCallback(
    (name: string) => {
      // Deleting needs the PIN too, so the prompt has to appear before the request goes out.
      if (pinConfigured && pinFor !== name) {
        setPinFor(name);
        setPinValue('');
        setPinMessage('pin_required');
        return;
      }
      void act(name, (c, sentPin) => api.secretDelete(c, name, sentPin));
    },
    [act, pinConfigured, pinFor],
  );

  return {
    present,
    draft,
    setDraft,
    editing,
    setEditing,
    busy,
    error,
    pinFor,
    pin,
    setPin,
    pinMessage,
    save,
    remove,
  };
}
