/** Loads the cut Eulenfuchs parts (textures, alpha for mesh culling, relief maps). */

export interface PartInfo {
  name: string;
  file: string;
  depth?: string;
  closed?: string;
  rect: [number, number, number, number];
  relief?: number;
}

export interface LoadedPart extends PartInfo {
  image: ImageBitmap;
  alpha: Uint8ClampedArray;
  depthData?: Uint8ClampedArray;
  closedImage?: ImageBitmap;
}

export interface PartSet {
  parts: LoadedPart[];
  eyes: LoadedPart[];
}

async function bitmap(url: string): Promise<ImageBitmap> {
  const blob = await (await fetch(url)).blob();
  return createImageBitmap(blob, { premultiplyAlpha: 'premultiply', colorSpaceConversion: 'none' });
}

function pixels(img: ImageBitmap): Uint8ClampedArray {
  const c = new OffscreenCanvas(img.width, img.height);
  const g = c.getContext('2d')!;
  g.drawImage(img, 0, 0);
  return g.getImageData(0, 0, img.width, img.height).data;
}

export async function loadParts(base: string, withDepth = false): Promise<PartSet> {
  const manifest = (await (await fetch(`${base}/parts.json`)).json()) as { parts: PartInfo[]; eyes: PartInfo[] };
  const load = async (p: PartInfo): Promise<LoadedPart> => {
    const image = await bitmap(`${base}/${p.file}`);
    const out: LoadedPart = { ...p, image, alpha: pixels(image) };
    if (p.closed) out.closedImage = await bitmap(`${base}/${p.closed}`);
    if (withDepth && p.depth) out.depthData = pixels(await bitmap(`${base}/${p.depth}`));
    return out;
  };
  return {
    parts: await Promise.all(manifest.parts.map(load)),
    eyes: await Promise.all(manifest.eyes.map(load)),
  };
}

/** Eye ellipse (ball + dark rim) inside each eye texture (uv), measured from the art. */
export const EYE_SHAPE: Record<string, { c: [number, number]; r: [number, number] }> = {
  eye_l: { c: [0.49, 0.53], r: [0.28, 0.265] },
  eye_r: { c: [0.556, 0.57], r: [0.285, 0.272] },
};
