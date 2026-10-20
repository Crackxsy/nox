/**
 * What kind of browser this page runs in, as far as the Devices page has to care.
 *
 * iOS keeps Safari and an app added to the home screen apart: separate storage, so a pairing made
 * in Safari is unknown to the home-screen app. The certificate tile says so - and the order that
 * works (certificate, home screen, then pair inside the app) - only where it applies.
 */

export interface PlatformInput {
  userAgent: string;
  maxTouchPoints: number;
  /** Safari's own flag for a page started from the home screen. */
  standalone?: boolean;
  /** `matchMedia('(display-mode: standalone)').matches`. */
  displayStandalone: boolean;
}

export function isIos(p: Pick<PlatformInput, 'userAgent' | 'maxTouchPoints'>): boolean {
  return /iPhone|iPad|iPod/.test(p.userAgent) || (/Macintosh/.test(p.userAgent) && p.maxTouchPoints > 1);
}

/** Started as an installed app (home screen), not in a browser tab. */
export function isStandalone(p: Pick<PlatformInput, 'standalone' | 'displayStandalone'>): boolean {
  return p.standalone === true || p.displayStandalone;
}

/** iOS in a browser tab: pairing here would not carry over to the home-screen app. */
export function isIosBrowserTab(p: PlatformInput): boolean {
  return isIos(p) && !isStandalone(p);
}

/** The live values, read once per call. */
export function currentPlatform(): PlatformInput {
  const nav = navigator as Navigator & { standalone?: boolean };
  return {
    userAgent: nav.userAgent,
    maxTouchPoints: nav.maxTouchPoints ?? 0,
    standalone: nav.standalone,
    displayStandalone: window.matchMedia?.('(display-mode: standalone)').matches ?? false,
  };
}
