/**
 * Geräte › Computer und Server › "Standardgeräte": where each tool family runs when nobody names a
 * device (`devices.defaults`, `node.defaults.set`). One row per family, worded as a question a
 * person would ask - "Wo Nox Systemwerte liest" - with a select and "Übernehmen". It applies to
 * every computer, so it sits on the page, not in one computer's sheet.
 *
 * Choosing alone changes nothing; "Übernehmen" does. A different default needs the PIN when one is
 * set (and from a paired device always); a paired device without a PIN set says where instead.
 */

import { useCallback, useId, useRef, useState } from 'react';

import { refusalText, useIpcAction } from '../../hooks';
import { type T } from '../../i18n';
import { type IpcClient, api } from '../../ipc';
import { type FamilyDefault, type NodeInfo, parseFamilyDefaults } from '../../model';
import { defaultWords, whereLabel } from './words';

export interface DefaultsSectionProps {
  t: T;
  client: IpcClient | null;
  device: boolean;
  pinSet: boolean;
  nodes: NodeInfo[];
  defaults: FamilyDefault[];
  onDefaults: (defaults: FamilyDefault[]) => void;
}

export function DefaultsSection({ t, client, device, pinSet, nodes, defaults, onDefaults }: DefaultsSectionProps) {
  const id = useId();
  const [pin, setPin] = useState('');
  const [saved, setSaved] = useState('');
  /** Chosen in a select, not yet applied: group -> device. */
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const pinSentRef = useRef(false);
  const pinRef = useRef<HTMLInputElement | null>(null);
  const describe = useCallback((e: unknown) => refusalText(t, e, pinSentRef.current, t('node_change_failed')), [t]);
  const { busy, error, setError, run } = useIpcAction(client, t, describe);
  const locked = device && !pinSet;
  const needsPin = (device || pinSet) && !locked;

  if (defaults.length === 0) return null;
  const others = nodes.filter((n) => !n.revoked);

  const apply = (entry: FamilyDefault, value: string) => {
    if (busy !== null || locked) return;
    setSaved('');
    if (needsPin && !pin.trim()) {
      setError(t('reason_pin_required'));
      pinRef.current?.focus();
      return;
    }
    void run(`default-${entry.group}`, async (c) => {
      const sent = needsPin ? pin.trim() : '';
      pinSentRef.current = sent !== '';
      const answer = (await api.nodeDefaultsSet(c, entry.group, value, sent)) as { defaults?: unknown };
      const next = parseFamilyDefaults(answer.defaults);
      if (next) onDefaults(next);
      setDrafts((d) => {
        const rest = { ...d };
        delete rest[entry.group];
        return rest;
      });
      setSaved(t('node_default_saved'));
    });
  };

  return (
    <section className="node-defaults" aria-labelledby={`${id}-title`}>
      <h3 id={`${id}-title`} className="rail-title">
        {t('node_defaults_title')}
      </h3>
      <p className="hint">{t('node_defaults_lede')}</p>
      {error && (
        <p role="alert" className="alert">
          {error}
        </p>
      )}
      {locked && <p className="hint">{t('node_pin_missing_device')}</p>}
      {needsPin && (
        <div className="field">
          <label htmlFor={`${id}-pin`} className="label">
            {t('node_pin')}
          </label>
          <input
            ref={pinRef}
            id={`${id}-pin`}
            className="input input--pin"
            type="password"
            inputMode="numeric"
            autoComplete="off"
            value={pin}
            onChange={(e) => setPin(e.target.value)}
            aria-describedby={`${id}-pin-hint`}
          />
          <p id={`${id}-pin-hint`} className="hint">
            {device ? t('node_pin_hint_device') : t('node_pin_hint_defaults')}
          </p>
        </div>
      )}
      {defaults.map((entry) => {
        const field = `${id}-d-${entry.group}`;
        const known = ['auto', 'core', 'origin'].includes(entry.device) || others.some((n) => n.id === entry.device);
        const current = known ? entry.device : '';
        const draft = drafts[entry.group] ?? current;
        const label = whereLabel(t, entry.group);
        return (
          <div key={entry.group} className="field node-default">
            <label htmlFor={field} className="label">
              {label}
            </label>
            <div className="row-controls">
              <select
                id={field}
                className="select"
                value={draft}
                disabled={!client || locked}
                aria-disabled={busy !== null || undefined}
                onChange={(e) => {
                  const value = e.target.value;
                  if (busy === null) setDrafts((d) => ({ ...d, [entry.group]: value }));
                }}
              >
                {!known && (
                  <option value="" disabled>
                    {defaultWords(t, entry, nodes)}
                  </option>
                )}
                <option value="auto">{t('node_default_auto')}</option>
                <option value="origin">{t('node_default_origin')}</option>
                <option value="core">{t('node_default_core')}</option>
                {others.map((n) => (
                  <option key={n.id} value={n.id}>
                    {n.name}
                  </option>
                ))}
              </select>
              <button
                type="button"
                className="btn btn--sm"
                disabled={!client || locked || draft === current || draft === ''}
                aria-disabled={busy !== null || undefined}
                aria-label={`${t('node_default_apply')}: ${label}`}
                onClick={() => apply(entry, draft)}
              >
                {t('node_default_apply')}
              </button>
            </div>
          </div>
        );
      })}
      <p role="status" className={saved ? 'hint' : 'sr-only'}>
        {saved}
      </p>
    </section>
  );
}
