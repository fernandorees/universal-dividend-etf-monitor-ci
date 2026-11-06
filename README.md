# Universal Dividend-ETF Monitor — public offline CI snapshot

This is a **restricted, synthetic-data-only CI mirror** of selected, explicitly
approved source and test modules from a separate PRIVATE development project.
It is **not the authoritative development repository**, a trading system,
or a source of verified market signals.

- **No brokerage connection**, order execution, account authentication, or fills.
- No private archives, real portfolios, licensed price histories or credentials.
- Never use any output for investment decisions: `UNVERIFIED_DO_NOT_TRADE`.
- The financial strategy implementation in approved modules is publicly readable;
  no license is granted by this repository's contents or this README.
- The manifest under `ci/public_snapshot_manifest.json` pins the public file
  contents using SHA-256, without publishing the private repository's Git history.
- Public CI tests ONLY the published subset, not confidential historical-reference
  regressions or live-provider/operational integrity.

The project owner manually reviews and publishes approved snapshots. This
repository never downloads or authenticates into the private development repo.
Please do not upload secrets or real market/portfolio data here.
