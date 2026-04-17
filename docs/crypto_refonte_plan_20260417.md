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
- deux vetoes pattern actifs:
  - `trend1h_negative`
  - `trend4h_positive_cci_mid`
- trois patterns en `watch-only`:
  - `vwap_weak_trend4h_positive`
  - `vwap_weak`
  - `trend4h_flat`

Validation exacte sur `server-data/replay_inputs/full_bot_latest_fetch.jsonl`:

- baseline sans pattern veto: `+311.89 USD`
- profil actif apres pattern vetoes + routing grace etendue: `+435.77 USD`
- gain net valide: `+123.88 USD`

References:

- `server-data/replay_reports/pod_a_pattern_veto_exact_validation_20260417.md`
- `server-data/replay_reports/pod_a_pattern_watch_candidate_validation_20260417.md`
- `server-data/replay_reports/pod_a_pattern_watch_summary_20260417.md`
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
- support `entry tranche + add-on unique` implemente derriere flag
- validation actuelle:
  - `campaign_addon_tight`: `+380.99 USD`
  - `campaign_relaxed_addon`: `+405.28 USD`
  - baseline actif: `+435.77 USD`
- lecture:
  - l'add-on ne se declenche pas encore sur la fenetre de validation
  - aucun scenario teste ne produit de gain net vs baseline
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
- decision:
  - garder `reversal_fade` desactive
  - ne pas promouvoir le short de retracement avant une logique de rejet confirmee plus robuste

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

Prochaine etape recommandee:

1. faire evoluer `Phase 2b` vers un modele de rejet confirme + reversal fade, au lieu d'un simple TP direct sur resistance
2. analyser et reduire les `routing_revoked` restants hors `campaign`
3. revalider ensuite `Crypto Regime V2` une fois le stack de setups stabilise
