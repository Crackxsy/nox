/**
 * The tabs inside an area ("Gespräch · Ansichten · Clips · Verlauf", "Geräte · Fähigkeiten"): one
 * WAI-ARIA tablist with roving focus (arrow keys, Home, End), scrolling sideways on a phone rather
 * than wrapping.
 *
 * Panels a person has opened stay mounted and are only hidden: unmounting them used to throw away a
 * chat draft, an unsaved clip tag or a pairing code on a stray tap.
 */

import { type ReactNode, useRef, useState } from 'react';

export interface SubTab<K extends string> {
  id: K;
  label: string;
  render: () => ReactNode;
}

export interface SubTabsProps<K extends string> {
  label: string;
  idBase: string;
  tabs: readonly SubTab<K>[];
  active: K;
  onSelect: (id: K) => void;
}

export function SubTabs<K extends string>({ label, idBase, tabs, active, onSelect }: SubTabsProps<K>) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const [visited, setVisited] = useState<ReadonlySet<K>>(() => new Set([active]));
  if (!visited.has(active)) setVisited(new Set([...visited, active]));

  const onKey = (e: React.KeyboardEvent, index: number) => {
    const last = tabs.length - 1;
    const map: Record<string, number> = {
      ArrowRight: index === last ? 0 : index + 1,
      ArrowLeft: index === 0 ? last : index - 1,
      Home: 0,
      End: last,
    };
    const next = map[e.key];
    const target = next === undefined ? undefined : tabs[next];
    if (next === undefined || !target) return;
    e.preventDefault();
    onSelect(target.id);
    refs.current[next]?.focus();
  };

  return (
    <>
      <div role="tablist" aria-label={label} className="seg seg--tabs">
        {tabs.map((tab, i) => (
          <button
            key={tab.id}
            ref={(el) => {
              refs.current[i] = el;
            }}
            type="button"
            role="tab"
            id={`${idBase}-tab-${tab.id}`}
            aria-selected={tab.id === active}
            aria-controls={`${idBase}-panel-${tab.id}`}
            tabIndex={tab.id === active ? 0 : -1}
            onClick={() => onSelect(tab.id)}
            onKeyDown={(e) => onKey(e, i)}
          >
            {tab.label}
          </button>
        ))}
      </div>
      {tabs.map((tab) =>
        visited.has(tab.id) ? (
          <div
            key={tab.id}
            role="tabpanel"
            id={`${idBase}-panel-${tab.id}`}
            aria-labelledby={`${idBase}-tab-${tab.id}`}
            hidden={tab.id !== active}
            className="tabpanel"
          >
            {tab.render()}
          </div>
        ) : null,
      )}
    </>
  );
}
