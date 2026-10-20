/**
 * The banner every page shows while Nox steers mouse and keyboard (`desktop.computer_use`).
 *
 * It has no close button on purpose: it disappears when the core says the session ended, not when
 * somebody clicks it away, so nobody can lose track of a running session. It sits under the top
 * bar and stays there while the page scrolls, and it names the hotkey that ends the session at
 * once — the one thing a person needs in that moment.
 */

import { showCombo } from './pages/settings/ChoiceFields';
import { type T, fill } from './i18n';
import type { ComputerUseNotice } from './model';

export interface ComputerUseBannerProps {
  t: T;
  lang: string;
  notice: ComputerUseNotice | null;
}

export function ComputerUseBanner({ t, lang, notice }: ComputerUseBannerProps) {
  if (!notice?.active) return null;
  const text = notice.stop
    ? fill(t('computer_use_banner'), showCombo(notice.stop, lang))
    : t('computer_use_banner_no_hotkey');
  return (
    <p role="alert" className="banner banner--control">
      {text}
    </p>
  );
}
