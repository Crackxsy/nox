/**
 * A picture of the creature `pet.variant` currently names, under its select.
 *
 * "Meereswolf" and "Koala-Flughund" are names, and a name is a poor way to choose how something
 * looks. The photographed creatures each ship a still frame next to their rig, served by the core
 * under `/pet/`, so the dashboard can simply show it. The drawn shapes have no picture to show -
 * they exist only as code in the pet window - and get none rather than a placeholder that would
 * pretend otherwise.
 *
 * If the picture cannot load (the dashboard running on its own dev server, say) the frame hides
 * itself. A broken-image icon next to a setting reads as the setting being broken.
 */

import { useEffect, useState } from 'react';

const SPRITE_PREFIX = 'sprite:';

/** Where the core serves a creature's still frame, or null for a shape that has none. */
export function previewUrl(variant: string): string | null {
  if (!variant.startsWith(SPRITE_PREFIX)) return null;
  const id = variant.slice(SPRITE_PREFIX.length);
  // The same rule the pet page applies to an id before it becomes a URL.
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(id) || id.includes('..')) return null;
  return `/pet/variants/${id}/idle.png`;
}

export interface PetPreviewProps {
  variant: string;
  alt: string;
}

export function PetPreview({ variant, alt }: PetPreviewProps) {
  const url = previewUrl(variant);
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [url]);

  if (url === null || failed) return null;
  return (
    <img
      className="pet-preview"
      src={url}
      alt={alt}
      width={96}
      height={96}
      loading="lazy"
      onError={() => setFailed(true)}
    />
  );
}
