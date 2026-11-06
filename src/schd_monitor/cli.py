"""Research-grade CLI; fail-closed until reconciliation and feed verification complete."""
from __future__ import annotations
import argparse
import csv
import json
import sys
from pathlib import Path
from datetime import datetime
from .data import bars_from_csv, position_from_json, expected_sessions_from_csv
from .indicators import calculate
from .models import MonitorDataError, validate_expected_sessions
from .exchange_calendar import completed_xnys_sessions
from .feed_integrity import compare_offline_feeds, feed_declaration_from_json
from .split_audit import audit_split_only_bars, split_ledger_from_json
from .dividend_audit import dividend_ledger_from_json, audit_dividend_ledger
from .manual_ledger import manual_ledger_from_json, audit_manual_ledger
from .peak_audit import audit_held_close_peak
from .signals import Strategy, evaluate


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Offline-only signal evaluation; DOES NOT execute trades')
    parser.add_argument('--csv',required=True,help='Split-only-adjusted OHLC CSV, not adjusted total-return closes')
    parser.add_argument('--position',required=True,help='Manually reconciled position JSON; never automated broker sync')
    parser.add_argument('--expected-sessions',help='Optional complete date-only CSV session manifest; does NOT verify market data')
    parser.add_argument('--xnys-asof',help='Optional timezone-aware ISO timestamp for offline XNYS schedule check; does NOT verify market data')
    parser.add_argument('--secondary-csv',help='Optional second, separately sourced offline split-only OHLC CSV')
    parser.add_argument('--raw-csv',help='Optional offline raw/unadjusted OHLC CSV for split audit')
    parser.add_argument('--split-ledger',help='Self-declared versioned split-event JSON; paired with --raw-csv')
    parser.add_argument('--dividend-ledger',help='Optional local UNATTESTED dividend ex-date and payment-open gross cash ledger')
    parser.add_argument('--manual-ledger',help='Optional local self-declared confirmed-event replay against position; no broker connection')
    parser.add_argument('--peak-audit',action='store_true',help='Optional unattested held-close peak check; requires --manual-ledger, --raw-csv and --split-ledger')
    parser.add_argument('--primary-feed-declaration',help='Self-declared primary CSV source metadata JSON; not attested')
    parser.add_argument('--secondary-feed-declaration',help='Self-declared secondary CSV source metadata JSON; not attested')
    parser.add_argument('--strategy',choices=[s.value for s in Strategy],default='CHAMPION')
    parser.add_argument('--output',help='Save immutable-style JSON result to local folder')
    parser.add_argument('--output-csv',help='Export one-row Excel-compatible CSV snapshot (never overwrite)')
    parser.add_argument('--rs20-underperformance',choices=['true','false'],help='Verified comparison, if using RS20')
    args=parser.parse_args(argv)
    try:
        if args.expected_sessions and args.xnys_asof:
            raise MonitorDataError("Choose one calendar gate: --expected-sessions OR --xnys-asof")
        expected = expected_sessions_from_csv(args.expected_sessions) if args.expected_sessions else None
        bars=bars_from_csv(args.csv,expected_sessions=expected)
        if args.xnys_asof:
            as_of = datetime.fromisoformat(args.xnys_asof)
            validate_expected_sessions(
                bars, completed_xnys_sessions(bars[0].session, as_of)
            )
        pos=position_from_json(args.position)
        if args.peak_audit and not all((args.manual_ledger, args.raw_csv, args.split_ledger)):
            raise MonitorDataError("Peak audit requires --manual-ledger, --raw-csv and --split-ledger")
        if bool(args.raw_csv) != bool(args.split_ledger):
            raise MonitorDataError("Offline split audit requires both --raw-csv and --split-ledger")
        split_audit = None
        if args.raw_csv:
            split_audit = audit_split_only_bars(
                bars_from_csv(args.raw_csv), bars,
                split_ledger_from_json(args.split_ledger),
                expected_symbol=pos.symbol.upper(),
            )
        dividend_audit = None
        if args.dividend_ledger:
            dividend_audit = audit_dividend_ledger(
                dividend_ledger_from_json(args.dividend_ledger),
                expected_symbol=pos.symbol.upper(),
                sessions=[bar.session for bar in bars],
            )
        manual_audit = None
        if args.manual_ledger:
            manual_audit = audit_manual_ledger(
                manual_ledger_from_json(args.manual_ledger),
                pos, sessions=[bar.session for bar in bars],
            )
        peak_audit = None
        if args.peak_audit:
            peak_audit = audit_held_close_peak(
                bars, manual_ledger_from_json(args.manual_ledger), pos,
                split_ledger=split_ledger_from_json(args.split_ledger),
            )
        feed_options = (args.secondary_csv, args.primary_feed_declaration,
                        args.secondary_feed_declaration)
        if any(feed_options) and not all(feed_options):
            raise MonitorDataError("Offline two-feed comparison requires --secondary-csv and both feed declarations")
        comparison = None
        if all(feed_options):
            secondary = bars_from_csv(args.secondary_csv)
            comparison = compare_offline_feeds(
                bars, secondary,
                feed_declaration_from_json(args.primary_feed_declaration),
                feed_declaration_from_json(args.secondary_feed_declaration),
                expected_symbol=pos.symbol.upper(),
            )
        choice=(None if args.rs20_underperformance is None
                else args.rs20_underperformance=='true')
        result=evaluate(bars,calculate(bars),pos,strategy=Strategy(args.strategy),
                        rs20_underperformance=choice,data_verified=False)
        message={'schema_version':1,'safety':'RESEARCH_ONLY_UNVERIFIED','result':result.as_json(),
                 'note':'No broker integration. Do not treat provisional signals as verified orders.'}
        if comparison is not None:
            message['feed_comparison'] = comparison.as_json()
        if split_audit is not None:
            message['split_audit'] = split_audit.as_json()
        if dividend_audit is not None:
            message['dividend_audit'] = dividend_audit.as_json()
        if manual_audit is not None:
            message['manual_ledger_audit'] = manual_audit.as_json()
        if peak_audit is not None:
            message['held_peak_audit'] = peak_audit.as_json()
        output=json.dumps(message,indent=2,sort_keys=True)
        if args.output:
            dest=Path(args.output)
            dest.parent.mkdir(parents=True,exist_ok=True)
            if dest.exists():raise MonitorDataError('Refusing to overwrite existing saved signal')
            dest.write_text(output+'\n',encoding='utf-8')
        if args.output_csv:
            dest=Path(args.output_csv)
            dest.parent.mkdir(parents=True,exist_ok=True)
            if dest.exists(): raise MonitorDataError('Refusing to overwrite existing saved CSV signal')
            fields=['signal_session','symbol','strategy','candidate_action','verification',
                    'latest_close','sma60','atr14','held_peak','stop_threshold',
                    'macd','macd_signal','bearish_cross','macd_decline',
                    'rapid_five_session','atr_gate','rs20_underperformance',
                    'execution','signal_reasons']
            row=result.as_json();row['signal_reasons']='|'.join(row['signal_reasons'])
            with dest.open('x',newline='',encoding='utf-8-sig') as f:
                writer=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore')
                writer.writeheader();writer.writerow(row)
        print(output)
        return 0
    except (MonitorDataError, OSError, KeyError, ValueError, TypeError) as exc:
        print(json.dumps({'status':'NO_VERIFIED_SIGNAL','error':str(exc)},indent=2),file=sys.stderr)
        return 2

if __name__=='__main__':
    raise SystemExit(main())
