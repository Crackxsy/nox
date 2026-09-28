/**
 * Chat page: `chat.send` with streamed display, in a 760 px reading column. Stream frames append
 * their delta live, the final response replaces the accumulated text with the authoritative
 * `AiResponse.text` and names the provider. Failures are shown as failures — no placeholder answer
 * is ever invented.
 *
 * Every turn stays an <article> with an <h3> byline (author, provider, degraded marker) and a <p>
 * bubble, so the structure screen readers and the e2e smoke test rely on is unchanged.
 *
 * Announcements: the transcript itself is `aria-live="off"`. A polite status region above it says
 * "Antwort vollständig." once per turn instead — otherwise a screen reader re-reads the whole
 * growing answer on every single delta.
 *
 * The transcript and the composer draft live in `App`, not here: a tab switch unmounts this
 * component, and losing a conversation to a stray click is not an acceptable way to lose it.
 *
 * Above this window's own messages sits the persisted conversation (`chat.history`, across
 * sessions), with its own designed states: a skeleton while it is fetched, a sentence and a retry
 * when it could not be, a note on what is never stored when there is nothing. "Ansicht leeren"
 * clears the screen only - the stored turns stay, and one click shows them again.
 */

import { useEffect, useRef, useState } from 'react';

import { uuid } from '../../../shared/envelope';
import { errorText } from '../../../shared/errors';
import { formatTimestamp } from '../../../shared/format';
import { type Key, type Lang, type T, providerByline } from '../i18n';
import { type Envelope, type IpcClient, api } from '../ipc';
import { type ChatMessage, type Provider, finalAnswer, providerName, streamDelta } from '../model';
import { Hero } from '../ui';
import type { ChatHistory } from './useChatHistory';

export interface ChatPageProps {
  t: T;
  lang: Lang;
  client: IpcClient | null;
  /** For the byline: the provider list is the only place display names exist. */
  providers: Provider[] | null;
  messages: ChatMessage[];
  onMessages: (update: (list: ChatMessage[]) => ChatMessage[]) => void;
  draft: string;
  onDraft: (value: string) => void;
  /** The persisted conversation shown above this window's messages. */
  history: ChatHistory;
}

const EXAMPLES: Key[] = ['chat_example_1', 'chat_example_2', 'chat_example_3'];

export function ChatPage({
  t,
  lang,
  client,
  providers,
  messages,
  onMessages,
  draft,
  onDraft,
  history,
}: ChatPageProps) {
  const [pending, setPending] = useState(false);
  const [announcement, setAnnouncement] = useState('');
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const logRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  // Follow new live messages; an older history page landing on top must not yank the view.
  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, history.status]);

  const patch = (id: string, update: Partial<ChatMessage>) =>
    onMessages((list) => list.map((m) => (m.id === id ? { ...m, ...update } : m)));

  const send = async (text: string) => {
    if (!text || !client || pending) return;
    const answerId = `a-${uuid()}`;
    onMessages((list) => [
      ...list,
      {
        id: `u-${uuid()}`,
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
    onDraft('');
    setPending(true);
    setAnnouncement('');
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
      setAnnouncement(t('chat_answered'));
    } catch (e) {
      patch(answerId, { streaming: false, text: streamed, error: errorText(t('chat_failed'), e) });
      setAnnouncement(t('chat_failed'));
    } finally {
      setPending(false);
      inputRef.current?.focus();
    }
  };

  const disabled = client === null;
  const nothingYet =
    messages.length === 0 && history.messages.length === 0 && history.status !== 'loading';

  const renderMessage = (m: ChatMessage) => (
    <article
      key={m.id}
      className={`msg ${m.role === 'user' ? 'msg--user' : 'msg--nox'}${m.at ? ' msg--history' : ''}`}
    >
      <h3 className="msg-meta">
        {m.role === 'user' ? t('chat_you') : t('chat_nox')}
        {m.at && (
          <span>
            {' '}
            · <time dateTime={m.at}>{formatTimestamp(m.at, lang)}</time>
          </span>
        )}
        {m.streaming && <span> · {t('chat_sending')}</span>}
        {m.provider && (
          <span>
            {' '}
            · {providerByline(t, lang, m.provider, providerName(providers, m.provider))}
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
  );

  return (
    <div className="page wrap">
      <Hero
        title={t('hero_chat')}
        sub={t('hero_chat_sub')}
        links={
          <button
            type="button"
            className="link"
            onClick={() => {
              onMessages(() => []);
              history.hide();
            }}
            disabled={(messages.length === 0 && history.messages.length === 0) || pending}
          >
            {t('chat_clear')}
          </button>
        }
      />

      <div className="chat">
        <p role="status" aria-live="polite" className="sr-only">
          {announcement}
        </p>
        <div
          ref={logRef}
          role="log"
          aria-live="off"
          aria-label={t('chat_title')}
          aria-busy={history.status === 'loading'}
          className="chat-log"
        >
          <HistoryHead t={t} history={history} disabled={disabled} />
          {history.messages.map(renderMessage)}
          {history.messages.length > 0 && messages.length > 0 && (
            <p className="chat-divider">
              <span>{t('chat_history_new')}</span>
            </p>
          )}
          {nothingYet && (
            <div className="chat-empty">
              <p className="muted">{t('chat_empty')}</p>
              <div className="chat-examples">
                {EXAMPLES.map((key) => (
                  <button
                    key={key}
                    type="button"
                    className="chip"
                    disabled={disabled || pending}
                    onClick={() => void send(t(key))}
                  >
                    {t(key)}
                  </button>
                ))}
              </div>
              {history.status === 'ready' && <p className="hint">{t('chat_history_note')}</p>}
            </div>
          )}
          {messages.map(renderMessage)}
        </div>

        <form
          onSubmit={(e) => {
            e.preventDefault();
            void send(draft.trim());
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
              className="textarea textarea--fixed"
              onChange={(e) => onDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
                  e.preventDefault();
                  void send(draft.trim());
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

/**
 * What sits above the stored turns: a skeleton while they are fetched, a sentence and a retry when
 * they could not be, "load older" while there are more, "show again" after the view was cleared.
 */
function HistoryHead({
  t,
  history,
  disabled,
}: {
  t: T;
  history: ChatHistory;
  disabled: boolean;
}) {
  if (history.status === 'loading') {
    return (
      <div className="chat-history-head">
        <span className="sr-only">{t('chat_history_loading')}</span>
        <div aria-hidden="true">
          <div className="skeleton skeleton-row" />
          <div className="skeleton skeleton-row" />
        </div>
      </div>
    );
  }
  if (history.status === 'failed') {
    return (
      <div className="chat-history-head">
        <p className="muted">{t('chat_history_failed')}</p>
        <button type="button" className="link" disabled={disabled} onClick={history.reload}>
          {t('chat_history_retry')}
        </button>
      </div>
    );
  }
  if (history.status === 'hidden') {
    return (
      <div className="chat-history-head">
        <button type="button" className="link" disabled={disabled} onClick={history.reload}>
          {t('chat_history_show')}
        </button>
      </div>
    );
  }
  if (history.status === 'ready' && history.hasMore) {
    return (
      <div className="chat-history-head">
        <button
          type="button"
          className="link"
          disabled={disabled || history.loadingOlder}
          onClick={history.loadOlder}
        >
          {t('chat_history_older')}
        </button>
        {history.olderFailed && <p className="muted">{t('chat_history_failed')}</p>}
      </div>
    );
  }
  return null;
}
