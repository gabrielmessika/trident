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
  --output data/research/pod_funding_dataset.jsonl
```

## 3. Lancer la research funding

```bash
python -m app.research.pod_funding_research \
  --input data/live_snapshots/2026-04-05.jsonl \
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

## Regle

- aucun de ces modules ne doit etre branche au superviseur ou a un pod live sans passer par:
  - research
  - replay/backtest
  - shadow dry-run
  - decision `go / park / kill`
