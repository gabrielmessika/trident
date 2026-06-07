from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from app.trident_ai.config import load_trident_ai_config
from app.trident_ai.replay import run_trident_ai_llm_replay
from app.trident_ai.shadow_runner import run_trident_ai_shadow


DEFAULT_REPLAY_INPUT = "server-data/replay_inputs/full_bot_latest_fetch.jsonl"
DEFAULT_SMOKE_SYMBOLS = ("BTC", "ETH", "SOL", "HYPE")
DEFAULT_ENV_FILE = ".env.tridentai"
ALLOWED_ENV_KEYS = {"OPENAI_API_KEY", "XAI_API_KEY"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run TRIDENT-AI local replay tools")
    subparsers = parser.add_subparsers(dest="command", required=True)

    shadow = subparsers.add_parser("shadow", help="Run deterministic shadow replay")
    _add_common_replay_args(shadow)
    shadow.add_argument("--status-path", default=None)

    llm = subparsers.add_parser("llm-replay", help="Run LLM replay/cache-fill")
    _add_common_replay_args(llm)
    llm.add_argument("--cache-dir", default=None)
    llm.add_argument("--report-json-path", default=None)
    llm.add_argument("--report-md-path", default=None)
    llm.add_argument("--allow-live-llm-calls", action="store_true")
    llm.add_argument("--max-live-calls", type=int, default=None)
    llm.add_argument("--max-incremental-cost-usd", type=float, default=None)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_trident_ai_env_file(args.env_file)
    config = load_trident_ai_config(args.config)
    symbols = tuple(args.symbols.split(",")) if args.symbols else DEFAULT_SMOKE_SYMBOLS

    if args.command == "shadow":
        result = run_trident_ai_shadow(
            args.input,
            config=config,
            journal_path=args.journal_path,
            status_path=args.status_path,
            max_records=args.max_records,
            max_contexts=args.max_contexts,
            symbols=symbols,
        )
    else:
        result = run_trident_ai_llm_replay(
            args.input,
            config=config,
            cache_dir=args.cache_dir,
            allow_live_llm_calls=args.allow_live_llm_calls,
            journal_path=args.journal_path,
            report_json_path=args.report_json_path,
            report_md_path=args.report_md_path,
            max_records=args.max_records,
            max_contexts=args.max_contexts,
            symbols=symbols,
            max_live_calls=args.max_live_calls,
            max_incremental_cost_usd=args.max_incremental_cost_usd,
        )

    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return 0


def _add_common_replay_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default="config/trident_ai.toml")
    parser.add_argument(
        "--env-file",
        default=DEFAULT_ENV_FILE,
        help="Optional local env file for API keys. Default: .env.tridentai",
    )
    parser.add_argument("--input", default=DEFAULT_REPLAY_INPUT)
    parser.add_argument("--journal-path", default=None)
    parser.add_argument("--max-records", type=int, default=20)
    parser.add_argument("--max-contexts", type=int, default=50)
    parser.add_argument(
        "--symbols",
        default=",".join(DEFAULT_SMOKE_SYMBOLS),
        help="Comma-separated symbol filter. Default: BTC,ETH,SOL,HYPE",
    )


def load_trident_ai_env_file(path: str | Path | None) -> dict[str, str]:
    if path is None or str(path).strip().lower() in {"", "none", "false"}:
        return {}
    env_path = Path(path)
    if not env_path.exists():
        return {}

    loaded: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if key not in ALLOWED_ENV_KEYS:
            continue
        if key in os.environ:
            continue
        value = _unquote_env_value(raw_value.strip())
        if not value:
            continue
        os.environ[key] = value
        loaded[key] = value
    return loaded


def _unquote_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


if __name__ == "__main__":
    raise SystemExit(main())
