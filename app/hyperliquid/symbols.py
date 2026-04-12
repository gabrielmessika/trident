from __future__ import annotations


def normalize_hl_symbol(symbol: str) -> str:
    raw = str(symbol).strip()
    if not raw:
        return ""
    if ":" not in raw:
        return raw.upper()
    dex, asset = raw.split(":", 1)
    dex = dex.strip()
    asset = asset.strip()
    if not dex or not asset:
        return raw.upper()
    return f"{dex.upper()}:{asset.upper()}"


def split_hl_symbol(symbol: str) -> tuple[str | None, str]:
    normalized = normalize_hl_symbol(symbol)
    if not normalized or ":" not in normalized:
        return None, normalized
    dex, asset = normalized.split(":", 1)
    return dex.lower(), f"{dex.upper()}:{asset.upper()}"


def group_hl_symbols_by_dex(symbols: list[str] | None) -> dict[str | None, list[str]]:
    grouped: dict[str | None, list[str]] = {}
    seen: set[str] = set()
    for symbol in symbols or []:
        dex, normalized = split_hl_symbol(symbol)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        grouped.setdefault(dex, []).append(normalized)
    return grouped


def ws_subscription_symbol(symbol: str) -> str:
    dex, normalized = split_hl_symbol(symbol)
    if dex is None or ":" not in normalized:
        return normalized
    _, asset = normalized.split(":", 1)
    return f"{dex}:{asset}"
