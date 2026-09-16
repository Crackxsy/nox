/**
 * Chat page: `chat.send` with streamed display, in a 760 px reading column. Stream frames append
 * their delta live, the final response replaces the accumulated text with the authoritative
 * `AiResponse.text` and names the provider. Failures are shown as failures — no placeholder answer
 * is ever invented.
 *
 * Every turn stays an <article> with an <h3> byline (author, provider, degraded marker) and a <p>
 * bubble, so the structure screen readers and the e2e smoke test rely on is unchanged.
 */

import { useEffect, useRef, useState } from 'react';

import type { T } from '../i18n';
import { type Envelope, type IpcClient, api } from '../ipc';
import { type ChatMessage, finalAnswer, streamDelta } from '../model';
import { Hero } from '../ui';

export interface ChatPageProps {
  t: T;
  client: IpcClient | null;
}

export function ChatPage({ t, client }: ChatPageProps) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [draft, setDraft] = useState('');
  const [pending, setPending] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const logRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages]);

  const patch = (id: string, update: Partial<ChatMessage>) =>
    setMessages((list) => list.map((m) => (m.id === id ? { ...m, ...update } : m)));

  const send = async () => {
    const text = draft.trim();
    if (!text || !client || pending) return;
    const answerId = `a${Date.now()}`;
    setMessages((list) => [
      ...list,
      {
        id: `u${Date.now()}`,
        role: 'user',
        text,
        streaming: false,
        provider: null,
        degraded: false,
        error: null,
      },
      {
        id: answerId,
        role: 'nox',
        text: '',
        streaming: true,
        provider: null,
        degraded: false,
        error: null,
      },
    ]);
    setDraft('');
    setPending(true);
    let streamed = '';
    try {
      const result = await api.chat(client, text, (env: Envelope) => {
        streamed += streamDelta(env.payload);
        patch(answerId, { text: streamed });
      });
      const final = finalAnswer(result);
      patch(answerId, {
        // The final AiResponse wins; the stream is only a preview of it.
        text: final.text ?? streamed,
        streaming: false,
        provider: final.provider,
        degraded: final.degraded,
        error: final.text === null && streamed === '' ? t('chat_failed') : null,
      });
    } catch (e) {
      patch(answerId, {
        streaming: false,
        text: streamed,
        error: `${t('chat_failed')}: ${e instanceof Error ? e.message : String(e)}`,
      });
    } finally {
      setPending(false);
      inputRef.current?.focus();
    }
  };

  const disabled = client === null;

  return (
    <div className="page wrap">
      <Hero
        title={t('hero_chat')}
        sub={t('hero_chat_sub')}
        links={
          <button
            type="button"
            className="link"
            onClick={() => setMessages([])}
            disabled={messages.length === 0 || pending}
          >
            {t('chat_clear')}
          </button>
        }
      />

      <div className="chat">
        <div
          ref={logRef}
          role="log"
          aria-live="polite"
          aria-label={t('chat_title')}
          className="chat-log"
        >
          {messages.length === 0 && <p className="muted">{t('chat_empty')}</p>}
          {messages.map((m) => (
            <article key={m.id} className={`msg ${m.role === 'user' ? 'msg--user' : 'msg--nox'}`}>
              <h3 className="msg-meta">
                {m.role === 'user' ? t('chat_you') : t('chat_nox')}
                {m.streaming && <span> · {t('chat_sending')}</span>}
                {m.provider && (
                  <span>
                    {' '}
                    · {t('chat_provider')}: {m.provider}
                    {m.degraded ? ` (${t('chat_degraded')})` : ''}
                  </span>
                )}
              </h3>
              <p className="msg-text">{m.text}</p>
              {m.error && (
                <p role="alert" className="msg-error">
                  {m.error}
                </p>
              )}
            </article>
          ))}
        </div>

        <form
          onSubmit={(e) => {
            e.preventDefault();
            void send();
          }}
        >
          <label htmlFor="chat-input" className="sr-only">
            {t('chat_input_label')}
          </label>
          <div className="composer">
            <textarea
              id="chat-input"
              ref={inputRef}
              rows={2}
              value={draft}
              disabled={disabled}
              placeholder={t('chat_input_label')}
              aria-describedby="chat-hint"
              aria-keyshortcuts="Control+Enter"
              className="textarea"
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
                  e.preventDefault();
                  void send();
                }
              }}
            />
            <div className="composer-row">
              <span id="chat-hint" className="hint">
                {t('chat_hint')}
              </span>
              <button
                type="submit"
                className="btn"
                disabled={disabled || pending || draft.trim() === ''}
              >
                {pending ? t('chat_sending') : t('chat_send')}
              </button>
            </div>
          </div>
        </form>
      </div>
    </div>
  );
}
