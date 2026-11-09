/**
 * PCs, laptops and servers paired as nodes (`nox node`, `node.list`), as plain data.
 *
 * Same rule as `lan.ts`: a shape this page does not recognise is `null`, never an invented
 * default - the page says "could not read Nox's answer" instead of showing an empty list.
 *
 * A capability group has three states the page shows apart: on (the device allows it and the
 * owner left it on), narrowed (the device allows it, the owner turned it off here - it can be
 * turned on again here), off on the device (only the device itself can turn it on).
 */

export type NodeOs = 'windows' | 'linux' | 'macos';

export interface NodeGroup {
  group: string;
  enabledOnDevice: boolean;
  narrowed: boolean;
  active: boolean;
}

export interface NodeInfo {
  id: string;
  /** The plain name typed on the device (or given when pairing). */
  name: string;
  /** The name and a short id, as the model sees it. */
  label: string;
  online: boolean;
  /** `false`: the node speaks an older protocol and needs an update. `null` while offline. */
  compatible: boolean | null;
  os: NodeOs | null;
  arch: string | null;
  version: string | null;
  pairedAt: string;
  lastSeenAt: string | null;
  revoked: boolean;
  tools: string[];
  groups: NodeGroup[];
}

/** One tool family and the device it runs on when nobody says: `auto`, `core`, `origin`, a node
 * id, `mixed` (its tools disagree) or a name no paired node has (`raw`). */
export interface FamilyDefault {
  group: string;
  tools: string[];
  device: string;
  raw: string;
}

export interface NodeList {
  enabled: boolean;
  nodes: NodeInfo[];
  defaults: FamilyDefault[];
}

export type NodeState = 'connected' | 'update' | 'offline' | 'removed';

function str(value: unknown): value is string {
  return typeof value === 'string';
}

function optStr(value: unknown): string | null | undefined {
  if (value === null || value === undefined) return null;
  return str(value) ? value : undefined;
}

function strings(value: unknown): string[] | null {
  return Array.isArray(value) && value.every(str) ? value : null;
}

function parseGroup(value: unknown): NodeGroup | null {
  if (typeof value !== 'object' || value === null) return null;
  const g = value as Record<string, unknown>;
  if (!str(g.group) || typeof g.enabled_on_device !== 'boolean' || typeof g.narrowed !== 'boolean') {
    return null;
  }
  return {
    group: g.group,
    enabledOnDevice: g.enabled_on_device,
    narrowed: g.narrowed,
    active: g.enabled_on_device && !g.narrowed,
  };
}

/** `node.narrow` answers with the groups as they are now. */
export function parseNodeGroups(value: unknown): NodeGroup[] | null {
  if (!Array.isArray(value)) return null;
  const groups = value.map(parseGroup);
  return groups.every((g): g is NodeGroup => g !== null) ? groups : null;
}

const OS: readonly string[] = ['windows', 'linux', 'macos'];

function parseNode(value: unknown): NodeInfo | null {
  if (typeof value !== 'object' || value === null) return null;
  const n = value as Record<string, unknown>;
  const tools = strings(n.tools ?? []);
  // An older core lists nodes without groups: none known, which the page shows as such.
  const groups = n.groups === undefined ? [] : parseNodeGroups(n.groups);
  const lastSeen = optStr(n.last_seen_at);
  const os = optStr(n.os);
  const arch = optStr(n.arch);
  const version = optStr(n.version);
  if (
    !str(n.id) ||
    !str(n.name) ||
    !str(n.paired_at) ||
    typeof n.online !== 'boolean' ||
    typeof n.revoked !== 'boolean' ||
    tools === null ||
    groups === null ||
    lastSeen === undefined ||
    os === undefined ||
    arch === undefined ||
    version === undefined
  ) {
    return null;
  }
  return {
    id: n.id,
    name: n.name,
    label: str(n.label) ? n.label : n.name,
    online: n.online,
    compatible: typeof n.compatible === 'boolean' ? n.compatible : null,
    os: os !== null && OS.includes(os) ? (os as NodeOs) : null,
    arch,
    version,
    pairedAt: n.paired_at,
    lastSeenAt: lastSeen,
    revoked: n.revoked,
    tools,
    groups,
  };
}

function parseDefault(value: unknown): FamilyDefault | null {
  if (typeof value !== 'object' || value === null) return null;
  const d = value as Record<string, unknown>;
  const tools = strings(d.tools);
  if (!str(d.group) || !str(d.device) || tools === null) return null;
  return { group: d.group, tools, device: d.device, raw: str(d.raw) ? d.raw : d.device };
}

/** `node.defaults.set` answers with the families as they are now. */
export function parseFamilyDefaults(value: unknown): FamilyDefault[] | null {
  if (!Array.isArray(value)) return null;
  const out = value.map(parseDefault);
  return out.every((d): d is FamilyDefault => d !== null) ? out : null;
}

export function parseNodeList(payload: unknown): NodeList | null {
  if (typeof payload !== 'object' || payload === null) return null;
  const p = payload as Record<string, unknown>;
  if (typeof p.enabled !== 'boolean' || !Array.isArray(p.nodes)) return null;
  const nodes = p.nodes.map(parseNode);
  if (!nodes.every((n): n is NodeInfo => n !== null)) return null;
  const defaults = p.defaults === undefined ? [] : parseFamilyDefaults(p.defaults);
  if (defaults === null) return null;
  return { enabled: p.enabled, nodes, defaults };
}

export function nodeState(node: NodeInfo): NodeState {
  if (node.revoked) return 'removed';
  if (!node.online) return 'offline';
  return node.compatible === false ? 'update' : 'connected';
}

/** The groups Nox may use there right now, in the core's order. */
export function activeGroups(node: NodeInfo): string[] {
  return node.groups.filter((g) => g.active).map((g) => g.group);
}

/** The families this node is the default device for. */
export function defaultFamilies(node: NodeInfo, defaults: FamilyDefault[]): string[] {
  return defaults.filter((d) => d.device === node.id).map((d) => d.group);
}

/** Which icon fits: a small board (a Raspberry Pi and the like) or a computer. */
export function nodeKind(node: NodeInfo): 'board' | 'computer' {
  const arch = (node.arch ?? '').toLowerCase();
  return node.os === 'linux' && (arch.startsWith('arm') || arch === 'aarch64') ? 'board' : 'computer';
}

/**
 * The calls this device's audit rows stand for: the core writes `device:<id> ...` into the target
 * of every call it routed there (`nox.node.router`).
 */
export function isNodeTarget(target: string, nodeId: string): boolean {
  return target === `device:${nodeId}` || target.startsWith(`device:${nodeId} `);
}

/** `node.pair.start`: a one-time code and the `nox-node://pair?...` link carrying the authority's
 * fingerprint, which the node pins before it sends anything. */
export interface NodePairing {
  code: string;
  expiresAt: string;
  link: string;
  qr: string;
  caFingerprint: string;
}

export function parseNodePairing(payload: unknown): NodePairing | null {
  if (typeof payload !== 'object' || payload === null) return null;
  const p = payload as Record<string, unknown>;
  if (!str(p.code) || !str(p.expires_at) || !str(p.link) || !p.link.startsWith('nox-node://pair?')) {
    return null;
  }
  // The QR code becomes an `<img src>`: only ever an inline image, never a URL to fetch.
  const qr = str(p.qr) && p.qr.startsWith('data:image/') ? p.qr : '';
  return {
    code: p.code,
    expiresAt: p.expires_at,
    link: p.link,
    qr,
    caFingerprint: str(p.ca_fingerprint) ? p.ca_fingerprint : '',
  };
}
