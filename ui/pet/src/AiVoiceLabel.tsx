/**
 * The "AI voice" label on the stream overlay (`stream.ai_voice_disclosure.overlay_label`).
 *
 * The voice model's license (OpenRAIL-M) asks for synthetic speech to be disclosed, and viewers
 * should know anyway — so the overlay carries a small, quiet label in a corner. The text is the
 * user's own setting, read from the core state (`system.ai_voice_label`); an empty text means no
 * label. Only the OBS overlay shows it: the desktop pet is seen by the user alone, who knows.
 */

export interface AiVoiceLabelProps {
  label: string;
  /** True on the OBS overlay (`?overlay=1`); the desktop pet never renders the label. */
  overlay: boolean;
}

export function AiVoiceLabel({ label, overlay }: AiVoiceLabelProps) {
  const text = label.trim();
  if (!overlay || !text) return null;
  return (
    <span className="pet-ai-voice" data-testid="ai-voice-label">
      {text}
    </span>
  );
}
