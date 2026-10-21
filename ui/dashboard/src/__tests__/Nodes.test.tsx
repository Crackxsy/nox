/**
 * Geräte › Computer und Server: the node rows, the detail sheet (narrowing only, defaults,
 * removing) and the add-a-computer flow, against a scripted core.
 */

import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { translator } from '../i18n';
import { type IpcClient, IpcError } from '../ipc';
import { type AuditRow, isNodeTarget, parseNodeList, parseNodePairing } from '../model';
import { ComputersSection, NODE_WATCH_MS } from '../pages/Nodes';

const t = translator('de');

const PC = 'a'.repeat(32);
const PI = 'b'.repeat(32);
const OLD = 'c'.repeat(32);

function node(id: string, over: Record<string, unknown> = {}) {
  return {
    id,
    name: 'Arbeits-PC',
    label: `Arbeits-PC (${id.slice(0, 4)})`,
    online: true,
    compatible: true,
    os: 'windows',
    arch: 'AMD64',
    version: '0.7.0',
    paired_at: '2026-10-20T09:00:00+00:00',
    last_seen_at: '2026-10-21T08:00:00+00:00',
    revoked: false,
    tools: ['system.info', 'notify.show'],
    linked_device_id: null,
    groups: [
      { group: 'system', enabled_on_device: true, narrowed: false, active: true },
      { group: 'notify', enabled_on_device: true, narrowed: false, active: true },
      { group: 'shell', enabled_on_device: false, narrowed: false, active: false },
    ],
    ...over,
  };
}

const DEFAULTS = [
  { group: 'system', tools: ['system.info'], device: 'auto', raw: 'auto' },
  { group: 'notify', tools: ['notify.show'], device: PC, raw: PC },
];

function listing(nodes: unknown[] = [node(PC)]) {
  return { enabled: true, nodes, defaults: DEFAULTS };
}

type Answers = Record<string, unknown | ((payload: Record<string, unknown>) => unknown)>;

function core(over: Answers = {}) {
  const calls: { name: string; payload: Record<string, unknown> }[] = [];
  const answers: Answers = {
    'node.list': listing(),
    'security.pin.status': { configured: false },
    'node.narrow': (p: Record<string, unknown>) => ({
      ok: true,
      groups: [
        { group: 'system', enabled_on_device: true, narrowed: false, active: true },
        { group: 'notify', enabled_on_device: true, narrowed: p.narrowed, active: !p.narrowed },
        { group: 'shell', enabled_on_device: false, narrowed: false, active: false },
      ],
    }),
    'node.defaults.set': (p: Record<string, unknown>) => ({
      ok: true,
      defaults: DEFAULTS.map((d) => (d.group === p.group ? { ...d, device: p.device, raw: p.device } : d)),
    }),
    'node.unpair': { ok: true },
    ...over,
  };
  const client = {
    request: vi.fn((name: string, payload: Record<string, unknown>) => {
      calls.push({ name, payload });
      const answer = answers[name];
      if (answer instanceof Error) return Promise.reject(answer);
      return Promise.resolve(typeof answer === 'function' ? answer(payload) : answer);
    }),
  } as unknown as IpcClient;
  return { client, calls, answers };
}

async function show(options: { answers?: Answers; device?: boolean; audit?: AuditRow[] } = {}) {
  const { client, calls, answers } = core(options.answers);
  const history = vi.fn();
  await act(async () => {
    render(
      <ComputersSection
        t={t}
        lang="de"
        client={client}
        device={options.device ?? false}
        revision={0}
        audit={options.audit ?? []}
        onOpenSettings={() => undefined}
        onOpenHistory={history}
      />,
    );
  });
  return { calls, answers, history };
}

async function openSheet(name = 'Arbeits-PC') {
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: `${name} öffnen` }));
  });
  return screen.getByRole('dialog', { name });
}

afterEach(() => {
  vi.useRealTimers();
});

describe('the list', () => {
  it('shows each computer with its state, what Nox may do there and what it is the default for', async () => {
    await show({
      answers: {
        'node.list': listing([
          node(PC),
          node(PI, { name: 'Wohnzimmer-Pi', os: 'linux', arch: 'aarch64', online: false, compatible: null }),
          node(OLD, { name: 'Laptop', compatible: false }),
        ]),
      },
    });
    const rows = screen.getAllByRole('listitem');
    expect(rows).toHaveLength(3);
    const pc = within(rows[0]);
    expect(pc.getByText('Arbeits-PC')).toBeTruthy();
    expect(pc.getByText('Verbunden')).toBeTruthy();
    expect(pc.getByText('Nox darf hier: Systemwerte lesen, Hinweise zeigen.')).toBeTruthy();
    expect(pc.getByText('Standard für: Hinweise zeigen')).toBeTruthy();
    expect(pc.getByText('Version 0.7.0')).toBeTruthy();
    expect(within(rows[1]).getByText(/^Offline seit /)).toBeTruthy();
    expect(within(rows[2]).getByText('Update nötig')).toBeTruthy();
  });

  it('shows nothing at all for a core without nodes', async () => {
    await show({ answers: { 'node.list': new IpcError('not_found', 'unknown request') } });
    expect(screen.queryByRole('heading', { name: t('node_section') })).toBeNull();
  });

  it('says how to start while there is no computer, and when network access is off', async () => {
    await show({ answers: { 'node.list': listing([]) } });
    expect(screen.getByText(t('node_empty'))).toBeTruthy();
    expect(screen.getByRole('button', { name: t('node_add') })).toBeTruthy();
  });

  it('offers no add button where network access is off', async () => {
    await show({ answers: { 'node.list': { enabled: false, nodes: [], defaults: [] } } });
    expect(screen.getByText(t('node_off'))).toBeTruthy();
    expect(screen.queryByRole('button', { name: t('node_add') })).toBeNull();
  });

  it('sends a paired device without a PIN to the computer Nox runs on', async () => {
    await show({ device: true });
    expect(screen.getByText(t('node_only_at_computer'))).toBeTruthy();
  });

  it('refuses a shape it does not know instead of showing an empty list', () => {
    expect(parseNodeList({ enabled: true, nodes: [{ id: 1 }] })).toBeNull();
    expect(parseNodeList({ enabled: true, nodes: [node(PC, { groups: [{ group: 'x' }] })] })).toBeNull();
    expect(parseNodeList({ enabled: true, nodes: [] })?.defaults).toEqual([]);
  });
});

describe('the detail sheet', () => {
  it('turns a group off here, without a PIN', async () => {
    const { calls } = await show();
    const sheet = await openSheet();
    const notify = within(sheet).getByRole('switch', { name: 'Hinweise zeigen' });
    expect(notify.getAttribute('aria-checked')).toBe('true');
    await act(async () => {
      fireEvent.click(notify);
    });
    expect(calls.find((c) => c.name === 'node.narrow')?.payload).toEqual({
      node_id: PC,
      group: 'notify',
      narrowed: true,
    });
    expect(notify.getAttribute('aria-checked')).toBe('false');
    expect(within(sheet).getByText(t('node_group_narrowed'))).toBeTruthy();
  });

  it('never switches on what the device does not allow, and says how to do it there', async () => {
    const { calls } = await show();
    const sheet = await openSheet();
    const shell = within(sheet).getByRole('switch', { name: 'Befehle ausführen' });
    expect(shell.getAttribute('aria-checked')).toBe('false');
    await act(async () => {
      fireEvent.click(shell);
    });
    expect(calls.some((c) => c.name === 'node.narrow')).toBe(false);
    expect(within(sheet).getByText(t('node_enable_title'))).toBeTruthy();
    expect(within(sheet).getByText('nox node allow shell')).toBeTruthy();
  });

  it('asks for the PIN to give a narrowed group back when one is set', async () => {
    const narrowed = node(PC, {
      groups: [{ group: 'notify', enabled_on_device: true, narrowed: true, active: false }],
    });
    const { calls } = await show({
      answers: { 'node.list': listing([narrowed]), 'security.pin.status': { configured: true } },
    });
    const sheet = await openSheet();
    const notify = within(sheet).getByRole('switch', { name: 'Hinweise zeigen' });
    await act(async () => {
      fireEvent.click(notify);
    });
    expect(calls.some((c) => c.name === 'node.narrow')).toBe(false);
    expect(within(sheet).getByRole('alert').textContent).toBe(t('reason_pin_required'));
    fireEvent.change(within(sheet).getByLabelText(t('node_pin')), { target: { value: '2468' } });
    await act(async () => {
      fireEvent.click(notify);
    });
    expect(calls.find((c) => c.name === 'node.narrow')?.payload).toEqual({
      node_id: PC,
      group: 'notify',
      narrowed: false,
      pin: '2468',
    });
  });

  it('says so when the core refuses to widen', async () => {
    const narrowed = node(PC, {
      groups: [{ group: 'notify', enabled_on_device: true, narrowed: true, active: false }],
    });
    await show({
      answers: {
        'node.list': listing([narrowed]),
        'node.narrow': new IpcError('validation.failed', 'only the device', false, { reason: 'enable_on_device' }),
      },
    });
    const sheet = await openSheet();
    await act(async () => {
      fireEvent.click(within(sheet).getByRole('switch', { name: 'Hinweise zeigen' }));
    });
    expect(within(sheet).getByRole('alert').textContent).toBe(t('node_enable_title'));
  });

  it('sets where a family runs when nobody names a device', async () => {
    const { calls } = await show();
    const sheet = await openSheet();
    const select = within(sheet).getByLabelText('Systemwerte lesen: Standardgerät') as HTMLSelectElement;
    expect(select.value).toBe('auto');
    await act(async () => {
      fireEvent.change(select, { target: { value: PC } });
    });
    expect(calls.find((c) => c.name === 'node.defaults.set')?.payload).toEqual({ group: 'system', device: PC });
    expect(within(sheet).getByText(t('node_default_saved'))).toBeTruthy();
    // The row now says it is the default for both families.
    expect(screen.getByText('Standard für: Systemwerte lesen, Hinweise zeigen')).toBeTruthy();
  });

  it('lists what Nox did on this device only', async () => {
    const row = (seq: number, target: string, tool: string): AuditRow => ({
      seq,
      actor: 'companion',
      tool,
      action: 'show',
      target,
      decision: 'allow',
      result: 'ok',
      taskId: null,
      ts: '2026-10-21T08:30:00+00:00',
    });
    const { history } = await show({
      audit: [row(1, `device:${PC} Essen ist fertig`, 'notify'), row(2, `device:${PI} x`, 'shell'), row(3, 'core', 'shell')],
    });
    const sheet = await openSheet();
    const done = within(sheet).getByRole('heading', { name: t('node_done_title') }).closest('section')!;
    expect(within(done as HTMLElement).getAllByRole('listitem')).toHaveLength(1);
    await act(async () => {
      fireEvent.click(within(sheet).getByRole('button', { name: t('node_done_all') }));
    });
    expect(history).toHaveBeenCalledOnce();
    expect(isNodeTarget(`device:${PC}`, PC)).toBe(true);
    expect(isNodeTarget(`device:${PC}x`, PC)).toBe(false);
  });

  it('removes only on the second press', async () => {
    const { calls, answers } = await show();
    const sheet = await openSheet();
    await act(async () => {
      fireEvent.click(within(sheet).getByRole('button', { name: t('lan_remove') }));
    });
    expect(calls.some((c) => c.name === 'node.unpair')).toBe(false);
    expect(within(sheet).getByText(/Nox kann auf Arbeits-PC dann nichts mehr tun/)).toBeTruthy();
    answers['node.list'] = listing([]);
    await act(async () => {
      fireEvent.click(within(sheet).getByRole('button', { name: t('lan_remove_armed') }));
    });
    expect(calls.find((c) => c.name === 'node.unpair')?.payload).toEqual({ device_id: PC });
    expect(screen.getByText('Arbeits-PC ist entfernt.')).toBeTruthy();
    expect(screen.queryByRole('dialog')).toBeNull();
  });
});

describe('adding a computer', () => {
  const PAIRING = {
    code: 'K7F29QAB',
    expires_at: new Date(Date.now() + 300_000).toISOString(),
    ttl_s: 300,
    link: 'nox-node://pair?h=192.168.1.20&p=47843&c=K7F29QAB&fp=' + 'ab'.repeat(32),
    links: [],
    qr: 'data:image/svg+xml;charset=utf-8,%3Csvg%3E',
    ca_fingerprint: '11:22:33:44:55:66:77:88:99:00',
  };

  it('picks the system, makes a code and flips to "connected" when the device arrives', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { calls, answers } = await show({ answers: { 'node.list': listing([]), 'node.pair.start': PAIRING } });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('node_add') }));
    });
    const sheet = screen.getByRole('dialog', { name: t('node_add') });
    fireEvent.click(within(sheet).getByLabelText('Linux'));
    fireEvent.change(within(sheet).getByLabelText(t('lan_pair_name')), { target: { value: 'Laptop' } });
    await act(async () => {
      fireEvent.click(within(sheet).getByRole('button', { name: t('lan_pair_button') }));
    });
    expect(calls.find((c) => c.name === 'node.pair.start')?.payload).toEqual({ name: 'Laptop' });
    expect(within(sheet).getByText('bash install-linux.sh --node')).toBeTruthy();
    expect(within(sheet).getByText(`nox node pair "${PAIRING.link}"`)).toBeTruthy();
    expect(within(sheet).getByRole('img', { name: t('node_add_qr_alt') })).toBeTruthy();
    expect(within(sheet).getByText('11:22:33:44:55:66:77:88')).toBeTruthy();
    expect(within(sheet).getByText(t('node_add_waiting'))).toBeTruthy();

    answers['node.list'] = listing([node(PC, { name: 'Laptop' })]);
    await act(async () => {
      vi.advanceTimersByTime(NODE_WATCH_MS + 10);
    });
    expect(within(sheet).getByText('Laptop ist verbunden.')).toBeTruthy();
  });

  it('accepts only a node pairing link and an inline QR image', () => {
    expect(parseNodePairing({ ...PAIRING, link: 'https://example.invalid/pair' })).toBeNull();
    expect(parseNodePairing({ ...PAIRING, qr: 'https://example.invalid/qr.png' })?.qr).toBe('');
  });
});
