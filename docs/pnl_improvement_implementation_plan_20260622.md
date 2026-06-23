# Plan d'implementation PnL - TRIDENT A/C et TRIDENT-HIP4

Date: 2026-06-22

Objectif principal: augmenter le PnL sans couper les bots. Le but n'est pas de
reduire l'activite a zero, mais de mieux choisir, mieux dimensionner et mieux
executer les trades.

Perimetre:

- Bot TRIDENT A/C: Pod A crypto core et Pod C tradfi builder-dex.
- Bot TRIDENT-HIP4: Pod B / `HIP4OutcomeEdgePod` en mainnet paper.
- Aucun passage live, changement de cap prod ou deploiement n'est inclus par ce
  document. Toute action pouvant envoyer de vrais ordres doit rester soumise a
  confirmation explicite.

## Constat recent

### TRIDENT A/C

L'infrastructure A/C est saine, mais le profil PnL est defavorable. Pod A reste
le principal probleme: les pertes sont frequentes et souvent superieures a 1$,
alors que les gains sont plus rares et souvent inferieurs a 1$. Les stops et les
sorties d'echec precoce concentrent une grande partie du drawdown, pendant que
les trailing stops restent l'une des rares sources de convexite positive.

Priorite A/C: conserver la capacite a trader, mais reduire l'exposition dans les
contextes ou le bot paie cher son erreur. Les meilleurs candidats sont donc des
garde-fous progressifs: sizing, probation symbole, qualite d'execution, reference
externe et microstructure, plutot qu'un arret global.

### TRIDENT-HIP4

HIP4 est plus encourageant, surtout depuis le passage en politique
`prob_stop_full`, mais le dossier ne justifie pas encore un passage live. Les
metriques agregees restent trop proches des seuils de refus: profit factor et
Brier doivent etre stabilises par bucket, pas seulement sur la periode recente.

Priorite HIP4: continuer le paper actif, enrichir la qualite des donnees et
tester les shadows qui semblent favorables, mais ne promouvoir que ce qui bat le
paper actif sur des settlements reels, avec couts et contraintes de fill.

## Recommandations reprises de l'analyse serveur

Cette section garde la trace directe des conclusions de la review serveur du
2026-06-22. Les items correspondants sont detailles ensuite dans le backlog.

### TRIDENT A/C

| Recommandation review | Traduction dans ce plan |
| --- | --- |
| Ne pas couper A/C: continuer a trader, mais mieux controler les pertes. | Backlog oriente cap-only, shadow et instrumentation plutot que kill-switch global. |
| Promouvoir prudemment P1-08 dynamic symbol guard. | `A-PNL-01`: promotion soft uniquement, avec `cap-reduced` en demi-cap et `quarantine` en tres petit cap ou no-new-entry temporaire. |
| Reduire le sizing Pod A des gros tickets et des contextes degradés. | `A-PNL-02`: echelle de notional par etat symbole, retour progressif au cap seulement apres expectancy/PF rolling positifs. |
| Neutraliser ou capper le boost A-grade live avant de l'etendre. | `A-PNL-03`: replay/shadow avec boost neutralise, sans changement live direct. |
| Ne pas traiter le cap comme unique cause: les trades acceptes sont deja cap-aware, mais le payoff reste mauvais. | `A-PNL-01`, `A-PNL-02`, `A-PNL-05` et `A-PNL-07`: combiner sizing, microstructure et qualite d'execution. |
| Surveiller/throttler les symboles qui concentrent les pertes recentes, notamment ENA, SOL, ZEC et LINK. | `A-PNL-01` et `A-PNL-08`: probation par symbole, hysteresis et rehabilitation, sans blocklist statique brutale. |
| Ne pas durcir `early_failure_exit` sans savoir s'il coupe des recoveries. | `A-PNL-04`: MFE/MAE post-sortie et PnL contrefactuel avant toute promotion. |
| Pod C: ne pas extrapoler P1-09 oil trop vite, car l'echantillon reste faible et l'unrealized doit compter. | `C-PNL-01`: stoplight oil avec closed + open mark-to-market avant toute hausse d'exposition. |

### Replay dedie P1-08 live sizing

Replay local lance apres redeploiement A/C live/mainnet sans activation:
`server-data/replay_reports/p108_dynamic_symbol_guard_live_sizing_halfsize_20260622T102000Z/`.

Fenetre: `2026-05-14T00:00:00Z` -> `2026-06-22T10:14:00Z`, live caps actifs,
`45782` records rejoues. Le resultat est `research_only_no_live_change`:

| Scenario | Total A/C | Delta | Pod A | Trades Pod A | PF Pod A | Max DD Pod A | Decision |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `current_ac` | `-40.19` | `+0.00` | `-31.54` | `88` | `0.6756` | `41.14` | Baseline live. |
| `live_sizing_55_75_cap50_cap50` | `-37.28` | `+2.91` | `-28.63` | `88` | `0.6879` | `37.37` | Candidat demi-cap, pas live encore. |
| `live_sizing_55_75_cap50_cap10_rejected` | `-47.11` | `-6.92` | `-38.46` | `71` | `0.4767` | `41.05` | Rejete: ne pas activer. |

Conclusion: ne pas activer `dynamic_symbol_guard_live_sizing_enabled` sur la
variante `quarantine=0.10`. La config locale dormante a ete ramenee a
`dynamic_symbol_guard_quarantine_multiplier=0.50` pour que la prochaine
activation candidate corresponde au seul replay positif, mais le flag reste
`false` et ce patch n'a pas ete redeploye apres le replay. Avant toute activation
live, il faut une confirmation explicite, un audit adapte au fait que
`symbol_guard_live_action_unchanged=false` devienne normal quand la policy agit,
et un fetch/review post-deploiement.

### Implementation A-PNL-01 audit P1-08 live-action

Implementation locale du 2026-06-23, sans activation live automatique:

- `scripts/fetch_trident_data.sh` distingue maintenant, dans l'audit P1-08,
  `expected_live_action_changed` et `unexpected_live_action_changed`.
- En shadow ou policy desactivee, `symbol_guard_live_action_unchanged=false`
  reste un `FAIL`. Si `dynamic_symbol_guard_live_sizing_enabled=true` est
  explicitement present dans la config rapatriee et que le plan porte
  `dynamic_symbol_guard_live_sizing_active=true`, le changement est compte comme
  attendu et ne fait plus echouer la review.
- Le rapport P108 expose aussi la config policy rapatriee, les records avec
  policy active, les records cappes, les raisons de sizing et les eventuelles
  actions recovery. Cela rend possible une review post-deploiement sans confondre
  activation volontaire et regression shadow.
- Review locale:
  `server-data/reviews/20260623T091959Z/`, status global `PASS`. P1-08:
  `2000/2000` records avec shadow, `live_action_unchanged_false=0`,
  `expected_live_action_changed=0`, `unexpected_live_action_changed=0`.
- Point de vigilance promotion: la config serveur rapatriee dans
  `server-data/config/trident.toml` a encore
  `dynamic_symbol_guard_quarantine_multiplier=0.10` avec le flag desactive,
  alors que la config locale candidate est revenue a `0.50`. Avant toute
  activation, il faut redeployer explicitement la variante `cap50/cap50` et
  verifier dans la review que la config serveur expose bien `0.50/0.50`.

### Implementation A-PNL-02 recovery sizing dormant

Implementation locale du 2026-06-22, sans activation live automatique:

- `PodARiskGate` expose maintenant des stats rolling par couple
  `symbol/setup`: nombre de trades fermes, PnL, expectancy et profit factor.
- Le live runner Pod A attache ces stats aux `setup_details` des plans; c'est
  une instrumentation shadow exploitable par les exports d'audit.
- Une policy dormante `dynamic_symbol_guard_recovery_sizing_enabled=false` peut
  appliquer une echelle de notional progressive:
  - etat degrade `throttle/quarantine`: multiplicateurs P1-08 existants;
  - etat normal non prouve: `recovery_base_multiplier=0.70`;
  - recovery partielle: `recovery_partial_multiplier=0.85`;
  - plein sizing seulement si `rolling_trades>=4`,
    `rolling_profit_factor>=1.05` et `rolling_expectancy_usd>0`.
- Le flag reste `false` dans `config/trident.toml`; aucun cap live, service,
  ordre, deploy ou comportement d'execution actif n'est modifie.
- Le replay P1-08 inclut maintenant le scenario counterfactual
  `live_sizing_recovery_55_75_base70_partial85` afin de comparer A-PNL-02 a la
  baseline courante et au candidat P1-08 demi-cap avant toute activation.
- Replay full-window:
  `server-data/replay_reports/p108_recovery_sizing_20260622/`, fenetre
  `2026-05-14T00:00:00Z` -> `2026-06-22T10:14:00Z`, `45782` records.
  Resultat: `research_only_no_live_change`. A-PNL-02 `base70/partial85`
  ameliore la baseline courante (`-37.40` vs `-40.19`, delta `+2.79`, Pod A
  `-28.75`, PF `0.6866`, max DD `37.49`) mais ne bat pas le candidat plus
  simple P1-08 `cap50/cap50` (`-37.28`, delta `+2.91`, Pod A `-28.63`,
  PF `0.6879`, max DD `37.37`) et applique davantage de reductions de cap
  (`4694` vs `3762`). Ne pas promouvoir cette variante telle quelle.

### Implementation A-PNL-03 A-grade headroom cap dormant

Implementation locale du 2026-06-22, sans activation live automatique:

- La piste P1-05 `strong_frozen_1p00` avait deja ete testee le 2026-06-15 et
  rejetee comme changement live: le replay ne montrait pas de gain materiel au
  freeze strong. A-PNL-03 ne reprend donc pas ce freeze comme proposition live.
- Une variante dormante `a_grade_size_headroom_cap_enabled=false` est ajoutee:
  elle garde le label A-grade et les exits elargis, mais si le flag est active,
  le scale de taille applique ne peut pas depasser la marge symbole ni le risk
  budget initial calcules avant boost.
- Les `setup_details` exposent `a_grade_requested_size_scale`,
  `a_grade_size_headroom_cap_active`, les raisons de cap et les budgets de cap;
  `scripts/export_trident_audit_pack.py` les exporte dans les closed trades.
- Le replay P105 accepte maintenant `--scenarios` et `--windows` pour rejouer
  seulement les variantes utiles. Nouveau scenario:
  `headroom_cap_current`.
- Replay live-window filtre:
  `server-data/replay_reports/p105_a_grade_headroom_cap_live_20260622/`,
  fenetre `2026-05-14T00:00:00Z` -> `2026-06-22T10:14:00Z`, `45782` records.
  Resultat: `research_only_no_live_change`. `headroom_cap_current` cappe `69`
  trades A-grade et ameliore tres legerement le courant (`-40.11` vs `-40.19`,
  delta `+0.08`; Pod A `-31.46` vs `-31.54`; PF `0.6762` vs `0.6756`; max DD
  `41.06` vs `41.14`). Effet trop faible pour une promotion live.

### Implementation A-PNL-04 early_failure_exit post-exit audit

Implementation locale du 2026-06-22, sans activation live automatique:

- Verification prealable: P1-02 avait deja teste la sensibilite globale des
  exits et `early_failure_enabled`; A-PNL-04 ne repropose donc pas de desactiver
  ou relaxer EFE globalement.
- Nouveau replay/audit `scripts/run_p116_early_failure_post_exit_audit.py`: il
  prend uniquement les trades Pod A effectivement fermes par
  `early_failure_exit`, desactive seulement ce trigger dans une simulation
  per-trade, puis suit le trade jusqu'au prochain stop/trailing/break-even/time
  stop/stop catastrophe naturel.
- Le rapport sort `early_failure_post_exit_summary.csv`,
  `early_failure_post_exit_trades.csv`,
  `p116_early_failure_post_exit_audit.json` et
  `p116_early_failure_post_exit_audit.md`, avec MFE/MAE total, MFE/MAE apres la
  sortie EFE originale, delai supplementaire, winners manques, pertes reduites
  manquees et pertes evitees par EFE.
- Replay complet live:
  `server-data/replay_reports/p116_early_failure_post_exit_20260622/`, base
  full-bot courante no-dedupe `-40.19` A/C (`-31.54` Pod A) sur
  `2026-05-14T00:00:00Z -> 2026-06-22T10:14:00Z`.
- Resultat P116 sur `41` trades EFE: PnL original `-72.38`, PnL naturel sans
  EFE `-80.84`, delta `-8.46`. EFE coupe `6` winners futurs et `6` pertes qui
  auraient ete moins mauvaises (`19.30` USD de recovery manquee), mais evite
  `29` degradations (`27.76` USD de perte evitee). Les sorties naturelles sans
  EFE auraient ete `17` catastrophic stops, `17` stop hits, `6` trailing stops
  et `1` break-even stop.
- Decision: `research_only_no_live_change`. Ne pas relaxer/desactiver EFE
  globalement; l'audit suggere plutot une piste future de classifieur de
  recovery tres cible, a comparer contre cette baseline.

### Implementation A-PNL-05 microstructure entry score shadow

Implementation locale du 2026-06-22, sans activation live automatique:

- Pod A attache maintenant a chaque signal un score
  `microstructure_shadow_score` borne `0..1`, avec bucket
  `poor/weak/ok/strong` et sous-scores spread, flow, microprice, profondeur,
  activite, range et churn.
- Le contexte Pod A consomme les champs microstructure deja presents dans
  `SymbolMarketSnapshot`: `bucket_notional_usd`, delta spread/flow/book,
  ratios volume/trades, depth 10bps, velocities et
  `microprice_dislocation_bps`.
- `scripts/export_trident_audit_pack.py` expose les champs
  `microstructure_shadow_*` et les champs counterfactual `p115_*` dans les
  closed trades.
- Nouveau replay `scripts/run_p115_microstructure_entry_replay.py`: baseline
  A/C + live-window, scenarios `current`, `micro_cap_poor50_lt42` et
  `micro_cap_weak50_lt56`. Les scenarios changent uniquement le sizing Pod A
  contrefactuel; aucun trade n'est bloque et aucun flag live n'est ajoute.
- Replay complet:
  `server-data/replay_reports/p115_microstructure_entry_20260622/`.
  Baseline avril/mai: aucun changement PnL (`77.08` total, Pod A `56.72`) meme
  avec `57` plan caps et `4` trades fermes cappes sur `<0.56`. Fenetre live
  `2026-05-14T00:00:00Z -> 2026-06-22T10:14:00Z`: `micro_cap_poor50_lt42`
  degrade de `-1.02` A/C (`-41.21` vs `-40.19`, Pod A `-32.56`, PF `0.6651`,
  DD `42.16`), et `micro_cap_weak50_lt56` degrade legerement de `-0.13` A/C
  (`-40.32`, Pod A `-31.67`, PF `0.6701`, DD `41.29`).
- Lecture bucket live: le bucket `poor` est gagnant (`+9.61` courant, `2/2`
  winners) et le pire PnL vient du bucket `strong` (`-21.62`). Le score est
  donc utile comme instrumentation/audit, mais la variante cap-only sur mauvais
  score est rejetee en `research_only_no_live_change`.

### Implementation A-PNL-07 fill-quality audit

Implementation locale du 2026-06-23, sans activation live automatique:

- Nouveau replay/audit `scripts/run_p117_fill_quality_audit.py`: il rejoue Pod A
  sur les snapshots live, ecrit un journal compact signal/trade, puis mesure par
  signal le cout d'entree attendu, spread, profondeur cote touche/10 bps, age des
  references disponibles, signaux acceptes mais non ouverts, rejets risk gate, et
  retours directionnels 1/5/15 minutes avec MFE/MAE court terme.
- `PodABacktestRunner` expose maintenant `skip_reason` dans le journal backtest
  et peut desactiver l'ecriture des `signal_review` filtres quand un audit n'en
  a pas besoin. Le comportement par defaut reste inchange.
- `scripts/export_trident_audit_pack.py` exporte aussi `execution.skip_reason`
  pour les logs live/fetches futurs.
- Replay full-window:
  `server-data/replay_reports/p117_fill_quality_audit_20260623/`, fenetre
  `2026-05-14T00:00:00Z -> 2026-06-23T00:00:00Z`, `94406` records.
  Resultat: `research_only_no_live_change`.
- Resultats clefs: `5718` signaux Pod A, `93` ouverts, `57` acceptes mais non
  ouverts, `5568` rejetes. Les trades ouverts font `-60.05` USD avec PF
  `0.8078`; leur retour directionnel moyen a 15m est pourtant legerement positif
  (`+7.63` bps) mais avec adverse moyen `33.18` bps et MAE 15m `-48.33` bps.
- Les buckets simples ne donnent pas de filtre live evident: profondeur
  `<1x` est positive (`+14.17` USD), `1-2x` est positive (`+31.77`), tandis que
  `gte_10x` perd `-42.58`; cout d'entree `4-8` bps est positif (`+30.10`) alors
  que `<1` bps perd `-44.93`. Ne pas promouvoir de veto/cap simple
  spread/depth/cost.
- Piste suivante: analyser les `57` `portfolio_open_rejected`/acceptes non
  ouverts et tester seulement ensuite un classifieur combine execution + regime +
  setup, ou un repricing/cap-only, contre la baseline full-bot.

### Implementation A-PNL-07b repeated-signal scale-in audit

Implementation locale du 2026-06-23, sans activation live automatique:

- Nouveau replay/audit `scripts/run_p118_repeated_signal_scale_in_audit.py`: il
  part du journal compact P117, isole les signaux acceptes mais non ouverts car
  une position meme symbole/sens etait deja ouverte, les rattache au trade parent
  et simule des add-ons hypothetique fermes avec ce parent.
- Six variantes research-only sont testees: premier add-on `25%` cappe,
  premier add-on `50%` cappe, tous les add-ons `25%` cappes, puis trois filtres
  live-compatibles qui exigent que le trade parent soit deja en gain latent
  (`>=0`, `>=25` ou `>=50` bps) au moment du signal repete. Aucun flag live,
  aucune config d'add-on, aucun ordre et aucun chemin d'execution live ne sont
  modifies.
- Replay:
  `server-data/replay_reports/p118_repeated_signal_scale_in_20260623/`, base
  P117 `server-data/replay_reports/p117_fill_quality_audit_20260623/`,
  `57` opportunites acceptees/non ouvertes et `57` rattachees a un parent.
- Resultats bruts: `first_add25_cap` fait `-9.62` USD, PF `0.6422`;
  `first_add50_cap` fait `-9.42` USD, PF `0.7235`; `all_add25_cap` fait
  seulement `+3.33` USD, PF `1.0825`, avec `8919.41` USD de notional
  hypothetique ajoute.
- Resultats filtres: `all_add25_parent_plus25_cap` monte a `+12.45` USD, PF
  `1.6558`, et `all_add25_parent_plus50_cap` monte a `+16.31` USD, PF `3.1495`,
  sur seulement `16` add-ons et `2351.15` USD de notional hypothetique.
- Lecture bucket: le filtre `parent_plus50` est la premiere piste vraiment
  interessante cote P118, car il utilise une information disponible en live
  (Pnl latent du parent) et retire les parents `early_failure_exit`. Mais le
  gain reste trop petit et trop concentre: INJ apporte `+16.86` USD et le PnL
  hors INJ tombe a `-0.55` USD. La stabilite temporelle est faible aussi:
  `+15.52` USD avant le 2026-06-03, puis seulement `+0.79` USD ensuite. Ne pas
  promouvoir de scale-in simple; garder `parent_plus50` comme hypothese a
  valider hors-echantillon dans un classifieur combine.

### Implementation A-PNL-08 loss-probation cap audit

Implementation locale du 2026-06-23, sans activation live automatique:

- `scripts/run_p108_dynamic_symbol_guard_replay.py` expose maintenant
  `--scenarios` et un scenario counterfactual
  `loss_probation_symbol_setup_cap50`, pour pouvoir comparer A-PNL-08 sans
  relancer toutes les variantes P108 historiques.
- Nouveau replay/audit rapide `scripts/run_p119_loss_probation_cap_audit.py`:
  il lit le journal compact P117, applique un cap-only `50%` aux trades deja
  ouverts quand le couple `symbol/setup` a au moins `2` trades rolling et un
  historique negatif, puis rehabilite au plein sizing si PF et expectancy
  rolling redeviennent positifs.
- Replay:
  `server-data/replay_reports/p119_loss_probation_cap_20260623/`, base P117
  full-window `93` trades Pod A ouverts. Resultat:
  `research_only_no_live_change`.
- Resultats: PnL Pod A ouvert `-60.05` -> `-33.94`, delta `+26.11`, PF
  `0.8078` -> `0.8584`, avec `42/93` trades cappes. Le split temporel reste
  positif en PnL (`+8.55` avant le 2026-06-03, `+17.57` apres), mais le PF
  post-split baisse (`0.5634` -> `0.5240`).
- Lecture: la piste coupe beaucoup de losers (`-145.69` USD de losers cappes)
  mais cappe aussi trop de winners (`+93.46` USD), notamment ARB, ZEC et INJ.
  Ne pas promouvoir tel quel; garder comme hypothese research pour une variante
  plus fine, avec replay full-bot seulement si elle reduit les winners cappes.

### Implementation C-PNL-02 external-reference cap-only

Implementation locale du 2026-06-23, sans activation live automatique:

- `scripts/run_p103_pod_c_external_reference_validation.py` expose maintenant
  des outcomes `cap50_*` en plus des anciens vetoes, avec champ `action` et
  nombre de trades touches. L'objectif est de tester une version soft gate de
  P1-03 sans bloquer les trades Pod C.
- Replay:
  `server-data/replay_reports/p103_pod_c_external_reference_cap50_20260623/`.
  Le rapport garde la recommandation
  `keep_open_recent_guardrail_candidate_needs_oos_or_shadow`.
- Resultats: sur la fenetre recente couverte a `91.67%`, les meilleurs cap-only
  ameliorent fortement le PnL Pod C:
  `cap50_candidate_default_5m` `+40.05`, `cap50_abs_premium_gt_50` `+22.31`,
  `cap50_candidate_loose_5m` `+16.09`, `cap50_missing_or_stale_15m` `+10.20`.
  Mais la baseline avril/mai a `0%` de coverage reference; impossible de valider
  hors-echantillon.
- Lecture: C-PNL-02 reste une vraie piste shadow si la coverage reference est
  restauree sur une baseline comparable. Pas de live maintenant, pas de deploy,
  pas de changement fetch.

### Implementation C-PNL-03 oil relative-value P120

Implementation locale du 2026-06-23, sans activation live automatique:

- Nouveau replay/audit `scripts/run_p120_oil_relative_value_audit.py`: il lit
  les observations `p109_oil_shadow_*` de `server-data/logs/pod_c_live.jsonl`,
  classe les candidats en `pair_confirmed` quand CL et BRENTOIL confirment au
  meme timestamp, dedupe par symbole sur `240m`, et calcule un proxy short
  `240m` avec notional `200` USD et fees roundtrip `7` bps.
- Replay:
  `server-data/replay_reports/p120_oil_relative_value_20260623/`,
  `10124` observations oil shadow.
- Resultats: le flux brut repete est tres negatif (`pair_confirmed` non dedupe
  `-394.68`, PF `0.6764`, `1234` maturations). La version dedupee a un signal
  positif mais minuscule: `14` candidats, `12` maturations, PnL proxy `+6.98`,
  PF `2.0375`, WR `66.67%`. Les jours `high_vol/mixed` portent le gain, les
  jours `chop` perdent.
- Lecture: garder comme hypothese shadow/OOS autour de P1-09 oil, pas comme
  changement live. Le signal utile ressemble plus a "un candidat independant par
  symbole et par jour vers 07:00 UTC" qu'a un filtre CL/BRENTOIL brut.

### Implementation C-PNL-04 session/liquidite P121

Implementation locale du 2026-06-23, sans activation live automatique:

- Nouveau replay/audit `scripts/run_p121_pod_c_session_liquidity_audit.py`: il
  lit la baseline avril/mai et le replay live courant P116 no-dedupe, regroupe
  les trades Pod C par session UTC (`asia_overnight`, `europe_morning`,
  `us_premarket`, `us_cash`, `us_late`) et par buckets d'activite/trade count,
  puis simule des policies cap-only `50%`.
- Replay:
  `server-data/replay_reports/p121_pod_c_session_liquidity_20260623/`, `84`
  trades Pod C.
- Resultats: aucun cap session n'est robuste. `cap_session_non_us_cash` aide le
  live (`+4.28`) mais detruit la baseline (`-37.79`). `cap_session_us_late`
  aide la baseline (`+1.70`) mais degrade legerement le live (`-0.06`).
  `cap_not_high_activity` aide le live (`+0.88`) mais degrade la baseline
  (`-5.07`).
- Lecture: C-PNL-04 reste research-only. Les sessions sont utiles pour lire le
  risque, mais pas encore pour une regle live simple.

### Implementation C-PNL-05 execution-cost P122

Implementation locale du 2026-06-23, sans activation live automatique:

- Nouveau replay/audit `scripts/run_p122_pod_c_execution_cost_audit.py`: il lit
  les memes rapports full-bot que P121, calcule `fee_bps`, `spread_bps` et
  `entry_cost_bps = fee_bps + max(spread_bps, 0)`, puis teste des policies
  cap-only par spread/cout/liquidite. Il ne simule pas de maker/passive fill.
- Replay:
  `server-data/replay_reports/p122_pod_c_execution_cost_20260623/`, `84`
  trades Pod C.
- Resultats: les fees sont quasi constantes autour de `7` bps; le spread pur ne
  separe pas assez les losers. `cap_spread_gte_1` aide faiblement la baseline
  (`+0.94`) mais degrade legerement le live (`-0.06`). Le seul signal positif
  sur les deux fenetres est `cap_bucket_notional_lt100k`: `+4.70` baseline et
  `+1.56` live, mais le live touche seulement `6` trades GOLD et reste trop
  petit.
- Lecture: garder une hypothese shadow de liquidity floor/cap sur buckets tres
  minces, mais ne pas promouvoir un maker/taker ou spread threshold live sans
  fill model et OOS.

### TRIDENT-HIP4

| Recommandation review | Traduction dans ce plan |
| --- | --- |
| Ne pas passer HIP4 live maintenant. | Decision actuelle: rester paper; live seulement apres readiness par bucket, preflight, tiny caps et confirmation explicite. |
| Conserver `prob_stop_full` comme politique active paper. | `H-PNL-01`: readiness board post-2026-06-10 autour de `prob_stop_full`. |
| Ne pas promouvoir les shadows EV partial/full seulement parce qu'ils semblent favorables visuellement. | `H-PNL-01` et `H-PNL-08`: comparaison contre `prob_stop_full`, avec settlements reels et contraintes de capital. |
| Nautilus est utile pour observability, mais pas encore pour bloquer, capper ou envoyer des ordres. | `H-PNL-02`, `H-PNL-03` et `H-PNL-09`: quality gate, maker/taker et data reconciler en shadow/research only. |
| La qualite des donnees et les problemes de stale feed doivent etre traites avant toute promotion live. | `H-PNL-07` et `H-PNL-09`: stale/reference jump, REST snapshot, WS gap detection et reconciliation. |

## Sources verifiees

Sources locales:

- `docs/trident_active_plan.md`
- `docs/server_data_review_agent.md`
- `docs/resultat_audit.md`
- `server-data/reviews/20260622T074233Z/review_summary.md`
- `server-data/reviews/20260622T101548Z/review_summary.md`
- `server-data/replay_reports/p108_dynamic_symbol_guard_live_sizing_halfsize_20260622T102000Z/`
- `server-data/replay_reports/p117_fill_quality_audit_20260623/`
- `server-data/replay_reports/p118_repeated_signal_scale_in_20260623/`
- `server-data/replay_reports/p119_loss_probation_cap_20260623/`
- `server-data/replay_reports/p103_pod_c_external_reference_cap50_20260623/`
- `server-data/replay_reports/p120_oil_relative_value_20260623/`
- `server-data/replay_reports/p121_pod_c_session_liquidity_20260623/`
- `server-data/replay_reports/p122_pod_c_execution_cost_20260623/`
- `server-data/hip4/reviews/20260622T075700Z/hip4_outcome_run_review.md`
- `server-data/hip4/replay_reports/hip4_policy_market_audit_20260622T075232Z.md`
- Rapports de replay et d'audit P1-03, P1-08, P1-09, P1-11, P105, P108, P111.

Sources externes consultees:

- HftBacktest, order book imbalance, micro-price, VAMP et variantes de prix
  ponderes: https://hftbacktest.readthedocs.io/en/latest/tutorials/Market%20Making%20with%20Alpha%20-%20Order%20Book%20Imbalance.html
- Anatomy of a Decentralized Prediction Market, microstructure Polymarket:
  https://arxiv.org/html/2604.24366v1
- Hyperliquid market maker bot, VWAP fair price, spread capture et pairs:
  https://github.com/zer0cache/hyperliquid-market-maker-bot
- Hyperliquid Supurr Bot, spreads dynamiques, inventory skew, spot-perp arb:
  https://github.com/Supurr-App/Hyperliquid-Supurr-Bot
- Hyperliquid examples, Info API, order books, WebSocket/gRPC streams:
  https://github.com/quiknode-labs/hyperliquid-examples
- Hyperliquid arbitrage bot, spreads cross-exchange avec fees, slippage,
  liquidite et funding: https://github.com/Jackhuang166/hyberliquid-arbitrage-bot
- Hyperliquid realtime data collector, order book/trades/candles/funding/OI:
  https://github.com/bwroniszewski/hyperliquid-realtime-data
- Polymarket 5-min crypto up/down order book dataset, avec reserves pratiques
  sur queue position et fills: https://www.reddit.com/r/algotrading/comments/1u8fsg7/free_dataset_polymarket_5min_crypto_updown_order/
- Discussion Polymarket bot, risque de signaux fantomes via WebSocket stale et
  besoin de snapshot REST: https://www.reddit.com/r/PredictionsMarkets/comments/1s46vn7/my_polymarket_bot_wins_68_of_the_time_and_still/
- Prediction market maker/taker microstructure:
  https://www.jbecker.dev/research/prediction-market-microstructure
- Kelly Betting as Bayesian Model Evaluation, limites des scores statiques:
  https://arxiv.org/html/2602.09982v1
- Polymarket trade engine et harness 5/15m:
  https://github.com/KaustubhPatange/polymarket-trade-engine

## Ne pas reproposer tel quel

Ces pistes ont deja ete testees, rejetees ou classees non-promotables dans les
docs existantes. Elles ne doivent pas revenir comme "nouvelle idee" sans angle
different, nouveau dataset et comparaison full-bot.

### Pod A

- Ne pas reintroduire le veto cible HYPE `trend_pullback_long` tel quel: il a
  ete rollback.
- Ne pas reactiver les shorts globaux Pod A sans preuve nouvelle.
- Ne pas etendre les vetoes MTF sans replay full-bot.
- Ne pas promouvoir `evo1_adaptive_exit`: negatif, coupe la convexite.
- Ne pas promouvoir `evo2_fee_aware_be`: legerement negatif.
- Ne pas promouvoir `evo3_trend_health_sizing`: negatif, sous-dimensionne des
  gagnants.
- Ne pas promouvoir `evo4_symbol_health` en version brutale: negatif.
- Ne pas promouvoir `evo10_context_guardrail`: negatif, retire des reentrees
  gagnantes.
- Ne pas promouvoir `stop_grace_210m` sans validation out-of-sample.
- Ne pas globalement relaxer `RangeAuction` ou `DeadZone`.
- Ne pas promouvoir P111 micro-regime tel que teste:
  `veto_range_mid_vol_high`, `half_size_micro_adverse` et combinaison ont empire
  la baseline.
- Ne pas activer P1-08 en `quarantine_multiplier=0.10`: le replay live dedie du
  `2026-06-22` degrade A/C de `-6.92` vs courant et abaisse le PF Pod A a
  `0.4767`.

### Pod C

- Ne pas etendre `routing_revoked` grace a silver/gold sans preuve.
- Ne pas globalement relaxer `stop_hit`.
- Garder equity/fx en observation, pas comme branche active.
- Ne pas reactiver silver live: `XYZ:SILVER` reste bloque.
- Ne pas promouvoir `gold_medium_neutral_veto`: rejete.
- Ne pas promouvoir `global_065`, `global_070`, `gold_070`, `silver_070` ou
  `metals_070`.
- Ne pas etendre P1-09 oil apres quelques trades seulement: l'echantillon est
  trop petit et les positions ouvertes doivent etre integrees.

### TRIDENT-HIP4

- Ne pas passer mainnet live sans dataset, calibration, replay comparable,
  dry-run/preflight, tiny caps et confirmation manuelle.
- Ne pas considerer testnet comme preuve de performance economique.
- Ne pas promouvoir `SHORT_EXPIRY` hold-to-settlement proxy.
- Ne pas promouvoir le durcissement BUY_YES downtrend tel que teste.
- Ne pas promouvoir `shock_guard` one-hit ou `shock_guard_scale_2x`.
- Ne pas promouvoir des quotes maker sans modele de fill et de queue position.
- Ne pas utiliser Nautilus pour decisions, caps ou ordres avant validation: il
  reste observability/research.
- Ne pas introduire Kelly, ML ou sizing adaptatif live avant calibration robuste
  par bucket.

## Backlog priorise

Statuts:

- `todo`: a concevoir ou implementer.
- `shadow`: collecte active sans effet trading.
- `paper`: effet applique uniquement paper.
- `ready_review`: pret pour revue humaine.
- `blocked`: manque une preuve ou un pre-requis.

### Pod A - crypto core

| ID | Statut | Changement a faire | Pourquoi ca peut augmenter le PnL | Validation minimale |
| --- | --- | --- | --- | --- |
| A-PNL-01 | ready_review | P1-08 uniquement en sizing progressif demi-cap: `throttle=0.50`, `quarantine=0.50`, aucun blocage, avec logs `guard_state` et `live_action_changed`. Implementation 2026-06-22: code cap-only disponible via config `dynamic_symbol_guard_live_sizing_enabled=false` par defaut; la variante `quarantine=0.10` est rejetee. Audit P108 adapte le 2026-06-23 pour distinguer changements live attendus/inattendus. | Les donnees recentes montrent que les symboles en etat degrade concentrent des pertes; le demi-cap conserve l'activite et reduit legerement le drawdown sans supprimer les trades Pod A sur la fenetre live. | Replay dedie positif mais faible (`+2.91` A/C, PF Pod A `0.6879`). Audit P108 local PASS (`unexpected_live_action_changed=0`). Avant live: redeployer explicitement config `cap50/cap50`, verifier que la config serveur n'est plus en `cap50/cap10`, puis review post-deploiement; aucune activation automatique. |
| A-PNL-02 | shadow | Echelle de notional par etat symbole implementee en dormant: stats rolling `symbol/setup` exposees, base `0.70`, partiel `0.85`, plein sizing seulement apres PF/expectancy rolling positifs. Flag `dynamic_symbol_guard_recovery_sizing_enabled=false` par defaut. | Les pertes recentes ne viennent pas d'un manque d'activite mais d'un mauvais payoff; reduire la taille dans les contextes mediocres ameliore l'esperance sans couper. | Replay full-window `p108_recovery_sizing_20260622`: positif vs courant (`+2.79`) mais inferieur a P1-08 `cap50/cap50` (`+2.91`) avec plus de reductions. Statut `research_only_no_live_change`; ne pas activer tel quel, garder dormant pour variantes futures. |
| A-PNL-03 | shadow | Cap headroom A-grade implemente en dormant: `a_grade_size_headroom_cap_enabled=false`; garde le label/exits A-grade mais limite le scale taille a la marge symbole et au risk budget initial si active. Le freeze strong P1-05 reste rejete. | Reduire la convexite des losers A-grade sans supprimer le signal ni les exits, tout en evitant de reproposer le freeze deja teste. | Replay live-window `p105_a_grade_headroom_cap_live_20260622`: positif mais non materiel (`+0.08` A/C, PF Pod A `0.6762` vs `0.6756`, DD `41.06` vs `41.14`). Statut `research_only_no_live_change`; ne pas activer tel quel. |
| A-PNL-04 | shadow | Audit P116 implemente pour `early_failure_exit`: replay per-trade sans EFE, jusqu'au stop/trailing/break-even/time-stop/cat-stop naturel, avec MFE/MAE post-sortie. | Les sorties precoces reduisent certaines pertes mais peuvent tuer des recoveries; l'audit mesure le cout d'opportunite sans reproposer le disable global deja couvert par P1-02. | Replay complet `p116_early_failure_post_exit_20260622`: sans EFE, les 41 trades EFE empirent de `-8.46` USD. `6` winners + `6` loss-cuts manques, mais `29` pertes evitees; garder EFE, ne pas promouvoir une relaxation globale. |
| A-PNL-05 | shadow | Score microstructure entree implemente en shadow/export: sous-scores spread, flow, microprice, depth, activite, range et churn. Replay P115 cap-only `<0.42` et `<0.56` ajoute, sans blocage ni flag live. | Les sources HFT indiquent que le desalignement prix mid vs micro-price/VAMP revele souvent l'adverse selection; utile en audit, mais la version cap-only testee ne separe pas assez les losers live. | Replay complet `p115_microstructure_entry_20260622`: baseline neutre, live negatif (`-1.02` poor50, `-0.13` weak50). Bucket `poor` live gagnant et pire bucket `strong`; garder shadow/audit, ne pas promouvoir le cap-only. |
| A-PNL-06 | shadow | Ajouter une reference crypto cross-exchange par symbole liquide: Binance/OKX/Bybit/Coinbase/Kraken selon disponibilite, avec premium HL et divergence momentum. | Si Hyperliquid est temporairement en avance ou en retard contre le marche large, le bot peut entrer sur un prix local defavorable. | Aucun effet trading au debut; verifier PnL par bucket de divergence et fraicheur reference. |
| A-PNL-07 | shadow | Audit fill-quality P117 implemente: `skip_reason`, cout d'entree attendu, depth/touch notional, accepted-skipped, rejected et retours 1/5/15m. Audit P118 ajoute pour tester les accepted-skipped en scale-in hypothetique. Aucun flag live. | Un bon signal peut devenir mauvais si l'execution paie le spread ou chase un carnet mince; l'audit cherche une variante cap-only/repricing plus precise qu'un veto simple, et verifie si les confirmations repetees valent un add-on. | Replay P117: ouvertures `-60.05` USD, PF `0.8078`; buckets simples spread/depth/cost non monotoniques. Replay P118: premiers add-ons negatifs; `parent_plus50` positif (`+16.31`, PF `3.1495`) mais trop petit et INJ-concentre. `research_only_no_live_change`; tester ensuite seulement un classifieur combine/OOS. |
| A-PNL-08 | shadow | Audit P119 implemente: cap-only `50%` apres pertes rolling par couple `symbol/setup`, rehabilitation si PF/expectancy rolling positifs. Scenario P108 `loss_probation_symbol_setup_cap50` disponible pour replay cible. | Evite le piege de `evo4_symbol_health` trop brutal: on reduit l'exposition au lieu d'effacer durablement des symboles qui peuvent redevenir bons. | P119 ameliore le PnL cap-only (`+26.11`) mais cappe trop de winners (`+93.46`) et degrade le PF post-split; `research_only_no_live_change`, pas live tel quel. |

### Pod C - tradfi builder-dex

| ID | Statut | Changement a faire | Pourquoi ca peut augmenter le PnL | Validation minimale |
| --- | --- | --- | --- | --- |
| C-PNL-01 | ready_review | Encadrer P1-09 oil short par un stoplight dedie: pas d'augmentation d'exposition tant que les positions fermees et latentes ne valident pas le edge. Implementation 2026-06-22: `fetch_trident_data.sh` expose `oil_stoplight`, closed PnL promu et latent oil ouvert dans `p109_oil_shadow_audit.*`. | Les premiers trades fermes ne suffisent pas; integrer l'unrealized evite de promouvoir un profil qui gagne seulement par hasard de timing. | Rapport quotidien oil: closed + open mark-to-market, PF, MAE, nombre de setups independants. |
| C-PNL-02 | shadow | P103 enrichi avec variantes cap-only `50%` sur stale/reference/premium/momentum. | Les validations recentes etaient prometteuses mais pas assez OOS; une version sizing limite le risque de tuer de bons trades. | Replay P103 cap50: recent tres positif sur `candidate_default_5m` (`+40.05`) mais baseline avril/mai sans coverage reference (`0%`); aucune promotion sans OOS/reference coverage complete. |
| C-PNL-03 | shadow | Audit P120 CL/BRENTOIL relative-value: buckets `pair_confirmed` vs `solo_confirmed`, dedupe 240m et proxy short 240m. | Les repos Hyperliquid market making/pair trading montrent l'interet d'un prix juste relatif; Pod C a deja deux symboles oil exploitables. | P120 dedupe positif (`+6.98`, PF `2.04`, `12` maturations) mais brut repete tres negatif (`-394.68` pair). Garder en shadow/OOS, pas live. |
| C-PNL-04 | shadow | Audit P121 session/liquidite calendar: sessions UTC, activity/trade_count buckets, cap-only session/liquidite. | Beaucoup de faux signaux tradfi viennent de carnets moins actifs ou references lentes; adapter le seuil par session garde le bot actif mais plus selectif. | Aucun cap session robuste: `non_us_cash` aide le live (`+4.28`) mais detruit la baseline (`-37.79`); garder en research. |
| C-PNL-05 | shadow | Audit P122 execution cost: fees/spread/activity/bucket notional, cap-only cout/liquidite; pas de simulation maker sans fill model. | Si le cout d'execution domine le signal, le PnL peut s'ameliorer par timing/price limit plutot que par nouveau signal alpha. | `bucket_notional<100k` cap50 positif mais petit (`+4.70` baseline, `+1.56` live, live GOLD-only); spread/cost pur non promotable. |
| C-PNL-06 | shadow | Rehabilitation silver uniquement en shadow: profil silver avec reference externe stricte, cap minuscule simule, no-live. | Silver a ete rejete en live, mais peut rester une source future si on impose reference + liquidite + sizing; ne pas le debloquer directement. | Replay silver separe, puis paper shadow, avec comparaison contre blocage actuel. |
| C-PNL-07 | todo | Cooldown dynamique apres cluster de stop/loss par actif: allonger apres losses rapides, raccourcir seulement si le setup suivant a reference externe favorable. | Reduit les sequences de pertes sans couper les trades isoles de bonne qualite. | Comparer clusters de losses, missed winners, delai moyen entre trades. |
| C-PNL-08 | todo | Calibration par features plutot que multiplicateur global: `external_reference_age_seconds`, premium bps, spread, activity bucket, flow support. | Les tests de seuils globaux ont echoue; un modele de calibration par contexte peut augmenter la precision sans tuer tout le volume. | Shadow score par bucket, puis replay cap-only; aucun changement global de seuil sans preuve. |

### TRIDENT-HIP4 - HIP4OutcomeEdgePod

| ID | Statut | Changement a faire | Pourquoi ca peut augmenter le PnL | Validation minimale |
| --- | --- | --- | --- | --- |
| H-PNL-01 | ready_review | Garder `prob_stop_full` comme politique active paper et publier un tableau readiness post-2026-06-10 par underlying, side, expiry bucket et market type. Implementation 2026-06-22: `hip4_outcome` run review ajoute `readiness_buckets` et une section Markdown `Readiness Buckets`. | Le resultat recent est favorable, mais le live exige de savoir ou le edge existe vraiment. | PF > 1.15 et Brier <= 0.23 par buckets significatifs, pas seulement en agregat. |
| H-PNL-02 | shadow | Refaire la quality gate Nautilus en shadow avec attribution PnL `would_block`, `would_downsize`, `would_allow`. | Le shadow semble favorable visuellement, mais les donnees recentes montrent que certains trades low-quality etaient gagnants; il faut eviter un block premature. | Promotion seulement si `would_block` est net negatif sur plusieurs fenetres et ne retire pas les meilleurs gagnants. |
| H-PNL-03 | shadow | Decomposer maker vs taker: role theorique, spread capture, adverse selection 5/30/120s, queue position simulee. | Les sources prediction markets suggerent que le role maker peut capter une prime, mais seulement si le modele de fill est realiste. | Simulation fills avec queue position; pas de quote maker live avant validation. |
| H-PNL-04 | todo | Construire un harness short-expiry queue-aware avec order book seconde par seconde et settlement labels propres. | L'ancienne idee short-expiry hold-to-settlement a ete rejetee; la version utile doit inclure fills, spread, stale book et couts. | Backtest sur dataset propre, avec fills manques et crossing costs; comparer a `prob_stop_full`. |
| H-PNL-05 | todo | Calibration temporelle: evaluer la trajectoire de probabilite par temps avant expiry, pas seulement Brier final. | Les scores statiques punissent mal certaines erreurs de timing; HIP4 trade une dynamique, pas une prediction ponctuelle. | Courbes reliability/log-loss/Brier par minute-to-expiry et underlying; seuils separes par bucket. |
| H-PNL-06 | shadow | Kelly fractionnel en research only, bloque par calibration et min order: sizing simule, drawdown cap, no-live. | Peut convertir un edge faible en sizing plus efficace, mais seulement apres calibration robuste; evite les ordres arrondis absurdes sous minimum. | Comparer fixed size vs fractional Kelly shadow avec couts, min size, max drawdown et ruin risk. |
| H-PNL-07 | shadow | Ajouter un veto stale/reference jump: si underlying bouge fortement mais le book Polymarket ne s'est pas ajuste, downsize ou skip en shadow. | Les discussions de bots Polymarket pointent les signaux fantomes WebSocket; le snapshot/reference frais peut eviter d'acheter un carnet stale. | Log REST snapshot age, WS age, reference move bps, PnL des trades qui auraient ete touches. |
| H-PNL-08 | todo | Allocateur multi-marches: quand plusieurs candidats sont ouverts/refuses par `market_already_open`, choisir le meilleur couple marche/side selon expected value et occupancy. | Une partie du PnL se joue dans le choix du marche quand le capital est occupe; meilleur ranking peut augmenter PnL sans augmenter le risque brut. | Replay avec contraintes max open markets et min order, comparer PnL par capital-hour. |
| H-PNL-09 | todo | Source-of-truth data reconciler: REST snapshot periodique, WS gap detection, book checksum logique, settlement reconciliation. | Les marches prediction sont sensibles aux donnees stale; ameliorer la qualite de feed peut augmenter le PnL en supprimant les faux edges. | Audit quotidien gaps/staleness et PnL des trades proches d'anomalies feed. |

## Ordre d'implementation recommande

1. Instrumentation sans effet trading:
   A-PNL-04, A-PNL-05, A-PNL-07, C-PNL-05, H-PNL-02, H-PNL-03, H-PNL-07,
   H-PNL-09.

2. Replays full-bot et paper/shadow:
   A-PNL-01, A-PNL-02, A-PNL-03, C-PNL-02, C-PNL-03, C-PNL-04, C-PNL-05,
   H-PNL-01, H-PNL-04, H-PNL-05, H-PNL-08.

3. Changements cap-only ou paper-only apres preuve:
   A-PNL-01, A-PNL-02, C-PNL-02, variante future C-PNL-05 liquidity floor. Pour
   HIP4, rester paper tant que les seuils de readiness ne sont pas tenus par
   bucket.

4. Revue live manuelle:
   uniquement apres rapport de replay, rapport paper/shadow, preflight, tiny cap
   documente et confirmation explicite.

## Fichiers et scripts probablement impactes

TRIDENT A/C:

- `config/trident.toml` pour les flags shadow/paper/cap-only.
- Modules Pod A et Pod C qui calculent signal, sizing, exits et logs de trade.
- `scripts/fetch_trident_data.sh` si de nouveaux fichiers de logs/reports sont
  ajoutes.
- `scripts/trident_dry_run_review.sh` et exports d'audit si les nouveaux champs
  doivent etre visibles dans les reviews.
- Scripts de replay P1/P10x/P11x si les nouvelles variantes doivent etre
  comparees a la baseline full-bot.
- Impact A-PNL-02 du 2026-06-22: `scripts/export_trident_audit_pack.py` expose
  les nouveaux champs rolling/recovery, et
  `scripts/run_p108_dynamic_symbol_guard_replay.py` expose le scenario
  `live_sizing_recovery_55_75_base70_partial85`; les scripts de deploy/fetch ne
  necessitent pas de modification car aucun nouvel artefact serveur n'est cree.
- Impact A-PNL-01 du 2026-06-23: `scripts/fetch_trident_data.sh` adapte l'audit
  P108 pour que `live_action_unchanged=false` soit un echec seulement si le
  changement live est inattendu. Le rapport expose `expected_live_action_changed`,
  `unexpected_live_action_changed`, la config policy et les raisons de sizing.
  Aucun script de deploy a modifier, aucun flag live active.
- Impact A-PNL-03 du 2026-06-22: `scripts/export_trident_audit_pack.py` expose
  les champs de cap headroom A-grade et `scripts/run_p105_a_grade_replay.py`
  expose le scenario `headroom_cap_current` avec filtres `--scenarios` et
  `--windows`; aucun script de deploy/fetch a modifier, aucun nouvel artefact
  serveur requis, flag live dormant `a_grade_size_headroom_cap_enabled=false`.
- Impact A-PNL-04 du 2026-06-22: nouveau replay local
  `scripts/run_p116_early_failure_post_exit_audit.py`, qui reutilise le rapport
  full-bot et les snapshots live existants. Aucun script de deploy/fetch a
  modifier: pas de nouveau champ serveur, pas de config live, pas d'ordre, et
  aucune activation automatique de sortie.
- Impact A-PNL-05 du 2026-06-22: `scripts/export_trident_audit_pack.py` expose
  les champs `microstructure_shadow_*` et `p115_*`; nouveau replay local
  `scripts/run_p115_microstructure_entry_replay.py`. Aucun script de deploy ou
  fetch a modifier: le score reutilise les champs deja collectes dans les
  snapshots/live logs et n'ajoute aucun ordre, cap live ou service serveur.
- Impact A-PNL-07 du 2026-06-23: nouveau replay local
  `scripts/run_p117_fill_quality_audit.py`; `PodABacktestRunner` expose
  `skip_reason` et peut omettre les `signal_review` filtres pour les audits qui
  n'en ont pas besoin; `scripts/export_trident_audit_pack.py` exporte
  `execution.skip_reason`. Aucun script de deploy ou fetch a modifier, aucun
  flag live, aucun ordre et aucun service serveur.
- Impact A-PNL-07b du 2026-06-23: nouveau replay local
  `scripts/run_p118_repeated_signal_scale_in_audit.py`, consomme uniquement le
  journal P117 local pour simuler des add-ons contrefactuels. Aucun script de
  deploy ou fetch a modifier, aucun flag live, aucune config d'add-on, aucun
  ordre et aucun service serveur.
- Impact A-PNL-08 du 2026-06-23: `scripts/run_p108_dynamic_symbol_guard_replay.py`
  expose `--scenarios` et le scenario `loss_probation_symbol_setup_cap50`;
  nouveau replay local `scripts/run_p119_loss_probation_cap_audit.py` qui
  consomme le journal P117. Aucun script de deploy/fetch a modifier, aucun flag
  live, aucun ordre et aucun service serveur.
- Impact C-PNL-02 du 2026-06-23:
  `scripts/run_p103_pod_c_external_reference_validation.py` expose des outcomes
  cap-only `50%` en plus des vetoes P1-03. Aucun script de deploy/fetch a
  modifier, aucun flag live, aucun ordre et aucun service serveur.
- Impact C-PNL-03 du 2026-06-23: nouveau replay local
  `scripts/run_p120_oil_relative_value_audit.py`, qui consomme uniquement les
  logs Pod C deja rapatries pour mesurer CL/BRENTOIL en shadow P1-09. Aucun
  script de deploy/fetch a modifier, aucun flag live, aucun ordre et aucun
  service serveur.
- Impact C-PNL-04 du 2026-06-23: nouveau replay local
  `scripts/run_p121_pod_c_session_liquidity_audit.py`, qui consomme des rapports
  full-bot locaux et produit des buckets session/liquidite. Aucun script de
  deploy/fetch a modifier, aucun flag live, aucun ordre et aucun service serveur.
- Impact C-PNL-05 du 2026-06-23: nouveau replay local
  `scripts/run_p122_pod_c_execution_cost_audit.py`, qui consomme des rapports
  full-bot locaux et audite fees/spread/liquidite sans simulation maker. Aucun
  script de deploy/fetch a modifier, aucun flag live, aucun ordre et aucun
  service serveur.

TRIDENT-HIP4:

- `trident-hip4` config pour flags shadow Nautilus/quality/maker-taker.
- Collecteurs book/reference/settlement si REST snapshot, WS gap detection ou
  queue-aware harness sont ajoutes.
- `trident-hip4/fetch_data.sh` pour rapatrier les nouveaux logs.
- Reviews HIP4 et rapports de policy audit pour inclure readiness par bucket.

Verification avant toute PR:

- `rtk uv run pytest`
- `rtk bash -n scripts/fetch_trident_data.sh`
- `rtk bash -n trident-hip4/fetch_data.sh`
- `rtk bash -n scripts/fetch_all_data.sh`
- Review locale des donnees apres fetch si les champs de logs changent.

Validations locales A-PNL-01 audit P108 du 2026-06-23:

- `rtk bash -n scripts/fetch_trident_data.sh`: OK.
- `rtk uv run pytest tests/test_fetch_trident_data_p108_audit.py -q`: OK
  (`2` tests). Les tests couvrent le meme log avec
  `symbol_guard_live_action_unchanged=false`: PASS quand
  `dynamic_symbol_guard_live_sizing_enabled=true`, FAIL quand la policy est
  desactivee.
- `rtk uv run pytest tests/test_fetch_trident_data_p108_audit.py tests/test_p108_dynamic_symbol_guard_replay.py tests/test_pod_a_live_runner.py tests/test_reporting.py -q`:
  OK (`35` tests).
- `rtk ./scripts/fetch_trident_data.sh --review-only`: OK,
  `server-data/reviews/20260623T091959Z/review_summary.md` en `PASS`; P1-08
  `with_shadow=2000/2000`, `unexpected_live_action_changed=0`.

Validations locales A-PNL-02 du 2026-06-22:

- `rtk uv run python -m py_compile app/settings.py app/risk/pod_a_gate.py app/trident/pod_a/live_risk.py app/live/pod_a_live_runner.py scripts/export_trident_audit_pack.py`: OK.
- `rtk uv run python -m unittest tests.test_settings tests.test_risk_gate tests.test_pod_a_live_runner tests.test_p108_dynamic_symbol_guard_replay`: OK (`42` tests).
- `rtk bash -n scripts/fetch_trident_data.sh trident-hip4/fetch_data.sh scripts/fetch_all_data.sh`: OK.
- Smoke replay technique:
  `rtk uv run python scripts/run_p108_dynamic_symbol_guard_replay.py --window live --live-input server-data/live_snapshots/2026-06-20.jsonl --live-start 2026-06-20T00:00:00Z --live-end 2026-06-20T01:00:00Z --output-dir tmp/p108_recovery_smoke_20260622`:
  OK. Le scenario `live_sizing_recovery_55_75_base70_partial85` sort `-4.29`
  vs baseline `-4.31` sur `3` trades; c'est un smoke technique, pas une preuve
  de promotion.
- Replay full-window A-PNL-02:
  `rtk uv run python scripts/run_p108_dynamic_symbol_guard_replay.py --window live --live-input server-data/live_snapshots --live-start 2026-05-14T00:00:00Z --live-end 2026-06-22T10:14:00Z --output-dir server-data/replay_reports/p108_recovery_sizing_20260622`:
  OK, `research_only_no_live_change`; A-PNL-02 positif vs courant mais inferieur
  au demi-cap P1-08 simple.
- Suite complete `rtk uv run pytest`: OK (`664` passed, `1` warning pytest
  historique sur `TestnetOutcomeExecutor`). Les tests supervisor/report/backtest
  ont ete isoles des artefacts runtime locaux (`runtime/trident/*`,
  `logs/*_live_status.json`) et des blocklists prod quand ils testent une
  hypothese de routing generique; le test small-wallet desactive A-grade pour
  verifier le sizing brut sans boost.

Validations locales A-PNL-03 du 2026-06-22:

- `rtk uv run python -m py_compile app/settings.py app/trident/pod_a/planner.py scripts/run_p105_a_grade_replay.py scripts/export_trident_audit_pack.py`: OK.
- `rtk uv run pytest tests/test_settings.py tests/test_pod_a.py::AnchorTrendServiceTests::test_trade_planner_applies_a_grade_boost_and_wider_exits tests/test_pod_a.py::AnchorTrendServiceTests::test_trade_planner_can_cap_a_grade_size_to_initial_headroom tests/test_p105_a_grade_replay.py -q`: OK (`8` tests).
- Smoke replay technique:
  `rtk uv run python scripts/run_p105_a_grade_replay.py --baseline-input server-data/live_snapshots/2026-06-20.jsonl --live-input server-data/live_snapshots --live-start 2026-06-20T00:00:00Z --live-end 2026-06-21T00:00:00Z --output-dir tmp/p105_headroom_smoke_20260622`:
  OK; `headroom_cap_current` cappe `3` trades A-grade sur ce smoke, PnL neutre
  vs courant (`-4.31` vs `-4.31`).
- Replay live-window filtre:
  `rtk uv run python scripts/run_p105_a_grade_replay.py --windows live --scenarios current,headroom_cap_current --live-input server-data/live_snapshots --live-start 2026-05-14T00:00:00Z --live-end 2026-06-23T00:00:00Z --output-dir server-data/replay_reports/p105_a_grade_headroom_cap_live_20260622`:
  OK, `research_only_no_live_change`; delta `+0.08` A/C, insuffisant pour
  promotion live.
- Suite complete `rtk uv run pytest`: OK (`667` passed, `1` warning pytest
  historique sur `TestnetOutcomeExecutor`).

Validations locales A-PNL-04 du 2026-06-22:

- `rtk uv run python -m py_compile scripts/run_p116_early_failure_post_exit_audit.py scripts/run_p102_exit_sensitivity.py`: OK.
- `rtk uv run pytest tests/test_p116_early_failure_post_exit_audit.py tests/test_p102_exit_sensitivity.py -q`: OK (`6` tests).
- Base full-bot courante no-dedupe pour comparabilite P115:
  `rtk uv run python app/backtest/full_bot_replay.py --input server-data/replay_reports/p115_microstructure_entry_20260622/input_live_post_baseline --no-dedupe-timestamps --apply-live-notional-caps --report-output server-data/replay_reports/p116_early_failure_post_exit_20260622/full_bot_current_live_nodedupe.json --summary-output server-data/replay_reports/p116_early_failure_post_exit_20260622/full_bot_current_live_nodedupe.md`:
  OK, `-40.19` A/C, Pod A `-31.54`, Pod C `-8.65`, `45782` records.
- Replay/audit complet P116:
  `rtk uv run python scripts/run_p116_early_failure_post_exit_audit.py --replay-report server-data/replay_reports/p116_early_failure_post_exit_20260622/full_bot_current_live_nodedupe.json --snapshot-input server-data/replay_reports/p115_microstructure_entry_20260622/input_live_post_baseline --output-dir server-data/replay_reports/p116_early_failure_post_exit_20260622`:
  OK, `research_only_no_live_change`; delta naturel sans EFE `-8.46` USD sur
  les `41` trades EFE. Ne pas promouvoir une relaxation globale.
- `rtk uv run pytest`: OK (`678` passed, `1` warning pytest historique sur
  `TestnetOutcomeExecutor`).
- `rtk git diff --check`: OK.

Validations locales A-PNL-05 du 2026-06-22:

- `rtk uv run python -m py_compile app/trident/pod_a/microstructure_shadow.py app/trident/pod_a/service.py scripts/run_p115_microstructure_entry_replay.py`: OK.
- `rtk uv run pytest tests/test_pod_a_microstructure_shadow.py tests/test_p115_microstructure_entry_replay.py tests/test_pod_a.py::AnchorTrendServiceTests::test_generates_long_signal_in_trend_expansion -q`: OK (`9` tests).
- Smoke replay technique:
  `rtk uv run python scripts/run_p115_microstructure_entry_replay.py --windows live --live-input server-data/live_snapshots --live-start 2026-06-20T00:00:00Z --live-end 2026-06-21T00:00:00Z --scenarios current,micro_cap_poor50_lt42,micro_cap_weak50_lt56 --output-dir tmp/p115_microstructure_smoke_fast_20260622`:
  OK; PnL neutre vs courant sur `3` trades (`-4.31`), `micro_cap_weak50_lt56`
  cappe `3` plans mais aucun trade ferme effectivement cappe.
- Replay complet A-PNL-05:
  `rtk uv run python scripts/run_p115_microstructure_entry_replay.py --windows baseline,live --baseline-input server-data/replay_inputs/external_reference_multisource_20260405_20260513_baseline.jsonl --live-input server-data/live_snapshots --live-start 2026-05-14T00:00:00Z --live-end 2026-06-23T00:00:00Z --scenarios current,micro_cap_poor50_lt42,micro_cap_weak50_lt56 --output-dir server-data/replay_reports/p115_microstructure_entry_20260622`:
  OK, `research_only_no_live_change`; baseline neutre, live negatif (`-1.02`
  et `-0.13` A/C). Ne pas promouvoir le cap-only microstructure tel quel.
- `rtk uv run pytest`: OK (`675` passed, `1` warning pytest historique sur
  `TestnetOutcomeExecutor`).
- `rtk git diff --check`: OK.

Validations locales A-PNL-07 du 2026-06-23:

- `rtk uv run python -m py_compile scripts/run_p117_fill_quality_audit.py app/backtest/pod_a_runner.py scripts/export_trident_audit_pack.py`:
  OK.
- `rtk uv run pytest tests/test_p117_fill_quality_audit.py tests/test_backtest_runner.py -q`:
  OK (`9` tests).
- Smoke replay technique:
  `rtk uv run python scripts/run_p117_fill_quality_audit.py --live-input server-data/live_snapshots --live-start 2026-06-20T00:00:00Z --live-end 2026-06-21T00:00:00Z --output-dir tmp/p117_fill_quality_smoke_20260623`:
  OK, `2918` records, `84` signaux, `5` ouvertures, PnL `-21.88`, decision
  `research_only_no_live_change`.
- Replay full-window A-PNL-07:
  `rtk uv run python scripts/run_p117_fill_quality_audit.py --live-input server-data/live_snapshots --live-start 2026-05-14T00:00:00Z --live-end 2026-06-23T00:00:00Z --output-dir server-data/replay_reports/p117_fill_quality_audit_20260623`:
  OK, `94406` records, `5718` signaux, `93` ouvertures, PnL `-60.05`, PF
  `0.8078`; aucun filtre simple spread/depth/cost n'est promotable.

Validations locales A-PNL-07b du 2026-06-23:

- `rtk uv run python -m py_compile scripts/run_p118_repeated_signal_scale_in_audit.py tests/test_p118_repeated_signal_scale_in_audit.py`:
  OK.
- `rtk uv run pytest tests/test_p118_repeated_signal_scale_in_audit.py -q`:
  OK (`3` tests).
- Replay P118:
  `rtk uv run python scripts/run_p118_repeated_signal_scale_in_audit.py --p117-journal server-data/replay_reports/p117_fill_quality_audit_20260623/pod_a_fill_quality_journal.jsonl --output-dir server-data/replay_reports/p118_repeated_signal_scale_in_20260623 --max-add-on-notional-usd 200`:
  OK, `57` opportunites, `57` matchees. `first_add25_cap` `-9.62` USD,
  `first_add50_cap` `-9.42` USD, `all_add25_cap` `+3.33` USD mais concentre sur
  INJ et parents `trailing_stop`. Les filtres parent en gain latent ameliorent
  le profil: `parent_plus25` `+12.45` USD, PF `1.6558`; `parent_plus50`
  `+16.31` USD, PF `3.1495`, mais sur `16` add-ons seulement et PnL hors INJ
  `-0.55` USD; split temporel fragile (`+15.52` avant 2026-06-03, `+0.79`
  apres). Ne pas promouvoir; valider OOS dans un classifieur combine.

Validations locales A-PNL-08/P119 du 2026-06-23:

- `rtk uv run python -m py_compile scripts/run_p108_dynamic_symbol_guard_replay.py tests/test_p108_dynamic_symbol_guard_replay.py scripts/run_p119_loss_probation_cap_audit.py tests/test_p119_loss_probation_cap_audit.py`:
  OK.
- `rtk uv run pytest tests/test_p108_dynamic_symbol_guard_replay.py tests/test_p119_loss_probation_cap_audit.py -q`:
  OK (`9` tests).
- Smoke replay P108 cible:
  `rtk uv run python scripts/run_p108_dynamic_symbol_guard_replay.py --window live --scenarios current_ac,loss_probation_symbol_setup_cap50 --live-input server-data/live_snapshots/2026-06-20.jsonl --live-start 2026-06-20T00:00:00Z --live-end 2026-06-20T01:00:00Z --output-dir tmp/p108_loss_probation_smoke_20260623`:
  OK; smoke neutre sur `3` trades, utile seulement pour valider l'integration.
- Audit P119:
  `rtk uv run python scripts/run_p119_loss_probation_cap_audit.py --p117-journal server-data/replay_reports/p117_fill_quality_audit_20260623/pod_a_fill_quality_journal.jsonl --output-dir server-data/replay_reports/p119_loss_probation_cap_20260623`:
  OK; delta `+26.11` USD mais `42/93` trades cappes, dont `+93.46` USD de
  winners. Ne pas promouvoir tel quel.

Validations locales C-PNL-02/C-PNL-03/C-PNL-04/C-PNL-05 du 2026-06-23:

- C-PNL-02/P103:
  `rtk uv run python -m py_compile scripts/run_p103_pod_c_external_reference_validation.py tests/test_p103_pod_c_external_reference_validation.py`:
  OK.
- C-PNL-02/P103:
  `rtk uv run pytest tests/test_p103_pod_c_external_reference_validation.py -q`:
  OK (`3` tests).
- Replay P103 cap-only:
  `rtk uv run python scripts/run_p103_pod_c_external_reference_validation.py --output-dir server-data/replay_reports/p103_pod_c_external_reference_cap50_20260623`:
  OK; recent positif (`cap50_candidate_default_5m` `+40.05`) mais baseline
  reference coverage `0%`, donc pas promotable.
- C-PNL-03/P120:
  `rtk uv run python -m py_compile scripts/run_p120_oil_relative_value_audit.py tests/test_p120_oil_relative_value_audit.py`:
  OK.
- C-PNL-03/P120:
  `rtk uv run pytest tests/test_p120_oil_relative_value_audit.py -q`:
  OK (`3` tests).
- Replay P120:
  `rtk uv run python scripts/run_p120_oil_relative_value_audit.py --pod-c-log server-data/logs/pod_c_live.jsonl --output-dir server-data/replay_reports/p120_oil_relative_value_20260623`:
  OK; dedupe `+6.98` USD proxy mais seulement `12` maturations, brut repete
  negatif.
- C-PNL-04/P121:
  `rtk uv run python -m py_compile scripts/run_p121_pod_c_session_liquidity_audit.py tests/test_p121_pod_c_session_liquidity_audit.py`:
  OK.
- C-PNL-04/P121:
  `rtk uv run pytest tests/test_p121_pod_c_session_liquidity_audit.py -q`:
  OK (`3` tests).
- Replay P121:
  `rtk uv run python scripts/run_p121_pod_c_session_liquidity_audit.py --output-dir server-data/replay_reports/p121_pod_c_session_liquidity_20260623`:
  OK; aucun cap session robuste.
- C-PNL-05/P122:
  `rtk uv run python -m py_compile scripts/run_p122_pod_c_execution_cost_audit.py tests/test_p122_pod_c_execution_cost_audit.py`:
  OK.
- C-PNL-05/P122:
  `rtk uv run pytest tests/test_p122_pod_c_execution_cost_audit.py -q`:
  OK (`3` tests).
- Replay P122:
  `rtk uv run python scripts/run_p122_pod_c_execution_cost_audit.py --output-dir server-data/replay_reports/p122_pod_c_execution_cost_20260623`:
  OK; `bucket_notional<100k` positif mais trop petit/GOLD-concentre, pas live.

## Definition de "promotable"

Une piste est promotable seulement si elle remplit toutes les conditions
suivantes:

- Elle bat la baseline full-bot pertinente, pas seulement un sous-ensemble choisi.
- Elle augmente le PnL ou le profit factor sans degrader fortement le max
  drawdown, le nombre de winners conserves ou la convexite des trailing stops.
- Elle reste positive sur plusieurs buckets: symbole, regime, session, side,
  expiry ou underlying selon le pod.
- Elle inclut les couts reels: spread, slippage, fees, min order, fills manques,
  stale data et capital occupe.
- Elle preserve le comportement operationnel: fetch, review, preflight, deploy
  et reconciliation restent fonctionnels.
- Elle peut etre rollbackee par config.

## Candidats live priorises

Liste evolutive a maintenir apres chaque replay, fetch/review ou decision
operateur. Elle ne declenche aucune activation: tout passage live exige toujours
preflight, rapport de replay/paper, confirmation explicite et rollback par config.

| Priorite | Candidat | Etat actuel | Condition avant live |
| --- | --- | --- | --- |
| 1 | `A-PNL-01` / P1-08 `cap50/cap50` Pod A | Candidat le plus proche: `+2.91` A/C vs courant, gain faible. Audit P108 adapte et review locale PASS, mais la config serveur rapatriee montre encore `quarantine_multiplier=0.10` avec flag desactive. | Confirmation explicite, redeploiement de la config candidate `dynamic_symbol_guard_live_sizing_enabled=true` + `throttle=0.50` + `quarantine=0.50`, puis fetch/review post-deploiement avec `unexpected_live_action_changed=0` et rollback par config. |
| 2 | `C-PNL-01` stoplight P1-09 oil Pod C | Garde-fou operationnel pret, mais stoplight courant `hold_exposure` avec closed+open oil negatif. | Aucune hausse d'exposition oil tant que closed+open PnL, PF, MAE et nombre de setups independants ne passent pas au vert. |
| 3 | `H-PNL-01` readiness HIP4 `prob_stop_full` | Paper actif seulement; readiness buckets ajoutes, pas de live. | Buckets significatifs avec PF `>1.15`, Brier `<=0.23`, fills/capital realistes, preflight et tiny caps confirmes. |
| 4 | Variante future issue de `A-PNL-07` fill-quality | Audits P117/P118 implementes. `parent_plus50` est prometteur en research (`+16.31`, PF `3.1495`) mais trop petit et trop INJ-concentre; aucun filtre simple spread/depth/cost ni scale-in simple n'est promotable. | Tester un classifieur combine ou repricing/cap-only qui bat la baseline full-bot, n'efface pas les winners et ne concentre pas le gain sur un symbole/regime. |
| 5 | `C-PNL-02` external reference cap50 Pod C | Tres prometteur sur la fenetre recente couverte (`cap50_candidate_default_5m` `+40.05`) mais invalide OOS pour l'instant car la baseline reference coverage est `0%`. | Restaurer coverage reference sur baseline comparable, refaire P103/P121 full-bot, puis seulement envisager un cap-only rollbackable. |
| 6 | `C-PNL-03` oil CL/BRENTOIL pair dedupe | P120 dedupe positif (`+6.98`, PF `2.04`) mais seulement `12` maturations et brut repete tres negatif. | Shadow/OOS sur plus de jours oil, integrer closed+open P1-09 et verifier que le signal ne depend pas uniquement de 07:00 UTC/high-vol. |
| 7 | `C-PNL-05` liquidity floor Pod C | P122 `bucket_notional<100k` cap50 est positif sur baseline (`+4.70`) et live (`+1.56`) mais petit, live GOLD-only. | Valider OOS et avec fill/slippage; ne pas promouvoir de maker/taker sans modele de fill. |
| 8 | `A-PNL-02` recovery sizing Pod A | Code dormant; positif vs courant (`+2.79`) mais inferieur a P1-08 `cap50/cap50` et plus reducteur. | Ne remonter dans la liste que si une variante bat P1-08 simple avec moins de drawdown et moins de reductions inutiles. |

Non-candidats live actuels: P1-08 `cap50/cap10`, A-PNL-03 headroom cap,
relaxation globale `early_failure_exit`, A-PNL-05 cap-only microstructure et
P118 scale-in simple sur signaux repetes, P119 loss-probation cap-only tel quel,
C-PNL-04 session calendar cap-only tel quel, C-PNL-05 spread/cout pur, P120
oil pair brut non dedupe, plus les anciennes pistes listees dans
"Ne pas reproposer tel quel".

## Decision actuelle

- A/C: ne pas couper. Priorite a la reduction de taille conditionnelle, a la
  qualite d'execution et a la microstructure. P1-08 `cap50/cap10` est rejete;
  P1-08 `cap50/cap50` reste le candidat le plus proche, mais seulement apres
  confirmation live explicite et audit adapte. A-PNL-02 est code en dormant
  pour replay/shadow, mais la variante `base70/partial85` ne bat pas P1-08
  `cap50/cap50`. A-PNL-05 garde son score microstructure en shadow/audit, mais
  le cap-only `<0.42`/`<0.56` est rejete car negatif sur la fenetre live.
  A-PNL-04 montre que `early_failure_exit` evite plus de pertes qu'il ne manque
  de recoveries sur la fenetre live; ne pas le relaxer globalement. Aucun flag
  live, aucune activation live ni hausse de cap n'est incluse. A-PNL-07 ajoute
  un audit fill-quality utile pour les prochaines variantes, mais ne valide pas
  de filtre live simple sur spread, profondeur ou cout d'entree. P118 montre
  aussi qu'un scale-in simple sur les signaux repetes est negatif en premier
  add-on et trop fragile/concentre quand tous les add-ons sont pris. La variante
  filtree `parent_plus50` devient une hypothese research a tester
  hors-echantillon, pas un changement live. A-PNL-08/P119 ameliore le PnL
  cap-only sur les trades ouverts, mais cappe trop de winners et degrade le PF
  post-split; garder en research, pas live. Cote Pod C, C-PNL-02, C-PNL-03 et
  C-PNL-05 donnent des signaux a conserver en shadow, mais aucun ne passe le
  seuil live: C-PNL-02 manque une baseline reference couverte, C-PNL-03 est trop
  petit et C-PNL-05 est trop GOLD-concentre. C-PNL-04 session calendar ne sort
  pas de regle robuste.
- HIP4: ne pas passer live maintenant. Continuer `prob_stop_full` en paper actif,
  enrichir Nautilus/observability et ne promouvoir le shadow que s'il prouve une
  amelioration nette sur settlements reels avec fills realistes.
