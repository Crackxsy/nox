import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { CaptureIndicator } from './CaptureIndicator';
import { type ConnStatus, type Envelope, type IpcClient, createPetClient } from './ipc';
import { Pet } from './Pet';
import { INITIAL_STATE, type PetState, deriveAnim, reduceEvent, stillState, toInput } from './petState';
import { getVariant } from './variants';

export interface AppProps {
  token: string | null;
  /** OBS browser-source mode: no interaction, no capture indicator (PRD §20), transparent. */
  overlay: boolean;
  /** OP-1 creature concept, from `?variant=`; falls back to `neutral` for unknown/missing ids. */
  variantId: string | null;
  /** Dev-only still-frame override (`?still=1`): renders one frame with no WebSocket, used by
   * `scripts/render_variants.py` to screenshot each variant/expression combination headlessly. */
  still: boolean;
  /** `?expression=` for still mode: one of petState.ts STILL_EXPRESSIONS, else falls back to `normal`. */
  stillExpression: string | null;
  /** `?size=` canvas size override in px, for exact-size screenshots (default 260). */
  size?: number;
}

export function App({ token, overlay, variantId, still, stillExpression, size }: AppProps) {
  const variant = useMemo(() => getVariant(variantId), [variantId]);
  const [state, setState] = useState<PetState>(() =>
    still ? stillState(stillExpression) : INITIAL_STATE,
  );
  const [status, setStatus] = useState<ConnStatus>(still ? 'online' : 'offline');
  const [statusDetail, setStatusDetail] = useState<string | undefined>(undefined);
  const clientRef = useRef<IpcClient | null>(null);

  useEffect(() => {
    if (still || !token) return;
    let cancelled = false;
    createPetClient(token, {
      onEvent: (env: Envelope) => setState((s) => reduceEvent(s, env.name, env.payload)),
      onStatus: (s, detail) => {
        setStatus(s);
        setStatusDetail(detail);
        setState((prev) => ({ ...prev, connected: s === 'online' }));
      },
    }).then((c) => {
      if (cancelled) c.close();
      else clientRef.current = c;
    });
    return () => {
      cancelled = true;
      clientRef.current?.close();
      clientRef.current = null;
    };
  }, [token, still]);

  const params = useMemo(
    () => deriveAnim(toInput(state), { hue: variant.palette.bodyHue, sat: variant.palette.bodySat, light: variant.palette.bodyLightOnDark }),
    [state, variant],
  );

  const onSpeakingDecay = useCallback((level: number) => {
    setState((s) => (s.speakingLevel === level ? s : { ...s, speakingLevel: level }));
  }, []);

  const onInteract = useCallback((type: 'click', x: number, y: number) => {
    const c = clientRef.current;
    if (!c || c.status !== 'online') return;
    c.request('pet.interact', { type, x, y }).catch(() => {
      /* permission/unavailable errors are the core's decision; nothing to fake here */
    });
  }, []);

  return (
    <div style={{ position: 'relative', width: '100%', height: '100%', background: 'transparent' }}>
      <Pet
        params={params}
        speakingLevel={state.speakingLevel}
        onSpeakingDecay={onSpeakingDecay}
        interactive={!overlay}
        onInteract={onInteract}
        variant={variant}
        size={size}
      />
      {!overlay && !still && (
        <CaptureIndicator
          capture={state.capture}
          connected={state.connected}
          privacyMode={state.privacyMode}
          muted={state.muted}
        />
      )}
      {!overlay && !still && !token && (
        <p role="alert" style={noteStyle}>
          Kein Sitzungs-Token – Seite über die Nox-Shell öffnen / no session token, open via the Nox shell
        </p>
      )}
      {!overlay && !still && status === 'auth_failed' && (
        <p role="alert" style={noteStyle}>
          Authentifizierung abgelehnt / auth denied{statusDetail ? `: ${statusDetail}` : ''}
        </p>
      )}
    </div>
  );
}

const noteStyle: React.CSSProperties = {
  position: 'absolute',
  bottom: 4,
  left: 8,
  right: 8,
  margin: 0,
  fontSize: 10,
  textAlign: 'center',
  background: 'rgba(11,11,18,0.85)',
  borderRadius: 8,
  padding: '3px 6px',
};
