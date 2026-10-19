/**
 * "Fähigkeit hinzufügen": one external MCP server, either a program on this computer or an
 * address on the net.
 *
 * A value marked "geheim" never reaches `mcp.servers.add`. It is written to the secret store first
 * (`secrets.set nox/mcp/<id>/<key>`, with the PIN when one is configured), and the server entry
 * only carries the reference `secret:nox/mcp/<id>/<key>`. If storing a secret fails, nothing is
 * added. The core's refusal of the entry itself is shown in its own words.
 */

import { useEffect, useId, useRef, useState } from 'react';

import { reasonText } from '../../../../shared/errors';
import { type T, fill } from '../../i18n';
import { type IpcClient, IpcError, api } from '../../ipc';
import {
  BLANK_SERVER_DRAFT,
  type KeyValueRow,
  type McpServerDraft,
  addPayload,
  draftProblem,
  draftSecrets,
  needsPinnedVersion,
  parseActionResult,
  pinErrorKey,
  splitCommandLine,
} from '../../model';
import { PinField } from './shared';

export interface McpAddFormProps {
  t: T;
  client: IpcClient | null;
  takenIds: string[];
  pinConfigured: boolean;
  /** After a successful add, with the new server's id. */
  onAdded: (id: string) => void;
  onCancel: () => void;
}

const BLANK_ROW: KeyValueRow = { key: '', value: '', secret: false };

export function McpAddForm({ t, client, takenIds, pinConfigured, onAdded, onCancel }: McpAddFormProps) {
  const uid = useId();
  const [draft, setDraft] = useState<McpServerDraft>(BLANK_SERVER_DRAFT);
  const [pin, setPin] = useState('');
  /** The core asked for a PIN although none was reported as configured. */
  const [pinNeeded, setPinNeeded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [tried, setTried] = useState(false);
  const headingRef = useRef<HTMLHeadingElement | null>(null);

  // The form replaces the button that opened it; focus follows, so a keyboard user is not left
  // on a button that no longer exists.
  useEffect(() => {
    headingRef.current?.focus();
  }, []);

  const set = (patch: Partial<McpServerDraft>) => setDraft((d) => ({ ...d, ...patch }));
  const setRow = (index: number, patch: Partial<KeyValueRow>) =>
    setDraft((d) => ({
      ...d,
      rows: d.rows.map((row, i) => (i === index ? { ...row, ...patch } : row)),
    }));

  const program = draft.kind === 'program';
  const argv = program ? splitCommandLine(draft.commandLine) : null;
  const pinned = argv !== null && needsPinnedVersion(argv);
  const problem = draftProblem(draft, takenIds);
  const hasSecret = draft.rows.some((row) => row.secret && row.key.trim() !== '');
  const askPin = (pinConfigured || pinNeeded) && hasSecret;
  const field = (name: string) => `${uid}-${name}`;

  const submit = async () => {
    setTried(true);
    if (!client || busy || problem) return;
    setBusy(true);
    setError('');
    try {
      for (const secret of draftSecrets(draft)) {
        try {
          await api.secretSet(client, secret.name, secret.value, askPin ? pin : '');
        } catch (e) {
          const key = e instanceof IpcError ? pinErrorKey(e.code, e.message, askPin && pin !== '') : null;
          if (key) setPinNeeded(true);
          setError(
            key ? t(key) : fill(t('caps_add_secret_failed'), secret.name, reasonText(e) || t('unknown')),
          );
          return;
        }
      }
      const result = parseActionResult(await api.mcpServerAdd(client, addPayload(draft)));
      if (!result.ok) {
        setError(fill(t('caps_refused'), result.reason || t('unknown')));
        return;
      }
      onAdded(draft.id);
      setDraft(BLANK_SERVER_DRAFT);
      setTried(false);
    } catch (e) {
      setError(fill(t('caps_refused'), reasonText(e) || t('unknown')));
    } finally {
      setPin('');
      setBusy(false);
    }
  };

  const disabled = client === null || busy;

  return (
    <form
      className="caps-form"
      aria-labelledby={field('title')}
      noValidate
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
    >
      <h4 id={field('title')} ref={headingRef} tabIndex={-1} className="tile-title">
        {t('caps_add_title')}
      </h4>

      <fieldset className="caps-choice">
        <legend className="label">{t('caps_add_kind')}</legend>
        {(['program', 'address'] as const).map((kind) => (
          <label key={kind} className="caps-radio">
            <input
              type="radio"
              name={field('kind')}
              value={kind}
              checked={draft.kind === kind}
              disabled={disabled}
              onChange={() => set({ kind })}
            />
            <span>{t(kind === 'program' ? 'caps_add_kind_program' : 'caps_add_kind_address')}</span>
          </label>
        ))}
      </fieldset>

      <div className="field field--spaced">
        <label htmlFor={field('name')} className="label">
          {t('caps_add_name')}
        </label>
        <input
          id={field('name')}
          className="input"
          maxLength={80}
          disabled={disabled}
          value={draft.name}
          onChange={(e) => set({ name: e.target.value })}
        />
      </div>

      <div className="field field--spaced">
        <label htmlFor={field('id')} className="label">
          {t('caps_add_id')}
        </label>
        <input
          id={field('id')}
          className="input mono-input"
          maxLength={32}
          autoCapitalize="off"
          spellCheck={false}
          disabled={disabled}
          aria-describedby={field('id-hint')}
          aria-invalid={tried && (problem === 'caps_add_err_id' || problem === 'caps_add_err_id_taken')}
          value={draft.id}
          onChange={(e) => set({ id: e.target.value.trim() })}
        />
        <p id={field('id-hint')} className="hint">
          {t('caps_add_id_hint')}
        </p>
      </div>

      {program ? (
        <div className="field field--spaced">
          <label htmlFor={field('command')} className="label">
            {t('caps_add_command')}
          </label>
          <input
            id={field('command')}
            className="input mono-input"
            autoCapitalize="off"
            spellCheck={false}
            disabled={disabled}
            placeholder="npx -y @modelcontextprotocol/server-filesystem@2025.8.21 C:/Daten"
            aria-describedby={field('command-hint')}
            value={draft.commandLine}
            onChange={(e) => set({ commandLine: e.target.value })}
          />
          <p id={field('command-hint')} className="hint">
            {t('caps_add_command_hint')}
          </p>
          {argv && argv.length > 0 && (
            <div className="hint">
              <span>{t('caps_add_argv')}</span>
              <ol className="argv" aria-label={t('caps_add_argv')}>
                {argv.map((part, i) => (
                  // Position is the identity of an argument; the same word may appear twice.
                  <li key={i}>
                    <code className="mono">{part}</code>
                  </li>
                ))}
              </ol>
            </div>
          )}
        </div>
      ) : (
        <div className="field field--spaced">
          <label htmlFor={field('url')} className="label">
            {t('caps_add_url')}
          </label>
          <input
            id={field('url')}
            className="input mono-input"
            type="url"
            inputMode="url"
            autoCapitalize="off"
            spellCheck={false}
            disabled={disabled}
            placeholder="https://"
            value={draft.url}
            onChange={(e) => set({ url: e.target.value })}
          />
        </div>
      )}

      <div className="field field--spaced">
        <label htmlFor={field('version')} className="label">
          {t('caps_add_version')}
        </label>
        <input
          id={field('version')}
          className="input mono-input"
          maxLength={64}
          autoCapitalize="off"
          spellCheck={false}
          disabled={disabled}
          aria-describedby={field('version-hint')}
          aria-invalid={tried && problem === 'caps_add_err_version'}
          value={draft.version}
          onChange={(e) => set({ version: e.target.value })}
        />
        <p id={field('version-hint')} className={pinned ? 'hint caps-emph' : 'hint'}>
          {t('caps_add_version_hint')}
        </p>
      </div>

      {program && (
        <label className="caps-check field--spaced">
          <input
            type="checkbox"
            checked={draft.network}
            disabled={disabled}
            aria-describedby={field('network-hint')}
            onChange={(e) => set({ network: e.target.checked })}
          />
          <span>{t('caps_add_network')}</span>
        </label>
      )}
      {program && (
        <p id={field('network-hint')} className="hint">
          {t('caps_add_network_hint')}
        </p>
      )}

      <fieldset className="caps-rows field--spaced">
        <legend className="label">{t(program ? 'caps_add_env' : 'caps_add_headers')}</legend>
        <p className="hint">{t('caps_add_secret_hint')}</p>
        {draft.rows.map((row, i) => {
          const rowLabel = fill(t('caps_add_row_n'), i + 1);
          return (
            // A row has no identity but its place in the list.
            <div key={i} className="caps-row">
              <div className="field">
                <label htmlFor={field(`key-${i}`)} className="label">
                  {t('caps_add_key')}
                  <span className="sr-only"> {rowLabel}</span>
                </label>
                <input
                  id={field(`key-${i}`)}
                  className="input mono-input"
                  autoCapitalize="off"
                  spellCheck={false}
                  disabled={disabled}
                  value={row.key}
                  onChange={(e) => setRow(i, { key: e.target.value })}
                />
              </div>
              <div className="field">
                <label htmlFor={field(`value-${i}`)} className="label">
                  {t('caps_add_value')}
                  <span className="sr-only"> {rowLabel}</span>
                </label>
                <input
                  id={field(`value-${i}`)}
                  className="input"
                  type={row.secret ? 'password' : 'text'}
                  autoComplete="off"
                  spellCheck={false}
                  disabled={disabled}
                  value={row.value}
                  onChange={(e) => setRow(i, { value: e.target.value })}
                />
              </div>
              <div className="caps-row-tools">
                <label className="caps-check">
                  <input
                    type="checkbox"
                    checked={row.secret}
                    disabled={disabled}
                    onChange={(e) => setRow(i, { secret: e.target.checked })}
                  />
                  <span>
                    {t('caps_add_secret')}
                    <span className="sr-only"> {rowLabel}</span>
                  </span>
                </label>
                <button
                  type="button"
                  className="link link--plain"
                  disabled={disabled}
                  onClick={() => setDraft((d) => ({ ...d, rows: d.rows.filter((_, j) => j !== i) }))}
                >
                  {t('caps_add_row_remove')}
                  <span className="sr-only"> {rowLabel}</span>
                </button>
              </div>
            </div>
          );
        })}
        <button
          type="button"
          className="btn btn--quiet btn--sm"
          disabled={disabled}
          onClick={() => setDraft((d) => ({ ...d, rows: [...d.rows, BLANK_ROW] }))}
        >
          {t('caps_add_row_add')}
        </button>
      </fieldset>

      <div className="field field--spaced">
        <label htmlFor={field('ro')} className="label">
          {t('caps_add_ro_tools')}
        </label>
        <textarea
          id={field('ro')}
          className="textarea mono-input"
          rows={3}
          spellCheck={false}
          disabled={disabled}
          aria-describedby={field('ro-hint')}
          value={draft.readOnlyTools}
          onChange={(e) => set({ readOnlyTools: e.target.value })}
        />
        <p id={field('ro-hint')} className="hint">
          {t('caps_add_ro_tools_hint')}
        </p>
      </div>

      {askPin && (
        <PinField
          t={t}
          id={field('pin')}
          value={pin}
          disabled={disabled}
          onChange={setPin}
          hint={t('pin_hint')}
        />
      )}

      {tried && problem && (
        <p role="alert" className="setting-error">
          {t(problem)}
        </p>
      )}
      {error && (
        <p role="alert" className="alert">
          {error}
        </p>
      )}

      <div className="tile-actions">
        <button type="submit" className="btn" disabled={disabled}>
          {busy ? t('caps_add_busy') : t('caps_add_submit')}
        </button>
        <button type="button" className="btn btn--quiet" disabled={busy} onClick={onCancel}>
          {t('caps_cancel')}
        </button>
      </div>
    </form>
  );
}
