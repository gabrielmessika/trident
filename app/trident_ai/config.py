from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.trident_ai.types import (
    AgentProposalValidationConfig,
    TRIDENT_AI_INITIAL_SYMBOLS,
)


TRIDENT_AI_CONFIG_ENV = "TRIDENT_AI_CONFIG_PATH"
TRIDENT_AI_DEFAULT_CONFIG_PATH = "config/trident_ai.toml"

_ALLOWED_MODES = {"shadow", "dry-run", "testnet", "mainnet-paper", "live"}
_ALLOWED_PROVIDERS = {"openai", "anthropic", "gemini", "xai", "deepseek", "mistral"}


@dataclass(frozen=True, slots=True)
class TridentAIPathsConfig:
    runtime_dir: str = "./runtime/trident_ai"
    llm_cache_dir: str = "./runtime/trident_ai/llm_cache"
    log_dir: str = "./logs"
    status_path: str = "./logs/trident_ai_status.json"
    shadow_journal_path: str = "./logs/trident_ai_shadow.jsonl"
    replay_output_dir: str = "./server-data/replay_reports"


@dataclass(frozen=True, slots=True)
class TridentAIRiskConfig:
    min_confidence: float = 0.55
    live_max_order_notional_usd: float = 25.0
    max_daily_loss_usd: float = 5.0
    max_open_positions: int = 1
    max_trades_per_day: int = 3
    max_leverage: float = 1.0
    max_proposal_age_seconds: float = 300.0
    max_market_context_age_seconds: float = 300.0
    max_intel_digest_age_seconds: float = 1800.0
    max_clock_skew_seconds: float = 60.0
    require_stop: bool = True
    require_evidence: bool = True

    def to_validation_config(
        self,
        *,
        allowed_symbols: tuple[str, ...],
    ) -> AgentProposalValidationConfig:
        return AgentProposalValidationConfig(
            allowed_symbols=allowed_symbols,
            min_confidence=self.min_confidence,
            max_notional_usd=self.live_max_order_notional_usd,
            max_leverage=self.max_leverage,
            max_proposal_age_seconds=self.max_proposal_age_seconds,
            max_market_context_age_seconds=self.max_market_context_age_seconds,
            max_intel_digest_age_seconds=self.max_intel_digest_age_seconds,
            max_clock_skew_seconds=self.max_clock_skew_seconds,
            require_stop=self.require_stop,
            require_evidence=self.require_evidence,
        )


@dataclass(frozen=True, slots=True)
class TridentAILLMConfig:
    provider: str = "openai"
    model: str = "gpt-5.4-mini"
    verifier_provider: str = "openai"
    verifier_model: str = "gpt-5.4"
    temperature: float = 0.1
    timeout_seconds: float = 20.0
    max_retries: int = 1
    cache_enabled: bool = True


@dataclass(frozen=True, slots=True)
class TridentAIPaperConfig:
    taker_fee_bps: float = 3.5
    slippage_bps: float = 0.5
    spread_multiplier: float = 0.5
    force_close_at_end: bool = True


@dataclass(frozen=True, slots=True)
class TridentAIIntelConfig:
    enabled: bool = False
    provider: str = "xai"
    model: str = "grok-4.3"
    cache_dir: str = "./runtime/trident_ai/intel_cache"
    digest_ttl_seconds: int = 1800
    max_live_calls_per_digest: int = 2
    max_x_search_calls_per_day: int = 24
    max_web_search_calls_per_day: int = 12
    max_incremental_cost_usd: float = 0.02
    x_search_enabled: bool = True
    web_search_enabled: bool = False
    x_search_cost_per_1000_calls_usd: float = 5.0
    web_search_cost_per_1000_calls_usd: float = 5.0
    allowed_x_handles: tuple[str, ...] = ()
    allowed_web_domains: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TridentAIConfig:
    enabled: bool = False
    mode: str = "shadow"
    max_monthly_ai_budget_usd: float = 30.0
    decision_interval_seconds: int = 900
    max_symbols_per_cycle: int = 5
    tradable_symbols: tuple[str, ...] = TRIDENT_AI_INITIAL_SYMBOLS
    require_independent_hyperliquid_account: bool = True
    paths: TridentAIPathsConfig = field(default_factory=TridentAIPathsConfig)
    risk: TridentAIRiskConfig = field(default_factory=TridentAIRiskConfig)
    llm: TridentAILLMConfig = field(default_factory=TridentAILLMConfig)
    paper: TridentAIPaperConfig = field(default_factory=TridentAIPaperConfig)
    intel: TridentAIIntelConfig = field(default_factory=TridentAIIntelConfig)

    def proposal_validation_config(self) -> AgentProposalValidationConfig:
        return self.risk.to_validation_config(allowed_symbols=self.tradable_symbols)


class TridentAIConfigError(ValueError):
    """Raised when tridentAI config is missing or unsafe."""


def load_trident_ai_config(path: str | Path | None = None) -> TridentAIConfig:
    config_path = Path(path or os.getenv(TRIDENT_AI_CONFIG_ENV, TRIDENT_AI_DEFAULT_CONFIG_PATH))
    data = _load_config_data(config_path)
    return _parse_config(data, source_path=config_path)


def _parse_config(data: dict[str, Any], *, source_path: Path) -> TridentAIConfig:
    root = data.get("trident_ai", {})
    if not isinstance(root, dict):
        raise TridentAIConfigError(f"{source_path}: [trident_ai] must be a table")

    paths = _parse_paths(root.get("paths", {}), source_path=source_path)
    risk = _parse_risk(root.get("risk", {}), source_path=source_path)
    llm = _parse_llm(root.get("llm", {}), source_path=source_path)
    paper = _parse_paper(root.get("paper", {}), source_path=source_path)
    intel = _parse_intel(root.get("intel", {}), source_path=source_path)
    mode = _str(root, "mode", "shadow").lower()
    if mode not in _ALLOWED_MODES:
        raise TridentAIConfigError(f"{source_path}: invalid trident_ai.mode={mode!r}")
    symbols = _symbol_tuple(root.get("tradable_symbols", list(TRIDENT_AI_INITIAL_SYMBOLS)))
    invalid_symbols = sorted(set(symbols) - set(TRIDENT_AI_INITIAL_SYMBOLS))
    if invalid_symbols:
        raise TridentAIConfigError(
            f"{source_path}: tradable_symbols outside initial universe {invalid_symbols}"
        )
    max_symbols_per_cycle = _int(root, "max_symbols_per_cycle", 5)
    if max_symbols_per_cycle <= 0:
        raise TridentAIConfigError(f"{source_path}: max_symbols_per_cycle must be positive")
    decision_interval_seconds = _int(root, "decision_interval_seconds", 900)
    if decision_interval_seconds <= 0:
        raise TridentAIConfigError(f"{source_path}: decision_interval_seconds must be positive")

    config = TridentAIConfig(
        enabled=_bool(root, "enabled", False),
        mode=mode,
        max_monthly_ai_budget_usd=_float(root, "max_monthly_ai_budget_usd", 30.0),
        decision_interval_seconds=decision_interval_seconds,
        max_symbols_per_cycle=max_symbols_per_cycle,
        tradable_symbols=symbols,
        require_independent_hyperliquid_account=_bool(
            root,
            "require_independent_hyperliquid_account",
            True,
        ),
        paths=paths,
        risk=risk,
        llm=llm,
        paper=paper,
        intel=intel,
    )
    _validate_config(config, source_path=source_path)
    return config


def _parse_paths(raw: object, *, source_path: Path) -> TridentAIPathsConfig:
    if not isinstance(raw, dict):
        raise TridentAIConfigError(f"{source_path}: [trident_ai.paths] must be a table")
    return TridentAIPathsConfig(
        runtime_dir=_str(raw, "runtime_dir", "./runtime/trident_ai"),
        llm_cache_dir=_str(raw, "llm_cache_dir", "./runtime/trident_ai/llm_cache"),
        log_dir=_str(raw, "log_dir", "./logs"),
        status_path=_str(raw, "status_path", "./logs/trident_ai_status.json"),
        shadow_journal_path=_str(raw, "shadow_journal_path", "./logs/trident_ai_shadow.jsonl"),
        replay_output_dir=_str(raw, "replay_output_dir", "./server-data/replay_reports"),
    )


def _parse_risk(raw: object, *, source_path: Path) -> TridentAIRiskConfig:
    if not isinstance(raw, dict):
        raise TridentAIConfigError(f"{source_path}: [trident_ai.risk] must be a table")
    return TridentAIRiskConfig(
        min_confidence=_float(raw, "min_confidence", 0.55),
        live_max_order_notional_usd=_float(raw, "live_max_order_notional_usd", 25.0),
        max_daily_loss_usd=_float(raw, "max_daily_loss_usd", 5.0),
        max_open_positions=_int(raw, "max_open_positions", 1),
        max_trades_per_day=_int(raw, "max_trades_per_day", 3),
        max_leverage=_float(raw, "max_leverage", 1.0),
        max_proposal_age_seconds=_float(raw, "max_proposal_age_seconds", 300.0),
        max_market_context_age_seconds=_float(raw, "max_market_context_age_seconds", 300.0),
        max_intel_digest_age_seconds=_float(raw, "max_intel_digest_age_seconds", 1800.0),
        max_clock_skew_seconds=_float(raw, "max_clock_skew_seconds", 60.0),
        require_stop=_bool(raw, "require_stop", True),
        require_evidence=_bool(raw, "require_evidence", True),
    )


def _parse_llm(raw: object, *, source_path: Path) -> TridentAILLMConfig:
    if not isinstance(raw, dict):
        raise TridentAIConfigError(f"{source_path}: [trident_ai.llm] must be a table")
    provider = _str(raw, "provider", "openai").lower()
    verifier_provider = _str(raw, "verifier_provider", provider).lower()
    if provider not in _ALLOWED_PROVIDERS:
        raise TridentAIConfigError(f"{source_path}: invalid trident_ai.llm.provider={provider!r}")
    if verifier_provider not in _ALLOWED_PROVIDERS:
        raise TridentAIConfigError(
            f"{source_path}: invalid trident_ai.llm.verifier_provider={verifier_provider!r}"
        )
    return TridentAILLMConfig(
        provider=provider,
        model=_str(raw, "model", "gpt-5.4-mini"),
        verifier_provider=verifier_provider,
        verifier_model=_str(raw, "verifier_model", "gpt-5.4"),
        temperature=_float(raw, "temperature", 0.1),
        timeout_seconds=_float(raw, "timeout_seconds", 20.0),
        max_retries=_int(raw, "max_retries", 1),
        cache_enabled=_bool(raw, "cache_enabled", True),
    )


def _parse_paper(raw: object, *, source_path: Path) -> TridentAIPaperConfig:
    if not isinstance(raw, dict):
        raise TridentAIConfigError(f"{source_path}: [trident_ai.paper] must be a table")
    return TridentAIPaperConfig(
        taker_fee_bps=_float(raw, "taker_fee_bps", 3.5),
        slippage_bps=_float(raw, "slippage_bps", 0.5),
        spread_multiplier=_float(raw, "spread_multiplier", 0.5),
        force_close_at_end=_bool(raw, "force_close_at_end", True),
    )


def _parse_intel(raw: object, *, source_path: Path) -> TridentAIIntelConfig:
    if not isinstance(raw, dict):
        raise TridentAIConfigError(f"{source_path}: [trident_ai.intel] must be a table")
    provider = _str(raw, "provider", "xai").lower()
    if provider not in _ALLOWED_PROVIDERS:
        raise TridentAIConfigError(f"{source_path}: invalid trident_ai.intel.provider={provider!r}")
    return TridentAIIntelConfig(
        enabled=_bool(raw, "enabled", False),
        provider=provider,
        model=_str(raw, "model", "grok-4.3"),
        cache_dir=_str(raw, "cache_dir", "./runtime/trident_ai/intel_cache"),
        digest_ttl_seconds=_int(raw, "digest_ttl_seconds", 1800),
        max_live_calls_per_digest=_int(raw, "max_live_calls_per_digest", 2),
        max_x_search_calls_per_day=_int(raw, "max_x_search_calls_per_day", 24),
        max_web_search_calls_per_day=_int(raw, "max_web_search_calls_per_day", 12),
        max_incremental_cost_usd=_float(raw, "max_incremental_cost_usd", 0.02),
        x_search_enabled=_bool(raw, "x_search_enabled", True),
        web_search_enabled=_bool(raw, "web_search_enabled", False),
        x_search_cost_per_1000_calls_usd=_float(
            raw,
            "x_search_cost_per_1000_calls_usd",
            5.0,
        ),
        web_search_cost_per_1000_calls_usd=_float(
            raw,
            "web_search_cost_per_1000_calls_usd",
            5.0,
        ),
        allowed_x_handles=_string_tuple(raw.get("allowed_x_handles", []), "allowed_x_handles"),
        allowed_web_domains=_string_tuple(raw.get("allowed_web_domains", []), "allowed_web_domains"),
    )


def _validate_config(config: TridentAIConfig, *, source_path: Path) -> None:
    if config.max_monthly_ai_budget_usd < 0:
        raise TridentAIConfigError(f"{source_path}: max_monthly_ai_budget_usd must be >= 0")
    if config.risk.min_confidence < 0 or config.risk.min_confidence > 1:
        raise TridentAIConfigError(f"{source_path}: risk.min_confidence must be between 0 and 1")
    if config.risk.live_max_order_notional_usd <= 0:
        raise TridentAIConfigError(f"{source_path}: risk.live_max_order_notional_usd must be positive")
    if config.risk.max_daily_loss_usd <= 0:
        raise TridentAIConfigError(f"{source_path}: risk.max_daily_loss_usd must be positive")
    if config.risk.max_open_positions <= 0:
        raise TridentAIConfigError(f"{source_path}: risk.max_open_positions must be positive")
    if config.risk.max_trades_per_day <= 0:
        raise TridentAIConfigError(f"{source_path}: risk.max_trades_per_day must be positive")
    if config.risk.max_leverage <= 0:
        raise TridentAIConfigError(f"{source_path}: risk.max_leverage must be positive")
    if config.llm.temperature < 0:
        raise TridentAIConfigError(f"{source_path}: llm.temperature must be >= 0")
    if config.llm.timeout_seconds <= 0:
        raise TridentAIConfigError(f"{source_path}: llm.timeout_seconds must be positive")
    if config.llm.max_retries < 0:
        raise TridentAIConfigError(f"{source_path}: llm.max_retries must be >= 0")
    if config.paper.taker_fee_bps < 0:
        raise TridentAIConfigError(f"{source_path}: paper.taker_fee_bps must be >= 0")
    if config.paper.slippage_bps < 0:
        raise TridentAIConfigError(f"{source_path}: paper.slippage_bps must be >= 0")
    if config.paper.spread_multiplier < 0:
        raise TridentAIConfigError(f"{source_path}: paper.spread_multiplier must be >= 0")
    if config.intel.digest_ttl_seconds <= 0:
        raise TridentAIConfigError(f"{source_path}: intel.digest_ttl_seconds must be positive")
    if config.intel.max_live_calls_per_digest < 0:
        raise TridentAIConfigError(f"{source_path}: intel.max_live_calls_per_digest must be >= 0")
    if config.intel.max_x_search_calls_per_day < 0:
        raise TridentAIConfigError(f"{source_path}: intel.max_x_search_calls_per_day must be >= 0")
    if config.intel.max_web_search_calls_per_day < 0:
        raise TridentAIConfigError(f"{source_path}: intel.max_web_search_calls_per_day must be >= 0")
    if config.intel.max_incremental_cost_usd < 0:
        raise TridentAIConfigError(f"{source_path}: intel.max_incremental_cost_usd must be >= 0")
    if config.intel.x_search_cost_per_1000_calls_usd < 0:
        raise TridentAIConfigError(f"{source_path}: intel.x_search_cost_per_1000_calls_usd must be >= 0")
    if config.intel.web_search_cost_per_1000_calls_usd < 0:
        raise TridentAIConfigError(f"{source_path}: intel.web_search_cost_per_1000_calls_usd must be >= 0")
    if len(config.intel.allowed_x_handles) > 20:
        raise TridentAIConfigError(f"{source_path}: intel.allowed_x_handles max is 20")
    if config.mode in {"testnet", "mainnet-paper", "live"} and not config.require_independent_hyperliquid_account:
        raise TridentAIConfigError(
            f"{source_path}: execution modes require an independent Hyperliquid account"
        )


def _load_config_data(config_path: Path) -> dict[str, Any]:
    resolved = config_path.expanduser()
    if not resolved.is_absolute():
        resolved = resolved.resolve()
    with resolved.open("rb") as handle:
        data: dict[str, Any] = tomllib.load(handle)
    return data


def _str(data: dict[str, object], key: str, default: str) -> str:
    value = data.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise TridentAIConfigError(f"invalid string field {key!r}")
    return value.strip()


def _float(data: dict[str, object], key: str, default: float) -> float:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TridentAIConfigError(f"invalid numeric field {key!r}")
    return float(value)


def _int(data: dict[str, object], key: str, default: int) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TridentAIConfigError(f"invalid integer field {key!r}")
    return int(value)


def _bool(data: dict[str, object], key: str, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise TridentAIConfigError(f"invalid boolean field {key!r}")
    return value


def _symbol_tuple(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise TridentAIConfigError("tradable_symbols must be a list")
    symbols: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise TridentAIConfigError("tradable_symbols must contain non-empty strings")
        symbol = item.strip().upper()
        if symbol not in symbols:
            symbols.append(symbol)
    if not symbols:
        raise TridentAIConfigError("tradable_symbols must not be empty")
    return tuple(symbols)


def _string_tuple(raw: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise TridentAIConfigError(f"{field_name} must be a list")
    values: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise TridentAIConfigError(f"{field_name} must contain non-empty strings")
        value = item.strip()
        if value not in values:
            values.append(value)
    return tuple(values)
