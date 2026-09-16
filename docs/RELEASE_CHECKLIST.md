# v1.0 public release checklist

Checkable form of the v1.0 public-release acceptance criteria (EPIC-21). Check an item only once
its "Evidence" actually exists and has been looked at — not because the related code merged. The
license decision itself (OP-D) was the product owner's call and is now closed; everything else here
is process this repository's tooling can verify.

- [x] **1. License decided and applied** — **OP-D closed 2026-09-16: Apache-2.0 for the code; the
      voice extra carries a GPL notice; a Kokoro/ONNX replacement for Piper is tracked for wave 2.**
      `LICENSE` (full Apache-2.0 text) and `NOTICE` present at repo root, `pyproject.toml` and both
      `ui/*/package.json` declare `Apache-2.0`, `README.md`'s license section matches.
      _Evidence: `LICENSE`, `NOTICE`, README license section._
- [x] **2. `LICENSE-PENDING.md` removed or superseded** — deleted 2026-09-16; no remaining
      "pending" license reference anywhere in the repo.
      _Evidence: diff/commit, `grep -ri "license.pending" .` empty._
- [ ] **3. Third-party license audit complete** — `docs/THIRD_PARTY_LICENSES.md` present, generated
      by `scripts/license_audit.py` against the actual venv and both `ui/*/package-lock.json`
      files, not hand-written. _Evidence: the file itself, `python scripts/license_audit.py`
      output._
- [ ] **4. No dependency/model license blocks the chosen project license or redistribution** —
      `python scripts/license_audit.py --check` exits 0 against `docs/license_policy.yaml`.
      **Resolved 2026-09-16** by a written PO exception in `docs/license_policy.yaml`: `piper-tts`
      (the `voice` extra) declares `GPL-3.0-or-later`; Apache-2.0 is one-way compatible with
      GPL-3.0, so a build bundling the voice extra ships under GPL-3.0 terms as a combined work
      (stated in `NOTICE`), while the source distribution stays Apache-2.0. A permissively licensed
      TTS engine (Kokoro/ONNX) is tracked for wave 2.
      _Evidence: `docs/THIRD_PARTY_LICENSES.md` "DENIED" section is empty; policy sign-off note._
- [ ] **5. Onboarding wizard covers all seven FR-15.1 items, skippable/re-runnable** — name,
      language, data/vault paths, optional Twitch, mic/camera consent, AI backend, each declinable
      with a safe default and re-runnable via `nox onboard`. _Evidence: manual walkthrough,
      `tests/unit/onboarding` green._
- [ ] **6. Onboarding wizard writes only to the User config layer** —
      `tests/unit/onboarding/test_wizard.py::test_defaults_yaml_is_never_touched_by_any_helper`
      (and the CLI-level equivalents) green. _Evidence: that test run._
- [ ] **7. Clean install + onboarding on a machine that is not the dev machine** — not yet
      performed as of this writing; needs a non-dev Windows 11 machine or VM.
      _Evidence: test log, screenshots._
- [ ] **8. Update, smoke test, rollback proven under injected failure** — ST-21-04, not yet
      performed. _Evidence: integration test log._
- [ ] **9. Uninstall separates program/data; vault never deleted silently** — ST-21-04, not yet
      performed. _Evidence: manual test log._
- [ ] **10. Documentation set published** — `docs/USER_GUIDE.md`, `docs/PRIVACY.md`,
      `docs/SECURITY.md`, `docs/PLUGIN_AUTHORING.md` all present and linked from `README.md`
      (links added 2026-09-16, verified by `scripts/check_links.py`).
      _Evidence: doc links, `python scripts/check_links.py`._
- [ ] **11. External security review scope delivered; findings triaged** — needs a reviewer
      identified first (open point, see `docs/EXTERNAL_SECURITY_REVIEW.md`). _Evidence: review
      report + triage log._
- [ ] **12. Security Model §11 negative-test suite green** — run as part of release CI, not just ad
      hoc. _Evidence: CI run link._
- [ ] **13. Secrets scan clean on tree and full history** —
      `python scripts/secrets_scan.py --check` exits 0. **Verified 2026-09-14: 0 open findings**
      (13 findings, all reviewed test fixtures/constants, documented in
      `docs/scan_policy_secrets.yaml`). _Evidence: scan output, CI `release-hygiene` job._
- [ ] **14. Dependency vulnerability scan: no unaddressed critical/high** — not yet wired in (only
      the license audit and secrets scan are CI gates as of this pass); add `pip-audit`/`npm audit`
      as a follow-up. _Evidence: scan report._
- [ ] **15. CI green on the public repo configuration, no secret leakage via logs** — the
      `release-hygiene` job (license audit + secrets scan) is wired into
      `.github/workflows/ci.yml`; confirm it is green (see item 4's known red state) and that no
      step echoes a secret value. _Evidence: CI run, log review._
- [ ] **16. `CHANGELOG.md` present, versioning process documented** — done this pass
      (`CHANGELOG.md` at repo root, Keep-a-Changelog style, v0.1.0/v0.2.0 backfilled from git log).
      Its compare links now use the tag-based form (`.../compare/vX...vY`,
      `.../releases/tag/vX`) the publication runbook (item 20) is expected to create tags for,
      not commit hashes from this pre-publication history, which would 404 on the public
      repository (#29) — confirm `v0.1.0`/`v0.2.0` are actually tagged as part of item 20 before
      the repo goes public, so those links resolve. _Evidence: the file itself; the tags existing
      on the published repository once item 20 runs._
- [x] **17. Support policy published** — see `README.md` "Support" section and the note below.
      _Evidence: doc link._
- [ ] **18. No known critical security vulnerability remains** — ties to items 11/12; cannot be
      checked until the external review (or its explicit PO-accepted deferral) lands.
- [ ] **19. No secrets, personal data, or vault contents in the published repo or its history** —
      ties to item 13; **verified 2026-09-14 for the current history** (0 open findings) and the
      working tree was scrubbed of personal data on 2026-09-16. The published *history* is handled
      by the squashed-snapshot route in `PUBLISHING.md`; re-run both checks immediately before the
      repo actually goes public, since new commits land between now and then.
- [ ] **20. Publication runbook executed** — `docs/PUBLISHING.md` end to end: archive rename, new
      public repository, squashed snapshot with a noreply author, repository security settings,
      branch protection via `scripts/github_branch_protection.ps1`, CI green, archive deleted.
      _Evidence: the new repository, a green CI run, `gh repo list` without the archive._

## Support policy (per Spec v1.0 §10; PO-approved tone)

Nox is a solo/small-project open-source release. Support channel: GitHub Issues. There is no
guaranteed response time and no paid support tier — issues are triaged as time allows, security
reports get priority (see `../SECURITY.md`). Nox is provided as-is, under the warranty disclaimer
of the Apache License 2.0 (§7/§8) — no promise beyond that.

## Open points this checklist cannot close on its own

- Who performs the external security review (item 11) — not decided.
- Replacing Piper with a permissively licensed TTS engine so bundled builds can ship under
  Apache-2.0 alone (wave 2) — the GPL notice in `NOTICE` is the interim answer, not the end state.
- Items 7, 8, 9 need physical/VM access to a non-development machine.
