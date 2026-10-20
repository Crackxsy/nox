/**
 * The command line behind "Sag Nox, was er tun soll" (Spec UX-Neubau §4): a centred dialog on a
 * desktop, a full-screen sheet on a phone, opened by Ctrl+K / Cmd+K or by typing into the field at
 * the bottom of any page.
 *
 * ARIA combobox with a listbox: focus stays in the text field, Up/Down move the active option
 * (`aria-activedescendant`), Enter runs it, Escape closes. Results come from the synchronous matcher
 * in `palette.ts`; what an action did is said in a status line, never only shown.
 */

import { useId, useMemo, useRef, useState } from 'react';

import type { T } from '../i18n';
import { gotoIndex } from './gotoIndex';
import { Icon } from './icons';
import { type PaletteGroup, type PaletteItem, type PaletteSources, paletteItems } from './palette';
import { Sheet } from './Sheet';

const GROUP_KEY = {
  now: 'ux_pal_now',
  flows: 'ux_pal_flows',
  goto: 'ux_pal_goto',
  ask: 'ux_pal_ask',
} as const;

export interface CommandPaletteProps {
  t: T;
  open: boolean;
  initialQuery: string;
  /** Everything but the page index, which the palette builds itself. */
  sources: Omit<PaletteSources, 'pages'>;
  /** Nox is stopped: only "Gehe zu" stays, everything that would act is withheld. */
  stopped: boolean;
  onClose: () => void;
  /** Runs an item; resolves to the sentence to show (or '' when the palette closes anyway). */
  onRun: (item: PaletteItem) => Promise<string>;
}

export function CommandPalette(props: CommandPaletteProps) {
  return (
    <Sheet open={props.open} onClose={props.onClose} label={props.t('ux_palette')} variant="palette">
      {props.open && <PaletteBody {...props} />}
    </Sheet>
  );
}

function PaletteBody({ t, initialQuery, sources, stopped, onClose, onRun }: CommandPaletteProps) {
  const base = useId();
  const listId = `${base}-list`;
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [query, setQuery] = useState(initialQuery);
  /** -1: not moved yet, the default option is active. */
  const [active, setActive] = useState(-1);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState('');

  const pages = useMemo(() => gotoIndex(t), [t]);
  const items = useMemo(() => {
    const all = paletteItems(query, { ...sources, pages });
    return stopped ? all.filter((item) => item.group === 'goto') : all;
  }, [query, sources, pages, stopped]);


  // When the only thing "Sofort erledigen" offers is the sentence as a home command, the likelier
  // wish is a question: Enter then asks Nox, and the command is one arrow key away.
  const preferred = items[0]?.action.kind === 'command' ? items.findIndex((i) => i.group === 'ask') : 0;
  const index = active === -1 ? Math.max(0, preferred) : active;
  const current = items[Math.min(index, items.length - 1)];
  const optionId = (item: PaletteItem) => `${base}-o-${items.indexOf(item)}`;

  const run = async (item: PaletteItem | undefined) => {
    if (!item || busy) return;
    setBusy(true);
    try {
      const said = await onRun(item);
      if (said) setResult(said);
    } finally {
      setBusy(false);
    }
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setActive((i) => (items.length === 0 ? 0 : ((i === -1 ? Math.max(0, preferred) : i) + 1) % items.length));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActive((i) => {
        const from = i === -1 ? Math.max(0, preferred) : i;
        return items.length === 0 ? 0 : (from - 1 + items.length) % items.length;
      });
    } else if (e.key === 'Home') {
      e.preventDefault();
      setActive(0);
    } else if (e.key === 'End') {
      e.preventDefault();
      setActive(Math.max(0, items.length - 1));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      void run(current);
    }
  };

  const groups: { group: PaletteGroup; items: PaletteItem[] }[] = [];
  for (const item of items) {
    const last = groups[groups.length - 1];
    if (last && last.group === item.group) last.items.push(item);
    else groups.push({ group: item.group, items: [item] });
  }

  return (
    <div className="pal">
      <div className="pal-in">
        <Icon name="search" />
        <label htmlFor={`${base}-q`} className="sr-only">
          {t('ux_composer_placeholder')}
        </label>
        <input
          id={`${base}-q`}
          ref={inputRef}
          role="combobox"
          data-autofocus
          aria-expanded="true"
          aria-controls={listId}
          aria-autocomplete="list"
          aria-activedescendant={current ? optionId(current) : undefined}
          autoComplete="off"
          spellCheck={false}
          placeholder={t('ux_composer_placeholder')}
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setActive(-1);
            setResult('');
          }}
          onKeyDown={onKeyDown}
        />
        <button type="button" className="ib" aria-label={t('ux_close')} onClick={onClose}>
          <Icon name="close" />
        </button>
      </div>
      {stopped && <p className="pal-note">{t('ux_input_stopped')}</p>}
      {items.length === 0 && <p className="pal-empty">{t('ux_pal_empty')}</p>}
      <div id={listId} role="listbox" aria-label={t('ux_pal_results')} className="pal-list">
        {groups.map(({ group, items: rows }) => (
          <div key={group} role="group" aria-labelledby={`${base}-g-${group}`}>
            <div role="presentation" id={`${base}-g-${group}`} className="pal-g">
              {t(GROUP_KEY[group])}
            </div>
            {rows.map((item) => (
              // Options are reached from the text field (aria-activedescendant) and run with Enter there;
              // the click is the pointer's way to the same thing, so they need no focus of their own.
              // eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/interactive-supports-focus
              <div
                key={item.id}
                id={optionId(item)}
                role="option"
                aria-selected={item === current}
                className="pal-o"
                onMouseMove={() => setActive(items.indexOf(item))}
                onClick={() => void run(item)}
              >
                <span className="pal-l">{item.label}</span>
                {item.detail && <span className="pal-d">{item.detail}</span>}
              </div>
            ))}
          </div>
        ))}
      </div>
      <p role="status" className="pal-status">
        {busy ? t('ux_pal_running') : result}
      </p>
      <p className="pal-foot" aria-hidden="true">
        <span>
          <kbd>↑</kbd> <kbd>↓</kbd> {t('ux_pal_key_pick')}
        </span>
        <span>
          <kbd>Enter</kbd> {t('ux_pal_key_run')}
        </span>
        <span>
          <kbd>Esc</kbd> {t('ux_close')}
        </span>
      </p>
    </div>
  );
}
