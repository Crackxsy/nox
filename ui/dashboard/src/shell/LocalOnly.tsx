/**
 * A page a paired device may look at but not operate: one line saying where it works, and every
 * control inside disabled by the browser itself (a disabled fieldset), so nothing is offered that
 * the core would refuse (`nox.ipc.roles`).
 */

import type { ReactNode } from 'react';

import type { Key, T } from '../i18n';

export function LocalOnly({
  t,
  device,
  note = 'device_local_only',
  children,
}: {
  t: T;
  device: boolean;
  note?: Key;
  children: ReactNode;
}) {
  if (!device) return <>{children}</>;
  return (
    <>
      <p role="note" className="note device-note">
        {t(note)}
      </p>
      <fieldset disabled className="fieldset-plain">
        {children}
      </fieldset>
    </>
  );
}
