> `STATUS: RESEARCH_REFERENCE_ONLY`
>
> Ce document reste une note de faisabilite research. Il ne constitue plus un plan actif.
> Le plan actif est [docs/trident_active_plan.md](/workspaces/trident/docs/trident_active_plan.md).

# Pod Liq Data Feasibility

## Etat actuel

- Observable maintenant dans les snapshots TRIDENT:
  - `spread_bps`
  - `book_imbalance`
  - `trade_flow_bias`
  - `bucket_trade_count`
  - `bucket_volume`
  - `bucket_range_bps`
  - `structure_score`
- Observable via `metaAndAssetCtxs` Hyperliquid:
  - `funding`
  - `openInterest`
  - `markPx`
  - `oraclePx`
  - `premium`

## Ce qui est exploitable tout de suite

- recherche `observables-first` sur bursts de flow / imbalance / spread
- polling periodic de `openInterest` et `funding` hors run principal
- calcul de deltas `openInterest` apres constitution d'un historique local

## Ce qui est seulement approximable

- episodes de squeeze deduits par:
  - saut de spread
  - acceleration du flow
  - hausse de l'intensite de prints
  - futur delta d'`openInterest` si le collecteur tourne

## Ce qui n'est pas propre aujourd'hui

- carte explicite des zones de liquidation
- reconstruction discretionary d'une heatmap de liquidation
- promotion d'un pod live fonde sur une source non historisee localement

## Decision de travail

- continuer en `research / shadow only`
- ne pas creer de pod dedie tant qu'un signal:
  - observable en temps reel
  - definissable objectivement
  - reproductible hors echantillon
