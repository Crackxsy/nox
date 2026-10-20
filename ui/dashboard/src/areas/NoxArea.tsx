/**
 * Nox: what was said and what Nox did (Spec UX-Neubau §3) - Gespräch, Ansichten (formerly Board),
 * Clips, Verlauf (formerly Audit, in plain words). Loaded on first visit.
 *
 * The conversation carries the open permission question inline, at the place of the action; the
 * other tabs leave it to the sheet.
 */

import type { ReactNode } from 'react';

import type { Lang, T } from '../i18n';
import type { IpcClient } from '../ipc';
import type { ChatMessage, DashboardState } from '../model';
import { BoardPage } from '../pages/Board';
import { ChatPage } from '../pages/Chat';
import { ClipsPage } from '../pages/Clips';
import { HistoryPage } from '../pages/History';
import { LocalOnly } from '../shell/LocalOnly';
import type { NoxTab, Route } from '../shell/routes';
import { SubTabs } from '../shell/SubTabs';

export interface NoxAreaProps {
  t: T;
  lang: Lang;
  state: DashboardState;
  onState: (updater: (s: DashboardState) => DashboardState) => void;
  client: IpcClient | null;
  device: boolean;
  tab: NoxTab;
  navigate: (route: Route) => void;
  messages: ChatMessage[];
  onMessages: (update: (list: ChatMessage[]) => ChatMessage[]) => void;
  draft: string;
  onDraft: (value: string) => void;
  outbox: { text: string; seq: number } | null;
  stopped: boolean;
  /** The open permission question, shown inline under the conversation. */
  inlinePrompt: ReactNode;
}

export default function NoxArea(props: NoxAreaProps) {
  const { t, lang, state, onState, client, device, tab, navigate } = props;
  return (
    <div className="page">
      <header className="page-h">
        <h1>{t('ux_area_nox')}</h1>
      </header>
      <SubTabs<NoxTab>
        label={t('ux_area_nox')}
        idBase="nox"
        active={tab}
        onSelect={(next) => navigate({ area: 'nox', tab: next })}
        tabs={[
          {
            id: 'talk',
            label: t('ux_tab_talk'),
            render: () => (
              <ChatPage
                t={t}
                lang={lang}
                client={client}
                providers={state.providers}
                messages={props.messages}
                onMessages={props.onMessages}
                draft={props.draft}
                onDraft={props.onDraft}
                outbox={props.outbox}
                stopped={props.stopped}
                inline={props.inlinePrompt}
              />
            ),
          },
          {
            id: 'views',
            label: t('ux_tab_views'),
            render: () => <BoardPage t={t} lang={lang} client={client} revision={state.viewsRevision} />,
          },
          {
            id: 'clips',
            label: t('ux_tab_clips'),
            render: () => (
              <LocalOnly t={t} device={device}>
                <ClipsPage t={t} lang={lang} state={state} client={client} onState={onState} />
              </LocalOnly>
            ),
          },
          {
            id: 'history',
            label: t('ux_tab_history'),
            render: () => <HistoryPage t={t} lang={lang} rows={state.audit} />,
          },
        ]}
      />
    </div>
  );
}
