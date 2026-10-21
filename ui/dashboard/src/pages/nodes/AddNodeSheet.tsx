/**
 * "PC oder Laptop hinzufügen" (Spec C §1.10): pick the system, get a one-time code, follow three
 * steps on the new device. The code travels as a `nox-node://pair?...` link (and its QR code)
 * that carries the fingerprint of Nox's own authority, so the new device pins it before it sends
 * anything. While the sheet is open the list is refreshed every few seconds, and the status line
 * flips from "waiting" to "paired" to "connected" as the device arrives.
 *
 * The code is shown exactly as the core issued it and never kept; closing the sheet loses it.
 */

import { useCallback, useEffect, useId, useRef, useState } from 'react';

import { formatTimestamp } from '../../../../shared/format';
import { refusalText, useIpcAction } from '../../hooks';
import { fill, type Key, type Lang, type T } from '../../i18n';
import { type IpcClient, api } from '../../ipc';
import {
  type NodeInfo,
  type NodeOs,
  type NodePairing,
  groupCode,
  parseNodePairing,
  shortFingerprint,
  spellCode,
} from '../../model';
import { Sheet } from '../../shell/Sheet';
import { Icon } from '../../shell/icons';

/** Where the node installers are published (signed releases, R1). */
export const DOWNLOADS_URL = 'https://github.com/Crackxsy/nox/releases/latest';

const SYSTEMS: { os: NodeOs; label: Key; step: Key; command: string }[] = [
  { os: 'windows', label: 'node_os_windows', step: 'node_add_step_windows', command: '' },
  { os: 'linux', label: 'node_os_linux', step: 'node_add_step_linux', command: 'bash install-linux.sh --node' },
  { os: 'macos', label: 'node_os_macos', step: 'node_add_step_macos', command: 'bash install-macos.sh --node' },
];

export interface AddNodeSheetProps {
  t: T;
  lang: Lang;
  client: IpcClient | null;
  open: boolean;
  pinSet: boolean;
  nodes: NodeInfo[];
  onClose: () => void;
  /** A code was made: the page starts watching for the new device. */
  onCode: () => void;
}

type Copy = 'idle' | 'copied' | 'failed';

export function AddNodeSheet(props: AddNodeSheetProps) {
  return (
    <Sheet open={props.open} onClose={props.onClose} labelledBy="node-add-title">
      {props.open && <AddNode {...props} />}
    </Sheet>
  );
}

function AddNode({ t, lang, client, pinSet, nodes, onClose, onCode }: AddNodeSheetProps) {
  const id = useId();
  const [os, setOs] = useState<NodeOs>('windows');
  const [name, setName] = useState('');
  const [pin, setPin] = useState('');
  const [pairing, setPairing] = useState<NodePairing | null>(null);
  /** The nodes that existed when the code was made: a new id is the device that used it. */
  const [known, setKnown] = useState<Set<string> | null>(null);
  const [copy, setCopy] = useState<Copy>('idle');
  const [now, setNow] = useState(() => Date.now());
  const pinSentRef = useRef(false);
  const describe = useCallback((e: unknown) => refusalText(t, e, pinSentRef.current), [t]);
  const { busy, error, run } = useIpcAction(client, t, describe);
  const system = SYSTEMS.find((s) => s.os === os) ?? SYSTEMS[0];

  useEffect(() => {
    if (!pairing) return;
    const timer = window.setInterval(() => setNow(Date.now()), 5000);
    return () => window.clearInterval(timer);
  }, [pairing]);

  const expired = pairing !== null && Date.parse(pairing.expiresAt) <= now;
  const arrived = known === null ? undefined : nodes.find((n) => !n.revoked && !known.has(n.id));

  const start = () => {
    pinSentRef.current = pin.trim() !== '';
    void run('pair', async (c) => {
      const result = parseNodePairing(await api.nodePairStart(c, name.trim(), pin.trim()));
      if (!result) throw new Error(t('lan_unreadable'));
      setKnown(new Set(nodes.map((n) => n.id)));
      setPairing(result);
      setCopy('idle');
      setNow(Date.now());
      setPin('');
      onCode();
    });
  };

  const pairCommand = pairing ? `nox node pair "${pairing.link}"` : '';
  const copyCommand = async () => {
    try {
      await navigator.clipboard.writeText(pairCommand);
      setCopy('copied');
    } catch {
      setCopy('failed');
    }
  };

  const status = arrived
    ? fill(t(arrived.online ? 'node_add_connected' : 'node_add_paired'), arrived.name)
    : pairing && !expired
      ? copy === 'copied'
        ? t('lan_pair_copied')
        : copy === 'failed'
          ? t('lan_pair_copy_failed')
          : t('node_add_waiting')
      : expired
        ? t('lan_pair_expired')
        : '';

  return (
    <>
      <div className="dlg-h">
        <h2 id="node-add-title">{t('node_add')}</h2>
        <button type="button" className="ib" aria-label={t('ux_close')} onClick={onClose}>
          <Icon name="close" />
        </button>
      </div>
      <p className="lede">{t('node_add_lede')}</p>
      {error && (
        <p role="alert" className="alert">
          {error}
        </p>
      )}
      <fieldset className="choice node-os">
        <legend className="label">{t('node_add_os')}</legend>
        {SYSTEMS.map((s) => (
          <label key={s.os} className={os === s.os ? 'opt opt--sel' : 'opt'}>
            <input
              type="radio"
              name={`${id}-os`}
              value={s.os}
              checked={os === s.os}
              onChange={() => setOs(s.os)}
            />
            <span className="opt-t">{t(s.label)}</span>
          </label>
        ))}
      </fieldset>

      {(!pairing || expired) && (
        <div className="row-controls">
          <div className="field field--grow">
            <label htmlFor={`${id}-name`} className="label">
              {t('lan_pair_name')}
            </label>
            <input
              id={`${id}-name`}
              className="input"
              value={name}
              onChange={(e) => setName(e.target.value)}
              maxLength={64}
              autoComplete="off"
            />
          </div>
          {pinSet && (
            <div className="field">
              <label htmlFor={`${id}-pin`} className="label">
                {t('lan_pair_pin')}
              </label>
              <input
                id={`${id}-pin`}
                className="input input--pin"
                type="password"
                inputMode="numeric"
                autoComplete="off"
                value={pin}
                onChange={(e) => setPin(e.target.value)}
              />
            </div>
          )}
          <button type="button" className="btn" disabled={!client || busy === 'pair'} onClick={start}>
            {expired ? t('lan_pair_again') : t('lan_pair_button')}
          </button>
        </div>
      )}

      {pairing && !expired && (
        <ol className="steps node-steps">
          <li>
            <p>{t(system.step)}</p>
            {system.command && <p className="mono break">{system.command}</p>}
            <a className="link" href={DOWNLOADS_URL} target="_blank" rel="noreferrer">
              {t('node_add_download')}
            </a>
          </li>
          <li>
            <p>{t('node_add_step_pair')}</p>
            <div className="pairing">
              {pairing.qr && <img className="pairing-qr" src={pairing.qr} alt={t('node_add_qr_alt')} />}
              <div className="pairing-facts">
                <p className="mono break">{pairCommand}</p>
                <div className="pairing-buttons">
                  <button type="button" className="btn btn--quiet btn--sm" onClick={() => void copyCommand()}>
                    {t('node_add_command')}
                  </button>
                </div>
                <p className="label">{t('lan_pair_code')}</p>
                <p className="code-display">
                  <span className="code-display-value" aria-hidden="true">
                    {groupCode(pairing.code)}
                  </span>
                  <span className="sr-only">{fill(t('lan_pair_code_spelled'), spellCode(pairing.code))}</span>
                </p>
                <p className="hint">{fill(t('lan_pair_valid_until'), formatTimestamp(pairing.expiresAt, lang))}</p>
              </div>
            </div>
          </li>
          <li>
            <p>{t('node_add_step_check')}</p>
            {pairing.caFingerprint && (
              <>
                <p className="label">{t('lan_ca_fingerprint')}</p>
                <p className="fingerprint">{shortFingerprint(pairing.caFingerprint)}</p>
              </>
            )}
          </li>
        </ol>
      )}
      <p role="status" className={status ? (arrived ? 'banner banner--ok' : 'hint pairing-status') : 'sr-only'}>
        {status}
      </p>
    </>
  );
}
