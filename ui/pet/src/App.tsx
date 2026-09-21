import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { CaptureIndicator } from './CaptureIndicator';
import {
  type ConnStatus,
  type Envelope,
  type IpcClient,
  createPetClient,
  petVariantChanged,
  variantFromStateReply,
} from './ipc';
import { Pet } from './Pet';
import { INITIAL_STATE, type PetState, deriveAnim, reduceEvent, stillState, toInput } from './petState';
import { type LoadedRig, type RigRenderer, loadRig } from './rig';
import { RiggedPet } from './RiggedPet';
import { SpritePet, loadImageElement } from './SpritePet';
import { type Lang, petTranslator } from './strings';
import { useThemeMode } from './theme';
import { getVariant } from './variants';
import {
  type LoadedSprite,
  loadSprite,
  reasonOf,
  spriteBaseUrl,
  spriteExpressionFor,
  spriteVariantId,
} from './variants/sprite';

export interface AppProps {
  token: string | null;
  /** One language per window; chosen once in `main.tsx` and reflected on `<html lang>`. */
  lang: Lang;
  /** OBS browser-source mode: no interaction, no capture indicator (PRD §20), transparent. */
  overlay: boolean;
  /** OP-1 creature concept, from `?variant=`; falls back to `neutral` for unknown/missing ids.
   * `sprite:<id>` selects a sprite set instead of a procedural variant (#19). */
  variantId: string | null;
  /** Dev-only still-frame override (`?still=1`): renders one frame with no WebSocket, used by
   * `scripts/render_variants.py` to screenshot each variant/expression combination headlessly. */
  still: boolean;
  /** Dev-only motion preview (`?animate=1`): the `?still=1` state presets with the loop running,
   * so `scripts/render_rig.py` can screenshot a rig in motion without a core behind it. */
  animate: boolean;
  /** `?expression=` for still mode: one of petState.ts STILL_EXPRESSIONS, else falls back to `normal`. */
  stillExpression: string | null;
  /** `?size=` canvas size override in px, for exact-size screenshots (default 260). */
  size?: number;
}

export function App({ token, lang, overlay, variantId, still, animate, stillExpression, size }: AppProps) {
  const t = useMemo(() => petTranslator(lang), [lang]);
  // The variant is state, not just a prop: `config.set pet.variant` swaps it live, without a page
  // reload (#24). `variantId` is only the value the shell put in the URL at load time.
  const [activeVariant, setActiveVariant] = useState<string | null>(variantId);
  useEffect(() => setActiveVariant(variantId), [variantId]);

  const [sprite, setSprite] = useState<LoadedSprite | null>(null);
  const [rig, setRig] = useState<LoadedRig | null>(null);
  const variant = useMemo(() => getVariant(activeVariant), [activeVariant]);
  const theme = useThemeMode();
  const [state, setState] = useState<PetState>(() =>
    still || animate ? stillState(stillExpression) : INITIAL_STATE,
  );
  const [status, setStatus] = useState<ConnStatus>(still || animate ? 'online' : 'offline');
  const [statusDetail, setStatusDetail] = useState<string | undefined>(undefined);
  const clientRef = useRef<IpcClient | null>(null);

  // `settings.changed` carries paths only, never values (Event Model), so the new variant has to be
  // read back. The pet role may call `state.get`; if the core does not expose `pet.variant` there
  // the swap does not happen here and the shell's page reload (shell/app.py) is what applies it.
  const onEvent = useCallback((env: Envelope) => {
    setState((s) => reduceEvent(s, env.name, env.payload));
    if (!petVariantChanged(env)) return;
    const client = clientRef.current;
    if (!client || client.status !== 'online') return;
    client
      .request('state.get', { path: 'pet.variant' })
      .then((reply) => {
        const next = variantFromStateReply(reply);
        if (next) setActiveVariant(next);
        else console.info('pet.variant_not_readable', 'waiting for the shell to reload the page');
      })
      .catch((err: unknown) => console.info('pet.variant_read_failed', reasonOf(err)));
  }, []);

  useEffect(() => {
    if (still || animate || !token) return;
    let cancelled = false;
    createPetClient(token, {
      onEvent,
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
  }, [token, still, animate, onEvent]);

  // Sprite variants (#19): fetch + validate + preload the whole set before showing anything. Any
  // failure logs a reason and leaves `sprite` null, which renders the procedural variant instead.
  useEffect(() => {
    const id = spriteVariantId(activeVariant);
    if (id === null) {
      setSprite(null);
      return;
    }
    let cancelled = false;
    loadSprite(id, {
      base: import.meta.env.BASE_URL,
      fetchJson: async (url) => {
        const res = await fetch(url, { cache: 'no-store' });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      },
      loadImage: loadImageElement,
    })
      .then((loaded) => {
        if (!cancelled) setSprite(loaded);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        console.warn('pet.sprite_fallback', { variant: activeVariant, reason: reasonOf(err) });
        setSprite(null);
      });
    return () => {
      cancelled = true;
    };
  }, [activeVariant]);

  // Deformation rig (`rig.json`). Strictly an upgrade on top of the sprite set: a variant without
  // one, or with one that will not load, keeps the sprite renderer and its static frames, and the
  // reason is logged rather than swallowed.
  useEffect(() => {
    const id = spriteVariantId(activeVariant);
    if (id === null) {
      setRig(null);
      return;
    }
    let cancelled = false;
    loadRig(id, {
      variantBase: spriteBaseUrl(import.meta.env.BASE_URL, id),
      fetchJson: async (url) => {
        const res = await fetch(url, { cache: 'no-store' });
        if (res.status === 404) return null;
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      },
      loadImage: loadImageElement,
    })
      .then((loaded) => {
        if (!cancelled) setRig(loaded);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        console.warn('pet.rig_fallback', { variant: activeVariant, reason: reasonOf(err) });
        setRig(null);
      });
    return () => {
      cancelled = true;
    };
  }, [activeVariant]);

  // A rig running on Canvas 2D is a working pet with a coarser mesh, not a failure — but it is a
  // downgrade, and the reason for it belongs in the log rather than nowhere.
  const onRigBackend = useCallback((backend: RigRenderer['backend'], reason: string | null) => {
    if (reason) console.info('pet.rig_backend', backend, reason);
  }, []);

  const input = useMemo(() => toInput(state), [state]);
  const params = useMemo(() => {
    const derived = deriveAnim(input, {
      hue: variant.palette.bodyHue,
      sat: variant.palette.bodySat,
      // #25: the window is transparent, so the creature sits on whatever the active theme paints
      // behind it. Each variant ships a lightness tuned for each case; pick the matching one.
      light: theme === 'dark' ? variant.palette.bodyLightOnDark : variant.palette.bodyLightOnLight,
    });
    // A still frame is by definition paused: `render_variants.py` wants one frame, not a loop.
    return still ? { ...derived, paused: true } : derived;
  }, [input, variant, theme, still]);

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
    <div className="pet-root">
      {sprite && rig ? (
        <RiggedPet
          rig={rig}
          input={input}
          params={params}
          expression={spriteExpressionFor(input)}
          interactive={!overlay && !still}
          onInteract={onInteract}
          size={size}
          still={still}
          preview={still || animate}
          petLabel={sprite.manifest.name}
          onBackend={onRigBackend}
        />
      ) : sprite ? (
        <SpritePet
          sprite={sprite}
          expression={spriteExpressionFor(input)}
          params={params}
          interactive={!overlay && !still}
          onInteract={onInteract}
          size={size}
          still={still}
          petLabel={sprite.manifest.name}
        />
      ) : (
        <Pet
          params={params}
          speakingLevel={state.speakingLevel}
          onSpeakingDecay={onSpeakingDecay}
          interactive={!overlay && !still}
          onInteract={onInteract}
          variant={variant}
          size={size}
          theme={theme}
          label={variant.name}
        />
      )}
      {!overlay && !still && (
        <CaptureIndicator
          t={t}
          capture={state.capture}
          connected={state.connected}
          privacyMode={state.privacyMode}
          muted={state.muted}
        />
      )}
      {!overlay && !still && !token && (
        <p role="alert" className="pet-chip pet-note">
          {t('no_token')}
        </p>
      )}
      {!overlay && !still && status === 'auth_failed' && (
        <p role="alert" className="pet-chip pet-note">
          {t('auth_denied')}
          {statusDetail ? `: ${statusDetail}` : ''}
        </p>
      )}
    </div>
  );
}
