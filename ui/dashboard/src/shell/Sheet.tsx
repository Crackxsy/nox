/**
 * Every floating layer of the shell - the mode and privacy menus, the command palette, the pairing
 * and add sheets - is one native `<dialog>` opened with `showModal()`: the browser traps focus,
 * makes the page behind inert, and turns Escape into a `cancel` event. On a phone the same dialog
 * is a sheet sliding up from the bottom (CSS only).
 *
 * Focus goes to `initialFocus` (the safe or the current choice) or to a `data-autofocus` element
 * when the dialog opens, and back to whatever had it before when it closes.
 */

import { type ReactNode, type RefObject, useEffect, useRef } from 'react';

import { StopButton } from './stop';

export interface SheetProps {
  open: boolean;
  onClose: () => void;
  /** id of the heading that names the dialog. */
  labelledBy?: string;
  /** Accessible name when there is no visible heading (the command palette). */
  label?: string;
  /** `pop`: anchored menu under the header on wide screens; `palette`: the command line. */
  variant?: 'sheet' | 'pop' | 'palette';
  initialFocus?: RefObject<HTMLElement | null>;
  className?: string;
  /** `corner` (default): Stopp at the header's spot, since the header is inert behind the sheet;
   * `none`: the content shows its own (the command line). */
  stop?: 'corner' | 'none';
  children: ReactNode;
}

export function Sheet({
  open,
  onClose,
  labelledBy,
  label,
  variant = 'sheet',
  initialFocus,
  className,
  stop = 'corner',
  children,
}: SheetProps) {
  const ref = useRef<HTMLDialogElement | null>(null);
  const restoreRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      restoreRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      dialog.showModal();
      // The given element, else one marked `data-autofocus` (the palette's text field), else the
      // dialog itself. Children cannot focus themselves earlier: before `showModal` they are hidden.
      const target = initialFocus?.current ?? dialog.querySelector<HTMLElement>('[data-autofocus]') ?? dialog;
      target.focus();
      if (target instanceof HTMLInputElement) target.setSelectionRange(target.value.length, target.value.length);
    } else if (!open && dialog.open) {
      dialog.close();
    }
  }, [open, initialFocus]);

  // Give focus back on close, however the dialog was closed.
  useEffect(() => {
    if (open) return;
    const target = restoreRef.current;
    restoreRef.current = null;
    if (target?.isConnected) target.focus();
  }, [open]);

  const classes = ['dlg', `dlg--${variant}`];
  if (className) classes.push(className);

  return (
    // A click on the backdrop closes like Escape does; the keyboard path is the native `cancel`.
    // eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-noninteractive-element-interactions
    <dialog
      ref={ref}
      className={classes.join(' ')}
      aria-labelledby={labelledBy}
      aria-label={labelledBy ? undefined : label}
      tabIndex={-1}
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
      onClick={(e) => {
        // A click on the backdrop lands on the dialog element itself.
        if (e.target === e.currentTarget) onClose();
      }}
    >
      {open ? <div className="dlg-in">{children}</div> : null}
      {open && stop === 'corner' ? <StopButton placement="corner" /> : null}
    </dialog>
  );
}
