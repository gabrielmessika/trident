from __future__ import annotations

from dataclasses import dataclass, replace

from app.settings import AppConfig


def _dedupe_upper(items: list[str] | None) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for item in items or []:
        symbol = str(item).strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        values.append(symbol)
    return values


@dataclass(slots=True)
class SpecialSymbolsSelection:
    tradable_symbols: list[str]
    observe_only_symbols: list[str]
    observation_universe: list[str]


def resolve_special_symbols_selection(
    config: AppConfig,
    *,
    tradable_symbols: list[str] | None = None,
    observe_only_symbols: list[str] | None = None,
) -> SpecialSymbolsSelection:
    configured_observation = _dedupe_upper(
        config.hyperliquid.observation_universe or config.hyperliquid.default_coins
    )
    configured_blocked = {
        symbol for symbol in _dedupe_upper(config.hyperliquid.tradable_blocked_symbols)
    }

    resolved_tradable = _dedupe_upper(tradable_symbols)
    if not resolved_tradable:
        resolved_tradable = [
            symbol for symbol in configured_observation if symbol not in configured_blocked
        ]

    resolved_observe_only = _dedupe_upper(observe_only_symbols)
    if not resolved_observe_only:
        resolved_observe_only = [
            symbol for symbol in configured_observation if symbol not in set(resolved_tradable)
        ]

    observation_universe = _dedupe_upper(resolved_observe_only + resolved_tradable)
    return SpecialSymbolsSelection(
        tradable_symbols=resolved_tradable,
        observe_only_symbols=resolved_observe_only,
        observation_universe=observation_universe,
    )


def build_special_symbols_runtime_config(
    config: AppConfig,
    *,
    tradable_symbols: list[str] | None = None,
    observe_only_symbols: list[str] | None = None,
) -> tuple[AppConfig, SpecialSymbolsSelection]:
    selection = resolve_special_symbols_selection(
        config,
        tradable_symbols=tradable_symbols,
        observe_only_symbols=observe_only_symbols,
    )
    blocked_for_trading = _dedupe_upper(
        list(config.hyperliquid.tradable_blocked_symbols) + selection.observe_only_symbols
    )
    runtime_config = replace(
        config,
        hyperliquid=replace(
            config.hyperliquid,
            observation_universe=list(selection.observation_universe),
            default_coins=list(selection.observation_universe),
            tradable_blocked_symbols=blocked_for_trading,
        ),
        pod_a=replace(
            config.pod_a,
            enabled=True,
            blocked_symbols=[],
        ),
        pod_b=replace(config.pod_b, enabled=False),
        pod_c=replace(config.pod_c, enabled=False),
    )
    return runtime_config, selection
