/**
 * The chat composer and transcript.
 *
 * What is pinned here: a turn is sent exactly once, the streamed preview is replaced by the
 * authoritative answer, a failure is shown *as* a failure rather than as an empty bubble, the
 * transcript is not a live region during streaming (it used to re-announce the whole growing
 * answer on every delta), and the empty state offers something to press instead of one grey line.
 */

import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { translator } from '../i18n';
import type { Envelope, IpcClient } from '../ipc';
import type { ChatMessage } from '../model';
import { ChatPage } from '../pages/Chat';

const t = translator('de');

function frame(delta: string): Envelope {
  return {
    v: 1,
    id: 'f',
    ts: '',
    kind: 'stream',
    name: 'chat.send',
    corr: 'c',
    src: { role: 'core', id: 'core' },
    payload: { delta, done: false },
  };
}

interface FakeChat {
  answer?: string;
  deltas?: string[];
  fail?: Error;
}

function fakeClient(options: FakeChat = {}) {
  const sent: string[] = [];
  const client = {
    request: (
      name: string,
      _payload: Record<string, unknown>,
      onStream?: (env: Envelope) => void,
    ) => {
      sent.push(name);
      if (name !== 'chat.send') return Promise.resolve({});
      if (options.fail) return Promise.reject(options.fail);
      for (const d of options.deltas ?? []) onStream?.(frame(d));
      return Promise.resolve({
        text: options.answer ?? 'Antwort',
        provider: 'ollama',
        degraded: false,
      });
    },
  } as unknown as IpcClient;
  return { client, sent };
}

function renderChat(options: FakeChat = {}, lang: 'de' | 'en' = 'de') {
  const { client, sent } = fakeClient(options);
  let messages: ChatMessage[] = [];
  let draft = '';
  const view = render(
    <ChatPage
      t={translator(lang)}
      lang={lang}
      client={client}
      providers={[
        {
          id: 'ollama',
          displayName: 'Ollama (llama3.2:3b)',
          local: true,
          roles: [],
          status: 'available',
          reason: '',
        },
      ]}
      messages={messages}
      onMessages={(update) => {
        messages = update(messages);
        view.rerender(tree());
      }}
      draft={draft}
      onDraft={(value) => {
        draft = value;
        view.rerender(tree());
      }}
    />,
  );
  function tree() {
    return (
      <ChatPage
        t={translator(lang)}
        lang={lang}
        client={client}
        providers={[
          {
            id: 'ollama',
            displayName: 'Ollama (llama3.2:3b)',
            local: true,
            roles: [],
            status: 'available',
            reason: '',
          },
        ]}
        messages={messages}
        onMessages={(update) => {
          messages = update(messages);
          view.rerender(tree());
        }}
        draft={draft}
        onDraft={(value) => {
          draft = value;
          view.rerender(tree());
        }}
      />
    );
  }
  return { sent, get messages() {
    return messages;
  } };
}

describe('the chat composer', () => {
  it('will not send an empty message', () => {
    const { sent } = renderChat();
    const send = screen.getByRole('button', { name: t('chat_send') });
    expect(send.hasAttribute('disabled')).toBe(true);
    fireEvent.click(send);
    expect(sent).toHaveLength(0);
  });

  it('sends one chat.send and shows the authoritative answer, not the stream preview', async () => {
    const chat = renderChat({ deltas: ['Ant', 'wo'], answer: 'Antwort von Nox.' });
    fireEvent.change(screen.getByLabelText(t('chat_input_label')), { target: { value: 'Hallo' } });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('chat_send') }));
    });
    expect(chat.sent).toEqual(['chat.send']);
    await waitFor(() => expect(screen.getByText('Antwort von Nox.')).toBeDefined());
    expect(chat.messages.map((m) => m.role)).toEqual(['user', 'nox']);
  });

  it('shows a failure as a failure', async () => {
    renderChat({ fail: new Error('kein Anbieter') });
    fireEvent.change(screen.getByLabelText(t('chat_input_label')), { target: { value: 'Hallo' } });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('chat_send') }));
    });
    await waitFor(() =>
      expect(screen.getByText(`${t('chat_failed')}: kein Anbieter`)).toBeDefined(),
    );
  });

  it('Strg+Enter sends', async () => {
    const { sent } = renderChat();
    const box = screen.getByLabelText(t('chat_input_label'));
    fireEvent.change(box, { target: { value: 'Hallo' } });
    await act(async () => {
      fireEvent.keyDown(box, { key: 'Enter', ctrlKey: true });
    });
    expect(sent).toEqual(['chat.send']);
  });

  it('has no resize grip inside the designed composer', () => {
    renderChat();
    expect(screen.getByLabelText(t('chat_input_label')).className).toContain('textarea--fixed');
  });

  it('is disabled, not hidden, while the core is unreachable', () => {
    render(
      <ChatPage
        t={t}
        lang="de"
        client={null}
        providers={null}
        messages={[]}
        onMessages={() => undefined}
        draft=""
        onDraft={() => undefined}
      />,
    );
    expect(screen.getByLabelText(t('chat_input_label')).hasAttribute('disabled')).toBe(true);
  });
});

describe('the transcript', () => {
  it('does not re-announce itself while an answer streams', () => {
    renderChat();
    expect(screen.getByRole('log').getAttribute('aria-live')).toBe('off');
  });

  it('offers example prompts instead of one grey line when empty', () => {
    const { sent } = renderChat();
    expect(screen.getByText(t('chat_empty'))).toBeDefined();
    const example = screen.getByRole('button', { name: t('chat_example_1') });
    fireEvent.click(example);
    expect(sent).toEqual(['chat.send']);
  });

  it('names the provider the way the Status page does, in German', async () => {
    renderChat({ answer: 'Antwort' });
    fireEvent.change(screen.getByLabelText(t('chat_input_label')), { target: { value: 'Hallo' } });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: t('chat_send') }));
    });
    await waitFor(() =>
      expect(screen.getByText(/Antwort von Ollama \(llama3\.2:3b\)/)).toBeDefined(),
    );
  });

  it('keeps the machine-readable provider id in the English byline', async () => {
    renderChat({ answer: 'Answer' }, 'en');
    const en = translator('en');
    fireEvent.change(screen.getByLabelText(en('chat_input_label')), { target: { value: 'Hi' } });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: en('chat_send') }));
    });
    await waitFor(() => expect(screen.getByText(/Provider: ollama/)).toBeDefined());
  });
});
