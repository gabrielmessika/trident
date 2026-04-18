# Crypto Refonte Plan

Date: `2026-04-17`

## Objectif

Construire une version crypto du bot deployable rapidement en production, avec une trajectoire credible vers un objectif tres agressif de `+50% sur 10 jours`.

Point d'honnetete:

- `+50% / 10 jours` est un objectif de type venture / prop desk, pas une cible raisonnablement garantie.
- La refonte doit donc viser d'abord:
  - un moteur qui capture vraiment les impulsions crypto
  - une forte reduction du churn et des faux signaux
  - une voie de mise en prod rapide avec garde-fous

## Diagnostic

Les analyses live et replay du `2026-04-15` au `2026-04-17` montrent:

- le marche crypto a bien monte, mais le bot a perdu car il coupe trop vite ses longs
- le regime global change trop souvent
- le routing inter-pods crypto cree du churn destructeur
- `Pod B` n'a pas d'edge robuste sur les impulsions recentes
- `Pod A` contient encore des setups qui detruisent le PnL (`vwap_reclaim_long`, puis `liquidity_sweep_reclaim_long` sur la fenetre recente)
- le stack actuel sait acheter certains pullbacks de continuation, mais ne sait pas encore faire proprement:
  - target long sur resistance structurelle
  - puis reversal short vers support quand le rejet est confirme

## Direction Cible

La refonte proposee n'est pas "plus d'indicateurs". Elle est architecturale.

### 1. Crypto Core Simplifie

Un seul moteur crypto principal en production:

- `Pod A` devient le moteur principal de capture de tendance
- `Pod B` sort du chemin critique et passe en `shadow-only`
- le supervisor cesse de faire du ping-pong intra-day entre pods crypto

### 2. Trading En Campagne

Le moteur crypto doit tenir des positions plus longtemps:

- moins de micro-trades
- stops moins fragiles, bases sur structure et ATR
- pyramiding/event-driven adds seulement si l'impulsion est confirmee
- sorties plus progressives, pas uniquement take-profit court ou stop sec

### 3. Regime Crypto Multi-Timeframe

Remplacer le regime global trop bruité par un regime crypto base sur:

- leadership `BTC/ETH`
- breadth crypto (nombre de symbols alignes)
- impulsion HTF (`15m / 1h / 4h`)
- dispersion inter-alts

Le regime doit piloter:

- autorisation des setups
- aggressivite du sizing
- filtres d'entree

### 4. Risk Layer En Ligne

Ajouter des garde-fous online par:

- `setup`
- `symbol + setup`
- session / jour

Quand une branche derive, elle doit se couper automatiquement sans attendre une intervention manuelle.

### 5. Structural Targets Et Reversal Fade

Ajouter un moteur structurel separe du trend-following pur:

- prendre les longs de continuation jusqu'a une resistance structurelle credible
- eviter les sorties purement basees sur des multiples fixes quand un niveau de marche est clairement visible
- autoriser un short de retracement seulement si le rejet de resistance est confirme
- viser ensuite un retour vers un support structurel, sans auto-flip aveugle

Les niveaux cibles devront venir des briques deja presentes:

- `range_high_1h` / `range_low_1h`
- `swing_high_1h` / `swing_low_1h`
- confirmations de BOS et de rejet

Ce moteur doit rester experimental tant qu'il n'a pas ete valide en replay complet, car en crypto les shorts contre impulsion se font souvent detruire.

## Etat Actuel Valide

Le profil de reference courant est:

- `config/trident_crypto_launch_fast_crypto_only.toml`
- `Pod A` seulement
- `trend_pullback_long` seulement
- `Pod B` hors chemin critique
- grace de `routing_revoked` etendue a `24h` sur les principaux symbols crypto effectivement trades
- `setup_runner` conservateur actif sur `trend_pullback_long`
- deux vetoes pattern actifs:
  - `trend1h_negative`
  - `trend4h_positive_cci_mid`
- trois patterns en `watch-only`:
  - `vwap_weak_trend4h_positive`
  - `vwap_weak`
  - `trend4h_flat`

Validation exacte sur `server-data/replay_inputs/full_bot_latest_fetch.jsonl`:

- baseline active sans `setup_runner`: `+385.44 USD`
- profil actif avec `setup_runner` conservateur: `+391.64 USD`
- gain net valide le plus recent: `+6.20 USD`
- lecture:
  - la variante conservatrice allonge le hold moyen (`0.8396h -> 1.371h`)
  - elle reduit les frais (`109.19 -> 95.81`)
  - elle ameliore la fenetre recente `2026-04-13 -> 2026-04-17` (`+133.36 -> +135.36`)
  - elle degrade legerement `2026-04-05 -> 2026-04-12` (`+341.74 -> +338.67`)
  - elle reste donc promue seulement en version prudente, pas en mode runner agressif

References:

- `server-data/replay_reports/pod_a_pattern_veto_exact_validation_20260417.md`
- `server-data/replay_reports/pod_a_pattern_watch_candidate_validation_20260417.md`
- `server-data/replay_reports/pod_a_pattern_watch_summary_20260417.md`
- `server-data/replay_reports/pod_a_setup_runner_validation_20260417.md`
- `server-data/replay_reports/campaign_addon_validation_20260417.md`
- `server-data/replay_reports/campaign_runner_refine_20260417.md`
- `server-data/replay_reports/pod_a_reversal_fade_validation_20260417.md`
- `server-data/replay_reports/pod_a_routing_grace_validation_20260417.md`
- `server-data/replay_reports/pod_a_setup_guardrail_validation_20260417.md`
- `server-data/replay_reports/pod_a_intraday_setup_guardrail_validation_20260417.md`
- `server-data/replay_reports/pod_a_structural_target_validation_20260417.md`

## Roadmap

### Phase 0. Launch Fast

Objectif: remettre le bot crypto sur une trajectoire positive le plus vite possible.

Actions:

- `Pod B` desactive en production
- `Pod A` limite a `trend_pullback_long`
- `vwap_reclaim_long` et `liquidity_sweep_reclaim_long` retires du launch profile
- cash plus eleve en `RangeAuction`
- garder `Pod C` independant si souhaite

Etat:

- implemente dans `config/trident_crypto_launch_fast.toml`
- variante la plus simple et la mieux validee a ce stade:
  - `config/trident_crypto_launch_fast_crypto_only.toml`
- support code ajoute pour `Pod A allowed_setups`
- deux vetoes pattern actifs ont ameliore le replay complet:
  - `trend1h_negative`
  - `trend4h_positive_cci_mid`
- trois patterns restent en observation seulement, pas en filtrage

### Phase 1. Risk Controls V2

Objectif: couper les derives en live.

Actions:

- garde-fou roulant `symbol + setup` sur `Pod A`
- kill-switch glissant par setup
- plafond de pertes intraday par branche
- tagging plus riche dans les journaux de rejection

Etat:

- garde-fou roulant `Pod A` implemente en code
- pas encore active par defaut dans le profil `launch-fast`
- kill-switch glissant global par setup implemente et valide
- resultat actuel:
  - aucun des reglages testes ne change le replay complet
  - il reste donc desactive dans le profil `launch-fast`
- kill-switch intraday par `date + setup` implemente et valide
- resultat actuel:
  - aucun des reglages testes, y compris un stress test agressif, ne change le replay complet
  - il reste donc desactive dans le profil `launch-fast`

### Phase 2. Campaign Engine

Objectif: transformer `Pod A` en vrai moteur de tendance crypto.

Actions:

- entree primaire sur pullback trend HTF
- add-on apres continuation validee
- stop structurel plus large
- trailing adaptatif selon ATR / impulse decay
- prise partielle uniquement quand le trade a deja prouve son edge

Etat:

- version `campaign` deja branchee pour `trend_pullback_long`
- utile mais pas encore transformante
- reste un moteur de continuation, pas encore un moteur de target structurelle
- un `setup_runner` explicite est maintenant branche pour `trend_pullback_long`
- variante promue:
  - `take_profit_multiplier = 0.0`
  - `break_even_multiplier = 1.0`
  - `trailing_activation_multiplier = 1.4`
  - `trailing_distance_multiplier = 0.8`
  - `min_confidence = 0.0`
- support `entry tranche + add-on unique` implemente derriere flag
- validation actuelle:
  - `campaign_addon_tight`: `+380.99 USD`
  - `campaign_relaxed_addon`: `+405.28 USD`
  - baseline sans `setup_runner`: `+385.44 USD`
  - profil actif avec `setup_runner` prudent: `+391.64 USD`
- lecture:
  - l'add-on ne se declenche pas encore assez pour justifier une promotion
  - le vrai gain recent vient du `setup_runner` prudent, pas de l'add-on
- decision:
  - laisser l'add-on desactive dans les profils `launch-fast`
  - garder l'infrastructure en code pour une future iteration si les signaux persistent davantage

### Phase 2b. Structural Targets & Pullback Reversal

Objectif: capturer le schema crypto frequent:

- impulsion
- extension vers resistance
- rejet eventuel
- retracement vers support

Actions:

- calculer une target structurelle long a partir de `swing_high_1h` / `range_high_1h`
- sortir le long un peu avant cette resistance quand elle est credible
- detecter un vrai rejet de resistance
- autoriser un short de retracement seulement si le rejet est confirme
- viser `swing_low_1h` / `range_low_1h` comme target short
- interdire l'auto-flip si le contexte reste en impulsion forte
- valider en replay complet recent et large avant toute activation

Etat:

- les briques de niveaux existent deja dans `Pod A`
- support experimental de target structurelle implemente
- resultat actuel:
  - les variantes simples de TP sur resistance degradent le replay complet
  - elles coupent trop tot les grosses journees de tendance
- support experimental `reversal_fade_short` implemente
- resultat actuel:
  - variante stricte: `+329.70 USD` soit `-22.23 USD` vs profil actif precedent
  - variante loose: `+321.13 USD` soit `-30.80 USD` vs profil actif precedent
- validation supplementaire `2026-04-18` sur des shorts "tres stricts":
  - utile pour redonner un peu d'activite sur une fenetre live tres courte ou `Pod A` restait muet
  - mais non promouvable en profil reel
  - sur `pod_a_last12h_20260418T0855.jsonl`:
    - `trend_short_strict_v2`: `-8.91 USD`
    - `liq_short_strict_v2`: `+6.36 USD`
    - `combo_shorts_strict_v2`: `+10.86 USD`
  - sur les replays larges versionnes:
    - baseline: `+335.68 USD`
    - `trend_short_strict_v2`: `+203.63 USD`
    - `liq_short_strict_v2`: `+257.19 USD`
    - `combo_shorts_strict_v2`: `+157.73 USD`
  - lecture:
    - meme tres filtres, les shorts degradent fortement `Pod A` sur la vraie fenetre large
    - le besoin d'activite intraday ne justifie donc pas leur activation live a ce stade
- decision:
  - garder `reversal_fade` desactive
  - ne pas promouvoir le short de retracement avant une logique de rejet confirmee plus robuste
  - ne pas rouvrir de shorts `Pod A` uniquement pour corriger des periodes sans trade

### Phase 2c. Moteur Short Separe

Objectif: construire plus tard un vrai moteur short, sans polluer `Pod A`.

Principes:

- ne pas reutiliser les shorts actuels de `Pod A` comme solution de secours
- separer completement la logique short du moteur `trend_pullback_long`
- rester `shadow-only` jusqu'a validation large
- utiliser des confirmations plus fortes que les longs:
  - rejet confirme
  - faiblesse de flow
  - invalidation structurelle nette
  - contexte de regime compatible

Actions:

- definir un moteur short dedie, avec ses propres setups et ses propres vetoes
- journaliser les signaux shadow short separement
- valider sur fenetre recente et large avant tout test live

Etat:

- inscrit au plan
- aucune promotion live tant qu'un moteur distinct n'a pas ete valide

### Phase 3. Crypto Regime V2

Objectif: supprimer les faux flips de regime.

Actions:

- nouveau regime crypto MTF
- score de breadth par panier d'alts
- score leader / laggard
- hysteresis plus forte
- validation sur replays multi-fenetres

### Phase 4. Analyse Jour Par Jour

Objectif: identifier precisement les patterns qui marchent, ceux qui degradent le PnL, et les conditions de marche associees.

Actions:

- decouper chaque replay par jour, regime, symbole et setup
- isoler les journees d'impulsion, de grind et de chop
- mesurer quels patterns tiennent sur plusieurs jours haussiers et lesquels rendent tout
- produire une shortlist de setups a renforcer, couper ou garder en shadow
- reinjecter ces conclusions dans `Regime V2`, les allowlists et les kill-switch

Etat:

- script et rapports jour-par-jour en place
- deux patterns robustement perdants ont ete promus en veto
- trois patterns potentiellement dangereux ont ete testes et gardes en `watch-only`

### Phase 4b. - réduire les `routing_revoked` restants hors `campaign`
Objectif: eliminer les sources de churn les plus evidentes hors campagne.
Actions:
- identifier les routes les plus revokees dans les journaux
- analyser pourquoi elles sont revokees (faux signal, regime change, etc)
- couper ou corriger ces routes pour qu'elles ne rentrent plus en jeu dans les conditions normales de marche
- valider que le nombre de `routing_revoked` hors campagne devient marginal
Etat:
- validation exacte faite sur `full_bot_latest_fetch.jsonl`
- `routing_grace_6h_all_traded`: `+379.41 USD`, `routing_revoked 18 -> 2`
- `routing_grace_24h_all_traded`: `+435.77 USD`, `routing_revoked 18 -> 1`
- decision:
  - promouvoir `24h` de grace de routing sur les principaux symbols crypto trades dans les profils `launch-fast`
  - ne pas toucher plus loin au routing tant qu'un nouveau diagnostic ne montre pas une fuite nette residuelle

## Prochaine Etape Recommandee

- Le prochain levier a tester n'est plus le routing ni l'add-on campagne.
- La meilleure piste restante est un `trend runner` plus configurable pour `trend_pullback_long` lui-meme:
  - sans TP fixe trop court
  - avec trailing plus tardif
  - et sans elargir trop les cas `campaign` qui rendent le PnL.

### Phase 5. Pod B Rebuild

Objectif: ne remettre `Pod B` en live que s'il a une edge nette.

Actions:

- conserver `Pod B` en shadow
- reconstruire autour d'un breakout HTF plus propre
- pas de retour en production sans validation claire

### Phase 6. Transfert Pod A -> Pod C

Objectif: reutiliser les bonnes idees de `Pod A` nouvelle version sur `Pod C`, sans copier les briques qui n'ont pas le bon contexte.

Photo courante sur `server-data/live_snapshots`:

- `Pod A`
  - `519` signaux
  - `186` acceptes
  - `98` trades clos
  - `+368.82 USD`
  - `80.05 USD` de frais
  - `52.65 USD` de drawdown max
  - hold moyen `1.0908h`
- `Pod C`
  - `150` signaux
  - `45` acceptes
  - `18` trades clos
  - `+26.11 USD`
  - `9.13 USD` de frais
  - `7.46 USD` de drawdown max
  - hold moyen `1.6176h`

Lecture:

- `Pod C` est deja plus selectif et tient deja plus longtemps ses positions que `Pod A`
- le vrai edge courant de `Pod C` vient du filtrage, pas d'une sophistication d'exit
- il ne faut donc pas lui copier en priorite les briques de type `runner`, `campaign` ou `reversal` sans d'abord enrichir son contexte

Actions:

- garder et renforcer la suppression des branches faibles deja validees dans `Pod C`
- enrichir `setup_details` et le contexte propage pour rendre `Pod C` observable comme `Pod A`
- lancer une analyse jour-par-jour `Pod C` par `cluster x setup x side x regime`
- promouvoir ensuite des `pattern_vetoes` et `pattern_watchers` cluster-aware sur `Pod C`
- seulement apres cela:
  - tester des `cluster_modes`
  - re-tester un `setup_runner` par cluster
  - re-tester des `structural_targets`
  - re-tester un `reversal_fade` ultra-strict

Etat:

- transferts deja valides:
  - oui a la suppression brutale des branches faibles
  - preuve actuelle:
    - `cluster_aware_v2 = true`: `+26.11 USD`, `18` trades, `7.46 USD` de drawdown
    - `cluster_aware_v2 = false`: `-63.66 USD`, `73` trades, `87.27 USD` de drawdown
- transferts testes et rejetes a ce stade:
  - `setup_runner` global de type `Pod A`
    - baseline `Pod C`: `+26.11 USD`
    - runner soft: `+22.92 USD`
    - runner type `Pod A`: `+15.02 USD`
  - hausse globale du `min_confidence`
    - `0.66`, `0.72`, `0.75`: aucun impact, replay identique
- transferts prometteurs mais bloques par manque de contexte:
  - `pattern_vetoes`
  - `pattern_watchers`
  - `structural_targets`
  - `reversal_fade`
  - `cluster_modes`
- Phase 6.1 demarree:
  - enrichment de `setup_details` dans `Pod C`
  - propagation jusqu'aux trades clos et journaux live/backtest
  - report jour-par-jour cluster-aware ajoute
  - premiere lecture sur `server-data/live_snapshots`:
    - `silver|silver_breakout_long`: `+23.65 USD`
    - `index|index_breakout_long`: `+10.00 USD`
    - `oil|oil_pullback_long`: `-7.54 USD`
    - pattern perdant le plus net: `oil|supportive|strong|normal` a `-12.14 USD` sur `3` trades, `100%` de jours negatifs
  - decision:
    - ne pas bloquer `oil` par symbole
    - prioriser un futur veto cluster-aware sur certains buckets `oil` seulement apres validation replay exacte
- Phase 6.2 validee:
  - support `pattern_vetoes` et `pattern_watchers` ajoute a `Pod C`
  - validation exacte sur `server-data/live_snapshots`:
    - baseline sans veto: `+26.11 USD`
    - watch-only `oil_pullback_long`: `+26.11 USD`
    - veto precis `oil|supportive|strong|normal`: `+32.05 USD`
    - veto large `oil_pullback_long`: `+33.65 USD`
  - decision:
    - promouvoir le veto cluster-aware `oil_pullback_long`
    - ne pas activer le veto plus fin `oil|supportive|strong|normal` pour l'instant
    - continuer a traiter `oil` comme une branche a reconstruire plutot qu'a elargir
- Phase 6.3 validee:
  - support `cluster_modes` ajoute a `Pod C`
  - validation exacte sur `server-data/live_snapshots`:
    - baseline actif: `+33.65 USD`
    - `index_runner`: `+39.40 USD`
    - `silver_runner`: `+31.87 USD`
    - `index + silver`: `+37.62 USD`
  - decision:
    - promouvoir `pod_c.cluster_modes.index`
    - ne pas activer de `cluster_mode` sur `silver`
    - garder `silver` sur ses exits actuels tant qu'une autre variante ne bat pas `+23.65 USD`
- Phase 6.4 validee:
  - recherche d'un veto pattern-aware plus fin sur `index`
  - validation exacte sur `server-data/live_snapshots`:
    - profil actif: `+39.40 USD`
    - veto `index_soft_trend`: `+35.57 USD`
    - veto `index_extension_entry`: `+36.21 USD`
  - decision:
    - aucun veto `index` n'est promu
    - ajouter seulement des `watchers`:
      - `index_soft_trend_watch`
      - `index_extension_entry_watch`
    - garder `index` en `flow gagnant`, pas en `flow a couper`
- Phase 6.5 validee:
  - sweep dedie `silver` sur `server-data/live_snapshots`
  - baseline actif mis a jour: `+39.78 USD`
  - `silver_tp_extend`: `+45.04 USD`
  - `silver_defensive`: `+40.43 USD`
  - `silver_size_boost`: `+37.40 USD`
  - `silver_tp_extend_size_boost`: `+41.03 USD`
  - decision:
    - promouvoir `pod_c.cluster_modes.silver`
    - ne changer que `take_profit_multiplier = 1.08`
    - ne pas toucher au sizing ni au trailing `silver` pour l'instant
- Phase 6.6 validee:
  - relecture `gold`:
    - pas de flow ferme exploitable sur le dataset courant
    - aucune promotion `gold` n'est justifiee a ce stade
  - raffinage du mode `index` sur `server-data/live_snapshots`
  - baseline actif mis a jour: `+45.04 USD`
  - `index_time_extend`: `+44.33 USD`
  - `index_tp_extend`: `+44.55 USD`
  - `index_runner_looser`: `+45.32 USD`
  - `index_tp_extend_tighter_trail`: `+46.25 USD`
  - decision:
    - promouvoir un `index` un peu plus ambitieux mais encore propre
    - config retenue:
      - `time_stop_hours = 9`
      - `take_profit_multiplier = 1.28`
      - `break_even_multiplier = 1.08`
      - `trailing_activation_multiplier = 1.30`
      - `trailing_distance_multiplier = 1.00`
- Phase 6.7 validee:
  - aucun pattern `index` perdant assez robuste n'apparait sur le report jour-par-jour mis a jour
  - l'effort a donc ete deplace sur la reconstruction de `oil`
  - autopsie sans veto:
    - `oil`: `-6.74 USD` sur `8` trades
    - poches perdantes surtout sur des pullbacks trop profonds ou trop crowded
  - reconstruction testee en replay exact:
    - baseline actif: `+46.25 USD`
    - `oil_rebuild_v1`: `+57.62 USD`
    - `oil_rebuild_v2`: `+55.84 USD`
  - config promue dans le service:
    - `trend_bps >= 9.0`
    - `structure_score >= 0.24`
    - `trade_flow_bias >= 0.25`
    - `-2.6 <= vwap_distance_bps <= -1.0`
    - `activity_ratio >= 1.7`
    - `0.75 <= trade_flow_bias + book_imbalance <= 1.15`
  - decision:
    - supprimer le vieux veto global `oil_pullback_strategy`
    - promouvoir la branche `oil` reconstruite au niveau du service
    - garder `oil` comme branche tres selective, pas comme cluster a elargir
- Phase 6.8 validee:
  - robustesse multi-fenetres de `oil` reconstruite:
    - `window_0413_0417`: `+52.22 -> +63.59 USD`
    - `full_latest_fetch`: `+46.25 -> +57.62 USD`
    - `window_0405_0412`: aucun trade dans les deux variantes
  - `gold` d'abord reteste en ouverture naive:
    - gain brut oui, mais drawdown trop eleve
  - `gold` ensuite raffine avec une branche beaucoup plus selective:
    - `cluster_regime = TrendExpansion`
    - `global_regime in {TrendExpansion, PanicSqueeze}`
    - `trend_bps >= 8.0`
    - `structure_score >= 0.22`
    - `trade_flow_bias >= 0.02`
    - `0.5 <= vwap_distance_bps <= 3.5`
    - `activity_ratio >= 1.1`
    - `bucket_range_bps >= 14.0`
    - `spread_bps <= 2.0`
  - validation exacte:
    - `live_snapshots`: `+57.62 -> +84.98 USD`
    - `window_0413_0417`: `+63.59 -> +90.95 USD`
    - `full_latest_fetch`: `+57.62 -> +84.98 USD`
    - drawdown reste `4.22 USD`
  - decision:
    - debloquer `XYZ:GOLD`
    - promouvoir `gold_breakout_long` dans `TradfiTrendService`
    - garder `gold` sur une logique tres selective, sans mode d'exit specifique pour l'instant
- Phase 6.9 validee:
  - test d'un `cluster_mode.gold` dedie sur la branche `gold_breakout_long`
  - variantes testées:
    - `gold_runner_soft`
    - `gold_tp_extend`
    - `gold_tighter_trail`
  - validation exacte:
    - `live_snapshots`: `+84.98 -> +89.79 USD`
    - `window_0413_0417`: `+90.95 -> +95.76 USD`
    - `full_latest_fetch`: `+84.98 -> +89.79 USD`
    - drawdown stable `4.22 USD`
  - config promue:
    - `time_stop_hours = 6`
    - `take_profit_multiplier = 1.08`
    - `break_even_multiplier = 1.00`
    - `trailing_activation_multiplier = 1.10`
    - `trailing_distance_multiplier = 1.10`
  - decision:
    - promouvoir `pod_c.cluster_modes.gold`
    - garder un mode simple et leger, sans reouvrir un chantier de trailing plus complexe

Decision:

- pour `Pod C`, la bonne transposition de `Pod A` n'est pas "plus de trailing"
- la bonne transposition est:
  - plus de contexte
  - plus d'analyse jour-par-jour
  - plus de filtrage pattern-aware

References:

- `docs/pod_c_vs_pod_a_transfer_20260418.md`
- `server-data/replay_reports/pod_c_day_by_day_patterns_20260418.md`
- `server-data/replay_reports/pod_c_pattern_veto_validation_20260418.md`
- `server-data/replay_reports/pod_c_cluster_mode_validation_20260418.md`
- `server-data/replay_reports/pod_c_index_pattern_validation_20260418.md`
- `server-data/replay_reports/pod_c_silver_mode_validation_20260418.md`
- `server-data/replay_reports/pod_c_index_mode_refine_20260418.md`
- `server-data/replay_reports/pod_c_oil_rebuild_validation_20260418.md`
- `server-data/replay_reports/pod_c_oil_multiframe_validation_20260418.md`
- `server-data/replay_reports/pod_c_gold_prototype_validation_20260418.md`
- `server-data/replay_reports/pod_c_gold_multiframe_validation_20260418.md`
- `server-data/replay_reports/pod_c_gold_refine_validation_20260418.md`
- `server-data/replay_reports/pod_c_gold_cluster_mode_validation_20260418.md`
- `server-data/reviews/pod_c_cluster_experiment_20260414.json`
- `server-data/reviews/pod_c_cluster_v2_integration_20260414.json`
- `server-data/reviews/pod_c_long_only_experiment_20260414.json`
- `docs/pod_c_research_latest.md`

## Critere De Go-Live

Le launch rapide peut etre deploye avant la refonte complete si:

- replay recent (`15 -> 17 avril 2026`) positif
- replay elargi (`5 -> 17 avril 2026`) au moins non degradant vs baseline
- `Pod B` hors chemin critique
- journaux live capables d'expliquer toute entree et toute sortie

## Implémentation Demarree

Fait dans ce lot:

- plan de refonte ecrit
- `Pod A allowed_setups` ajoute
- garde-fou roulant `Pod A` ajoute
- branchement live/backtest du garde-fou ajoute
- profil `config/trident_crypto_launch_fast.toml` ajoute
- profil `config/trident_crypto_launch_fast_crypto_only.toml` ajoute
- mode `Pod A campaign` ajoute pour `trend_pullback_long` crypto
- analyse jour par jour ajoutee au plan de refonte
- `Crypto Regime V2` demarre avec enrichissement de snapshot et confirmations plus strictes
- `Crypto Regime V2` reste derriere le flag experimental `trident.regime.crypto_v2_enabled`
- réduire les `routing_revoked` restants hors `campaign`
- analyse jour par jour implemente avec rapports dedies
- support generique de `pattern_vetoes` ajoute a `Pod A`
- `trend1h_negative` active en prod-like
- `trend4h_positive_cci_mid` active en prod-like
- support `pattern_watchers` ajoute pour suivre les patterns suspects sans les filtrer
- `vwap_weak_trend4h_positive`, `vwap_weak` et `trend4h_flat` laisses en observation seulement
- replay complet relance sur `full_bot_latest_fetch.jsonl` avec resultat stable `+351.93 USD`
- support de `setup_guardrail` global ajoute a `Pod A`
- validation exacte du `setup_guardrail` faite: aucun impact sur le replay complet
- support de `intraday_setup_guardrail` ajoute a `Pod A`
- validation exacte du `intraday_setup_guardrail` faite: aucun impact sur le replay complet, meme en stress test
- support de `structural_targets` ajoute a `Pod A`
- validation exacte des `structural_targets` faite: degradation du replay complet, feature gardee desactivee
- comparaison `Pod C` vs `Pod A` nouvelle version faite sur `server-data/live_snapshots`
- phase `Transfert Pod A -> Pod C` ajoutee au plan
- premiere shortlist des transferts valides / rejetes / bloques sur `Pod C` formalisee
- `Pod C` Phase 6.1 implementee:
  - `setup_details` riches
  - report cluster-aware jour-par-jour
  - premiere shortlist de patterns perdants non symboliques sur `oil`
- `Pod C` Phase 6.2 implementee:
  - support de `pattern_vetoes/watchers`
  - veto `oil_pullback_long` promu dans la config
  - replay exact valide a `+33.65 USD` vs `+26.11 USD` baseline
- `Pod C` Phase 6.3 implementee:
  - support de `cluster_modes`
  - `index_runner` promu
  - replay exact valide a `+39.40 USD` vs `+33.65 USD` baseline actif
- `Pod C` Phase 6.4 implementee:
  - aucun veto `index` promu apres validation exacte
  - deux `watchers` `index` ajoutes a la config
- `Pod C` Phase 6.5 implementee:
  - `cluster_mode` `silver` promu avec `take_profit_multiplier = 1.08`
  - replay exact valide a `+45.04 USD` vs `+39.78 USD` baseline actif
- `Pod C` Phase 6.6 implementee:
  - `cluster_mode` `index` raffine
  - replay exact valide a `+46.25 USD` vs `+45.04 USD` baseline actif
  - aucun chantier `gold` promu faute d'echantillon ferme exploitable
- `Pod C` Phase 6.7 implementee:
  - report jour-par-jour mis a jour: aucun veto `index` supplementaire a promouvoir
  - `oil` reconstruit directement dans le service au lieu d'etre bloque globalement
  - veto `oil_pullback_strategy` retire de la config
  - replay exact valide a `+57.62 USD` vs `+46.25 USD` baseline actif
- `Pod C` Phase 6.8 implementee:
  - robustesse multi-fenetres de `oil` validee
  - `gold_breakout_long` ajoute au service
  - `XYZ:GOLD` debloque dans la config
  - replay exact valide a `+84.98 USD` vs `+57.62 USD` baseline actif
  - validation recente `0413_0417` a `+90.95 USD`
- `Pod C` Phase 6.9 implementee:
  - `cluster_mode.gold` promu
  - replay exact valide a `+89.79 USD` vs `+84.98 USD` baseline actif
  - validation recente `0413_0417` a `+95.76 USD`

Prochaine etape recommandee:

1. faire evoluer `Phase 2b` vers un modele de rejet confirme + reversal fade, au lieu d'un simple TP direct sur resistance
2. analyser et reduire les `routing_revoked` restants hors `campaign`
3. lancer `Pod C` Phase 6.10: verifier s'il reste un vrai levier `equity/fx`, sinon geler `Pod C` et revenir sur `Crypto Regime V2`
4. revalider ensuite `Crypto Regime V2` une fois le stack de setups stabilise
