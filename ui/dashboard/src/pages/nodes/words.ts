/**
 * The words the node screens share: a capability group in plain language, a node's state, and
 * where a tool family runs by default.
 */

import { formatTimestamp } from '../../../../shared/format';
import { fill, type Key, type Lang, type T } from '../../i18n';
import { type FamilyDefault, type NodeInfo, nodeState } from '../../model';

const GROUP_KEYS: Record<string, Key> = {
  system: 'node_group_system',
  notify: 'node_group_notify',
  media: 'node_group_media',
  files: 'node_group_files',
  shell: 'node_group_shell',
  packages: 'node_group_packages',
  screen: 'node_group_screen',
  camera: 'node_group_camera',
  browser: 'node_group_browser',
  desktop: 'node_group_desktop',
  computer: 'node_group_computer',
  voice: 'node_group_voice',
  obs: 'node_group_obs',
};

/** "Hinweise zeigen" / "show notices"; an unknown group by its identifier. */
export function groupWords(t: T, group: string): string {
  const key = GROUP_KEYS[group];
  return key ? t(key) : group;
}

/** The same, starting with a capital letter (a label rather than part of a sentence). */
export function groupLabel(t: T, group: string): string {
  const words = groupWords(t, group);
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** "Verbunden" / "Offline seit 14:02" / "Update nötig". */
export function stateWords(t: T, lang: Lang, node: NodeInfo): string {
  switch (nodeState(node)) {
    case 'connected':
      return t('node_state_connected');
    case 'update':
      return t('node_state_update');
    case 'removed':
      return t('lan_state_removed');
    default:
      return node.lastSeenAt
        ? fill(t('node_state_offline_since'), formatTimestamp(node.lastSeenAt, lang))
        : t('node_state_offline');
  }
}

/** The command that enables a group on the device itself (`nox node allow`). */
export function allowCommand(t: T, group: string): string {
  return group === 'files'
    ? `nox node allow files --folder ${t('node_enable_folder')}`
    : `nox node allow ${group}`;
}

/** Where a family runs when nobody says, in words. */
export function defaultWords(t: T, entry: FamilyDefault, nodes: NodeInfo[]): string {
  switch (entry.device) {
    case 'auto':
      return t('node_default_auto');
    case 'core':
      return t('node_default_core');
    case 'origin':
      return t('node_default_origin');
    case 'mixed':
      return t('node_default_mixed');
    default: {
      const node = nodes.find((n) => n.id === entry.device);
      return node ? node.name : fill(t('node_default_unknown'), entry.raw);
    }
  }
}
