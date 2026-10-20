/**
 * Showing a key combination in words ("ctrl+alt+space" -> "Strg + Alt + Leertaste"): the
 * hotkey field and the computer-use strip both need it, and the strip is in the start bundle.
 */

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
