# External security review — readiness package

This document is what an external reviewer needs to start immediately, without reverse-engineering
scope or threat model from the code first. **Who performs the review is not decided** — this is a
template/checklist for that reviewer, not the review itself, and not a statement that one has been
arranged.

## 1. What the reviewer gets, up front

- Repository access (public repo once released, or a private invite before then).
- A reproducible install: `README.md` "Development" section, or the signed installer once that
  work is complete.
- This document, plus:
  - `docs/SECURITY.md` (permission model, hard prohibitions, kill switch, Rocket League boundary,
    audit logging, secrets handling, plugin sandboxing, IPC auth — summarized for a reviewer).
  - On request, the full internal Security Model document that `docs/SECURITY.md` summarizes
    (shared with the reviewer under NDA if one is in place).
  - `docs/PRIVACY.md` (what the product claims about capture/storage/egress — useful as a claim to
    verify, not just background).
  - `docs/PLUGIN_AUTHORING.md` (the plugin sandboxing boundary from the author's side).

No additional access request should be needed to begin — if the reviewer hits a wall doing so,
that is itself a finding about this package, not the reviewer's problem to route around.

## 2. Threat model and hard prohibitions (given as input, not to be discovered)

See `docs/SECURITY.md` "Threat model" and "Hard prohibitions" — reproduced there directly from
Security Model §1 and §4 so the reviewer has both the "what we defend against" table and the exact
prohibited-action list (`game.input.send`, `game.memory.read`, `game.process.inject`,
`anticheat.bypass`, `stream.key.read`, `stream.stop`, `recording.delete`,
`security.core.modify_without_pin`, `permission.self_elevate`) without having to grep for it.

## 3. Explicit review scope

The review should cover, at minimum:

1. **Permission engine** (`nox.security.permissions`, Security Model §2) — evaluation order,
   whether hard prohibitions and kill-switch/safe-mode really do short-circuit everything else,
   whether the default-by-risk table is actually enforced where tools are registered.
2. **Rocket League observation-only boundary** (Security Model §10) and its CI-enforced technical
   barrier — the `guard-security-model` job in `.github/workflows/ci.yml` that greps `src/` for
   `SendInput|pydirectinput|pyautogui|ReadProcessMemory|WriteProcessMemory`. Confirm the pattern
   list is actually sufficient (are there other input-synthesis/memory APIs worth adding?) and
   that the job is a required check, not merely present.
3. **Secrets handling** (Security Model §7) — keyring-only storage, naming convention
   (`nox/<component>/<key>`), never logged/serialized/prompted. Cross-check against
   `scripts/secrets_scan.py`'s findings (§5 below) rather than trusting the policy alone.
4. **Kill switch** (Security Model §6) — every trigger path, the SAFE_MODE effect, the
   role-restricted resume path, and the PIN-on-security-path-resume rule. Specifically worth
   attacking: can a plugin, worker, or AI-originated request resume from a kill? (It should be
   rejected outright.)
5. **Plugin sandboxing boundary** (P11, `docs/PLUGIN_AUTHORING.md`) — manifest validation, the
   `<id>.` tool namespace, the `nox/<id>/...` secret namespace, and the two-layer egress check
   (core authorization before spawn, `PluginEgressGuard` inside the worker). Specifically worth
   attacking: can a plugin reach an endpoint outside its declared `network.egress`, or read a
   secret it did not declare?
6. **IPC authentication** — loopback-only, per-client session tokens, per-role request allow-list.
   Specifically worth attacking: an unauthenticated client, and a correctly-authenticated client of
   the *wrong role* attempting a role-restricted request (e.g. `pet` attempting a kill-switch
   resume).

## 4. Security negative-test suite — confirmed green, not left for the reviewer to find

The following negative-path tests must be run and shown green as review *input*, not discovered
missing by the reviewer: a denied tool, a hard-prohibition bypass attempt via config removal,
PRIVATE mode with a cloud provider selected, a capture attempt while a privacy zone is active, a
kill switch triggered during streaming TTS, an audit-chain tamper, a secret appearing in a log
line, an unauthenticated IPC client, and a wrong-role request. Paste the actual `pytest` run
(`tests/unit/security`, plus wherever the kill-switch/audit/IPC-role tests live) into the package
handed to the reviewer — a claim of "these exist" without a run is not sufficient.

## 5. Repo hygiene findings, provided as input

Run immediately before handing off, and include the actual output (not a summary claiming
"clean"):

```
python scripts/license_audit.py --check
python scripts/secrets_scan.py --check
```

As of 2026-09-14: the secrets scan is clean (0 open findings across the working tree and full git
history — 13 findings, all reviewed test fixtures/placeholder literals, documented in
`docs/scan_policy_secrets.yaml`). The license audit currently fails on one real finding
(`piper-tts` declares `GPL-3.0-or-later`) — tell the reviewer this is a known, tracked license
issue, not a secret-handling or security defect, so it doesn't consume review time as if it were
one.

## 6. What the reviewer should attack

Beyond the specific items in §3, a useful review actively tries to break the *guarantees*, not
just read the code that claims them:

- Can any layer above the security core (a plugin, a tool call, a crafted prompt) cause a
  hard-prohibited action to execute, directly or via a confusable near-miss name?
- Does the kill switch actually cut TTS audio within the claimed 200 ms, under load (mid-stream)?
- Does switching to PRIVATE/OFFLINE actually cut every non-allow-listed loopback connection, or
  only the ones the developer thought to test?
- Does the audit hash chain actually detect a tampered entry, not just a missing one?
- Can a plugin manifest smuggle a hard-prohibited tool name past validation (case, unicode
  look-alikes, whitespace)?

## 7. Findings triage

Every finding gets one of two outcomes before release, tracked in writing (not a silent skip):

- **Fixed** — linked commit/PR.
- **Accepted risk** — a written reason and who accepted it.

No finding may remain in neither state at release time — "silently open, pending approval" is not
an acceptable end state for a finding.

## 8. Open points

- **Reviewer identity** — person, paid service, or community volunteer: not decided. This is the
  release checklist's second-highest-priority open item after the license decision
  (`docs/RELEASE_CHECKLIST.md` item 1).
- **Budget/timeline**, if a paid service is chosen — not decided.
- If no reviewer can be arranged in a reasonable window, the "no known critical security
  vulnerabilities" release criterion still applies without external validation — that requires an
  explicit, written risk acceptance, not a silent skip of this whole item.
