/**
 * The editor for one registered program.
 *
 * A program is split into its path and its arguments on purpose: the field for the path is one
 * line and the arguments are one per line, so nothing has to be quoted and a path with spaces
 * cannot be mistaken for two words. That mirrors what actually happens - the command reaches the
 * operating system as a list, with no shell in between.
 */

import { type PresetAction } from '../../model';
import { type T } from '../../i18n';

export interface ActionFormProps {
  t: T;
  action: PresetAction;
  onChange: (action: PresetAction) => void;
}

export function ActionForm({ t, action, onChange }: ActionFormProps) {
  const [program = '', ...args] = action.command;

  const setCommand = (nextProgram: string, nextArgs: string[]) => {
    onChange({ ...action, command: [nextProgram, ...nextArgs] });
  };

  return (
    <div className="settings-group">
      <label className="setting">
        <span className="setting-name">{t('presets_name')}</span>
        <input
          className="input"
          value={action.name}
          onChange={(e) => onChange({ ...action, name: e.target.value })}
          placeholder="DPI 800"
        />
      </label>

      <label className="setting">
        <span className="setting-name">{t('presets_action_program')}</span>
        <input
          className="input"
          value={program}
          onChange={(e) => setCommand(e.target.value, args)}
          placeholder="C:/Program Files/AutoHotkey/AutoHotkey.exe"
          spellCheck={false}
        />
      </label>

      <label className="setting">
        <span className="setting-name">{t('presets_action_args')}</span>
        <textarea
          className="input"
          rows={3}
          value={args.join('\n')}
          onChange={(e) =>
            setCommand(
              program,
              e.target.value.split('\n').filter((line) => line.trim() !== ''),
            )
          }
          spellCheck={false}
        />
      </label>
      <p className="hint">{t('presets_action_args_hint')}</p>

      <label className="setting">
        <span className="setting-name">{t('presets_action_timeout')}</span>
        <input
          className="input"
          type="number"
          min={1}
          max={300}
          value={action.timeout_s}
          onChange={(e) => onChange({ ...action, timeout_s: Number(e.target.value) || 20 })}
        />
      </label>
    </div>
  );
}
