"""Third-party license audit.

Enumerates every installed Python package (via `importlib.metadata` against whatever interpreter
runs this script — invoke it with the project venv) and every npm package recorded in
`ui/pet/package-lock.json` and `ui/dashboard/package-lock.json`, classifies each declared license
against `docs/license_policy.yaml`, and writes `docs/THIRD_PARTY_LICENSES.md`.

Usage:
    python scripts/license_audit.py                 # writes docs/THIRD_PARTY_LICENSES.md
    python scripts/license_audit.py --check          # also exits 1 on any deny/unknown finding

No network calls, no bundled/downloadable AI model licenses (Ollama/Whisper/Piper *voices* are a
separate, manual review item — see the "Not covered" section this script writes into its own
report; it audits installed Python/JS packages only, not model weights or the full surface a
dedicated `pip-licenses`/`license-checker`-equivalent tool would cover).
"""

from __future__ import annotations

import argparse
import importlib.metadata as importlib_metadata
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = REPO_ROOT / "docs" / "license_policy.yaml"
REPORT_PATH = REPO_ROOT / "docs" / "THIRD_PARTY_LICENSES.md"
NPM_LOCKFILES = [
    ("ui/pet", REPO_ROOT / "ui" / "pet" / "package-lock.json"),
    ("ui/dashboard", REPO_ROOT / "ui" / "dashboard" / "package-lock.json"),
]

CATEGORY_ORDER = {"allow": 0, "allow_with_note": 1, "deny": 2, "unknown": 3}


@dataclass(frozen=True, slots=True)
class Finding:
    ecosystem: str  # "python" | "ui/pet" | "ui/dashboard"
    name: str
    version: str
    declared_license: str
    category: str  # allow | allow_with_note | deny | unknown
    reason: str


def load_policy(path: Path = POLICY_PATH) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {
        "allow": [str(x) for x in data.get("allow", [])],
        "allow_with_note": [str(x) for x in data.get("allow_with_note", [])],
        "deny": [str(x) for x in data.get("deny", [])],
        "package_exceptions": {
            (str(e["ecosystem"]).lower(), str(e["name"]).lower()): e
            for e in data.get("package_exceptions", [])
        },
    }


def _classify_token(token: str, policy: dict[str, Any]) -> str | None:
    t = token.strip()
    if not t:
        return None
    tl = t.lower()
    # Most-specific first: "LGPL" must win over the bare "GPL" substring it contains.
    for pattern in policy["deny"]:
        if pattern.lower() in tl and "lgpl" not in pattern.lower():
            if pattern.lower() == "gpl" and "lgpl" in tl:
                continue
            return "deny"
    for pattern in policy["allow_with_note"]:
        if pattern.lower() in tl:
            return "allow_with_note"
    for pattern in policy["deny"]:
        if pattern.lower() in tl:
            return "deny"
    for pattern in policy["allow"]:
        if pattern.lower() in tl:
            return "allow"
    return None


def classify_license(license_text: str | None, policy: dict[str, Any]) -> tuple[str, str]:
    """Return (category, note). Handles simple SPDX-ish `OR`/`AND`/`,` expressions."""
    if not license_text or not license_text.strip():
        return "unknown", "no license metadata declared"
    text = license_text.strip()
    if " OR " in text or text.startswith("(") and " OR " in text:
        tokens = [tok.strip(" ()") for tok in text.split(" OR ")]
        results = [(_classify_token(tok, policy), tok) for tok in tokens]
        resolved = [(cat, tok) for cat, tok in results if cat is not None]
        if not resolved:
            return "unknown", f"none of the OR alternatives matched a known pattern: {text}"
        resolved.sort(key=lambda pair: CATEGORY_ORDER[pair[0]])
        best_cat, best_tok = resolved[0]
        return best_cat, f"OR expression, using the {best_tok!r} alternative ({text})"
    if " AND " in text:
        tokens = [tok.strip(" ()") for tok in text.split(" AND ")]
        and_results = [(_classify_token(tok, policy), tok) for tok in tokens]
        and_resolved: list[tuple[str, str]] = [
            (cat, tok) for cat, tok in and_results if cat is not None
        ]
        if len(and_resolved) != len(and_results):
            unresolved = ", ".join(tok for cat, tok in and_results if cat is None)
            return "unknown", f"AND expression with unresolved component(s): {unresolved} ({text})"
        worst = max(and_resolved, key=lambda pair: CATEGORY_ORDER[pair[0]])
        return worst[0], f"AND expression, all components required ({text})"
    if "," in text and " OR " not in text and " AND " not in text:
        tokens = [tok.strip() for tok in text.split(",")]
        if len(tokens) > 1:
            results = [(_classify_token(tok, policy), tok) for tok in tokens]
            resolved = [(cat, tok) for cat, tok in results if cat is not None]
            if resolved:
                resolved.sort(key=lambda pair: CATEGORY_ORDER[pair[0]])
                best_cat, best_tok = resolved[0]
                return best_cat, f"comma-separated alternatives, using {best_tok!r} ({text})"
            return "unknown", f"comma-separated, none matched: {text}"
    single = _classify_token(text, policy)
    if single is None:
        return "unknown", f"license text did not match a known pattern: {text}"
    return single, text


def apply_exception(finding: Finding, policy: dict[str, Any]) -> Finding:
    exc = policy["package_exceptions"].get((finding.ecosystem, finding.name.lower()))
    if exc is None:
        return finding
    return Finding(
        ecosystem=finding.ecosystem,
        name=finding.name,
        version=finding.version,
        declared_license=finding.declared_license,
        category=str(exc["category"]),
        reason=f"[package exception] {str(exc['reason']).strip()}",
    )


def scan_python(policy: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    for dist in importlib_metadata.distributions():
        meta = dist.metadata
        name = meta.get("Name") or (dist.metadata.get("Summary") and "") or "unknown"
        if not name:
            continue
        version = meta.get("Version", "0")
        license_text = meta.get("License-Expression") or meta.get("License")
        if not license_text:
            classifiers = meta.get_all("Classifier") or []
            license_classifiers = [c.split("::")[-1].strip() for c in classifiers if "License" in c]
            license_text = "; ".join(license_classifiers) or None
        category, reason = classify_license(license_text, policy)
        finding = Finding(
            ecosystem="python",
            name=str(name),
            version=str(version),
            declared_license=license_text or "",
            category=category,
            reason=reason,
        )
        findings.append(apply_exception(finding, policy))
    findings.sort(key=lambda f: f.name.lower())
    return findings


def _npm_package_name(path_key: str) -> str:
    """`node_modules/@scope/pkg` or `node_modules/a/node_modules/pkg` -> the last package name."""
    parts = path_key.split("node_modules/")
    return parts[-1]


def scan_npm(policy: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    for ecosystem, lockfile in NPM_LOCKFILES:
        if not lockfile.is_file():
            continue
        data = json.loads(lockfile.read_text(encoding="utf-8"))
        packages: dict[str, Any] = data.get("packages", {})
        for path_key, info in packages.items():
            if not path_key or "node_modules/" not in path_key:
                continue  # skip the root project entry
            name = _npm_package_name(path_key)
            version = str(info.get("version", "0"))
            license_text = info.get("license")
            if not license_text and isinstance(info.get("licenses"), list):
                license_text = " OR ".join(
                    str(entry.get("type", "")) for entry in info["licenses"] if entry.get("type")
                )
            category, reason = classify_license(license_text, policy)
            finding = Finding(
                ecosystem=ecosystem,
                name=name,
                version=version,
                declared_license=license_text or "",
                category=category,
                reason=reason,
            )
            findings.append(apply_exception(finding, policy))
    findings.sort(key=lambda f: (f.ecosystem, f.name.lower()))
    return findings


def render_report(python_findings: list[Finding], npm_findings: list[Finding]) -> str:
    lines: list[str] = []
    lines.append("# Third-party licenses")
    lines.append("")
    lines.append(
        "Generated by `scripts/license_audit.py`. "
        "Do not hand-edit — re-run the script and commit the regenerated file."
    )
    lines.append("")
    lines.append(
        "Scope: every installed Python package (`importlib.metadata` against the project venv) "
        "and every npm package recorded in `ui/pet/package-lock.json` and "
        "`ui/dashboard/package-lock.json`. **Not covered by this script** (manual review items, "
        "tracked in the parent spec/story, not resolved here): bundled/downloadable AI models "
        "(Ollama models, faster-whisper model weights, Piper TTS voices, any future vision model) "
        "— each has its own, separate license that this script cannot discover from package "
        "metadata."
    )
    lines.append("")

    for title, findings in (
        ("Python dependencies", python_findings),
        ("ui/pet (npm)", [f for f in npm_findings if f.ecosystem == "ui/pet"]),
        ("ui/dashboard (npm)", [f for f in npm_findings if f.ecosystem == "ui/dashboard"]),
    ):
        lines.append(f"## {title}")
        lines.append("")
        lines.append("| Package | Version | Declared license | Category | Note |")
        lines.append("|---|---|---|---|---|")
        for f in findings:
            lic = f.declared_license.replace("|", "\\|") or "(none declared)"
            note = f.reason.replace("|", "\\|")
            lines.append(f"| {f.name} | {f.version} | {lic} | {f.category} | {note} |")
        lines.append("")

    all_findings = python_findings + npm_findings
    denied = [f for f in all_findings if f.category == "deny"]
    unknown = [f for f in all_findings if f.category == "unknown"]
    noted = [f for f in all_findings if f.category == "allow_with_note"]

    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Total packages scanned: {len(all_findings)}")
    lines.append(f"- `allow`: {sum(1 for f in all_findings if f.category == 'allow')}")
    lines.append(f"- `allow_with_note`: {len(noted)}")
    lines.append(f"- `deny`: {len(denied)}")
    lines.append(f"- `unknown`: {len(unknown)}")
    lines.append("")
    if denied:
        lines.append("### DENIED — must be resolved before public release")
        lines.append("")
        for f in denied:
            lines.append(
                f"- **{f.ecosystem}/{f.name}** {f.version}: `{f.declared_license}` — {f.reason}"
            )
        lines.append("")
    if unknown:
        lines.append("### UNKNOWN — needs a policy decision or a package exception")
        lines.append("")
        for f in unknown:
            lines.append(
                f"- **{f.ecosystem}/{f.name}** {f.version}: `{f.declared_license}` — {f.reason}"
            )
        lines.append("")
    if noted:
        lines.append("### allow_with_note — compliance approach documented, verify at release time")
        lines.append("")
        for f in noted:
            lines.append(f"- **{f.ecosystem}/{f.name}** {f.version}: {f.reason}")
        lines.append("")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if any package is 'deny' or 'unknown' (CI gate). Still writes the report.",
    )
    args = parser.parse_args(argv)

    policy = load_policy()
    python_findings = scan_python(policy)
    npm_findings = scan_npm(policy)
    report = render_report(python_findings, npm_findings)
    REPORT_PATH.write_text(report, encoding="utf-8")

    all_findings = python_findings + npm_findings
    bad = [f for f in all_findings if f.category in ("deny", "unknown")]
    print(f"license_audit: wrote {REPORT_PATH} ({len(all_findings)} packages, {len(bad)} blocking)")
    if args.check and bad:
        print("license_audit --check: FAIL", file=sys.stderr)
        for f in bad:
            print(
                f"  {f.category}: {f.ecosystem}/{f.name} {f.version} ({f.declared_license!r})",
                file=sys.stderr,
            )
        return 1
    if args.check:
        print("license_audit --check: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
