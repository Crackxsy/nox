/**
 * "Was Nox über dich weiß": the facts are listed in the words they were stored in, each can be
 * forgotten, and one typed in by the user is sent as a fact to remember.
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { translator } from '../i18n';
import type { IpcClient } from '../ipc';
import { ProfileTile, parseProfileFacts } from '../pages/settings/ProfileTile';

const t = translator('de');

interface Fact {
  id: number;
  type: string;
  text: string;
  created_at: string;
}

function fakeClient(items: Fact[]) {
  const sent: [string, unknown][] = [];
  const client = {
    request: async (name: string, payload: Record<string, unknown> = {}) => {
      sent.push([name, payload]);
      if (name === 'memory.profile.list') return { items };
      if (name === 'memory.profile.add') {
        items.push({
          id: 99,
          type: 'user',
          text: String(payload.text),
          created_at: '2026-10-14T20:00:00',
        });
        return { ok: true, id: 99 };
      }
      return { ok: true };
    },
  } as unknown as IpcClient;
  return { client, sent };
}

describe('parseProfileFacts', () => {
  it('keeps well-formed facts and drops the rest', () => {
    const facts = parseProfileFacts({
      items: [
        {
          id: 1,
          type: 'relationship',
          text: 'Der beste Freund heißt Tim.',
          created_at: '2026-10-14',
        },
        { id: 'x', text: 'broken' },
        { id: 2, text: '' },
      ],
    });
    expect(facts.map((f) => f.id)).toEqual([1]);
    expect(parseProfileFacts(null)).toEqual([]);
  });
});

describe('ProfileTile', () => {
  it('lists what Nox knows and forgets a fact on request', async () => {
    const { client, sent } = fakeClient([
      {
        id: 7,
        type: 'preference',
        text: 'Streamt Minecraft.',
        created_at: '2026-10-14T20:00:00',
      },
    ]);
    render(<ProfileTile t={t} lang="de" client={client} />);
    await waitFor(() => expect(screen.getByText('Streamt Minecraft.')).toBeTruthy());

    fireEvent.click(
      screen.getByRole('button', {
        name: `${t('profile_forget')}: Streamt Minecraft.`,
      }),
    );
    await waitFor(() => expect(screen.queryByText('Streamt Minecraft.')).toBeNull());
    expect(sent).toContainEqual(['memory.profile.forget', { id: 7 }]);
  });

  it('sends a fact the user types in and shows it afterwards', async () => {
    const { client, sent } = fakeClient([]);
    render(<ProfileTile t={t} lang="de" client={client} />);
    await waitFor(() => expect(screen.getByText(t('profile_empty'))).toBeTruthy());

    fireEvent.change(screen.getByLabelText(t('profile_add_label')), {
      target: { value: 'Mein bester Freund heißt Tim.' },
    });
    fireEvent.click(screen.getByRole('button', { name: t('profile_add') }));
    await waitFor(() => expect(screen.getByText('Mein bester Freund heißt Tim.')).toBeTruthy());
    expect(sent).toContainEqual(['memory.profile.add', { text: 'Mein bester Freund heißt Tim.' }]);
  });
});
