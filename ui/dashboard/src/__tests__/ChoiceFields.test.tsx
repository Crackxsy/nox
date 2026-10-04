/**
 * Picking instead of typing: the key recorder writes what the shell's hotkey listener reads, and
 * the choice list keeps an order where the order matters.
 */

import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { translator } from '../i18n';
import {
  HotkeyField,
  type KeyLike,
  MultiChoice,
  comboFromKey,
  showCombo,
} from '../pages/settings/ChoiceFields';

const t = translator('de');

function key(code: string, key = code, mods: Partial<KeyLike> = {}): KeyLike {
  return { key, code, ctrlKey: false, altKey: false, shiftKey: false, metaKey: false, ...mods };
}

describe('comboFromKey', () => {
  it('names keys the way the shell listener does', () => {
    expect(comboFromKey(key('Home'))).toBe('home');
    expect(comboFromKey(key('F8'))).toBe('f8');
    expect(comboFromKey(key('PageDown'))).toBe('page_down');
    expect(comboFromKey(key('Space', ' ', { ctrlKey: true, altKey: true }))).toBe(
      'ctrl+alt+space',
    );
  });

  it('uses the physical key, so Shift does not change the digit', () => {
    expect(comboFromKey(key('Digit1', '!', { shiftKey: true }))).toBe('shift+1');
    expect(comboFromKey(key('KeyM', 'M', { shiftKey: true }))).toBe('shift+m');
  });

  it('waits while only modifiers are held', () => {
    expect(comboFromKey(key('ControlLeft', 'Control', { ctrlKey: true }))).toBeNull();
  });

  it('shows combinations in words', () => {
    expect(showCombo('ctrl+alt+space', 'de')).toBe('Strg + Alt + Leertaste');
    expect(showCombo('home', 'de')).toBe('Pos1');
  });
});

describe('HotkeyField', () => {
  it('records the next key press after the button is pressed', () => {
    const onChange = vi.fn();
    render(<HotkeyField id="k" t={t} lang="de" value="home" disabled={false} onChange={onChange} />);
    expect(screen.getByText('Pos1')).toBeTruthy();
    const button = screen.getByRole('button', { name: 'Taste festlegen' });
    fireEvent.click(button);
    expect(screen.getByText('Drück jetzt die Taste …')).toBeTruthy();
    fireEvent.keyDown(button, { key: 'F9', code: 'F9' });
    expect(onChange).toHaveBeenCalledWith('f9');
  });

  it('Escape cancels without changing anything', () => {
    const onChange = vi.fn();
    render(<HotkeyField id="k" t={t} lang="de" value="home" disabled={false} onChange={onChange} />);
    const button = screen.getByRole('button', { name: 'Taste festlegen' });
    fireEvent.click(button);
    fireEvent.keyDown(button, { key: 'Escape', code: 'Escape' });
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByText('Pos1')).toBeTruthy();
  });
});

describe('MultiChoice', () => {
  const label = (o: string) => o.toUpperCase();

  it('ticking adds and unticking removes', () => {
    const onChange = vi.fn();
    render(
      <MultiChoice
        id="m"
        t={t}
        options={['obs', 'twitch', 'home']}
        value={['twitch']}
        ordered={false}
        disabled={false}
        label={label}
        onChange={onChange}
      />,
    );
    fireEvent.click(screen.getByRole('checkbox', { name: 'OBS' }));
    expect(onChange).toHaveBeenLastCalledWith(['twitch', 'obs']);
    fireEvent.click(screen.getByRole('checkbox', { name: 'TWITCH' }));
    expect(onChange).toHaveBeenLastCalledWith([]);
  });

  it('keeps and changes the order where it matters', () => {
    const onChange = vi.fn();
    render(
      <MultiChoice
        id="m"
        t={t}
        options={['claude_code', 'ollama', 'rules']}
        value={['ollama', 'claude_code']}
        ordered
        disabled={false}
        label={label}
        onChange={onChange}
      />,
    );
    const names = screen.getAllByRole('checkbox').map((box) => box.closest('label')?.textContent);
    expect(names).toEqual(['1.OLLAMA', '2.CLAUDE_CODE', 'RULES']);
    fireEvent.click(screen.getByRole('button', { name: 'CLAUDE_CODE: nach oben' }));
    expect(onChange).toHaveBeenLastCalledWith(['claude_code', 'ollama']);
  });
});
