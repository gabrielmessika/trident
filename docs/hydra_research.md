# Hydra Research Workflows

Ces commandes restent volontairement hors du run principal TRIDENT.

## 1. Collecter funding / OI en autonome

```bash
python -m app.live.funding_collector \
  --config config/trident.toml \
  --output data/funding_history/current.jsonl \
  --iterations 1
```

## 2. Construire un dataset funding depuis des snapshots TRIDENT

```bash
python -m app.research.pod_funding_dataset \
  --input data/live_snapshots/2026-04-05.jsonl \
  --funding-history data/funding_history/current.jsonl \
  --output data/research/pod_funding_dataset.jsonl
```

## 3. Lancer la research funding

```bash
python -m app.research.pod_funding_research \
  --input data/live_snapshots/2026-04-05.jsonl \
  --funding-history data/funding_history/current.jsonl \
  --output-json docs/pod_funding_research_latest.json \
  --output-md docs/pod_funding_research_latest.md
```

## 4. Extraire les features liq observables-first

```bash
python -m app.research.pod_liq_features \
  --input data/live_snapshots/2026-04-05.jsonl \
  --output data/research/pod_liq_features.jsonl
```

## 5. Lancer la research liq / OI observables-first

```bash
python -m app.research.pod_liq_research \
  --input data/live_snapshots/2026-04-05.jsonl \
  --output-json docs/pod_liq_research_latest.json \
  --output-md docs/pod_liq_research_latest.md
```

## 6. Collecter et analyser le top 30 crypto Hyperliquid

```bash
python -m app.research.hyperliquid_top30_research \
  --mode run \
  --dataset-dir data/research/hyperliquid_top30/current \
  --output-json docs/hyperliquid_top30_latest.json \
  --output-md docs/hyperliquid_top30_latest.md \
  --refresh
```

Notes:

- la selection du top 30 est faite a partir du snapshot courant `metaAndAssetCtxs` trie par `dayNtlVlm` puis `open_interest_usd`
- les bougies sont stockees gzip pour reutilisation ulterieure dans `data/research/hyperliquid_top30/current/raw/`
- `fundingHistory` est collecte sur toute la fenetre demandee avec pagination
- l'analyse combine maintenant un pack etendu d'indicateurs:
  - `EMA`, `RSI`, `MACD`, `ATR`, `ADX`
  - `Bollinger`, `Donchian`, `Ichimoku`
  - `Stoch RSI`, `VWAP` roulant, `CCI`
  - `Keltner`, `TTM squeeze`, `Supertrend`
  - `OBV`, `MFI`, `funding z-score`
- `candleSnapshot` officiel ne donne que les 5000 dernieres bougies:
  - `1h` et `2h` couvrent 6 mois
  - `15m` et `30m` seront tronques par cette limite

## Regle

- aucun de ces modules ne doit etre branche au superviseur ou a un pod live sans passer par:
  - research
  - replay/backtest
  - shadow dry-run
  - decision `go / park / kill`

## Note funding

- si les snapshots live ne portent pas un `funding_rate` exploitable, passez `--funding-history`
- le builder aligne alors chaque snapshot avec la derniere observation funding/OI fraiche du meme symbole
