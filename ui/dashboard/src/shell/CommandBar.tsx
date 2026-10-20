/**
 * "Sag Nox, was er tun soll" at the bottom of every page except the conversation, where the chat
 * field takes its place (Spec UX-Neubau §4). Typing into it, tapping it, or pressing Enter opens the
 * command palette with what was typed so far; Tab passes through it without opening anything.
 *
 * There is no microphone button: push-to-talk belongs to the desktop shell (`voice.ptt` is not a
 * dashboard request), and a button that only looks like it listens would be a lie.
 */

import type { T } from '../i18n';
import { Icon } from './icons';

export interface CommandBarProps {
  t: T;
  /** Nox is stopped: the field is locked and says why. */
  stopped: boolean;
  onOpen: (query: string) => void;
}

export function CommandBar({ t, stopped, onOpen }: CommandBarProps) {
  return (
    <form
      className="cmdbar"
      aria-label={t('ux_composer_label')}
      onSubmit={(e) => {
        e.preventDefault();
        onOpen('');
      }}
    >
      <div className="cmdbox">
        <label htmlFor="cmd-input" className="sr-only">
          {t('ux_composer_placeholder')}
        </label>
        <input
          id="cmd-input"
          type="text"
          autoComplete="off"
          enterKeyHint="go"
          aria-haspopup="dialog"
          aria-keyshortcuts="Control+K Meta+K"
          aria-describedby={stopped ? 'cmd-stopped' : undefined}
          placeholder={stopped ? t('ux_input_stopped_short') : t('ux_composer_placeholder')}
          disabled={stopped}
          value=""
          onChange={(e) => onOpen(e.target.value)}
          onClick={() => onOpen('')}
        />
        <kbd className="cmd-kbd" aria-hidden="true">
          {t('ux_key_ctrl')} K
        </kbd>
        <button type="submit" className="ib ib--send" aria-label={t('ux_palette_open')} disabled={stopped}>
          <Icon name="arrow" />
        </button>
      </div>
      {stopped && (
        <p id="cmd-stopped" className="sr-only">
          {t('ux_input_stopped')}
        </p>
      )}
    </form>
  );
}
