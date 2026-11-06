"""Fail closed on public CI file additions, deletions or modifications.

No private repository path/commit, token, authenticated API or brokerage data.
The public manifest is a release snapshot's local integrity check, NOT an
independent provenance attestation or permission to trade.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = "ci/public_snapshot_manifest.json"


def verify() -> None:
    document = json.loads((ROOT / MANIFEST_PATH).read_text(encoding="utf-8"))
    if (set(document) != {"schema_version", "scope", "source_file_count",
                          "synthetic_test_module_count", "sha256"}
            or type(document["schema_version"]) is not int
            or document["schema_version"] != 1
            or document["scope"] != "APPROVED_PUBLIC_SYNTHETIC_SUBSET_ONLY"):
        raise ValueError("Unsupported or malformed public snapshot manifest")
    checksums = document["sha256"]
    if type(checksums) is not dict or len(checksums) != 56:
        raise ValueError("Manifest does not contain exactly 56 approved file hashes")
    if sum(p.startswith("src/schd_monitor/") for p in checksums) != 28:
        raise ValueError("Public source allowlist count mismatch")
    if sum(p.startswith("tests/") for p in checksums) != 23:
        raise ValueError("Public test allowlist count mismatch")
    if document["source_file_count"] != 28 or document["synthetic_test_module_count"] != 23:
        raise ValueError("Unsupported published test/source scope")
    approved_paths = set(['.github/workflows/ci.yml', '.gitignore', 'README.md', 'ci/verify_snapshot.py', 'pyproject.toml', 'src/schd_monitor/__init__.py', 'src/schd_monitor/__main__.py', 'src/schd_monitor/alpaca_offline_contract.py', 'src/schd_monitor/cli.py', 'src/schd_monitor/data.py', 'src/schd_monitor/dividend_audit.py', 'src/schd_monitor/exchange_calendar.py', 'src/schd_monitor/feed_integrity.py', 'src/schd_monitor/historical_champion.py', 'src/schd_monitor/indicators.py', 'src/schd_monitor/local_audit_index.py', 'src/schd_monitor/local_snapshot_receipt.py', 'src/schd_monitor/manual_ledger.py', 'src/schd_monitor/massive_offline_contract.py', 'src/schd_monitor/mechanics_probe.py', 'src/schd_monitor/models.py', 'src/schd_monitor/offline_provider_pair.py', 'src/schd_monitor/paper_accounting.py', 'src/schd_monitor/peak_audit.py', 'src/schd_monitor/publication_claim_gate.py', 'src/schd_monitor/reference_parity.py', 'src/schd_monitor/session_feed_gate.py', 'src/schd_monitor/signals.py', 'src/schd_monitor/snapshot_chain_guard.py', 'src/schd_monitor/snapshot_revision_guard.py', 'src/schd_monitor/split_audit.py', 'src/schd_monitor/synthetic_champion_bridge.py', 'src/schd_monitor/synthetic_trace_export.py', 'tests/test_alpaca_offline_contract.py', 'tests/test_dividend_audit.py', 'tests/test_engine.py', 'tests/test_exchange_calendar.py', 'tests/test_feed_integrity.py', 'tests/test_historical_champion.py', 'tests/test_local_audit_index.py', 'tests/test_local_snapshot_receipt.py', 'tests/test_manual_ledger.py', 'tests/test_massive_offline_contract.py', 'tests/test_mechanics_probe.py', 'tests/test_offline_provider_pair.py', 'tests/test_paper_accounting.py', 'tests/test_peak_audit.py', 'tests/test_publication_claim_gate.py', 'tests/test_reference_parity.py', 'tests/test_session_coverage.py', 'tests/test_session_feed_gate.py', 'tests/test_snapshot_chain_guard.py', 'tests/test_snapshot_revision_guard.py', 'tests/test_split_audit.py', 'tests/test_synthetic_champion_bridge.py', 'tests/test_synthetic_trace_export.py'])
    if set(checksums) != approved_paths:
        raise ValueError('Published path differs from owner-approved allowlist')
    for name, expected in checksums.items():
        rel = Path(name)
        if (not name or rel.is_absolute() or ".." in rel.parts
                or "\\" in name or name.startswith("./")
                or not isinstance(expected, str) or len(expected) != 64
                or any(c not in "0123456789abcdef" for c in expected)):
            raise ValueError("Unsafe public path or malformed checksum")
        location = ROOT / rel
        if location.is_symlink() or not location.is_file():
            raise ValueError("Missing or symlinked public file: " + name)
        # A checked-out text file can contain LF (Linux) or CRLF (Windows),
        # while git may normalize it during commit. Only these newline forms
        # are interchangeable; all other content remains SHA-256 bound.
        raw = location.read_bytes()
        raw.decode("utf-8", errors="strict")
        canonical_lf = raw.replace(b"\r\n", b"\n")
        if b"\r" in canonical_lf:
            raise ValueError("Unreviewed carriage return in public file: " + name)
        allowed_digests = {
            hashlib.sha256(canonical_lf).hexdigest(),
            hashlib.sha256(canonical_lf.replace(b"\n", b"\r\n")).hexdigest(),
        }
        if expected not in allowed_digests:
            raise ValueError("Public SHA-256 mismatch: " + name)
    expected_paths = set(checksums) | {MANIFEST_PATH}
    actual = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    if len(actual) != len(set(actual)) or set(actual) != expected_paths:
        extra = sorted(set(actual) - expected_paths)
        missing = sorted(expected_paths - set(actual))
        raise ValueError("Public tracked/visible file set differs from allowlist: "
                         + "unexpected=" + repr(extra) + " missing=" + repr(missing))
    print("APPROVED_PUBLIC_SYNTHETIC_SUBSET_SHA256_VERIFIED: "
          "28 sources, 23 synthetic test modules, 56 SHA-256-bound files; "
          "NO private-repository provenance or operational signal attestation")


if __name__ == "__main__":
    try:
        verify()
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print("PUBLIC_SNAPSHOT_FAIL_CLOSED: " + str(exc), file=sys.stderr)
        raise SystemExit(2)
