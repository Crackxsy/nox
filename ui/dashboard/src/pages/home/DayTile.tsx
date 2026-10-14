/**
 * "Dein Tag": today's appointments and reminders in one tile at the top of the Zuhause page.
 *
 * Three honest states instead of an empty box: no calendar connected (with the way to connect
 * one), a calendar that could not be loaded just now (in the core's words), and a day with
 * nothing in it. Reminders can be withdrawn here and added here; one added here is spoken
 * exactly like one the model set in conversation.
 */

import { useCallback, useState } from 'react';

import { errorText, failureKind } from '../../../../shared/errors';
import { useRefreshOnConnect } from '../../hooks';
import { type T, fill } from '../../i18n';
import { type IpcClient, api } from '../../ipc';
import {
  type DayReminder,
  type DayToday,
  clockTime,
  nextFullHour,
  parseDayToday,
  shortDate,
} from '../../model';
import { Detail, Tile } from '../../ui';

export interface DayTileProps {
  t: T;
  lang: string;
  client: IpcClient | null;
  onOpenSettings: () => void;
}

export function DayTile({ t, lang, client, onOpenSettings }: DayTileProps) {
  const [day, setDay] = useState<DayToday | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [text, setText] = useState('');
  const [when, setWhen] = useState(() => nextFullHour());
  const [addError, setAddError] = useState('');

  const load = useCallback(
    async (c: IpcClient, cancelled: () => boolean = () => false) => {
      try {
        const parsed = parseDayToday(await api.dayToday(c));
        if (cancelled()) return;
        setDay(parsed);
        setError('');
      } catch (e) {
        if (cancelled()) return;
        // A refusal is already explained by the page; repeating the core's sentence here would
        // show the same line twice. Anything else is named, because nothing else will name it.
        setError(
          failureKind(e) === 'refused' ? t('day_unavailable') : errorText(t('day_unavailable'), e),
        );
      }
    },
    [t],
  );
  useRefreshOnConnect(client, load);

  const cancel = async (id: string) => {
    if (!client || busy) return;
    setBusy(true);
    try {
      await api.dayReminderCancel(client, id);
      await load(client);
    } catch (e) {
      setError(errorText(t('error_prefix'), e));
    } finally {
      setBusy(false);
    }
  };

  const add = async () => {
    const cleaned = text.trim();
    if (!client || busy || !cleaned || !when) return;
    setBusy(true);
    setAddError('');
    try {
      const reply = (await api.dayReminderAdd(client, cleaned, when)) as {
        ok?: boolean;
      };
      if (reply.ok === false) {
        setAddError(t('day_add_past'));
        return;
      }
      setText('');
      await load(client);
    } catch (e) {
      setAddError(errorText(t('error_prefix'), e));
    } finally {
      setBusy(false);
    }
  };

  const failing = day?.calendars.find((feed) => feed.configured && feed.error);

  return (
    <Tile id="day" title={t('day_title')} lede={t('day_hint')}>
      {error ? (
        <p className="muted">{error}</p>
      ) : day === null ? (
        <p className="muted">{t('day_loading')}</p>
      ) : (
        <>
          {!day.configured ? (
            <p className="hint">
              {t('day_no_calendar')}{' '}
              <button type="button" className="link" onClick={onOpenSettings}>
                {t('day_no_calendar_link')}
              </button>
            </p>
          ) : day.events.length === 0 ? (
            <p className="muted">{t('day_empty')}</p>
          ) : (
            <ul className="list day-list">
              {day.events.map((event) => (
                <li key={`${event.source}-${event.start}-${event.title}`}>
                  <span className="day-time">
                    {event.allDay ? t('day_all_day') : clockTime(event.start, lang)}
                  </span>
                  <Detail label={event.title} detail={event.location || null} />
                </li>
              ))}
            </ul>
          )}
          {failing && <p className="hint">{fill(t('day_calendar_error'), failing.error)}</p>}

          <h4 className="day-subtitle">{t('day_reminders_title')}</h4>
          {day.reminders.length === 0 && day.later.length === 0 ? (
            <p className="muted">{t('day_reminders_empty')}</p>
          ) : (
            <ul className="list day-list">
              {day.reminders.map((reminder) => (
                <ReminderRow
                  key={reminder.id}
                  t={t}
                  reminder={reminder}
                  when={clockTime(reminder.dueAt, lang)}
                  busy={busy || !client}
                  onCancel={cancel}
                />
              ))}
              {day.later.map((reminder) => (
                <ReminderRow
                  key={reminder.id}
                  t={t}
                  reminder={reminder}
                  when={`${shortDate(reminder.dueAt, lang)} ${clockTime(reminder.dueAt, lang)}`}
                  busy={busy || !client}
                  onCancel={cancel}
                />
              ))}
            </ul>
          )}

          <div className="field">
            <label htmlFor="day-add-text" className="label">
              {t('day_add_label')}
            </label>
            <input
              id="day-add-text"
              className="input"
              value={text}
              disabled={!client || busy}
              placeholder={t('day_add_placeholder')}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') void add();
              }}
            />
          </div>
          <div className="field">
            <label htmlFor="day-add-when" className="label">
              {t('day_add_when')}
            </label>
            <input
              id="day-add-when"
              className="input"
              type="datetime-local"
              value={when}
              disabled={!client || busy}
              onChange={(e) => setWhen(e.target.value)}
            />
          </div>
          {addError && (
            <p role="alert" className="alert">
              {addError}
            </p>
          )}
          <div className="tile-actions">
            <button
              type="button"
              className="btn"
              disabled={!client || busy || text.trim() === '' || when === ''}
              onClick={() => void add()}
            >
              {t('day_add')}
            </button>
          </div>
        </>
      )}
    </Tile>
  );
}

function ReminderRow({
  t,
  reminder,
  when,
  busy,
  onCancel,
}: {
  t: T;
  reminder: DayReminder;
  when: string;
  busy: boolean;
  onCancel: (id: string) => Promise<void>;
}) {
  return (
    <li className={reminder.fired ? 'day-fired' : undefined}>
      <span className="day-time">{when}</span>
      <Detail label={reminder.text} detail={reminder.fired ? t('day_fired') : null} />
      {!reminder.fired && (
        <button
          type="button"
          className="btn btn--quiet btn--sm"
          disabled={busy}
          onClick={() => void onCancel(reminder.id)}
          aria-label={`${t('day_cancel')}: ${reminder.text}`}
        >
          {t('day_cancel')}
        </button>
      )}
    </li>
  );
}
