/**
 * The persisted conversation on the Chat page: fetched once when the page connects, shown above
 * this window's messages, with a designed state for loading, failure, nothing stored and "cleared".
 */

import { act, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { translator } from '../i18n';
import type { IpcClient } from '../ipc';
import { type ChatMessage, parseChatHistory } from '../model';
import { ChatPage } from '../pages/Chat';
import { type ChatHistory, useChatHistory } from '../pages/useChatHistory';

const t = translator('de');

function history(overrides: Partial<ChatHistory> = {}): ChatHistory {
  return {
    status: 'ready',
    messages: [],
    hasMore: false,
    loadingOlder: false,
    olderFailed: false,
    loadOlder: vi.fn(),
    reload: vi.fn(),
    hide: vi.fn(),
    ...overrides,
  };
}

const STORED: ChatMessage[] = [
  {
    id: 'h-1',
    role: 'user',
    text: 'Merk dir: Zahnarzt am Dienstag',
    streaming: false,
    provider: null,
    degraded: false,
    error: null,
    at: '2026-09-20T08:00:00Z',
  },
  {
    id: 'h-2',
    role: 'nox',
    text: 'Ist notiert.',
    streaming: false,
    provider: 'rules',
    degraded: false,
    error: null,
    at: '2026-09-20T08:00:01Z',
  },
];

function renderPage(h: ChatHistory, messages: ChatMessage[] = [], onMessages = vi.fn()) {
  return render(
    <ChatPage
      t={t}
      lang="de"
      client={{ request: vi.fn() } as unknown as IpcClient}
      providers={null}
      messages={messages}
      onMessages={onMessages}
      draft=""
      onDraft={() => undefined}
      history={h}
    />,
  );
}

describe('the stored conversation on the Chat page', () => {
  it('shows earlier turns with when they were said, above the live ones', () => {
    const live: ChatMessage = { ...STORED[0], id: 'u-1', text: 'Und heute?', at: undefined };
    renderPage(history({ messages: STORED }), [live]);
    const texts = screen.getAllByRole('article').map((a) => a.querySelector('.msg-text')?.textContent);
    expect(texts).toEqual(['Merk dir: Zahnarzt am Dienstag', 'Ist notiert.', 'Und heute?']);
    expect(screen.getAllByRole('article')[0].querySelector('time')?.getAttribute('dateTime')).toBe(
      '2026-09-20T08:00:00Z',
    );
    expect(screen.getByText(t('chat_history_new'))).toBeDefined();
    expect(screen.queryByText(t('chat_empty'))).toBeNull();
  });

  it('marks the log busy and says so while the history is fetched', () => {
    renderPage(history({ status: 'loading' }));
    expect(screen.getByRole('log').getAttribute('aria-busy')).toBe('true');
    expect(screen.getByText(t('chat_history_loading'))).toBeDefined();
    expect(screen.queryByText(t('chat_empty'))).toBeNull();
  });

  it('says it failed and offers a retry, instead of looking empty', () => {
    const h = history({ status: 'failed' });
    renderPage(h);
    expect(screen.getByText(t('chat_history_failed'))).toBeDefined();
    fireEvent.click(screen.getByRole('button', { name: t('chat_history_retry') }));
    expect(h.reload).toHaveBeenCalledOnce();
  });

  it('with nothing stored, invites a first question and says what is never stored', () => {
    renderPage(history());
    expect(screen.getByText(t('chat_empty'))).toBeDefined();
    expect(screen.getByText(t('chat_history_note'))).toBeDefined();
  });

  it('offers older messages while there are more', () => {
    const h = history({ messages: STORED, hasMore: true });
    renderPage(h);
    fireEvent.click(screen.getByRole('button', { name: t('chat_history_older') }));
    expect(h.loadOlder).toHaveBeenCalledOnce();
  });

  it('"Ansicht leeren" clears the screen, not the stored turns, and one click brings them back', () => {
    const h = history({ messages: STORED });
    const onMessages = vi.fn();
    const { rerender } = renderPage(h, [], onMessages);
    fireEvent.click(screen.getByRole('button', { name: t('chat_clear') }));
    expect(h.hide).toHaveBeenCalledOnce();
    expect(onMessages).toHaveBeenCalledOnce();

    const hidden = history({ status: 'hidden' });
    rerender(
      <ChatPage
        t={t}
        lang="de"
        client={{ request: vi.fn() } as unknown as IpcClient}
        providers={null}
        messages={[]}
        onMessages={onMessages}
        draft=""
        onDraft={() => undefined}
        history={hidden}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: t('chat_history_show') }));
    expect(hidden.reload).toHaveBeenCalledOnce();
  });
});

describe('parseChatHistory', () => {
  const payload = {
    turns: [
      { id: 7, session_id: 'a', ts: '2026-09-20T08:00:00Z', role: 'user', text: 'alt', provider: '' },
      { id: 8, session_id: 'b', ts: '2026-09-28T10:00:05Z', role: 'assistant', text: 'neu', provider: 'rules' },
    ],
    has_more: true,
  };

  it('maps turns to messages, oldest first, with a cursor for the next page', () => {
    const page = parseChatHistory(payload);
    expect(page?.messages.map((m) => [m.id, m.role, m.text, m.provider])).toEqual([
      ['h-7', 'user', 'alt', null],
      ['h-8', 'nox', 'neu', 'rules'],
    ]);
    expect(page?.hasMore).toBe(true);
    expect(page?.oldestId).toBe(7);
  });

  it('leaves out turns said after the page opened: they are already on screen', () => {
    const page = parseChatHistory(payload, '2026-09-28T10:00:00Z');
    expect(page?.messages.map((m) => m.text)).toEqual(['alt']);
    expect(page?.oldestId).toBe(7);
  });

  it('returns null for something that is not a history', () => {
    expect(parseChatHistory({ nope: true })).toBeNull();
    expect(parseChatHistory(null)).toBeNull();
  });
});

describe('useChatHistory', () => {
  function client(pages: Record<string, unknown>[], sent: Record<string, unknown>[]) {
    return {
      request: (name: string, payload: Record<string, unknown>) => {
        sent.push({ name, ...payload });
        const next = pages.shift();
        return next ? Promise.resolve(next) : Promise.reject(new Error('down'));
      },
    } as unknown as IpcClient;
  }

  it('fetches the first page once, then pages backwards from the oldest turn', async () => {
    const sent: Record<string, unknown>[] = [];
    const c = client(
      [
        { turns: [{ id: 5, session_id: 's', ts: '2026-01-01T00:00:00Z', role: 'user', text: 'b' }], has_more: true },
        { turns: [{ id: 2, session_id: 's', ts: '2025-12-31T00:00:00Z', role: 'user', text: 'a' }], has_more: false },
      ],
      sent,
    );
    const { result, rerender } = renderHook(({ cl }) => useChatHistory(cl, 1), {
      initialProps: { cl: c as IpcClient | null },
    });
    await waitFor(() => expect(result.current.status).toBe('ready'));
    rerender({ cl: c });
    expect(sent).toEqual([{ name: 'chat.history', limit: 1 }]);

    act(() => result.current.loadOlder());
    await waitFor(() => expect(result.current.messages.map((m) => m.text)).toEqual(['a', 'b']));
    expect(sent[1]).toEqual({ name: 'chat.history', limit: 1, before: 5 });
    expect(result.current.hasMore).toBe(false);
  });

  it('reports a failed fetch instead of an empty history', async () => {
    const c = client([], []);
    const { result } = renderHook(() => useChatHistory(c));
    await waitFor(() => expect(result.current.status).toBe('failed'));
    expect(result.current.messages).toEqual([]);
  });

  it('asks nothing while offline', () => {
    const { result } = renderHook(() => useChatHistory(null));
    expect(result.current.status).toBe('idle');
  });
});
