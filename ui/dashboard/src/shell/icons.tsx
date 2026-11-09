/**
 * The few line icons the shell uses, inline (the CSP forbids external assets and an emoji would
 * not match the type). Always decorative: the text next to an icon - or the control's accessible
 * name - says what it means.
 */

const PATHS = {
  today: (
    <>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4" />
    </>
  ),
  nox: <path d="M4 5.5h16v10.5H10l-6 4z" />,
  devices: (
    <>
      <rect x="2.5" y="4.5" width="13" height="10" rx="1.5" />
      <rect x="17.5" y="8" width="4" height="12" rx="1" />
      <path d="M6 19h6M9 14.5V19" />
    </>
  ),
  settings: (
    <>
      <path d="M4 7h9M17 7h3M4 17h3M11 17h9" />
      <circle cx="15" cy="7" r="2" />
      <circle cx="9" cy="17" r="2" />
    </>
  ),
  shield: <path d="M12 3l7 3v5.5c0 4.3-3 7.6-7 9.5-4-1.9-7-5.2-7-9.5V6z" />,
  check: <path d="M5 12.5l4.5 4.5L19 7.5" />,
  chevron: <path d="M9 6l6 6-6 6" />,
  back: <path d="M15 6l-6 6 6 6" />,
  close: <path d="M6 6l12 12M18 6L6 18" />,
  mic: (
    <>
      <rect x="9" y="3" width="6" height="11" rx="3" />
      <path d="M5 11a7 7 0 0 0 14 0M12 18v3" />
    </>
  ),
  camera: (
    <>
      <rect x="3" y="7" width="13" height="10" rx="2" />
      <path d="M16 10.5l5-3v9l-5-3z" />
    </>
  ),
  screen: (
    <>
      <rect x="3" y="4.5" width="18" height="12" rx="1.5" />
      <path d="M8 20h8M12 16.5V20" />
    </>
  ),
  arrow: <path d="M5 12h13M13 6l6 6-6 6" />,
  bell: <path d="M6 16V11a6 6 0 0 1 12 0v5l1.5 2h-15zM10 20.5h4" />,
  search: (
    <>
      <circle cx="11" cy="11" r="6.5" />
      <path d="M16 16l4.5 4.5" />
    </>
  ),
  play: <path d="M8 5.5v13l10.5-6.5z" />,
  bolt: <path d="M13 3L5 13.5h6L10 21l8-10.5h-6z" />,
  info: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 11v5M12 7.5v.5" />
    </>
  ),
  computer: (
    <>
      <rect x="3" y="4.5" width="18" height="11.5" rx="1.5" />
      <path d="M8.5 20h7M12 16v4" />
    </>
  ),
  board: (
    <>
      <rect x="4" y="5" width="16" height="14" rx="1.5" />
      <rect x="9" y="9.5" width="6" height="5" rx="0.5" />
      <path d="M7 2.5V5M12 2.5V5M17 2.5V5M7 19v2.5M12 19v2.5M17 19v2.5" />
    </>
  ),
} as const;

export type IconName = keyof typeof PATHS | 'stop';

export function Icon({ name, size = 20 }: { name: IconName; size?: number }) {
  if (name === 'stop') {
    return (
      <svg className="i i--fill" width={size} height={size} viewBox="0 0 24 24" aria-hidden="true" focusable="false">
        <rect x="5" y="5" width="14" height="14" rx="2" />
      </svg>
    );
  }
  return (
    <svg className="i" width={size} height={size} viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      {PATHS[name]}
    </svg>
  );
}
