# Pod C vs Pod A Transfer

Date: `2026-04-18`

## Resume

Le `Pod C` actuel ne ressemble pas encore au `Pod A` nouvelle version en profondeur.

- `Pod A` est un moteur riche:
  - multi-setups
  - contexte MTF
  - pattern vetoes / watchers
  - campaign mode
  - `setup_runner`
  - observabilite detaillee via `setup_details`
- `Pod C` est un moteur beaucoup plus simple:
  - 2 familles de signaux (`tradfi_continuation_*`, `tradfi_reclaim_*`)
  - contexte court terme sans structure MTF
  - filtres hardcodes par cluster
  - exits statiques par setup / cluster
  - peu de details exploitables pour de la recherche type `Pod A`

La bonne lecture n'est donc pas "copier `Pod A` sur `Pod C`", mais:

- garder ce qui rend `Pod C` selectif
- ajouter seulement les briques de `Pod A` qui augmentent la selectivite
- eviter les briques qui augmentent surtout l'exposition ou la duree de trade sans meilleur contexte

## Photo Courante

Dataset utilise: `server-data/live_snapshots`

### Pod A

- `signal_count`: `519`
- `accepted_count`: `186`
- `closed_trade_count`: `98`
- `realized_pnl_usd`: `+368.82`
- `fees_usd`: `80.045688`
- `max_drawdown_usd`: `52.65`
- `average_hold_hours`: `1.0908`
- `average_confidence`: `0.7083`
- setups actifs sur ce run:
  - `trend_pullback_long`: `+155.78`
  - `liquidity_sweep_reclaim_long`: `+157.56`
  - `vwap_reclaim_short`: `+55.48`

### Pod C

- `signal_count`: `150`
- `accepted_count`: `45`
- `closed_trade_count`: `18`
- `realized_pnl_usd`: `+26.11`
- `fees_usd`: `9.132738`
- `max_drawdown_usd`: `7.46`
- `average_hold_hours`: `1.6176`
- `average_confidence`: `0.7793`
- setup actif sur ce run:
  - `tradfi_continuation_long`: `+26.11`

Lecture:

- `Pod C` traite beaucoup moins d'opportunites que `Pod A`.
- `Pod C` tient deja ses positions plus longtemps que `Pod A`.
- le vrai edge courant de `Pod C` vient de sa forte selectivite, pas d'une sophistication d'exit.

## Comparaison Structurelle

### Ce que `Pod A` a et `Pod C` n'a pas encore

- contexte MTF exploitable pour recherche (`trend_1h_bps`, `trend_4h_bps`, `structure_ready`, `swing_*`, `range_*`)
- `pattern_vetoes` et `pattern_watchers`
- `campaign_mode_active`
- `setup_runner_active`
- `routing_revoke_exempt`
- `setup_details` riches, persistants jusqu'aux trades clos

### Ce que `Pod C` a de different

- edge plus local et plus clusterise
- logique de qualite deja tres forte dans le service
- peu de trades, donc peu de marge pour des mecanismes lourds de type `campaign`
- pas de signal prouvant un besoin de `routing_grace`:
  - close reasons actuels: `take_profit_hit`, `stop_hit`, `trailing_stop`, `time_stop`, `break_even_stop`, `end_of_backtest`
  - aucun `routing_revoked` observe sur le replay standalone courant

## Transfert D'Idees Pod A -> Pod C

### Idees deja prouvees utiles sur Pod C

1. Couper brutalement les branches faibles

L'equivalent `Pod C` de la logique `Pod A = disable weak setups` a deja marche:

- `cluster_aware_v2 = false`
  - `2678` signaux
  - `73` trades
  - `-63.66 USD`
  - `87.27 USD` de drawdown
- `cluster_aware_v2 = true`
  - `150` signaux
  - `18` trades
  - `+26.11 USD`
  - `7.46 USD` de drawdown

Conclusion:

- oui, l'idee la plus transferable depuis `Pod A` est de supprimer des branches entieres
- c'est deja le coeur du `Pod C` gagnant actuel

2. Bloquer les shorts faibles

Le `Pod C` performant courant est deja un `long-only` de fait.

- `cluster_v2_off`: pertes sur `tradfi_continuation_short`, `tradfi_reclaim_long`, `tradfi_reclaim_short`
- `cluster_v2_on`: `tradfi_continuation_long` seulement, PnL positif

Conclusion:

- le meilleur enseignement de `Pod A` ici n'est pas "mieux shorter"
- c'est "ne shorter que quand il y a une preuve beaucoup plus forte"

### Idees testées et non validees sur Pod C

1. `setup_runner` / exit plus long

Tests sur `tradfi_continuation_long`:

- baseline actuelle: `+26.11`
- runner soft (`tp=0, be=0.95, ta=1.2, td=0.7`): `+22.92`
- runner type `Pod A` (`tp=0, be=1.0, ta=1.4, td=0.8`): `+15.02`

Conclusion:

- les sorties plus "runner" rejectees sur `Pod A` ne deviennent pas meilleures sur `Pod C`
- `Pod C` tient deja plus longtemps que `Pod A`; lui enlever son TP fixe degrade la courbe

2. Hausse du `min_confidence`

Tests:

- baseline `0.66`: `+26.11`
- `0.72`: `+26.11`
- `0.75`: `+26.11`

Conclusion:

- cette idee rejectee sur `Pod A` n'aide pas `Pod C`
- les trades effectivement ouverts sont deja dans la partie haute de la distribution de confiance

### Idees de `Pod A` prometteuses mais bloquees par manque de contexte

1. `pattern_vetoes` / `pattern_watchers`

Potentiel: eleve.

Blocage actuel:

- `Pod C` ne propage pas assez de features dans `setup_details`
- aujourd'hui on ne peut pas faire proprement:
  - analyse jour par jour
  - veto par pattern non symbolique
  - watchers cluster-aware

2. `structural_targets`

Potentiel: moyen a eleve sur `index` / `silver`.

Blocage actuel:

- `Pod C` n'a ni `swing_high`, ni `range_high`, ni structure MTF comparable a `Pod A`
- impossible donc de tester serieusement un TP structurel plutot qu'un TP multiple fixe

3. `reversal_fade`

Potentiel: faible a moyen, mais seulement apres enrichissement structurel.

Blocage actuel:

- l'edge valide de `Pod C` vient du filtrage long-only
- il n'y a aucune preuve qu'ajouter des shorts de rejection soit bon aujourd'hui

4. `campaign` / add-ons

Potentiel: faible a court terme.

Blocage actuel:

- `18` trades sur le replay courant
- pas assez de repetition pour justifier une logique d'add-on
- contrairement a `Pod A`, le probleme principal de `Pod C` n'est pas encore "je coupe trop mes gros gagnants"

## Pistes Priorisees

### Priorite 1

1. Enrichir `setup_details` de `Pod C`

Ajouter au minimum:

- `trend_bps`
- `structure_score`
- `vwap_distance_bps`
- `activity_ratio`
- `trade_count_ratio`
- `bucket_range_bps`
- `cluster_regime`
- `cluster_leader`
- `flow_alignment`

Objectif:

- rendre possible la meme boucle research -> veto -> watch que sur `Pod A`

2. Analyse jour par jour `Pod C`

Sorties voulues:

- par date
- par cluster
- par setup
- par side
- par regime de cluster
- par buckets de `trend_bps / structure_score / vwap_distance_bps / activity_ratio`

Objectif:

- identifier des patterns perdants non lies a un symbole precis
- surtout pour `oil`

3. Introduire des `pattern_vetoes` cluster-aware pour `Pod C`

Premier cas cible:

- `tradfi_continuation_long` sur `oil`

Pas sous forme:

- "bloquer `XYZ:CL`"

Mais plutot:

- "ne pas prendre le pullback oil si la qualite de flow/structure/reclaim est insuffisante"

### Priorite 2

4. Ajouter des `cluster_modes` pour `Pod C`

Equivalent conceptuel des `symbol_modes` de `Pod A`, mais par cluster:

- `oil_mode`
- `silver_mode`
- `index_mode`

Utilite:

- calibrer separement stop / TP / trailing / time-stop
- eviter un profil d'exit unique pour des microstructures tres differentes

5. Tester un `setup_runner` uniquement sur certains clusters

Le runner global est invalide.

En revanche, il peut rester un candidat:

- `silver` seulement
- `index` seulement

Jamais en promotion globale sans replay exact par cluster.

### Priorite 3

6. Ajouter un contexte structurel MTF a `Pod C`

Seulement apres avoir enrichi les snapshots / contextes.

Objectif:

- rendre testables `structural_targets`
- eventuellement `reversal_fade` sur rejection confirmee

7. Tester un `reversal_fade` ultra-strict par cluster

A ne faire qu'apres le point precedent.

Hypothese la plus plausible:

- `index` plus que `oil`

Etat actuel:

- aucune validation justifiant ce chantier en prod

## Conclusion

Le meilleur transfert actuel de `Pod A` vers `Pod C` n'est pas:

- plus de trailing
- plus d'exposition
- plus de complexite d'exit

Le bon transfert est:

- plus de contexte
- plus d'analyse
- plus de filtrage pattern-aware

En une phrase:

- `Pod A` gagne maintenant en ajoutant de la finesse sur un moteur deja riche
- `Pod C` gagnera surtout en ajoutant de la finesse de filtrage sur un moteur encore trop pauvre en contexte

## Update Phase 6.1

Etat implemente:

- `setup_details` de `Pod C` enrichis et propages jusqu'aux trades clos
- report cluster-aware jour-par-jour ajoute dans `app/research/pod_c_day_by_day_patterns.py`
- premier run sur `server-data/live_snapshots`:
  - `silver|silver_breakout_long`: `+23.65 USD`
  - `index|index_breakout_long`: `+10.00 USD`
  - `oil|oil_pullback_long`: `-7.54 USD`

Lecture:

- la premiere poche faible concrete a traiter n'est pas un symbole, mais un pattern `oil` de continuation
- candidat le plus net pour un futur veto cluster-aware:
  - `oil|supportive|strong|normal`: `-12.14 USD` sur `3` trades, `100%` de jours negatifs

## Update Phase 6.2

Validation exacte du premier veto `Pod C` sur `server-data/live_snapshots`:

- baseline sans veto: `+26.11 USD`
- watch-only `oil_pullback_long`: `+26.11 USD`
- veto precis `oil|supportive|strong|normal`: `+32.05 USD`
- veto large `oil_pullback_long`: `+33.65 USD`

Conclusion:

- le meilleur filtre promu n'est pas un blocage de symbole
- c'est un veto de branche:
  - `setup = tradfi_continuation_long`
  - `market_cluster = oil`
  - `cluster_strategy = oil_pullback_long`

Lecture strategique:

- `Pod C` confirme bien le pattern vu sur `Pod A`:
  - la suppression de branches faibles apporte plus que la sophistication des exits
- le prochain chantier logique n'est pas un `runner` plus agressif
- c'est un travail de calibration par cluster sur les branches deja gagnantes (`silver`, `index`)

## Update Phase 6.3

Calibration par cluster des exits sur `Pod C`:

- baseline actif: `+33.65 USD`
- `index_runner`: `+39.40 USD`
- `silver_runner`: `+31.87 USD`
- `index + silver`: `+37.62 USD`

Conclusion:

- `index` meritait bien un mode d'exit distinct
- `silver` non

Donc, nouveau profil promu:

- veto `oil_pullback_long`
- `cluster_mode` `index_runner`
- pas de `cluster_mode` sur `silver`

## Update Phase 6.4

Recherche d'un veto fin sur `index`:

- profil actif: `+39.40 USD`
- veto `index_soft_trend`: `+35.57 USD`
- veto `index_extension_entry`: `+36.21 USD`

Conclusion:

- `index` n'a pas encore de pattern perdant assez robuste pour etre coupe
- contrairement a `oil`, c'est une branche a observer, pas a filtrer

Decision:

- ajout de `watchers` seulement:
  - `index_soft_trend_watch`
  - `index_extension_entry_watch`

## Update Phase 6.5

Recherche d'une evolution utile sur `silver` sans recopier le `runner` de `Pod A`.

Validation exacte sur `server-data/live_snapshots`:

- baseline actif mis a jour: `+39.78 USD`
- `silver_tp_extend`: `+45.04 USD`
- `silver_defensive`: `+40.43 USD`
- `silver_size_boost`: `+37.40 USD`
- `silver_tp_extend_size_boost`: `+41.03 USD`

Lecture:

- `silver` ne voulait ni plus de sizing ni un stop plus serre
- en revanche, il monetisait trop tot une poche de trades deja tres propre
- un simple `take_profit_multiplier = 1.08` ameliore le cluster sans detruire son profil

Decision:

- promotion de `pod_c.cluster_modes.silver`
- scope minimal:
  - `allowed_setups = ["tradfi_continuation_long"]`
  - `take_profit_multiplier = 1.08`
- pas de changement de trailing ni de sizing sur `silver` pour l'instant

## Update Phase 6.6

Relecture `gold` et raffinement `index`.

Constat:

- `gold` n'a pas de flow ferme exploitable sur le dataset courant
- il ne sert donc a rien d'ajouter un mode `gold` pour l'instant

Validation exacte sur `server-data/live_snapshots` pour `index`:

- baseline actif mis a jour: `+45.04 USD`
- `index_time_extend`: `+44.33 USD`
- `index_tp_extend`: `+44.55 USD`
- `index_runner_looser`: `+45.32 USD`
- `index_tp_extend_tighter_trail`: `+46.25 USD`

Lecture:

- `index` supporte encore un peu plus d'ambition sur ses exits
- mais de maniere marginale et controlee
- la meilleure variante reste proche du mode deja promu, avec un TP plus large et un trailing un peu mieux cale

Decision:

- promotion du raffinage `index`
- nouvelle config:
  - `time_stop_hours = 9`
  - `take_profit_multiplier = 1.28`
  - `break_even_multiplier = 1.08`
  - `trailing_activation_multiplier = 1.30`
  - `trailing_distance_multiplier = 1.00`

## Update Phase 6.7

Le report jour-par-jour mis a jour ne montre toujours aucun pattern `index` perdant assez robuste pour meriter un veto supplementaire.

La bonne suite n'etait donc pas de couper `index`, mais de reouvrir `oil` proprement.

Autopsie `oil` sans veto global:

- `8` trades
- `-6.74 USD`
- pertes concentrees sur des pullbacks trop profonds ou trop crowded

Deux reconstructions ont ete testees en replay exact sur `server-data/live_snapshots`:

- baseline actif: `+46.25 USD`
- `oil_rebuild_v1`: `+57.62 USD`
- `oil_rebuild_v2`: `+55.84 USD`

Lecture:

- `oil` n'etait pas une branche a supprimer pour toujours
- c'etait une branche mal calibree
- la meilleure reconstruction est une branche `oil` tres selective:
  - pullback pas trop profond
  - activite deja elevee
  - flow positif mais pas excessif

Decision:

- suppression du veto global `oil_pullback_strategy`
- promotion d'un `oil_pullback_long` reconstruit directement dans `TradfiTrendService`
- regles promues:
  - `trend_bps >= 9.0`
  - `structure_score >= 0.24`
  - `trade_flow_bias >= 0.25`
  - `-2.6 <= vwap_distance_bps <= -1.0`
  - `activity_ratio >= 1.7`
  - `0.75 <= trade_flow_bias + book_imbalance <= 1.15`

## Update Phase 6.8

Validation multi-fenetres de `oil` et recherche d'un equivalent pour `gold`.

### `oil`

Validation exacte:

- `window_0413_0417`: `+52.22 -> +63.59 USD`
- `full_latest_fetch`: `+46.25 -> +57.62 USD`
- `window_0405_0412`: aucun trade dans les deux variantes

Lecture:

- `oil` reconstruit n'est pas juste un coup de chance sur la fenetre globale
- il ameliore aussi la fenetre recente utile, sans drawdown supplementaire

### `gold`

Premier prototype naive:

- oui en PnL
- non en drawdown et en frais

Refinage valide:

- `cluster_regime = TrendExpansion`
- `global_regime in {TrendExpansion, PanicSqueeze}`
- `trend_bps >= 8.0`
- `structure_score >= 0.22`
- `trade_flow_bias >= 0.02`
- `0.5 <= vwap_distance_bps <= 3.5`
- `activity_ratio >= 1.1`
- `bucket_range_bps >= 14.0`
- `spread_bps <= 2.0`

Validation exacte:

- `live_snapshots`: `+57.62 -> +84.98 USD`
- `window_0413_0417`: `+63.59 -> +90.95 USD`
- `full_latest_fetch`: `+57.62 -> +84.98 USD`
- drawdown stable a `4.22 USD`

Decision:

- `gold_breakout_long` promu dans le service
- `XYZ:GOLD` debloque dans la config
- pas de `cluster_mode.gold` pour l'instant
- on garde d'abord la branche simple qui gagne deja nettement

## Update Phase 6.9

Le test suivant consistait a verifier si `gold` avait deja besoin d'un mode d'exit dedie.

Validation exacte:

- `live_snapshots`: `+84.98 -> +89.79 USD`
- `window_0413_0417`: `+90.95 -> +95.76 USD`
- `full_latest_fetch`: `+84.98 -> +89.79 USD`
- drawdown stable a `4.22 USD`

Le meilleur mode teste est un `runner soft`, tres leger:

- `time_stop_hours = 6`
- `take_profit_multiplier = 1.08`
- `break_even_multiplier = 1.00`
- `trailing_activation_multiplier = 1.10`
- `trailing_distance_multiplier = 1.10`

Lecture:

- `gold` ne demandait pas une grosse refonte d'exit
- juste un peu plus d'espace pour monetiser ses rares trades gagnants

Decision:

- promotion de `pod_c.cluster_modes.gold`
- on garde une branche `gold` tres selective, avec un exit seulement un peu plus souple
