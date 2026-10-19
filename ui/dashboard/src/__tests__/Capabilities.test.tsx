/**
 * "Fähigkeiten": MCP servers, self-taught skills and watchers.
 *
 * What is pinned here:
 *  - a value marked "geheim" goes to `secrets.set` and never into `mcp.servers.add`, which only
 *    carries the `secret:` reference;
 *  - what a server says about its tools is rendered as text — an injected `<img onerror>` stays a
 *    string on screen and never becomes an element;
 *  - a first start needs an explicit "Ersten Start erlauben" next to the exact command and version;
 *  - removing and forgetting take a confirmation;
 *  - activation asks for the PIN only after the core answered `needs_pin`;
 *  - a skill that is being built is polled, and polling stops when it is done.
 */

import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { translator } from '../i18n';
import { type IpcClient, IpcError } from '../ipc';
import {
  BLANK_SERVER_DRAFT,
  addPayload,
  draftProblem,
  needsPinnedVersion,
  parseMcpServers,
  parseSkills,
  parseWatchers,
  splitCommandLine,
} from '../model';
import { CapabilitiesPage } from '../pages/Capabilities';
import { SKILLS_POLL_MS } from '../pages/capabilities/SkillsSection';

const t = translator('de');

type Handler = (payload: Record<string, unknown>) => unknown;

function fakeClient(handlers: Record<string, Handler | unknown>) {
  const sent: [string, Record<string, unknown>][] = [];
  const client = {
    request: async (name: string, payload: Record<string, unknown> = {}) => {
      sent.push([name, payload]);
      const h = handlers[name];
      if (typeof h === 'function') return (h as Handler)(payload);
      if (h === undefined) throw new IpcError('not_found', `unknown request ${name}`, false);
      return h;
    },
  } as unknown as IpcClient;
  return { client, sent, names: () => sent.map(([name]) => name) };
}

const INJECTED = '<img src=x onerror="window.__pwned=1">Liest Dateien';

const SERVER_RUNNING = {
  id: 'files',
  name: 'Dateien',
  transport: 'stdio',
  command: ['npx', '-y', '@mcp/files@1.2.3', 'C:/Daten'],
  url: '',
  version: '1.2.3',
  enabled: true,
  network: false,
  state: 'running',
  reason: '',
  sandboxed: false,
  secrets: [],
  tools: [
    {
      name: 'mcp.files.read',
      remote_name: 'read',
      description: INJECTED,
      read_only: true,
      server_says_read_only: true,
      blocked: '',
    },
    {
      name: '',
      remote_name: 'delete_everything',
      description: '',
      read_only: false,
      server_says_read_only: false,
      blocked: 'name collides with a reserved tool',
    },
  ],
};

const SERVER_PENDING = {
  ...SERVER_RUNNING,
  id: 'wetter',
  name: 'Wetter',
  transport: 'http',
  command: [],
  url: 'https://wetter.example/mcp',
  version: '2.0.0',
  network: true,
  state: 'needs_approval',
  tools: [],
};

const EMPTY_SKILLS = { available: true, reason: '', skills: [] };
const EMPTY_WATCH = { watchers: [] };

function renderPage(handlers: Record<string, Handler | unknown>) {
  const fake = fakeClient({
    'security.pin.status': { configured: false },
    'mcp.servers.list': { sandboxed: true, servers: [] },
    'skills.list': EMPTY_SKILLS,
    'watch.list': EMPTY_WATCH,
    ...handlers,
  });
  render(<CapabilitiesPage t={t} lang="de" client={fake.client} />);
  return fake;
}

afterEach(() => {
  vi.useRealTimers();
});

/* ------------------------------------------------------------- helpers - */

describe('the add-server helpers', () => {
  it('splits a command line on whitespace and keeps quoted parts together', () => {
    expect(splitCommandLine('npx -y  pkg@1.0.0')).toEqual(['npx', '-y', 'pkg@1.0.0']);
    expect(splitCommandLine('"C:\\Program Files\\srv.exe" --root \'C:/Meine Daten\'')).toEqual([
      'C:\\Program Files\\srv.exe',
      '--root',
      'C:/Meine Daten',
    ]);
    expect(splitCommandLine('srv ""')).toEqual(['srv', '']);
    expect(splitCommandLine('srv "open')).toBeNull();
    expect(splitCommandLine('   ')).toEqual([]);
  });

  it('knows which programs need a pinned version', () => {
    expect(needsPinnedVersion(['C:\\nodejs\\npx.cmd', 'x'])).toBe(true);
    expect(needsPinnedVersion(['uvx', 'mcp-server-time'])).toBe(true);
    expect(needsPinnedVersion(['python', '-m', 'srv'])).toBe(false);
    const draft = { ...BLANK_SERVER_DRAFT, id: 'zeit', commandLine: 'uvx mcp-server-time' };
    expect(draftProblem(draft, [])).toBe('caps_add_err_version');
    expect(draftProblem({ ...draft, version: '0.6.2' }, [])).toBeNull();
    expect(draftProblem({ ...draft, version: '0.6.2' }, ['zeit'])).toBe('caps_add_err_id_taken');
    expect(draftProblem({ ...draft, id: '1abc', version: '1' }, [])).toBe('caps_add_err_id');
    expect(draftProblem({ ...draft, kind: 'address', url: 'http://x' }, [])).toBe(
      'caps_add_err_url',
    );
  });

  it('turns a secret row into a reference and leaves the value out of the payload', () => {
    const payload = addPayload({
      ...BLANK_SERVER_DRAFT,
      id: 'gh',
      commandLine: 'gh-mcp',
      rows: [
        { key: 'GITHUB_TOKEN', value: 'ghp_supersecret', secret: true },
        { key: 'LOG', value: 'info', secret: false },
      ],
    });
    expect(payload.env).toEqual({ GITHUB_TOKEN: 'secret:nox/mcp/gh/github_token', LOG: 'info' });
    expect(JSON.stringify(payload)).not.toContain('ghp_supersecret');
  });

  it('parses malformed answers into empty lists instead of crashing', () => {
    expect(parseMcpServers('x')).toEqual({ sandboxed: false, servers: [] });
    expect(parseSkills(null).skills).toEqual([]);
    expect(parseWatchers({ watchers: [{ nope: 1 }] })).toEqual([]);
  });
});

/* ---------------------------------------------------------------- MCP - */

describe('MCP servers', () => {
  it('renders a tool description from the server as plain, labelled text', async () => {
    renderPage({ 'mcp.servers.list': { sandboxed: false, servers: [SERVER_RUNNING] } });
    const card = await screen.findByRole('region', { name: 'Dateien' });
    expect(within(card).getByText(INJECTED)).toBeTruthy();
    expect(card.querySelector('img')).toBeNull();
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
    expect(within(card).getByText(t('caps_mcp_untrusted'))).toBeTruthy();
  });

  it('shows a refused tool greyed out with its reason, and the missing sandbox honestly', async () => {
    renderPage({ 'mcp.servers.list': { sandboxed: false, servers: [SERVER_RUNNING] } });
    const card = await screen.findByRole('region', { name: 'Dateien' });
    const blocked = within(card).getByText('delete_everything').closest('li');
    expect(blocked?.className).toContain('caps-tool--blocked');
    expect(blocked?.textContent).toContain('name collides with a reserved tool');
    expect(screen.getByText(t('caps_mcp_not_sandboxed'))).toBeTruthy();
    expect(within(card).getByText(t('caps_mcp_badge_not_sandboxed'))).toBeTruthy();
    expect(within(card).getByText(t('caps_mcp_state_running'))).toBeTruthy();
  });

  it('asks for the first start next to the exact address and version', async () => {
    let state = 'needs_approval';
    const { sent } = renderPage({
      'mcp.servers.list': () => ({
        sandboxed: true,
        servers: [{ ...SERVER_PENDING, state }],
      }),
      'mcp.servers.approve': () => {
        state = 'running';
        return { ok: true, state: 'running', reason: '' };
      },
    });
    const card = await screen.findByRole('region', { name: 'Wetter' });
    const pre = card.querySelector('pre');
    expect(pre?.textContent).toContain('https://wetter.example/mcp');
    expect(pre?.textContent).toContain('2.0.0');
    fireEvent.click(within(card).getByRole('button', { name: t('caps_mcp_approve') }));
    await waitFor(() => expect(within(card).getByText(t('caps_mcp_state_running'))).toBeTruthy());
    expect(sent).toContainEqual(['mcp.servers.approve', { id: 'wetter' }]);
  });

  it('removes a server only after the confirmation', async () => {
    const { names, sent } = renderPage({
      'mcp.servers.list': { sandboxed: true, servers: [SERVER_RUNNING] },
      'mcp.servers.remove': { ok: true },
    });
    const card = await screen.findByRole('region', { name: 'Dateien' });
    fireEvent.click(within(card).getByRole('button', { name: t('caps_mcp_remove') }));
    expect(names()).not.toContain('mcp.servers.remove');
    const confirm = within(card).getByRole('button', { name: t('caps_mcp_remove_yes') });
    expect(document.activeElement).toBe(confirm);
    fireEvent.click(confirm);
    await waitFor(() => expect(sent).toContainEqual(['mcp.servers.remove', { id: 'files' }]));
  });

  it('switches a server off', async () => {
    const { sent } = renderPage({
      'mcp.servers.list': { sandboxed: true, servers: [SERVER_RUNNING] },
      'mcp.servers.enable': { ok: true, state: 'disabled' },
    });
    const card = await screen.findByRole('region', { name: 'Dateien' });
    fireEvent.click(within(card).getByRole('button', { name: t('caps_mcp_disable') }));
    await waitFor(() =>
      expect(sent).toContainEqual(['mcp.servers.enable', { id: 'files', enabled: false }]),
    );
  });
});

describe('adding an MCP server', () => {
  async function openForm(handlers: Record<string, Handler | unknown> = {}) {
    const fake = renderPage(handlers);
    fireEvent.click(await screen.findByRole('button', { name: t('caps_add_open') }));
    return fake;
  }

  function fill(label: string, value: string) {
    fireEvent.change(screen.getByLabelText(label), { target: { value } });
  }

  it('stores a secret first and sends only its reference', async () => {
    const { sent } = await openForm({
      'security.pin.status': { configured: true },
      'secrets.set': { ok: true },
      'mcp.servers.add': { ok: true, state: 'needs_approval' },
    });
    fill(t('caps_add_name'), 'GitHub');
    fill(t('caps_add_id'), 'github');
    fill(t('caps_add_command'), 'npx -y @mcp/github@0.6.2');
    fill(t('caps_add_version'), '0.6.2');
    fireEvent.click(screen.getByRole('button', { name: t('caps_add_row_add') }));
    fill(`${t('caps_add_key')} (Zeile 1)`, 'GITHUB_TOKEN');
    fill(`${t('caps_add_value')} (Zeile 1)`, 'ghp_supersecret');
    fireEvent.click(screen.getByLabelText(`${t('caps_add_secret')} (Zeile 1)`));
    fill(t('pin_label'), '4711');
    fireEvent.click(screen.getByRole('button', { name: t('caps_add_submit') }));

    await waitFor(() => expect(sent.map(([n]) => n)).toContain('mcp.servers.add'));
    expect(sent).toContainEqual([
      'secrets.set',
      { name: 'nox/mcp/github/github_token', value: 'ghp_supersecret', pin: '4711' },
    ]);
    const add = sent.find(([name]) => name === 'mcp.servers.add')?.[1];
    expect(add).toMatchObject({
      id: 'github',
      name: 'GitHub',
      command: ['npx', '-y', '@mcp/github@0.6.2'],
      version: '0.6.2',
      env: { GITHUB_TOKEN: 'secret:nox/mcp/github/github_token' },
    });
    expect(JSON.stringify(add)).not.toContain('ghp_supersecret');
    expect(JSON.stringify(add)).not.toContain('4711');
    await waitFor(() => expect(screen.getByText('„github“ ist eingetragen.')).toBeTruthy());
  });

  it('adds nothing when the secret could not be stored', async () => {
    const { names } = await openForm({
      'secrets.set': () => {
        throw new IpcError('permission.denied', 'PIN required', false);
      },
      'mcp.servers.add': { ok: true },
    });
    fireEvent.click(screen.getByLabelText(t('caps_add_kind_address')));
    fill(t('caps_add_id'), 'remote');
    fill(t('caps_add_url'), 'https://mcp.example');
    fireEvent.click(screen.getByRole('button', { name: t('caps_add_row_add') }));
    fill(`${t('caps_add_key')} (Zeile 1)`, 'Authorization');
    fill(`${t('caps_add_value')} (Zeile 1)`, 'Bearer abc');
    fireEvent.click(screen.getByLabelText(`${t('caps_add_secret')} (Zeile 1)`));
    fireEvent.click(screen.getByRole('button', { name: t('caps_add_submit') }));
    await waitFor(() => expect(screen.getByText(t('pin_required'))).toBeTruthy());
    expect(names()).not.toContain('mcp.servers.add');
    // The PIN the core asked for can now be typed.
    expect(screen.getByLabelText(t('pin_label'))).toBeTruthy();
  });

  it('refuses npx without a pinned version and sends nothing', async () => {
    const { names } = await openForm({ 'mcp.servers.add': { ok: true } });
    fill(t('caps_add_id'), 'files');
    fill(t('caps_add_command'), 'npx -y @mcp/files');
    fireEvent.click(screen.getByRole('button', { name: t('caps_add_submit') }));
    expect(await screen.findByText(t('caps_add_err_version'))).toBeTruthy();
    expect(names()).not.toContain('mcp.servers.add');
  });

  it('shows the core’s refusal verbatim', async () => {
    await openForm({
      'mcp.servers.add': { ok: false, reason: 'command: a server started through npx needs its pinned version set' },
    });
    fill(t('caps_add_id'), 'files');
    fill(t('caps_add_command'), 'node server.js');
    fireEvent.click(screen.getByRole('button', { name: t('caps_add_submit') }));
    const alert = await screen.findByText(/needs its pinned version set/);
    expect(alert.textContent).toBe(
      'Abgelehnt: command: a server started through npx needs its pinned version set',
    );
  });

  it('shows how the command line will be split', async () => {
    await openForm();
    fill(t('caps_add_command'), '"C:/Program Files/srv.exe" --flag');
    const argv = screen.getByRole('list', { name: t('caps_add_argv') });
    expect(within(argv).getAllByRole('listitem').map((li) => li.textContent)).toEqual([
      'C:/Program Files/srv.exe',
      '--flag',
    ]);
  });
});

/* ------------------------------------------------------------- skills - */

const READY = {
  id: 'bahn',
  intent: 'Abfahrten am Bahnhof Witten ansagen',
  state: 'ready',
  created_at: '2026-10-19T08:00:00+00:00',
  updated_at: '2026-10-19T08:05:00+00:00',
  version: 1,
  rounds: 2,
  problems: [],
  message: '',
  approved_hosts: [],
  summary: {
    tools: [{ name: 'bahn.departures', risk: 'low', description: '<b>fett</b> Abfahrten' }],
    hosts: ['v6.db.transport.rest'],
    new_hosts: ['v6.db.transport.rest'],
    secrets: [],
    secrets_present: {},
    tests_passed: 4,
    tests_failed: 0,
    test_output: '4 passed',
    needs_pin: true,
  },
};

describe('self-taught skills', () => {
  it('says honestly why learning is not possible', async () => {
    renderPage({
      'skills.list': { available: false, reason: 'Claude Code is not installed', skills: [] },
    });
    expect(
      await screen.findByText('Lernen ist gerade nicht möglich: Claude Code is not installed'),
    ).toBeTruthy();
  });

  it('shows what activating allows, with the new host marked', async () => {
    renderPage({ 'skills.list': { available: true, reason: '', skills: [READY] } });
    const card = await screen.findByRole('region', { name: READY.intent });
    expect(within(card).getByText('v6.db.transport.rest')).toBeTruthy();
    expect(within(card).getByText(t('caps_skill_host_new'))).toBeTruthy();
    expect(within(card).getByText(t('caps_risk_low'))).toBeTruthy();
    expect(within(card).getByText('<b>fett</b> Abfahrten')).toBeTruthy();
    expect(card.querySelector('b')).toBeNull();
    expect(within(card).getByText('4 bestanden, 0 fehlgeschlagen')).toBeTruthy();
  });

  it('asks for the PIN only after the core said it needs one', async () => {
    let state = 'ready';
    const { sent } = renderPage({
      'security.pin.status': { configured: true },
      'skills.list': () => ({ available: true, reason: '', skills: [{ ...READY, state }] }),
      'skills.activate': (p: Record<string, unknown>) => {
        if (p.pin !== '1234') return { ok: false, needs_pin: true, reason: 'PIN required' };
        state = 'active';
        return { ok: true, state: 'running' };
      },
    });
    const card = await screen.findByRole('region', { name: READY.intent });
    expect(within(card).queryByLabelText(t('pin_label'))).toBeNull();

    fireEvent.click(within(card).getByRole('button', { name: t('caps_skill_activate') }));
    await waitFor(() => expect(within(card).getByLabelText(t('pin_label'))).toBeTruthy());
    expect(sent).toContainEqual(['skills.activate', { id: 'bahn' }]);
    expect(within(card).getByText('PIN required')).toBeTruthy();

    fireEvent.change(within(card).getByLabelText(t('pin_label')), { target: { value: '1234' } });
    fireEvent.click(within(card).getByRole('button', { name: t('caps_skill_activate') }));
    await waitFor(() =>
      expect(within(card).getByRole('button', { name: t('caps_skill_deactivate') })).toBeTruthy(),
    );
    expect(sent).toContainEqual(['skills.activate', { id: 'bahn', pin: '1234' }]);
  });

  it('activates without a PIN when the core does not ask for one', async () => {
    const { sent } = renderPage({
      'skills.list': { available: true, reason: '', skills: [READY] },
      'skills.activate': { ok: true, state: 'running' },
    });
    const card = await screen.findByRole('region', { name: READY.intent });
    fireEvent.click(within(card).getByRole('button', { name: t('caps_skill_activate') }));
    await waitFor(() => expect(sent).toContainEqual(['skills.activate', { id: 'bahn' }]));
    expect(within(card).queryByLabelText(t('pin_label'))).toBeNull();
  });

  it('polls while a skill is being built and stops when it is done', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const replies = [
      { ...READY, state: 'building', rounds: 1, summary: null },
      { ...READY, state: 'building', rounds: 2, summary: null },
      READY,
    ];
    const { names } = renderPage({
      'skills.list': () => ({
        available: true,
        reason: '',
        skills: [replies.length > 1 ? replies.shift() : replies[0]],
      }),
    });
    expect(await screen.findByText('Nox lernt das gerade – Runde 1.')).toBeTruthy();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(SKILLS_POLL_MS);
    });
    expect(await screen.findByText('Nox lernt das gerade – Runde 2.')).toBeTruthy();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(SKILLS_POLL_MS);
    });
    await waitFor(() =>
      expect(screen.getByRole('button', { name: t('caps_skill_activate') })).toBeTruthy(),
    );
    const calls = names().filter((n) => n === 'skills.list').length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(SKILLS_POLL_MS * 3);
    });
    expect(names().filter((n) => n === 'skills.list').length).toBe(calls);
  });

  it('lists what went wrong for a failed skill', async () => {
    renderPage({
      'skills.list': {
        available: true,
        reason: '',
        skills: [
          {
            ...READY,
            state: 'failed',
            summary: null,
            message: 'gave up after 3 rounds',
            problems: ['tests failed: 2', 'manifest: unknown host'],
          },
        ],
      },
    });
    expect(await screen.findByText('tests failed: 2')).toBeTruthy();
    expect(screen.getByText('manifest: unknown host')).toBeTruthy();
    expect(screen.getByText('gave up after 3 rounds')).toBeTruthy();
  });

  it('stores a missing secret through secrets.set', async () => {
    const withSecret = {
      ...READY,
      summary: {
        ...READY.summary,
        secrets: ['nox/skill/bahn/api_key'],
        secrets_present: { 'nox/skill/bahn/api_key': false },
      },
    };
    const { sent } = renderPage({
      'skills.list': { available: true, reason: '', skills: [withSecret] },
      'secrets.set': { ok: true },
    });
    const field = await screen.findByLabelText('Wert für nox/skill/bahn/api_key');
    expect(field.getAttribute('type')).toBe('password');
    fireEvent.change(field, { target: { value: 'k-123' } });
    fireEvent.click(screen.getByRole('button', { name: t('caps_skill_secret_save') }));
    await waitFor(() =>
      expect(sent).toContainEqual(['secrets.set', { name: 'nox/skill/bahn/api_key', value: 'k-123' }]),
    );
  });

  it('forgets a skill only after the confirmation, and deactivates an active one', async () => {
    const { sent, names } = renderPage({
      'skills.list': { available: true, reason: '', skills: [{ ...READY, state: 'active' }] },
      'skills.forget': { ok: true },
      'skills.deactivate': { ok: true },
    });
    const card = await screen.findByRole('region', { name: READY.intent });
    fireEvent.click(within(card).getByRole('button', { name: t('caps_skill_deactivate') }));
    await waitFor(() => expect(sent).toContainEqual(['skills.deactivate', { id: 'bahn' }]));

    fireEvent.click(within(card).getByRole('button', { name: t('caps_skill_forget') }));
    expect(names()).not.toContain('skills.forget');
    fireEvent.click(within(card).getByRole('button', { name: t('caps_skill_forget_yes') }));
    await waitFor(() => expect(sent).toContainEqual(['skills.forget', { id: 'bahn' }]));
  });
});

/* ------------------------------------------------------------ watchers - */

const WATCHER = {
  id: 'w1',
  title: 'RB 40 verspätet',
  tool: 'web.fetch',
  args: { url: 'https://example.org' },
  condition: { path: '', near: 'RB 40', op: 'contains', value: 'Min' },
  interval_min: 15,
  cooldown_min: 60,
  created_at: '2026-10-19T07:00',
  last_checked_at: '2026-10-19T07:45',
  last_value: '+5 Min',
  last_fired_at: null,
  last_error: 'timeout after 20 s',
  checks: 3,
  fired: 0,
};

describe('watchers', () => {
  it('reads each watcher as a sentence with what it last saw', async () => {
    renderPage({ 'watch.list': { watchers: [WATCHER] } });
    expect(
      await screen.findByText('prüft web.fetch alle 15 min: enthält „Min“ nach „RB 40“'),
    ).toBeTruthy();
    expect(screen.getByText('+5 Min')).toBeTruthy();
    expect(screen.getByText('Letzter Fehler: timeout after 20 s')).toBeTruthy();
    expect(screen.getByText('3 Prüfungen, 0 Meldungen')).toBeTruthy();
  });

  it('deletes a watcher', async () => {
    let watchers = [WATCHER];
    const { sent } = renderPage({
      'watch.list': () => ({ watchers }),
      'watch.delete': () => {
        watchers = [];
        return { ok: true };
      },
    });
    fireEvent.click(await screen.findByRole('button', { name: t('caps_watch_delete') }));
    await waitFor(() => expect(screen.getByText(t('caps_watch_empty'))).toBeTruthy());
    expect(sent).toContainEqual(['watch.delete', { id: 'w1' }]);
  });

  it('explains in the empty state that watchers are made by voice or chat', async () => {
    renderPage({});
    expect(await screen.findByText(t('caps_watch_empty_hint'))).toBeTruthy();
  });

  it('says so when the core has no watcher extension', async () => {
    renderPage({ 'watch.list': undefined });
    expect(await screen.findByText(t('caps_not_installed'))).toBeTruthy();
  });
});
