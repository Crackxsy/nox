/**
 * The persisted conversation, as the Chat page shows it above this window's own messages.
 *
 * It lives in `App`, like the transcript, so a tab switch does not lose it. The first page is asked
 * for once, when the page first has a connection - not on every reconnect, which would show a turn
 * twice once it had been recorded. Turns said after the page opened are left out of that first page
 * for the same reason: they are already on screen as live messages.
 *
 * States: `idle` (nothing asked yet), `loading`, `ready`, `failed` (with a retry), and `hidden`
 * after "Ansicht leeren" - the history is still on disk, and one click shows it again.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import { type IpcClient, api } from '../ipc';
import { CHAT_HISTORY_PAGE, type ChatMessage, parseChatHistory } from '../model';

export type HistoryStatus = 'idle' | 'loading' | 'ready' | 'failed' | 'hidden';

export interface ChatHistory {
  status: HistoryStatus;
  messages: ChatMessage[];
  hasMore: boolean;
  /** True while an older page is on its way (the first page uses `status: 'loading'`). */
  loadingOlder: boolean;
  /** The last "load older" failed; the button stays so it can be tried again. */
  olderFailed: boolean;
  loadOlder: () => void;
  /** Ask again after a failure, or show the history again after it was hidden. */
  reload: () => void;
  hide: () => void;
}

export function useChatHistory(client: IpcClient | null, pageSize = CHAT_HISTORY_PAGE): ChatHistory {
  const [status, setStatus] = useState<HistoryStatus>('idle');
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [olderFailed, setOlderFailed] = useState(false);
  const oldestId = useRef<number | null>(null);
  /** Turns said from now on are this window's own; see the module comment. */
  const openedAt = useRef(new Date().toISOString());
  /** Incremented per first-page load; a stale answer is dropped. */
  const generation = useRef(0);

  const loadFirst = useCallback(
    async (c: IpcClient, notBefore: string | null) => {
      const mine = ++generation.current;
      setStatus('loading');
      try {
        const page = parseChatHistory(await api.chatHistory(c, pageSize), notBefore);
        if (mine !== generation.current) return;
        if (page === null) {
          setStatus('failed');
          return;
        }
        oldestId.current = page.oldestId;
        setMessages(page.messages);
        setHasMore(page.hasMore);
        setStatus('ready');
      } catch {
        if (mine === generation.current) setStatus('failed');
      }
    },
    [pageSize],
  );

  useEffect(() => {
    if (client && status === 'idle') void loadFirst(client, openedAt.current);
  }, [client, status, loadFirst]);

  const loadOlder = useCallback(() => {
    if (!client || loadingOlder || oldestId.current === null) return;
    const before = oldestId.current;
    setLoadingOlder(true);
    setOlderFailed(false);
    api
      .chatHistory(client, pageSize, before)
      .then((payload) => {
        const page = parseChatHistory(payload);
        if (page === null) {
          setOlderFailed(true);
          return;
        }
        oldestId.current = page.oldestId ?? oldestId.current;
        setMessages((list) => [...page.messages, ...list]);
        setHasMore(page.hasMore);
      })
      .catch(() => setOlderFailed(true))
      .finally(() => setLoadingOlder(false));
  }, [client, loadingOlder, pageSize]);

  const reload = useCallback(() => {
    // After "clear view" the live messages are gone from the screen too, so nothing is cut off.
    if (client) void loadFirst(client, status === 'hidden' ? null : openedAt.current);
  }, [client, loadFirst, status]);

  const hide = useCallback(() => {
    generation.current += 1;
    setMessages([]);
    setHasMore(false);
    setStatus('hidden');
  }, []);

  return { status, messages, hasMore, loadingOlder, olderFailed, loadOlder, reload, hide };
}
