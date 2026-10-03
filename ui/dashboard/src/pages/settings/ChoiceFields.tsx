/**
 * Controls for settings whose valid values are known: tick them, order them, or press them.
 *
 * "Aktive Plugins" and "Reihenfolge der KI-Backends" used to be text boxes that expected exact ids,
 * one per line, and the push-to-talk key a spelling the window process happened to understand. The
 * core now says which values exist (`options`) and whether their order matters (`ordered`), so the
 * user picks instead of types.
 *
 * The key recorder writes what the shell's hotkey listener reads (`nox.shell.hotkeys`): pynput key
 * names joined with `+`, modifiers first - `home`, `ctrl+alt+space`, `f8`. The physical key
 * (`KeyboardEvent.code`) is used for letters and digits so Shift does not turn `1` into `!`.
 */

import { useState } from 'react';

import type { T } from '../../i18n';

// ---- the key recorder -------------------------------------------------------------------------

const MODIFIER_KEYS = new Set(['Control', 'Alt', 'AltGraph', 'Shift', 'Meta', 'OS']);

/** `KeyboardEvent.code` -> pynput key name, for every key that is not a letter or a digit. */
const CODE_NAMES: Record<string, string> = {
  Space: 'space',
  Home: 'home',
  End: 'end',
  PageUp: 'page_up',
  PageDown: 'page_down',
  Insert: 'insert',
  Delete: 'delete',
  ArrowUp: 'up',
  ArrowDown: 'down',
  ArrowLeft: 'left',
  ArrowRight: 'right',
  Tab: 'tab',
  Enter: 'enter',
  Backspace: 'backspace',
  Pause: 'pause',
  ScrollLock: 'scroll_lock',
  CapsLock: 'caps_lock',
  PrintScreen: 'print_screen',
  ContextMenu: 'menu',
};

export interface KeyLike {
  key: string;
  code: string;
  ctrlKey: boolean;
  altKey: boolean;
  shiftKey: boolean;
  metaKey: boolean;
}

/** The combination a key press stands for, or null while only modifiers are held. */
export function comboFromKey(event: KeyLike): string | null {
  if (MODIFIER_KEYS.has(event.key)) return null;
  let main: string | null;
  const letter = /^Key([A-Z])$/.exec(event.code);
  const digit = /^Digit([0-9])$/.exec(event.code);
  const fn = /^F([0-9]{1,2})$/.exec(event.code);
  if (letter?.[1]) main = letter[1].toLowerCase();
  else if (digit?.[1]) main = digit[1];
  else if (fn?.[1]) main = `f${fn[1]}`;
  else main = CODE_NAMES[event.code] ?? null;
  if (main === null) return null;
  const parts: string[] = [];
  if (event.ctrlKey) parts.push('ctrl');
  if (event.altKey) parts.push('alt');
  if (event.shiftKey) parts.push('shift');
  if (event.metaKey) parts.push('cmd');
  parts.push(main);
  return parts.join('+');
}

const SHOWN_DE: Record<string, string> = {
  ctrl: 'Strg',
  alt: 'Alt',
  shift: 'Umschalt',
  cmd: 'Windows',
  space: 'Leertaste',
  home: 'Pos1',
  end: 'Ende',
  page_up: 'Bild ↑',
  page_down: 'Bild ↓',
  insert: 'Einfg',
  delete: 'Entf',
  up: '↑',
  down: '↓',
  left: '←',
  right: '→',
  enter: 'Enter',
  backspace: 'Rücktaste',
  tab: 'Tab',
  pause: 'Pause',
  scroll_lock: 'Rollen',
  caps_lock: 'Feststell',
  print_screen: 'Druck',
  menu: 'Menü',
};

const SHOWN_EN: Record<string, string> = {
  ctrl: 'Ctrl',
  alt: 'Alt',
  shift: 'Shift',
  cmd: 'Windows',
  space: 'Space',
  home: 'Home',
  end: 'End',
  page_up: 'Page Up',
  page_down: 'Page Down',
  insert: 'Insert',
  delete: 'Delete',
  up: '↑',
  down: '↓',
  left: '←',
  right: '→',
  menu: 'Menu',
};

/** "ctrl+alt+space" -> "Strg + Alt + Leertaste". An unknown part is shown in capitals. */
export function showCombo(combo: string, lang: string): string {
  const table = lang === 'de' ? SHOWN_DE : SHOWN_EN;
  return combo
    .split('+')
    .filter((part) => part.length > 0)
    .map((part) => table[part] ?? part.toUpperCase())
    .join(' + ');
}

export interface HotkeyFieldProps {
  id: string;
  t: T;
  lang: string;
  value: string;
  disabled: boolean;
  onChange: (combo: string) => void;
}

export function HotkeyField({ id, t, lang, value, disabled, onChange }: HotkeyFieldProps) {
  const [listening, setListening] = useState(false);
  return (
    <span className="hotkey">
      <kbd className="hotkey-keys" aria-live="polite">
        {listening ? t('hotkey_listening') : value ? showCombo(value, lang) : t('hotkey_none')}
      </kbd>
      <button
        id={id}
        type="button"
        className="btn btn--quiet"
        disabled={disabled}
        aria-pressed={listening}
        onClick={() => setListening((on) => !on)}
        onBlur={() => setListening(false)}
        onKeyDown={(event) => {
          if (!listening) return;
          event.preventDefault();
          if (event.key === 'Escape') {
            setListening(false);
            return;
          }
          const combo = comboFromKey(event);
          if (combo === null) return; // only modifiers so far: wait for the key itself
          onChange(combo);
          setListening(false);
        }}
      >
        {listening ? t('hotkey_cancel') : t('hotkey_record')}
      </button>
    </span>
  );
}

// ---- the choice list --------------------------------------------------------------------------

export interface MultiChoiceProps {
  id: string;
  t: T;
  options: string[];
  value: string[];
  ordered: boolean;
  disabled: boolean;
  label: (option: string) => string;
  onChange: (next: string[]) => void;
}

/**
 * One checkbox per option. When the order matters the chosen ones come first, in their order, each
 * with buttons to move it; the others follow and are appended at the end when ticked.
 */
export function MultiChoice({
  id,
  t,
  options,
  value,
  ordered,
  disabled,
  label,
  onChange,
}: MultiChoiceProps) {
  const chosen = value.filter((v) => options.includes(v));
  // Unordered choices read best alphabetically by what the user sees, not by their ids.
  const rows = ordered
    ? [...chosen, ...options.filter((o) => !chosen.includes(o))]
    : [...options].sort((a, b) => label(a).localeCompare(label(b)));

  const toggle = (option: string) =>
    onChange(chosen.includes(option) ? chosen.filter((v) => v !== option) : [...chosen, option]);

  const move = (index: number, by: -1 | 1) => {
    const next = [...chosen];
    const target = index + by;
    if (target < 0 || target >= next.length) return;
    const [item] = next.splice(index, 1);
    if (item !== undefined) next.splice(target, 0, item);
    onChange(next);
  };

  return (
    <ul id={id} className="choice-list">
      {rows.map((option) => {
        const on = chosen.includes(option);
        const position = chosen.indexOf(option);
        const name = label(option);
        return (
          <li key={option} className="choice-row">
            <label className="choice-label">
              <input
                type="checkbox"
                checked={on}
                disabled={disabled}
                onChange={() => toggle(option)}
              />
              {ordered && on && <span className="choice-rank">{position + 1}.</span>}
              <span>{name}</span>
            </label>
            {ordered && on && (
              <span className="choice-move">
                <button
                  type="button"
                  className="btn btn--quiet btn--icon"
                  disabled={disabled || position === 0}
                  aria-label={`${name}: ${t('choice_move_up')}`}
                  onClick={() => move(position, -1)}
                >
                  ↑
                </button>
                <button
                  type="button"
                  className="btn btn--quiet btn--icon"
                  disabled={disabled || position === chosen.length - 1}
                  aria-label={`${name}: ${t('choice_move_down')}`}
                  onClick={() => move(position, 1)}
                >
                  ↓
                </button>
              </span>
            )}
          </li>
        );
      })}
    </ul>
  );
}
