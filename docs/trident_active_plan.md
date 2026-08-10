# TRIDENT Active Plan

Date: `2026-05-24`

## Status

- `ACTIVE_SINGLE_SOURCE_OF_TRUTH`
- Ce fichier est la feuille de route courante. Les autres documents sont des archives, des notes de recherche, ou des details d'implementation.
- En cas de contradiction avec un ancien doc, ce fichier gagne.
- Decision ops `2026-05-24`: split en deux apps deployables separement.
  `TRIDENT` = `Pod A` + `Pod C` uniquement. `TRIDENT-HIP4` =
  `HIP4OutcomeEdgePod` uniquement, en mainnet paper par defaut.
- Objectif actuel TRIDENT: transformer le canary live testnet `Pod A` + `Pod C`
  valide techniquement en burn-in propre, puis preparer le canary mainnet
  tiny-size.
- `Pod B HIP-4 Outcome` ne fait plus partie du deploiement TRIDENT A/C. Il reste
  analyse/recherche mainnet paper dans l'app separee `TRIDENT-HIP4`.

## Split Ops `TRIDENT` / `TRIDENT-HIP4`

Source de verite operationnelle depuis le `2026-05-24`:

- App `TRIDENT`:
  - repertoire serveur par defaut: `/opt/trident`;
  - compose: `docker-compose.trident.yml`;
  - deploy: `./deploy.sh`;
  - serveur: `scripts/trident_server.sh`;
  - fetch: `scripts/fetch_trident_data.sh`;
  - services attendus: `trident-api`, `pod-a-live`, `pod-c-live`,
    `tradfi-funding-collector`, `funding-collector` si active;
  - UI dashboard: `/` et `/dashboard` exposent uniquement Pod A/Pod C, avec
    synthese operateur A/C, positions ouvertes/fermees et etats de marche
    explicites crypto + clusters hors crypto;
  - aucun service Pod B, HIP-4 outcome, `hip4-outcome-dry-run` ou observer
    mainnet ne doit etre demarre ni affiche depuis cette app.
- App `TRIDENT-HIP4`:
  - repertoire serveur par defaut: `/opt/trident-hip4`;
  - compose: `docker-compose.hip4.yml`;
  - deploy: `./trident-hip4/deploy.sh`;
  - serveur: `scripts/trident_hip4_server.sh`;
  - fetch: `./trident-hip4/fetch_data.sh`;
  - port dashboard/API par defaut: `3001`, UI HIP-4 native sur `/`,
    `/dashboard` et `/hip4-outcome`;
  - services attendus en paper: `hip4-api`, `hip4-outcome-paper`,
    `hip4-mainnet-observer`; l'observer mainnet standalone est actif par
    defaut pour completer la collecte, et peut etre coupe explicitement avec
    `--without-mainnet-observer`;
  - mode par defaut: `HIP4_OUTCOME_MODE=paper`, config
    `config/hip4_outcome_mainnet_paper.toml`;
  - onglet `Dashboard` HIP-4 ouvert par defaut: capital disponible/utilise,
    PnL, win rate, runtime, mode, network et positions ouvertes/fermees; les
    diagnostics detailles, short-expiry et opportunites restent dans `Details`
    / `Observation`.
- Si une section plus ancienne parle de "live hybride A/C + Pod B" dans le meme
  deploiement, cette section est historique. Le split ops ci-dessus gagne.

## Lecture Rapide

- Config prod/dry-run principale: `config/trident.toml`.
- Mode cible live TRIDENT: `Pod A` crypto core et `Pod C` tradfi en vrais
  ordres apres preflight. `Pod B HIP-4 Outcome` est gere par `TRIDENT-HIP4`.
- Etat serveur `2026-05-21`: redeploiement propre en `live/testnet` avec
  `Pod A` + `Pod C` en vrais ordres testnet et `Pod B HIP-4 Outcome` force en
  `mainnet paper`. Le baseline de burn-in repart du demarrage conteneur
  `2026-05-21T06:07:35Z`.
- Reconciliation post-redeploiement: `Pod C` a recupere la position SOL
  existante depuis le state store, `Pod A` l'a classee
  `external_known_positions`, et aucun `unknown_exchange_positions`,
  `missing_exchange_positions`, `side_mismatches`, `open_orders` inconnu ou
  `trigger_orders` orphelin n'a ete observe.
- Les valeurs exchange sont maintenant prioritaires pour les positions live
  existantes:
  - `target_notional_usd` local = `abs(size) * entryPx`;
  - `current_notional_usd` = `positionValue`;
  - `margin_usd` = `marginUsed`;
  - `unrealized_pnl_usd` = `unrealizedPnl`;
  - levier/isolation viennent aussi de Hyperliquid quand disponibles.
- Sur Hyperliquid `unifiedAccount` / portfolio margin, `perp_account_value_usd`
  peut rester a `0` alors que le capital utilisable est dans le solde spot USDC.
  TRIDENT expose alors `hl_available_usd` depuis `spot_usdc_available` avec
  source `unified_spot_usdc`; ne pas classer cette situation comme absence de
  collateral tant que `hl_available_usd` est positif et la reconciliation est
  `ready=true`.
- Ajustement operateur `2026-05-26`: `live_max_order_notional_usd` passe de
  `100` a `250` pour debloquer le canary Pod A sans ouvrir tout le sizing
  calcule; review requise apres les premiers cycles `open -> close`.
- Ajustement operateur `2026-06-02`: Pod A a rattrape les pertes liees au
  probleme de config des premiers jours live et repasse positif. Le cap live
  A/C passe de `250` a `500` notionnel max, par palier prudent avant toute
  ouverture plus large du sizing strategique.
- Ajustement Pod C `2026-06-02`: sur `silver`, les trades live montrent des
  excursions favorables qui approchent le break-even/trailing sans toujours
  securiser le trade. Le mode cluster `silver` garde son TP actuel mais abaisse
  `break_even_multiplier` a `0.90`, `trailing_activation_multiplier` a `0.75`
  et `trailing_distance_multiplier` a `0.75`; validation par replay requise
  avant de considerer ce reglage comme definitivement promu.
- Ajustement operateur `2026-06-04`: apres review serveur A/C, Pod A repasse
  negatif surtout a cause des stops catastrophe pendant la grace live
  `trend_pullback_long` sous cap `500`: les stops reels peuvent sortir bien
  au-dela du stop planifie. Le cap live A/C revient a `250`; ne pas remettre
  `live_block_stop_grace_setups=true` sauf freeze explicite, car ce guardrail
  bloque les entrees au lieu de corriger le chemin de stop. Pod C bloque
  temporairement `XYZ:SILVER` (`pod_c.blocked_symbols`) apres `4/4` stops
  perdants depuis le `2026-06-02`; continuer a observer silver, mais ne pas
  reautoriser l'execution sans replay/review dedie.
- Ajustement operateur `2026-06-05`: apres review serveur A/C pendant le selloff
  crypto proche de `BTC 60k`, garder `live_block_stop_grace_setups=false` pour
  ne pas neutraliser totalement les entrees de rebond, mais reduire le cap live
  A/C a `200` notionnel max. Les alts ayant produit les pires pertes recentes
  (`AAVE`, `ADA`, `AVAX`, `HYPE`, `ICP`, `NEAR`, `ONDO`, `PENDLE`, `TON`,
  `VVV`, `XRP`) sont exclus du pool tradable via
  `hyperliquid.tradable_blocked_symbols` et gardes en garde-fou via
  `pod_a.blocked_symbols`; continuer a les observer, mais ne pas les
  reautoriser sans review/replay dedie du chemin `trend_pullback_long` et de la
  perte reelle vs stop planifie.
- Ajustement Pod A live `2026-06-09`: objectif explicite = remonter le PnL sans
  arreter tous les trades. Pod A garde `live_block_stop_grace_setups=false`, mais
  le chemin live `trend_pullback_long` reduit le risque de queue: grace standard
  ramenee a `60m`, extension a `120m` seulement pour A-grade fort
  (`score>=9`, confidence `>=0.72`, sans watcher), stop catastrophe dynamique
  plafonne via multiplicateur/buffer/max bps, sortie locale `early_failure_exit`
  pendant la grace si le trade part rapidement contre le plan, sizing live
  qualite/correlation/loss-tax au lieu d'un blocage dur. Le cap live A/C reste
  `200` notionnel max et Silver reste bloque cote Pod C.
- Decision P1 `2026-06-18`: apres fetch/review frais, P1-03 est clos comme
  restauration reference externe Pod C + shadow valide, sans guardrail actif;
  P1-08 est clos en `research_only_no_live_change`; P1-09
  `oil_short_4h_time_gate` est promu en candidat short Pod C actif par decision
  operateur a risque accepte. La promotion est codee/configuree via
  `pod_c.p109_oil_short_enabled=true`, setup
  `p109_oil_short_4h_time_gate`, min confidence `0.67`, risk gate/caps live
  inchanges. Aucun redeploiement serveur ne doit etre suppose tant que
  `./deploy.sh` n'a pas ete lance explicitement apres preflight.
- Implementation PnL `2026-06-22`: premiere tranche du plan
  `docs/pnl_improvement_implementation_plan_20260622.md` codee sans activation
  live automatique. Pod A dispose maintenant d'une policy P1-08 cap-only
  configurable (`dynamic_symbol_guard_live_sizing_enabled=false` par defaut)
  qui peut reduire `throttle` et `quarantine` sans bloquer les trades; toute
  activation exige replay/review/preflight/confirmation. HIP-4 run review expose
  des `readiness_buckets` par underlying/side/market_type/expiry_bucket avec
  fenetre post `2026-06-10T00:00:00Z`. Le fetch A/C expose un stoplight P1-09
  oil combinant trades promus fermes et PnL latent oil ouvert; le smoke local
  `server-data/reviews/20260622T085010Z/` indique `hold_exposure`, donc pas de
  hausse d'exposition oil a ce stade.
- Deploiement A/C `2026-06-22`: redeploiement live/mainnet effectue sans
  activer `dynamic_symbol_guard_live_sizing_enabled`; preflight Pod A/Pod C OK,
  services `trident-api`, `pod-a-live`, `pod-c-live`,
  `tradfi-funding-collector` et `funding-collector` actifs. Fetch post-deploy
  `server-data/reviews/20260622T101548Z/review_summary.md` en `PASS`, mode
  `live`, network `mainnet`, reconciliations Pod A/Pod C ready, aucun conflit
  d'ownership. P1-09 oil est `hold_exposure` avec closed+open PnL `-0.4198`,
  donc pas de hausse d'exposition oil.
- Decision P1-08 live sizing `2026-06-22`: le replay dedie
  `server-data/replay_reports/p108_dynamic_symbol_guard_live_sizing_halfsize_20260622T102000Z/`
  interdit l'activation de la variante `cap50/cap10`:
  `live_sizing_55_75_cap50_cap10_rejected` degrade A/C de `-6.92` vs courant
  (`-47.11` total, Pod A `-38.46`, `71` trades Pod A, PF `0.4767`). La variante
  candidate `cap50/cap50` ameliore faiblement la fenetre live (`+2.91`,
  total `-37.28`, Pod A `-28.63`, PF `0.6879`, max drawdown Pod A `37.37`) mais
  reste `research_only_no_live_change`: ne pas activer live sans confirmation
  explicite, audit `live_action_unchanged` adapte et nouveau fetch/review. La
  config locale dormante est alignee sur `quarantine_multiplier=0.50`, mais ce
  patch n'a pas ete redeploye et n'a aucun effet live tant que
  `dynamic_symbol_guard_live_sizing_enabled=false`.
- Implementation A-PNL-01 audit P108 `2026-06-23`: `scripts/fetch_trident_data.sh`
  distingue maintenant `expected_live_action_changed` et
  `unexpected_live_action_changed` pour P1-08. En shadow/policy desactivee,
  `symbol_guard_live_action_unchanged=false` reste un `FAIL`; avec
  `dynamic_symbol_guard_live_sizing_enabled=true` dans la config rapatriee et
  `dynamic_symbol_guard_live_sizing_active=true` dans les logs, le changement
  devient attendu et ne fait pas echouer la review. Tests locaux OK et review
  locale `server-data/reviews/20260623T091959Z/` en `PASS`, P1-08
  `2000/2000` records avec shadow, `unexpected_live_action_changed=0`.
- Decision A-PNL-01 `2026-06-23`: l'audit P108 est pret pour une eventuelle
  review post-deploiement, mais aucune activation live n'est faite. Point de
  vigilance: la config serveur rapatriee a encore
  `dynamic_symbol_guard_quarantine_multiplier=0.10` avec le flag desactive,
  tandis que la config locale candidate est `0.50`. Avant tout live, il faudra
  confirmation explicite, redeploiement de la variante `cap50/cap50`, puis
  fetch/review verifiant `unexpected_live_action_changed=0` et la config serveur
  `throttle=0.50`, `quarantine=0.50`.
- Promotion A-PNL-01 `2026-06-23T09:59Z`: decision operateur executee, variante
  P1-08 `cap50/cap50` deployee en live/mainnet via
  `./deploy.sh --start --mode live --network mainnet --config config/trident.toml`.
  Config serveur verifiee avec `dynamic_symbol_guard_live_sizing_enabled=true`,
  `dynamic_symbol_guard_throttle_multiplier=0.50` et
  `dynamic_symbol_guard_quarantine_multiplier=0.50`. Preflight Pod A/Pod C OK,
  services `trident-api`, `pod-a-live`, `pod-c-live`,
  `tradfi-funding-collector` et `funding-collector` actifs. Fetch post-deploy
  `server-data/reviews/20260623T095928Z/review_summary.md` en `PASS`; P108
  `unexpected_live_action_changed=0`. Les logs recents n'avaient pas encore de
  record Pod A avec sizing actif au moment de la review
  (`live_sizing_active_records=0`), donc surveiller la prochaine fenetre de
  signaux pour confirmer les premiers caps effectifs.
- Implementation Pod A live loss-reaction `2026-06-24`: decision operateur a
  risque accepte. La config locale active `live_loss_reaction_enabled=true` pour
  Pod A uniquement: apres un parent `trend_pullback_long` ferme perdant par
  `stop_hit` ou `exchange_closed_stop_loss`, TRIDENT peut ouvrir une unique
  reaction `loss_reaction` en sens oppose, avec sizing/exits clones, cap live
  A/C applique, et garde-fou `live_ready_for_entries`; un trade
  `loss_reaction` perdant ne declenche jamais de cascade. Replay global
  research-only sur `2026-04-05 -> 2026-05-13`: `opposite_stop_hit_pod_a`
  ameliore de `+47.17` avec `2` reactions, tandis que la variante systematique
  sans garde-fous degrade de `-81.17`.
- Deploiement Pod A live loss-reaction `2026-06-24T15:26Z`: deploiement
  live/mainnet effectue via
  `./deploy.sh --start --mode live --network mainnet --config config/trident.toml`.
  Preflight Pod A/Pod C OK, services `trident-api`, `pod-a-live`, `pod-c-live`,
  `tradfi-funding-collector` et `funding-collector` actifs. Fetch post-deploy
  `server-data/reviews/20260624T152835Z/review_summary.md` en `PASS`, mode
  `live`, network `mainnet`, reconciliations Pod A/Pod C ready, aucun conflit
  d'ownership, aucune position ouverte et aucun open order inconnu. Le runtime
  Pod A expose `live_loss_reaction.enabled=true` avec compteurs reaction encore
  a `0`; surveiller `live_loss_reaction` dans le runtime status et les trades
  `setup=loss_reaction` sur les prochaines clotures perdantes.
- Promotion locale Pod A chart patterns `2026-07-06`: decision operateur a
  risque accepte malgre gate statistique refuse (`0/24` profils passes, sample
  `<10`). La config locale active `pod_a.chart_patterns.enabled=true` avec
  `max_new_signals_per_batch=1`, `max_open_positions=1`,
  `chart_double_bottom_long` et `chart_triangle_breakout_long`. Replay integre
  full-bot cap-aware apres promotion:
  `server-data/replay_reports/chart_pattern_promoted_livecaps_20260706T000000Z/`
  sort `+83.99 USD` total vs baseline cap-aware `+77.08`, Pod A
  `+63.63` vs `+56.72`, Pod C inchange `+20.36`. La contribution chartiste
  fermee est `+6.91 USD` via `2` trades `chart_double_bottom_long`; aucun trade
  `chart_triangle_breakout_long` accepte sur cette fenetre. Aucun redeploiement
  serveur n'est effectue dans cette passe: avant effet live serveur, lancer
  preflight/deploy explicite puis fetch/review post-deploy.
- Implementation Robust PnL Lab `2026-06-23`: demarrage du plan
  `docs/plan_evos_robustes.md` par un harness commun research-only
  `scripts/run_pnl_robust_candidate_lab.py` et sa suite
  `tests/test_pnl_robust_candidate_lab.py`. Rapport initial:
  `server-data/replay_reports/pnl_robust_candidate_lab_20260623/pnl_robust_candidate_lab.md`
  (`27` candidats, `49` periodes, `93` decisions Pod A). Aucun candidat n'est
  encore `promotable_candidate`; les meilleurs signaux restent shadow, surtout
  `pod_c_external_reference::cap50_candidate_default_5m` (`+40.05` USD sur la
  fenetre couverte, bloque par coverage/OOS insuffisant). Le candidat
  `pod_a_combined_sizing_v0` est rejete (`-16.73` USD, cappe trop de PnL
  gagnant). Aucun changement live/config/deploy/fetch.
- Implementation A-PNL-02 `2026-06-22`: Pod A expose les stats rolling
  `symbol/setup` dans les `setup_details` (`trades`, PnL, expectancy, profit
  factor) et dispose d'une policy dormante de recovery sizing
  (`dynamic_symbol_guard_recovery_sizing_enabled=false` par defaut). Le candidat
  reduit les symboles non prouves a `0.70`, les recoveries partielles a `0.85`,
  et ne revient au plein sizing qu'apres `>=4` trades rolling avec PF `>=1.05`
  et expectancy positive. Le replay P1-08 expose le scenario counterfactual
  `live_sizing_recovery_55_75_base70_partial85`. Aucun effet live, aucun
  changement de cap, aucun deploy et aucun nouvel artefact fetch tant que le flag
  reste `false`; validations locales OK, dont suite complete `rtk uv run pytest`
  (`664` passed, `1` warning historique), detaillees dans
  `docs/pnl_improvement_implementation_plan_20260622.md`.
- Decision A-PNL-02 `2026-06-22`: replay full-window
  `server-data/replay_reports/p108_recovery_sizing_20260622/` sur
  `2026-05-14T00:00:00Z -> 2026-06-22T10:14:00Z`. La variante
  `live_sizing_recovery_55_75_base70_partial85` ameliore le courant de `+2.79`
  (`-37.40` total, Pod A `-28.75`, PF `0.6866`, max DD `37.49`) mais reste
  inferieure au P1-08 simple `cap50/cap50` (`+2.91`, total `-37.28`, Pod A
  `-28.63`, PF `0.6879`, max DD `37.37`) avec plus de reductions de cap. Garder
  A-PNL-02 en `research_only_no_live_change`; ne pas activer live tel quel.
- Implementation A-PNL-03 `2026-06-22`: ajout d'un cap headroom A-grade dormant
  (`a_grade_size_headroom_cap_enabled=false`). Si active en replay, le label
  A-grade et les exits restent inchanges, mais le scale taille applique ne peut
  pas depasser la marge symbole ni le risk budget initial; les champs
  `a_grade_requested_size_scale` et `a_grade_size_headroom_cap_*` sont exportes.
  P1-05 `strong_frozen_1p00` reste une piste deja testee/rejetee, pas une
  proposition live. Validations locales OK, dont suite complete
  `rtk uv run pytest` (`667` passed, `1` warning historique).
- Decision A-PNL-03 `2026-06-22`: replay live-window filtre
  `server-data/replay_reports/p105_a_grade_headroom_cap_live_20260622/` sur
  `2026-05-14T00:00:00Z -> 2026-06-22T10:14:00Z`. `headroom_cap_current` cappe
  `69` trades A-grade et ameliore seulement de `+0.08` A/C (`-40.11` vs
  `-40.19`; Pod A `-31.46` vs `-31.54`; PF `0.6762` vs `0.6756`; max DD `41.06`
  vs `41.14`). Garder A-PNL-03 en `research_only_no_live_change`; ne pas activer
  live tel quel.
- Implementation A-PNL-04 `2026-06-22`: ajout de l'audit local
  `scripts/run_p116_early_failure_post_exit_audit.py` pour les trades Pod A
  fermes par `early_failure_exit`. P1-02 avait deja couvert la sensibilite
  globale des exits; P116 ne repropose pas de disable global, il desactive
  seulement EFE en simulation per-trade et suit le trade jusqu'au prochain
  stop/trailing/break-even/time-stop/stop catastrophe naturel. Aucun flag live,
  aucun deploy, aucun changement de fetch et aucun ordre.
- Decision A-PNL-04 `2026-06-22`: replay complet
  `server-data/replay_reports/p116_early_failure_post_exit_20260622/` a partir
  de la baseline full-bot courante no-dedupe `-40.19` A/C (`-31.54` Pod A) sur
  `2026-05-14T00:00:00Z -> 2026-06-22T10:14:00Z`. Sur les `41` trades EFE,
  PnL original `-72.38` vs PnL naturel sans EFE `-80.84`, delta `-8.46`.
  EFE manque `6` winners et `6` reductions de perte (`19.30` USD de recovery
  manquee), mais evite `29` deteriorations (`27.76` USD de perte evitee);
  avg post-exit MFE `30.77` bps vs MAE `94.47` bps. Garder
  `early_failure_exit` actif; ne pas promouvoir de relaxation/desactivation
  globale. Validations locales OK, dont suite complete `rtk uv run pytest`
  (`678` passed, `1` warning historique) et `rtk git diff --check`.
- Implementation A-PNL-05 `2026-06-22`: ajout d'un score microstructure Pod A
  en shadow dans les `setup_details` (`microstructure_shadow_score`, bucket
  `poor/weak/ok/strong`, sous-scores spread, flow, microprice, depth, activite,
  range et churn). Le contexte Pod A reutilise les champs microstructure deja
  presents dans les snapshots live; `scripts/export_trident_audit_pack.py`
  exporte les champs `microstructure_shadow_*` et `p115_*`. Nouveau replay local
  `scripts/run_p115_microstructure_entry_replay.py`, sans flag live, sans deploy
  et sans changement de fetch.
- Decision A-PNL-05 `2026-06-22`: replay complet
  `server-data/replay_reports/p115_microstructure_entry_20260622/` sur baseline
  officielle avril/mai et live
  `2026-05-14T00:00:00Z -> 2026-06-22T10:14:00Z`. Baseline neutre (`77.08`
  total, Pod A `56.72`) meme avec `57` plan caps et `4` trades fermes cappes
  sur `<0.56`. Live negatif: `micro_cap_poor50_lt42` degrade de `-1.02` A/C
  (`-41.21` vs `-40.19`, Pod A `-32.56`, PF `0.6651`, max DD `42.16`) et
  `micro_cap_weak50_lt56` degrade de `-0.13` A/C (`-40.32`, Pod A `-31.67`,
  PF `0.6701`, max DD `41.29`). Le bucket live `poor` est gagnant (`+9.61`) et
  le pire bucket est `strong` (`-21.62`); garder le score en audit/shadow, ne
  pas promouvoir la policy cap-only microstructure. Validations locales OK,
  dont `rtk uv run pytest` (`675` passed, `1` warning historique) et
  `rtk git diff --check`.
- Implementation A-PNL-07 `2026-06-23`: ajout de l'audit local
  `scripts/run_p117_fill_quality_audit.py` pour mesurer, sans effet live, cout
  d'entree attendu, spread, depth/touch notional, signaux acceptes mais non
  ouverts, rejets risk gate et retours directionnels 1/5/15m. `PodABacktestRunner`
  expose `skip_reason` dans le journal backtest et peut omettre les
  `signal_review` filtres pour ces audits; `scripts/export_trident_audit_pack.py`
  exporte aussi `execution.skip_reason`. Aucun flag live, aucun ordre, aucun
  deploy et aucun changement fetch requis.
- Decision A-PNL-07 `2026-06-23`: replay full-window
  `server-data/replay_reports/p117_fill_quality_audit_20260623/` sur
  `2026-05-14T00:00:00Z -> 2026-06-23T00:00:00Z`. `94406` records, `5718`
  signaux Pod A, `93` ouvertures, `57` acceptes non ouverts, `5568` rejetes.
  Les ouvertures font `-60.05` USD, PF `0.8078`; les buckets simples ne sont pas
  monotoniques (`depth <1x` positif, `1-2x` positif, `gte_10x` negatif; cout
  `4-8bps` positif, `<1bps` negatif). Garder A-PNL-07 en
  `research_only_no_live_change`; ne pas promouvoir de veto/cap simple
  spread/depth/cost. Prochaine piste: classifieur combine ou repricing/cap-only
  apres analyse des `57` acceptes non ouverts.
- Implementation A-PNL-07b/P118 `2026-06-23`: ajout de l'audit local
  `scripts/run_p118_repeated_signal_scale_in_audit.py`, qui consomme le journal
  P117 compact et simule des add-ons hypothetique sur les signaux acceptes mais
  non ouverts pour cause `portfolio_open_rejected`. Les scenarios incluent les
  add-ons bruts et des filtres live-compatibles bases sur le PnL latent du trade
  parent (`>=0`, `>=25`, `>=50` bps). Aucun flag live, aucune config d'add-on,
  aucun ordre, aucun deploy et aucun changement fetch requis.
- Decision P118 `2026-06-23`: replay
  `server-data/replay_reports/p118_repeated_signal_scale_in_20260623/`, `57`
  opportunites acceptees/non ouvertes et `57` rattachees a un trade parent.
  Les variantes premier add-on sont negatives (`first_add25_cap` `-9.62` USD,
  PF `0.6422`; `first_add50_cap` `-9.42` USD, PF `0.7235`). La variante
  `all_add25_cap` est seulement legerement positive (`+3.33` USD, PF `1.0825`)
  avec `8919.41` USD de notional hypothetique et un gain surtout concentre sur
  INJ/trades parents en `trailing_stop`. Le filtre `all_add25_parent_plus50_cap`
  ressort mieux (`+16.31` USD, PF `3.1495`, `16` add-ons, `2351.15` USD de
  notional hypothetique) mais reste trop petit et INJ-concentre: hors INJ, le
  PnL tombe a `-0.55` USD, et le split temporel est fragile (`+15.52` USD avant
  le 2026-06-03, `+0.79` USD ensuite). Ne pas promouvoir de scale-in simple;
  garder `parent_plus50` comme hypothese research a valider hors-echantillon
  dans un classifieur combine.
- Implementation A-PNL-08/P119 `2026-06-23`: ajout du scenario P108
  `loss_probation_symbol_setup_cap50` et du filtre CLI `--scenarios` pour rejouer
  seulement les variantes utiles. Ajout de l'audit rapide
  `scripts/run_p119_loss_probation_cap_audit.py`, qui consomme le journal P117 et
  simule un cap-only `50%` apres historique rolling negatif par couple
  `symbol/setup`, avec rehabilitation si PF et expectancy rolling redeviennent
  positifs. Aucun flag live, aucun ordre, aucun deploy et aucun changement fetch.
- Decision A-PNL-08/P119 `2026-06-23`: audit
  `server-data/replay_reports/p119_loss_probation_cap_20260623/` sur les `93`
  trades Pod A ouverts de P117. Le cap-only ameliore le PnL ouvert
  `-60.05 -> -33.94` (`+26.11`) et le PF global `0.8078 -> 0.8584`, mais cappe
  `42/93` trades, dont `+93.46` USD de winners; le PF post-split baisse
  `0.5634 -> 0.5240`. Ne pas promouvoir tel quel. Garder comme piste research
  pour une variante moins destructrice de winners; replay full-bot seulement si
  une variante plus fine garde le delta sans degrader le PF post-split.
- Implementation C-PNL-02/P103 `2026-06-23`: le replay Pod C external reference
  expose des outcomes cap-only `50%` en plus des vetoes historiques. Le rapport
  `server-data/replay_reports/p103_pod_c_external_reference_cap50_20260623/`
  montre une forte amelioration sur la fenetre recente couverte
  (`cap50_candidate_default_5m` `+40.05`, coverage `91.67%`), mais la baseline
  avril/mai a `0%` de coverage reference. Garder en shadow/OOS; aucune
  promotion sans baseline reference complete, aucun flag live/deploy/fetch.
- Implementation C-PNL-02 forward OOS `2026-06-23`: P103 sait maintenant lire
  les `trade_close` du journal live Pod C via `--journal` et les references
  externes embarquees dans `setup_details`; le lab robuste accepte plusieurs
  rapports P103. Fetch frais `server-data/reviews/20260623T123504Z/` en `PASS`
  (`journal_setup_coverage=1000/1000`, `shadow_live_action_unchanged_false=0`).
  Rapport forward
  `server-data/replay_reports/p103_pod_c_external_reference_forward_oos_20260623/`:
  coverage `100%`, `2026-06-15_to_2026-06-21` base `+1.50` et
  `cap50_candidate_default_5m=-0.75`, puis `2026-06-22_to_2026-06-23` base
  `-9.29` et `cap50_candidate_default_5m=+2.95`. Lab agrege
  `server-data/replay_reports/pnl_robust_candidate_lab_20260623/`: C-PNL-02
  reste seulement `shadow_candidate` (`+42.24` total couvert, mais baseline
  insuffisante et une fenetre couverte negative). Aucun changement live/config
  ou deploy; prochaine piste = variante `fresh-only` sans cap stale/missing.
- Implementation C-PNL-02 v2 fresh-only `2026-06-23`: P103 expose quatre
  variantes `fresh-only` (`fresh_abs_premium_gt_50`,
  `fresh_counter_momentum_5m_6bps`, `fresh_candidate_default_5m`,
  `fresh_candidate_loose_5m`) et leurs outcomes cap-only `50%`. Elles ignorent
  les references manquantes/stale et ne cappent que si la reference externe est
  disponible et agee de `<=900s`; les vieux payloads `setup_details` zeroes ne
  sont plus pris pour des references valides. Replay historique + forward:
  `server-data/replay_reports/p103_pod_c_external_reference_cap50_20260623/`
  et
  `server-data/replay_reports/p103_pod_c_external_reference_forward_oos_20260623/`.
  Lab agrege mis a jour
  `server-data/replay_reports/pnl_robust_candidate_lab_20260623/`: `31`
  candidats, `77` periodes, aucun `promotable_candidate`. Le meilleur signal
  est `pod_c_external_reference::cap50_fresh_candidate_default_5m`
  (`+34.28`, `3/0` periodes couvertes positives, concentration max `50%`),
  avec `+29.85` sur `2026-05-24_to_2026-06-11`, `+1.48` sur
  `2026-06-15_to_2026-06-21` et `+2.95` sur `2026-06-22_to_2026-06-23`.
  Il reste `shadow_candidate` a cause de la baseline ancienne non couverte
  (`insufficient_coverage_periods=1`). Aucun changement live/config/deploy;
  prochaine etape = plus de forward OOS frais avant tout flag live.
- Observabilite C-PNL-02 v2 `2026-06-23`: le shadow Pod C exporte maintenant
  les champs `would_block_external_reference_fresh_*` correspondant aux quatre
  variantes fresh-only, toujours avec
  `external_reference_shadow_live_action_unchanged=true`. Le fetch P1-03 compte
  ces gates dans `by_gate`, et `scripts/export_trident_audit_pack.py` les inclut
  dans le pack compact. Aucun comportement live/config ne change; un deploy
  code-only serait necessaire plus tard pour les voir dans de nouveaux journaux
  serveur.
- Implementation A-PNL-08/P119 v2 `2026-06-23`: la variante robuste
  `loss_probation cap50_lb8_min2_pnl-16_pf0p6` est codee en policy Pod A
  activable par config et desactivee par defaut:
  `dynamic_symbol_guard_loss_probation_sizing_enabled=false`,
  `multiplier=0.50`, `min_closed_trades=2`,
  `max_pnl_usd=-16.0`, `max_profit_factor=0.60`. Le fetch P1-08 et l'audit pack
  exposent maintenant `loss_probation_sizing_active_records` et les raisons de
  cap. Replay final:
  `server-data/replay_reports/p119_loss_probation_cap_v2_20260623/`; lab final:
  `server-data/replay_reports/pnl_robust_candidate_lab_20260623/`. Resultat:
  seul `promotable_candidate` du lab, `+30.95` USD, `2/0` periodes positives,
  PF global `0.8078 -> 0.8834`, concentration max `29.55%`. Aucun deploy/live
  change effectue.
- Implementation C-PNL-02 cap dormant `2026-06-23`: Pod C dispose maintenant
  d'une policy fresh-only cap-only configurable et desactivee par defaut:
  `external_reference_fresh_cap_sizing_enabled=false`,
  `external_reference_fresh_cap_gate="fresh_candidate_default_5m"`,
  `external_reference_fresh_cap_multiplier=0.50`. `PodCLiveRunner` applique la
  policy apres l'annotation shadow et avant le cap notional live/risk gate. Le
  fetch P1-03 distingue `expected_live_action_changed` et
  `unexpected_live_action_changed`; review locale
  `server-data/reviews/20260623T131803Z/review_summary.md` en `PASS` avec
  `unexpected_live_action_changed=0`. Replay/lab: `cap50_fresh_candidate_default_5m`
  reste le meilleur candidat Pod C (`+34.28`, `3/0` periodes couvertes
  positives) mais reste shadow/risk-accepted car la baseline ancienne a `0%` de
  coverage.
- Priorite promotion PnL `2026-06-23`: si l'operateur accepte une prise de
  risque controlee sans shadow long, promouvoir d'abord A-PNL-08/P119 v2
  loss-probation, puis C-PNL-02 fresh-only. A-PNL-01 cap50/cap50 reste live et
  a monitorer. Toutes les autres pistes robustes testees sont rejetees ou
  shadow-only: combined sizing v0, recovery sizing, A-grade headroom,
  microstructure, fill/depth simple, scale-in P118, oil dedupe, session/liquidity
  et execution-cost simples.
- Promotion PnL `2026-06-23T13:33Z`: decision operateur executee, les deux
  meilleures evolutions sont deployees en live/mainnet:
  `dynamic_symbol_guard_loss_probation_sizing_enabled=true` pour Pod A et
  `external_reference_fresh_cap_sizing_enabled=true` pour Pod C, avec caps `0.50`
  et seuils backtestes inchanges. Commande:
  `./deploy.sh --start --mode live --network mainnet --config config/trident.toml`.
  Preflight Pod A/Pod C OK, reconciliations ready, pas de position inconnue ni
  ordre orphelin. Fetch post-deploy
  `server-data/reviews/20260623T133321Z/review_summary.md` en `PASS`, mode
  `live`, network `mainnet`, config serveur verifiee avec les deux flags a
  `true`. Deuxieme review apres ~1 minute de runtime
  `server-data/reviews/20260623T133539Z/review_summary.md` egalement en `PASS`
  avec `runtime_symbols_enriched=17` cote P1-03. Aucun nouveau cap effectif
  observe (`loss_probation_sizing_active_records=0`,
  `fresh_cap_sizing_active_records=0`) et aucun changement inattendu
  (`unexpected_live_action_changed=0` sur P1-03/P1-08); suivre les prochains
  signaux pour confirmer les premiers caps reels.
- Implementation C-PNL-03/P120 `2026-06-23`: ajout de l'audit local
  `scripts/run_p120_oil_relative_value_audit.py` sur les observations
  `p109_oil_shadow_*` Pod C. Le rapport
  `server-data/replay_reports/p120_oil_relative_value_20260623/` indique que le
  flux CL/BRENTOIL brut repete est negatif (`pair_confirmed` `-394.68`, PF
  `0.6764`), tandis que la version dedupee 240m est positive mais minuscule
  (`+6.98`, PF `2.0375`, `12` maturations). Garder comme hypothese oil
  shadow/OOS, pas live.
- Implementation C-PNL-04/P121 `2026-06-23`: ajout de l'audit local
  `scripts/run_p121_pod_c_session_liquidity_audit.py` sur baseline avril/mai et
  replay live P116 no-dedupe. Le rapport
  `server-data/replay_reports/p121_pod_c_session_liquidity_20260623/` ne valide
  aucun cap session robuste: `non_us_cash` aide le live (`+4.28`) mais detruit
  la baseline (`-37.79`), `us_late` aide la baseline (`+1.70`) mais degrade le
  live (`-0.06`). Research-only, pas live.
- Implementation C-PNL-05/P122 `2026-06-23`: ajout de l'audit local
  `scripts/run_p122_pod_c_execution_cost_audit.py` sur fees/spread/liquidite Pod
  C, sans simulation maker. Le rapport
  `server-data/replay_reports/p122_pod_c_execution_cost_20260623/` rejette les
  seuils spread/cout purs; seul `bucket_notional<100k` cap50 est positif sur les
  deux fenetres (`+4.70` baseline, `+1.56` live), mais trop petit et live
  GOLD-only. Shadow/OOS uniquement, pas de maker/taker live sans fill model.
- Incident live `2026-06-07`: Pod A a ouvert une position ARB mainnet
  (`oid=461196360588`, long `2446.4`, entry `0.0817`, cap live ~`200 USDC`)
  mais le state/journal n'a pas garde la position avant crash/restart. Pod A
  est entre en crash loop sur `unknown_exchange_positions=['ARB']` et Pod C a
  pause les entrees en voyant ARB inconnue. Recovery operateur: import ARB dans
  `runtime/trident/live_state_pod_a.json`, preflight Pod A/Pod C OK, puis
  pose d'un SL reduce-only exchange connu par le state (`oid=461525656182`,
  trigger `0.08039`). Correction code: `LiveExecutionVenue` ecrit maintenant une
  `pending_position` durable immediatement apres fill d'entree, puis reecrit
  apres les ordres protecteurs via callback Pod A/C; le rounding de prix live
  respecte aussi la limite Hyperliquid `6 - szDecimals` pour eviter les rejets
  de triggers sub-dollar (`Order has invalid price`). Verification post-fix:
  review A/C `server-data/reviews/20260607T112401Z/review_summary.md` en
  `PASS`, Pod A/Pod C `ready=true`, `unknown_exchange_positions=[]`,
  `trigger_orders=[]`. Ne pas contourner ce type d'incident avec
  `TRIDENT_LIVE_ALLOW_UNKNOWN_POSITIONS=true`; reconstruire le state confirme
  ou fermer manuellement en reduce-only.
- Ajustement HIP-4 mainnet paper `2026-06-02`: le PnL reste trop fragile
  (`14` settlements, profit factor proche de `1.06`) avec quelques pertes qui
  absorbent la majorite des gains. Le profil paper active un gate d'entree
  `min_shadow_kelly_size_usdc = 2.0` et abaisse `max_position_usdc` a `12`,
  taille mini-pratique avec buffer de quantization pour les prix observes, afin
  de ne plus transformer un signal Kelly faible en position paper fixe de
  `50 USDC`. Validation counterfactual locale:
  `server-data/hip4/replay_reports/hip4_kelly_gate_counterfactual_20260602.md`.
- Ajustement HIP-4 mainnet paper `2026-06-10`: apres replay
  `server-data/hip4/replay_reports/hip4_policy_market_audit_20260609T074732Z.md`,
  la policy active paper passe a `early_exit_policy = "prob_stop_full"`:
  tenir les positions jusqu'au settlement sauf stop defensif de probabilite.
  Les sorties EV/TP actives sont coupees sous cette policy; elles restent
  comparees en shadow via `ev_plus_2pct_partial_runner`,
  `ev_plus_2pct_full`, `hold_to_settlement` et `prob_stop_full`. L'audit marche
  devient recurrent dans `./trident-hip4/fetch_data.sh` et doit confirmer les
  `priceBinary` BTC-only ou lister tout nouvel underlying non-BTC tradable
  avant toute decision. Aucune execution mainnet HIP-4 n'est activee.
- Ajustement HIP-4 mainnet paper `2026-06-18`: promotion paper du guard
  `block_reference_divergence = true` sur tous les underlyings/sides/edge
  types, avec `reference_divergence_max_bps = 50` et
  `reference_divergence_min_rejected_sources = 1`. Motivation: tester en flux
  paper actif un signal shadow concret; la review
  `server-data/hip4/reviews/20260618T095429Z/hip4_outcome_run_review.md`
  identifie un cas `reference_divergence` a `-11.555 USDC` et le status
  confirme des sources externes multi-exchanges sur BTC/ETH/SOL/HYPE. Le
  profil observer mainnet reste non bloquant afin de garder une baseline brute.
  Aucune execution mainnet HIP-4 n'est activee.
- Incident live `2026-05-27`: les runners `pod-a-live` et `pod-c-live` ont ete
  stoppes manuellement sur le serveur a `17:01Z` apres une serie de closes Pod A
  `exchange_closed` perdants et une reconciliation Pod C KO sur `XYZ:GOLD`
  locale absente de l'exchange. Cause racine Pod C identifiee: la lecture privee
  ne fusionnait pas encore le builder-dex `xyz`, alors que la position
  `XYZ:GOLD` et ses ordres protecteurs etaient bien presents sous `dex="xyz"`.
  Cause racine Pod A identifiee: le live posait
  un SL exchange immediat alors que `PodAExecutor` applique une grace de stop
  locale/backtest de `165m` sur `trend_pullback_long` crypto. Correction
  `2026-05-29`: le live ouvre maintenant ces setups avec un SL catastrophe
  exchange pendant la grace, puis remplace ce SL par le SL normal apres
  expiration de la fenetre; les metadonnees d'ordres sont rechargees au
  redemarrage pour faire le remplacement/cancel proprement. Ne pas relancer
  Pod A/C live sans redeploiement cible et preflight.
  Redeploiement A/C effectue ensuite avec preflight OK; le reporting live
  reconstruit maintenant les trades fermes depuis les journaux append-only
  `logs/pod_a_live.jsonl` et `logs/pod_c_live.jsonl`, afin que les closes
  Hyperliquid reels restent dans l'historique UI et le PnL realise apres un
  redemarrage de runner.
- Observabilite UI `2026-05-29`: les onglets `Pod A` et `Pod C` exposent une
  table `Opportunites recentes` issue des journaux live et du snapshot runtime.
  Chaque candidat affiche son verdict risk/execution, la cause normalisee avec
  tooltip operateur, le prix de reference, notional/marge et les prix SL/TP
  calcules quand disponibles. Les listes de trades ouverts/fermes et `Activity`
  affichent aussi prix d'entree, prix courant/sortie et prix SL/TP.
- Le sizing live Pod A/C est cap-aware avant risk gate: quand un plan depasse
  `live_max_order_notional_usd`, le runner live abaisse le notionnel et le
  levier modelise en conservant la marge allouee si possible. Le cap respecte
  aussi les limites de levier par symbole (`margin_usd * max_leverage`). Cela
  concerne Pod A et Pod C; les replays dry-run/backtest gardent le sizing
  strategique non cappe.
- Le close live reduce-only utilise la taille exacte de la position exchange,
  au lieu de reconstruire une taille depuis un notionnel local potentiellement
  stale.
- Alertes crash email optionnelles `2026-05-29`: `trident-api`,
  `pod-a-live` et `pod-c-live` appellent un notifier best-effort sur exception
  Python non interceptée. Les paramètres SMTP/sendmail restent exclusivement
  dans `/opt/trident/.env.trident`; voir `docs/deployment.md`.
- La page `Status > Pods` affiche maintenant `PnL realise` et `PnL latent` dans
  la carte de chaque pod.
- Pods actifs dry-run TRIDENT: `Pod A` crypto core avec `a_grade_enabled`,
  `Pod C` tradfi.
- Pod B historique directionnel: legacy / non demarre par defaut.
- Nouveau Pod B: `HIP4OutcomeEdgePod`, branche HIP-4 outcome en mainnet paper.
  Le testnet HIP-4 a ete arrete: ses donnees n'etaient pas representatives,
  mais il a valide l'architecture, les signatures, les ordres, la reconciliation
  et le format de settlement.
- UI:
  - dashboard principal TRIDENT A/C: `http://<server>:3000/` ou `/dashboard`
  - monitoring HIP-4 separe: `http://<server>:3001/` ou `/hip4-outcome`
  - API HIP-4 separee: `http://<server>:3001/api/hip4-outcome`
- Pod B HIP-4 expose maintenant un `operator_brief` et une `short_expiry_watchlist`
  dans son status/API, afin de piloter explicitement les fenêtres proches expiry
  sans transformer le mainnet paper en claim de performance.
- Les blocages HYPE issus du testnet ont ete retires: ils n'ont pas de sens
  comme regle mainnet tant qu'une review mainnet-paper ne prouve pas un
  guardrail entry-time.
- Regle de promotion: aucune logique HIP-4 ne passe en execution mainnet sans
  dataset mainnet complet, calibration, replay comparable, dry-run mainnet
  propre, preflight separe, caps tiny-size et confirmation manuelle.
- Decision live A/C: le burn-in `live/testnet` est relance proprement depuis
  le redeploiement du `2026-05-21T06:07:35Z`, apres correction de la selection
  de fills de close stale et de la conservation des metadonnees d'ordres dans
  le state store. Si aucun incident bloquant n'apparait pendant `72h`,
  reevaluation des criteres de passage mainnet tiny-size le
  `2026-05-24T06:07:35Z`.

## Reference Prod Courante

Config canonique:

- `config/trident.toml`

Backtest officiel de reference courant:

- `server-data/replay_reports/official_baseline_current_cli_20260513.md`
- `server-data/replay_reports/official_baseline_current_cli_20260513.json`
- Statut des references:
  `server-data/replay_reports/BACKTEST_REFERENCE_STATUS_20260513.md`
- Comparaison experimentale ayant servi a promouvoir `evo11`:
  `server-data/replay_reports/pod_a_improvement_levers_20260513/comparison.md`
- Source de validation chemin production avant copie officielle:
  `server-data/replay_reports/pod_a_evo11_promoted_20260513.md`

Resultat de reference avant promotion `evo11`:

| Total | Pod A | Pod B | Pod C |
|---:|---:|---:|---:|
| `+669.69 USD` | `+590.58` | `0.00` | `+79.11` |

Resultat officiel courant avec `evo11_a_grade_boost_wider_exits`:

| Total | Pod A | Pod B | Pod C |
|---:|---:|---:|---:|
| `+859.83 USD` | `+780.72` | `0.00` | `+79.11` |

Rejeu du meme input avec le repo/config courants le `2026-05-19`:

| Total | Pod A | Pod B | Pod C |
|---:|---:|---:|---:|
| `+872.74 USD` | `+793.63` | `0.00` | `+79.11` |

Notes importantes:

- L'input de reference couvre `2026-04-05T19:45:00Z -> 2026-05-13T07:56:49Z`.
- La baseline officielle archivee reste `+859.83 USD`, mais le replay actuel
  du meme JSONL sort `+872.74 USD` (`+12.91`). L'ecart vient uniquement de
  `6` trades `HYPE trend_pullback_long` Pod A reintroduits par le rollback du
  veto HYPE; Pod C reste strictement inchange a `+79.11 USD`.
- L'input courant saute plusieurs dates sans collecte locale (`2026-04-19`,
  `2026-04-28`, `2026-04-29`, `2026-05-09 -> 2026-05-11`).
- Les replays de parite doivent inclure `collector + maintenance_refresh`; le collector-only n'est pas suffisant.
- Les caps de levier crypto live manquants ont ete ajoutes dans `config/trident.toml`.
- Le full replay ne force-enable plus Pod B: Pod B HIP-4 est independant et ne
  doit plus retirer de symboles, budget ou marge a Pod A.
- L'univers crypto Pod A a ete elargi avec `STRK`, `ONDO`, `BIO`, `VVV`,
  `SAGA`, `JUP`, `PENGU`, `INJ`, `PENDLE`, `TIA`, `DYM`, `ICP`, `ATOM`.
  `WLFI` reste exclu. Ces nouveaux symbols ne sont pas dans le JSONL full-replay
  historique; leur validation PnL reste donc light/API HL puis dry-run live.
- Validation OOS Pod A / Pod C du `2026-05-05`:
  - rapport: `server-data/replay_reports/pod_a_c_shortlist_validation_20260505.md`
  - input: `server-data/replay_inputs/pod_a_c_shortlist_oos_20260430_20260505`
  - baseline OOS: total `+8.67`, Pod A `-11.50`, Pod C `+20.17`.

## Etat Des Pods

### Pod A - Crypto Core

Statut: actif, reference principale crypto. Chemin live/testnet valide
techniquement avec reconciliation exchange stricte.

Point live `2026-05-19`:

- redemarrage serveur en `live/testnet` teste avec une position BTC deja ouverte
  cote Hyperliquid;
- Pod A n'a pas repris BTC car la position etait connue par le state store Pod C;
- rapport attendu observe: `external_known_positions=["BTC"]`, `ready=true`,
  pas de position locale et `live_trading_paused=false`;
- ne pas passer au canary mainnet tant que le burn-in testnet n'a pas plusieurs
  cycles propres de restart, sync, close/reopen et review logs.

Promu dans le profil repo:

- `pod_a.stop_grace_minutes = 165`, scope utile: `trend_pullback_long`.
- `pod_a.opposite_signal_debounce_minutes = 15`.
- `pod_a.a_grade_enabled = true`: boost selectif des entrees A-grade
  `trend_pullback_long` crypto, avec scaling `1.25x` / `1.40x` et exits plus
  larges (`break_even x1.20`, `trailing_activation x1.15`,
  `trailing_distance x1.35`). Backtest `2026-04-05 -> 2026-05-13`:
  `+190.14 USD` vs baseline corrigee.
- Vetoes MTF Pod A valides le `2026-04-27`, non confirmes sur l'OOS
  `2026-04-30 -> 2026-05-05` (`-4.16`, `5` vetoes), mais redevenus
  positifs sur le latest fetch `2026-04-05 -> 2026-05-16` (`+32.97`,
  `94` vetoes). Statut: conserver actifs, pas etendre sans nouveau replay.
- Veto BTC overextension 4h, scope BTC long, et veto XRP overextension 4h,
  scope XRP long: `no_effect` sur l'OOS `2026-05-05`, mais `keep` sur latest
  fetch `2026-04-05 -> 2026-05-16` (`+26.20`, `3` vetoes). Statut:
  conserver actifs, ne pas elargir.
- Veto HYPE `trend_pullback_long`: rollback applique dans `config/trident.toml`
  le `2026-05-17` avec `hype_trend_pullback_long_targeted.enabled = false`.
  Decision prise apres rejet sur l'OOS `2026-04-30 -> 2026-05-05` (`-12.03`,
  `3` trades HYPE vetoes qui auraient ete gagnants) et latest fetch
  `2026-04-05 -> 2026-05-16` (`-14.72`, `13` vetoes). A ne pas confondre avec
  les anciens blocages HYPE HIP-4, eux aussi retires. Le replay de la baseline
  officielle du `2026-05-19` confirme l'impact attendu: `6` trades HYPE
  reintroduits, `+12.91 USD`, Pod C inchange.
- Leviers testes mais non promus:
  - `evo1_adaptive_exit`: negatif, coupe trop vite la convexite.
  - `evo2_fee_aware_be`: legerement negatif dans la baseline corrigee.
  - `evo3_trend_health_sizing`: negatif, sous-size les winners.
  - `evo4_symbol_health`: negatif, throttle trop brutal.
  - `evo10_context_guardrail`: negatif (`-70.37 USD`), retire surtout des
    re-entries gagnantes BTC/SOL/NEAR.

Principes:

- Ne pas reactiver les shorts Pod A globalement.
- Ne pas relacher globalement `RangeAuction` ou `DeadZone`.
- Ne pas promouvoir `stop_grace_210m` sans validation hors echantillon.
- Toute nouvelle regle Pod A doit battre la baseline full-bot, pas seulement un test isole.
- Surveiller en dry-run l'impact `a_grade` sur `max_open_notional_usd`,
  `max_open_expected_loss_usd`, drawdown et fees; le levier augmente le PnL en
  backtest mais augmente aussi l'exposition brute.

### Pod C - Tradfi

Statut: actif, quasi stabilise. Canary `live/mainnet` tiny-size serveur en cours.

Point live `2026-05-19`:

- restart reel Pod C avec position BTC deja ouverte cote Hyperliquid: reprise OK;
- state live Pod C mis a jour depuis Hyperliquid:
  `entry_price=77326.0`, `target_notional_usd=94.33772`,
  `margin_usd` et `unrealized_pnl_usd` lus depuis l'exchange;
- status runtime Pod C utilise les valeurs exchange pour le PnL latent et la
  valeur courante quand elles sont disponibles;
- logs post-restart verifies sans `Traceback`, sans `Decimal is not JSON
  serializable`, et sans echec de reconciliation.

Backtest `Pod C off` du `2026-05-13`:

- rapport: `server-data/replay_reports/no_pod_c_20260513.md`
- input identique a la baseline officielle `2026-04-05 -> 2026-05-13`.
- resultat: total `+780.72 USD`, Pod A `+780.72`, Pod B `0.00`,
  Pod C `0.00`.
- Pod A est strictement identique a la baseline officielle avec Pod C actif
  (`155` trades, `+780.72 USD`, memes fees/rejets/exposition/drawdown).
- Conclusion courante: Pod C ne bloque pas Pod A dans ce replay. Le couper
  retirerait seulement sa contribution positive `+79.11 USD`; ne pas desactiver
  tant qu'un conflit live explicite de marge/routing n'est pas observe.

Promu dans le profil repo:

- `routing_revoke_grace_minutes_by_symbol`:
  - `XYZ:SP500 = 540`
  - `XYZ:XYZ100 = 540`
- Veto `silver_strong_extension_veto`, historiquement promu mais non confirme
  sur l'OOS `2026-04-30 -> 2026-05-05` (`-2.56`, `1` veto), puis confirme
  positif sur latest fetch `2026-04-05 -> 2026-05-16` (`+18.52`, `13`
  vetoes). Statut: conserver actif, ne pas etendre sans preuve.

Principes:

- Ne pas etendre la grace `routing_revoked` a `silver` ou `gold` sans nouvelle preuve.
- Ne pas relacher globalement les `stop_hit` Pod C.
- Garder `equity` et `fx` en observation, pas en nouvelle branche active.
- Gold: `gold_soft_extension_veto` devient watch tiny-sample (`+6.26`, `1`
  veto) mais ne doit pas etre promu sans repetition; `gold_strong_neutral_veto`
  reste sans effet, `gold_medium_neutral_veto` reste rejete (`-32.78`, `3`
  vetoes).
- Sweep live recent `2026-05-24 -> 2026-05-26` sur
  `pod_c.size_multiplier`:
  `server-data/replay_reports/pod_c_size_multiplier_070_20260526/pod_c_size_multiplier_sweep.json`.
  `0.55` reste le meilleur compromis observe (`3` trades, `-0.86 USD`);
  `0.65` et `0.70` debloquent plus de trades mais degradent fortement le PnL
  (`-9.00` et `-10.81 USD`) dans ce runner isole. Cette alerte est supersedee
  pour la decision prod par le replay full-bot Pod C-only ci-dessous, plus
  comparable a la baseline officielle.
- Replays full-bot Pod C-only du `2026-05-26` sur la baseline globale
  officielle et la fenetre live recente:
  `server-data/replay_reports/pod_c_cluster_multiplier_global_20260526/pod_c_cluster_multiplier_compare.json`
  et
  `server-data/replay_reports/pod_c_cluster_multiplier_recent_20260526/pod_c_cluster_multiplier_compare.json`.
  Baseline globale `0.55`: `+79.11 USD`, `41` trades. `global_070` monte a
  `+105.56 USD` mais change fortement le regime d'activite (`68` trades,
  fees `36.56`, drawdown `25.02` vs `17.39`). `gold_070` est le levier le plus
  propre observe: `+86.07 USD`, `41` trades, aucun changement sur la fenetre
  recente. `silver_070` / `metals_070` sont rejetes malgre un mieux recent:
  ils degradent le global (`+67.90` / `+74.86`) en augmentant beaucoup les
  trades silver. Decision operateur `2026-05-26`: promouvoir `global_070` en
  canary live explicite (`pod_c.size_multiplier = 0.70`) pour debloquer
  l'activite Pod C; review courte requise apres les premiers signaux/trades,
  avec attention particuliere aux fees, au drawdown et au volume de trades.
- Addendum `2026-06-16`: la decision canary ci-dessus est supersedee pour les
  prochaines promotions par le replay P2-02 frais
  `server-data/replay_reports/p202_pod_c_cluster_multiplier_20260616T150601Z/pod_c_cluster_multiplier_compare.json`
  sur `2026-05-24 -> 2026-06-11`. Baseline `0.55` silver debloque:
  `-165.28 USD`, `61` trades, fees `37.99`. La forme prod actuelle
  `current_live_blocked` reste negative (`-144.45`, `47` trades) mais evite une
  partie des pertes silver. Toutes les variantes de promotion degradent la
  baseline fraiche: `global_065` `-191.79`, `global_070` `-209.50`,
  `gold_070` `-182.25`, `silver_070` `-167.81`, `metals_070` `-184.78`.
  Decision courante: aucune promotion `global_070`/`gold_070`, conserver
  `XYZ:SILVER` bloque; `gold`, `silver`, `index` et `oil` sont bloques pour ces
  multipliers, `equity`/`fx` restent a surveiller faute de trades.

### Pod B Directionnel Historique

Statut: remplace / legacy.

Conclusion courante:

- Les variantes Pod B testees n'ont pas ete portefeuille-additives sur les snapshots comparables.
- Le silence de Pod B venait en partie d'une incoherence allocation/regime, mais la correction brute cannibalisait Pod A.
- Le service Docker historique reste disponible seulement sous profil `legacy_pod_b`.
- Il ne doit pas redevenir actif sans nouvelle validation full-bot.

## Pod B HIP-4 Outcome

Statut: implemente, integre comme remplacement complet du Pod B, repo safe par
defaut en paper, serveur actuellement oriente mainnet paper. Le testnet reste
un profil historique / preflight, pas une source de donnees exploitable.

Fichiers principaux:

- `app/trident/hip4_outcome/`
- `app/live/hip4_outcome_runner.py`
- `app/backtest/hip4_outcome_replay.py`
- `config/hip4_outcome_mainnet_paper.toml`
- `config/hip4_outcome_testnet.toml`
- `config/hip4_outcome_mainnet_observer.toml`
- `tests/test_hip4_outcome_pod.py`

Integration bot complet:

- `app/live/trident_dry_run_launcher.py` lance HIP-4 comme resultat `pod_b`; l'ancien runner directionnel n'est plus lance.
- `scripts/trident_server.sh` mappe le profil `pod_b` vers le service `hip4-outcome-dry-run`.
- Redéploiement ciblé Pod B sans casser le burn-in Pod A/C:
  `./deploy.sh --start --only-pod-b`. Ce chemin build/recrée uniquement
  `hip4-outcome-dry-run`, sans `stop_unmanaged_services`, sans preflight A/C et
  sans recréer `pod-a-live` ou `pod-c-live`.
- `docker-compose.trident.yml` lance `hip4-outcome-dry-run` avec
  `config/hip4_outcome_mainnet_paper.toml` par defaut; l'ancien
  `hip4-outcome-mainnet-observer` reste defini seulement comme service
  legacy/manual, et l'ancien `pod-b-live` reste sous `legacy_pod_b`.
- HIP-4 ecrit aussi `logs/pod_b_live_status.json`, ce qui rend le reporting/UI Pod B compatible avec le nouveau pod.
- Le pod ne modifie pas le routing Pod A/Pod C.
- Aucun ordre mainnet reel n'est possible dans l'etat courant: le profil actif
  est `paper`, sans mode execution mainnet.

Modes:

| Mode | Effet |
|---|---|
| `observer` | lit les marches, calcule les signaux, loggue, aucun fill |
| `paper` | simule les fills au visible ask, estime le settlement |
| `testnet` | peut envoyer de vrais ordres testnet IOC si credentials et garde-fous sont actifs; le PnL settled vient des fills `Settlement` Hyperliquid |

Defaults repo:

- `mode = "paper"`
- `allow_testnet_orders = false`
- `require_testnet_url = false` dans le profil mainnet paper
- `pod_b_budget_usdc = 500`
- `max_position_usdc = 50`
- `max_total_outcome_exposure_usdc = 500`
- `max_per_underlying_outcome_exposure_usdc = 150`
- `max_outcome_markets_open = 3`
- `enforce_testnet_balance_check = false` dans le profil mainnet paper
- `testnet_balance_coin = "USDH"`
- `testnet_balance_buffer_usdc = 1`
- Les noms historiques `*_usdc` representent le budget notionnel; sur mainnet
  paper ils restent en USDC notionnel simule.

Profil testnet:

- arrete comme source d'analyse, car les prix/settlements etaient trop
  divergents pour conclure sur un edge;
- conserve pour preflight technique et regression d'architecture seulement;
- ne doit pas redevenir un critere de promotion mainnet.

Observation / paper mainnet:

- Config active: `config/hip4_outcome_mainnet_paper.toml`.
- Endpoint: `https://api.hyperliquid.xyz/info`.
- Statut Pod B: `logs/hip4_outcome_status.json` et alias
  `logs/pod_b_live_status.json`.
- Logs: `logs/hip4_outcome_mainnet_paper/`.
- State: `runtime/hip4_outcome_mainnet_paper_state.json`.
- API UI: `/api/hip4-outcome`, et resume integre dans `/dashboard`.
- Profil observer mainnet standalone: `config/hip4_outcome_mainnet_observer.toml`,
  `logs/hip4_outcome_mainnet/`, sans alias Pod B, actif par defaut dans l'app
  separee `TRIDENT-HIP4` en mode paper. Ce n'est pas un executor et aucun ordre
  mainnet reel n'est possible par ce service.
- Verification initiale mainnet `2026-05-03`: `outcomeMeta` expose un BTC
  `priceBinary` daily (`outcome=1`, `#10/#11`) avec books et mids actifs.

Sources de prix / observation:

- Le moteur sait lire Binance, OKX, Bybit, Coinbase, Kraken et Hyperliquid.
- La config active interroge les venues externes et Hyperliquid mainnet, puis
  ancre la reference a Hyperliquid mainnet pour eviter de prendre une venue
  externe isolee comme verite unique.
- Les sources externes sont conservees comme observation et garde-fou: si elles
  divergent trop de la reference Hyperliquid mainnet, le signal est rejete ou
  degrade au lieu d'etre pris comme edge exploitable.
- `max_source_deviation_bps = 50`, `min_reference_sources = 1`, `anchor_reference_to_hyperliquid = true`.
- `include_underlyings = []` signifie: accepter tous les `priceBinary` renvoyes par `outcomeMeta`.
- Le profil testnet conserve le meme mecanisme uniquement pour preflight
  technique. Ses divergences avec le marche mainnet ne sont plus utilisees pour
  definir des blocages mainnet.
- `market_observations.jsonl` loggue aussi les classes non tradees (`namedOutcome`, fallback, `priceBucket` incomplet, etc.) avec `sideSpecs`, coins, thresholds et un resume book YES/NO quand disponible.
- `priceBucket` est parse en mode paper/observer quand deux thresholds, ou plus de deux thresholds avec `index`, definissent une bande adjacente claire. Le detecteur `PRICE_BUCKET_MODEL` estime `P(lower <= price <= upper)` via le modele lognormal range, mais reste paper/observer seulement.
- `Named Outcome` reste strictement watch-only: pas de modele, pas d'execution, pas d'inference de verite tant que la source de resolution/vote n'est pas replayable.

Capital guard:

- Avant tout fill paper, `OutcomeCapitalGuard` plafonne la taille par budget
  Pod B simule et exposition ouverte.
- En profil testnet preflight seulement, il peut aussi verifier le solde spot
  quote outcome via `spotClearinghouseState` avant un ordre technique.
- Le statut expose `capital` dans `logs/hip4_outcome_status.json` et dans l'alias `logs/pod_b_live_status.json`.
- Le minimum ordre HL est traite comme `10 USDH` de valeur economique effective, avec `min(limit_price, 1 - limit_price)` pour les outcomes. Les rejets explicites sont `below_exchange_min_order_value_yes/no`.

Frais / PnL:

- Ouverture outcome: `outcome_open_fee_rate = 0.0`.
- Settlement/close outcome: `outcome_settlement_fee_rate = 0.002`.
- En `mainnet_paper`, le bot estime le settlement depuis la reference
  Hyperliquid mainnet et applique les frais configures.
- En profil testnet historique/preflight, le bot ne settle plus localement
  depuis la reference: il attend les fills `Settlement` Hyperliquid, lit
  `closedPnl`/`fee`, puis corrige l'etat, les CSV et l'alias Pod B avec cette
  source exchange.
- Les anciens settlements testnet estimes localement doivent etre consideres
  invalides si Hyperliquid renvoie un fill `Settlement` contradictoire.
- Le statut global et la page HIP-4 doivent lire la meme source d'agregation par coin, pour eviter un PnL Pod B visible sur `/dashboard` mais absent de `/hip4-outcome`.

Isolation Pod A / Pod B:

- Pod B HIP-4 mainnet paper utilise un budget simule dedie et ne reserve pas de
  marge directionnelle.
- Pod A et Pod B ne partagent donc plus ni capital live, ni marge, ni budget de
  risque.
- Un perp Pod A BTC/HYPE/etc. ne bloque plus un outcome HIP-4 sur le meme underlying.
- Les locks atomiques `runtime/hip4_overlap_locks/` ne sont plus utilises par Pod A ou Pod B.
- Les modules d'overlap/lock HIP-4 ont ete supprimes; il ne doit plus rester de cle `directional_overlap`, `hip4_overlap` ou `block_directional_overlap` dans les statuts/UI.
- Les garde-fous conserves sont internes au Pod B: budget, exposition max, `market_already_open`, minimum d'ordre HL, reconciliation/fills/settlement.
- L'UI HIP-4 affiche le budget, les positions paper et les executions simulees,
  sans carte d'overlap Pod A. Le solde testnet n'est pertinent que pour un
  preflight technique.

Edge types implementes:

- `MODEL`: proba lognormal static-vol vs prix YES/NO.
- `LATE_EXPIRY`: sous-jacent deja clairement au-dessus/sous le strike proche expiry.
- `PARITY`: achat YES+NO si le cout combine est sous 1.
- `SHORT_EXPIRY`: chemin OpenClaw-like pour marches tres courts.
- `PRICE_BUCKET_MODEL`: paper/observer seulement, proba d'une bande de prix type corridor binary; jamais execute en reel.

Review mainnet paper / calibration:

- La review automatique est en place via
  `app/backtest/hip4_outcome_run_review.py` et
  `app/trident/hip4_outcome/analysis.py`.
- Elle calcule PnL, profit factor, Brier score, log-loss, buckets de
  calibration, loss review et simulations de guardrails.
- Dernier rapport `2026-05-16`:
  `server-data/replay_reports/hip4_outcome_run_review_latest.md`.
- Statut `mainnet_paper`: `collect_more_data`, `27,650` opportunities,
  `4` trades approuves, `3` settlements, PnL `+96.0778`, PF `2.9315`,
  Brier `0.2695`.
- Blockers restants: settlements `3/20`, expiries/marches `3/5`, samples de
  calibration `3/20`, Brier cible non atteint (`0.2695` vs `<= 0.23`).
- Aucun blocage HYPE HIP-4 n'est actif en mainnet paper. Les anciens blocages
  HYPE testnet ont ete retires parce que le testnet n'est plus une source de
  performance representative.
- Decision dry-run `2026-05-23` apres backtest PnL levers:
  `server-data/replay_reports/hip4_outcome_pnl_lever_backtests_20260523T164104Z.md`.
  Pas de blocage statique par coin/cote (`blocked_opportunity_slices = []`).
  Le `shock_guard` reste global sur tous les `priceBinary`, mais il est moins
  agressif: il faut maintenant `2` fenetres adverses avant rejet
  (`shock_guard_min_adverse_windows = 2`). Les seuils restent `15m`, `1h`,
  `4h`, `1d`, `3d`, `7d` a `80`, `150`, `250`, `300`, `300`, `400` bps.
  Si l'historique shock est absent, il est seed depuis `opportunities.csv`.
- Sorties anticipees `2026-05-23`: `bid_over_conservative_hold_ev` reste actif
  et GO dry-run. Le `probability_stop` revient en dry-run paper avec seuil
  PnL-first de compromis: `early_exit_stop_probability = 0.35`,
  `early_exit_stop_max_loss_roi = 0.20`. Le `0.32/0.15` etait trop timide
  sur les candidats observes; le `0.35/0.20` garde le declenchement defensif
  tout en refusant les sorties deja trop abimees.
- Ajustement `2026-05-25` apres review mainnet paper: les sorties
  `bid_over_conservative_hold_ev` actives ne doivent plus fermer 100% par
  defaut. Elles sortent maintenant `50%` et gardent un runner, afin de ne pas
  liberer le meme marche pour des re-entries qui transforment des petits gains
  en churn asymetrique. Les exits full restants (`full_take_profit`,
  `probability_stop`, fenetre short-expiry libre) verrouillent le meme
  market/expiry jusqu'au settlement.
- GO observe ajoutes/maintenus:
  - `shadow_policy_ev_plus_2pct_full` dans `shadow_exit_policies.csv`;
  - `shadow_policy_ev_plus_2pct_partial_runner` dans
    `shadow_exit_policies.csv`;
  - `shadow_sizing_half_kelly` dans `shadow_sizing.csv`;
  - `shock_guard_two_window_confirmation` expose dans le status via
    `summary.pnl_levers`.
- `SHORT_EXPIRY` reste observation-only pendant la fenetre 48h
  (`short_expiry_observe_only = true`): les features/watchlist continuent
  d'etre logguees, mais aucune opportunite `SHORT_EXPIRY` ne doit etre ouverte.
- NOGO a ne pas promouvoir sur cette fenetre: variantes `SHORT_EXPIRY` teste
  hold-to-settlement proxy, durcissement BUY_YES downtrend par edge/rebound,
  `shock_guard` one-hit courant, `shock_guard_scale_2x`, maker quotes sans
  modele de fills.
- Revue prevue apres `48h` de dry-run avec ces reglages: comparer PnL realise,
  PnL si hold-to-settlement par raison de sortie, nombre de stops proba,
  `market_already_open`, et PnL par side/underlying.

Mode `SHORT_EXPIRY`:

- Statut courant `2026-05-23`: observation-only pendant 48h. Les features,
  watchlist et raisons de readiness sont logguees, mais l'edge type
  `SHORT_EXPIRY` ne genere pas d'entree paper.
- Priorise les marches dans `short_expiry_window_minutes`.
- Maintient un historique prix settlement-aligne dans le `state_path` configure.
- Calcule momentum 30s/60s/180s, distance au strike, vitesse, vol realisee courte.
- Combine:
  - distance au strike
  - momentum court terme
  - probabilite implicite du book YES/NO
  - imbalance du book
  - modele statique
- Loggue tous les snapshots, y compris warming/rejected, dans `short_expiry_features.csv`.

Sorties principales:

Mainnet paper Pod B actif:

- `logs/hip4_outcome_mainnet_paper/opportunities.csv`
- `logs/hip4_outcome_mainnet_paper/decisions.jsonl`
- `logs/hip4_outcome_mainnet_paper/trades.csv`
- `logs/hip4_outcome_mainnet_paper/settlements.csv`
- `logs/hip4_outcome_mainnet_paper/latency_stats.csv`
- `logs/hip4_outcome_mainnet_paper/edge_decay.csv`
- `logs/hip4_outcome_mainnet_paper/short_expiry_features.csv`
- `logs/hip4_outcome_mainnet_paper/market_observations.jsonl`
- `logs/hip4_outcome_mainnet_paper/daily_summary.csv`
- `logs/hip4_outcome_status.json`
- `logs/pod_b_live_status.json` (alias runtime Pod B pour l'UI/reporting)
- `runtime/hip4_outcome_mainnet_paper_state.json`

Testnet Pod B historique/preflight:

- `logs/hip4_outcome_testnet/opportunities.csv`
- `logs/hip4_outcome_testnet/decisions.jsonl`
- `logs/hip4_outcome_testnet/trades.csv`
- `logs/hip4_outcome_testnet/settlements.csv`
- `logs/hip4_outcome_testnet/latency_stats.csv`
- `logs/hip4_outcome_testnet/edge_decay.csv`
- `logs/hip4_outcome_testnet/short_expiry_features.csv`
- `logs/hip4_outcome_testnet/market_observations.jsonl`
- `logs/hip4_outcome_testnet/daily_summary.csv`
- `logs/hip4_outcome_status.json`
- `logs/pod_b_live_status.json` (alias runtime Pod B pour l'UI/reporting)
- `runtime/hip4_outcome_testnet_state.json`

Mainnet observer historique:

- `logs/hip4_outcome_mainnet/opportunities.csv`
- `logs/hip4_outcome_mainnet/decisions.jsonl`
- `logs/hip4_outcome_mainnet/latency_stats.csv`
- `logs/hip4_outcome_mainnet/edge_decay.csv`
- `logs/hip4_outcome_mainnet/short_expiry_features.csv`
- `logs/hip4_outcome_mainnet/market_observations.jsonl`
- `logs/hip4_outcome_mainnet/daily_summary.csv`
- `logs/hip4_outcome_mainnet_status.json`
- `runtime/hip4_outcome_mainnet_state.json`

Note: les anciens chemins `logs/hip4_outcome_paper/` et
`runtime/hip4_outcome_paper_state.json` peuvent exister dans les archives
locales; l'exploitation courante doit privilegier le `logs_dir` et
`state_path` de `config/hip4_outcome_mainnet_paper.toml`.

Etat d'observation et execution:

- Le mainnet paper a pris le relais du testnet comme source d'observation et
  de dry-run exploitable.
- La derniere review mainnet paper recalculee le `2026-05-23` reste
  `collect_more_data`: `53` trades, `52` settlements, win rate `24/52`,
  PnL `-34.1787`, PF `0.9106`, Brier `0.2405`. Le volume est maintenant
  suffisant pour tester des leviers paper, mais pas pour promotion mainnet.
- Le testnet a valide les briques techniques: signatures, ordres IOC,
  reconciliation exchange, parsing de `Settlement.closedPnl`/`fee`, alias Pod B
  et UI.
- Les premiers gros edges HYPE vus avec reference externe ne doivent pas etre
  consideres comme edge mainnet fiable: ils venaient d'une divergence testnet /
  venues externes. Les blocages HYPE qui en decoulaient ne pilotent plus le
  mainnet paper.

Commandes utiles:

Deploiement live hybride A/C live + B mainnet paper:

```bash
./deploy.sh --start --mode live --config config/trident.toml --without-funding
```

Deploiement dry-run complet avec le nouveau Pod B:

```bash
./deploy.sh --start --mode dry-run --config config/trident.toml --fresh-start
```

Couper le nouveau Pod B HIP-4:

```bash
./deploy.sh --start --mode dry-run --config config/trident.toml --without-pod-b
```

```bash
uv run python -m app.live.hip4_outcome_runner \
  --config config/hip4_outcome_mainnet_paper.toml \
  --mode paper
```

```bash
uv run python -m app.live.hip4_outcome_runner \
  --config config/hip4_outcome_mainnet_paper.toml \
  --mode paper \
  --once
```

```bash
uv run python -m app.backtest.hip4_outcome_replay \
  --profile mainnet_paper \
  --output logs/hip4_outcome_mainnet_paper/replay_latest.json
```

Replay observer mainnet:

```bash
uv run python -m app.backtest.hip4_outcome_replay \
  --profile mainnet \
  --output logs/hip4_outcome_mainnet/replay_latest.json
```

Review post-fetch mainnet paper / mainnet observer / archives:

```bash
uv run python -m app.backtest.hip4_outcome_run_review \
  --output-json server-data/replay_reports/hip4_outcome_run_review_latest.json \
  --output-md server-data/replay_reports/hip4_outcome_run_review_latest.md
```

Note: `scripts/fetch_trident_data.sh` lance maintenant cette review automatiquement via
`scripts/trident_dry_run_review.sh` quand les logs HIP-4 ont ete rapatries.
Le fetch rapatrie les dossiers HIP-4 entiers (`mainnet_paper`, `mainnet`,
`testnet`, `paper`), et la review inclut `market_observations.jsonl` dans
`hip4_outcome_run_review_latest.{json,md}`:
comptes par classe HIP-4, support status, raisons, underlyings, books observes,
`priceBucket` et `namedOutcome`.
Le rapport inclut une simulation de candidats guardrails: impact PnL/PF/Brier apres
exclusion, verdict `keep/watch/park/kill`, et separation entre slices entry-time
actionnables et categories de pertes post-trade.
Les candidats guardrail testnet HYPE sont archives; en mainnet paper, aucune
slice HYPE n'est bloquee tant qu'une review mainnet-paper ne prouve pas un
predicat entry-time actionnable.

Verification serveur apres deploiement:

```bash
ssh trident-hetzner <<'SH'
cd /opt/trident
set -a
[ -f .env.trident ] && . ./.env.trident
set +a
auth_args=()
if [ -n "${TRIDENT_UI_AUTH_USERNAME:-}" ] && [ -n "${TRIDENT_UI_AUTH_PASSWORD:-}" ]; then
  auth_args=(-u "${TRIDENT_UI_AUTH_USERNAME}:${TRIDENT_UI_AUTH_PASSWORD}")
fi
curl -fsS "${auth_args[@]}" http://127.0.0.1:3000/api/hip4-outcome \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("mode"), d.get("blocked_opportunity_slices"))'
SH
```

Attendu:

```text
paper []
```

Testnet technique:

- Ne plus l'utiliser comme preuve de performance.
- Le conserver seulement pour regression d'architecture ou preflight ponctuel.
- Les variables `HIP4_OUTCOME_MODE=testnet` et
  `HIP4_OUTCOME_ALLOW_TESTNET_ORDERS=true` ne doivent pas etre actives dans le
  dry-run mainnet paper.

Preparation execution mainnet future:

- Accumuler d'abord les logs mainnet paper via le deploiement standard.
- Rejouer `logs/hip4_outcome_mainnet_paper/opportunities.csv` avec
  `--profile mainnet_paper`.
- Comparer mainnet paper et mainnet observer historique: edge decay, spreads,
  profondeur, fills theoriques, reference prices, horaires d'expiry.
- Aucune execution mainnet ne doit etre ajoutee sans nouveau mode explicite
  `mainnet`, credentials mainnet dedies, preflight separe, caps tiny-size et
  confirmation manuelle.

Ce qui manque encore pour se rapprocher d'un bot type OpenClaw performant:

- WebSocket ou streaming plus bas-latence pour mids et order books.
- Statistiques par expiry apres settlement reel/estime.
- Mesure slippage/fill theorique mainnet paper, puis eventuel preflight tiny
  size separe si une execution mainnet est envisagee.
- Plus de donnees sur marches 5m/15m avant calibration.
- Une politique de sizing dynamique uniquement apres preuve mainnet paper.
- Une calibration probabiliste serieuse avant tout Kelly ou ML.
- Une base de snapshots complete pour rejouer decisions, books, references, fills, settlements et edge decay.
- Un mode mainnet execution explicite et separe de `paper`, seulement apres
  validation mainnet paper.
- Gestion de sortie anticipee HIP-4:
  - les outcomes sont des assets spot-like; sortir avant expiry revient a
    vendre au bid le token YES/NO detenu;
  - implementation cible d'abord `paper` mainnet: mark-to-book au bid,
    comparaison contre une EV hold-to-settlement conservative, exits partiels
    sur profit, exits totaux si le bid surpaie la fair value conservative ou
    si la probabilite se degrade;
  - aucune vente reelle testnet/mainnet sans executor sell dedie,
    reconciliation spot, logs de fills et confirmation operateur.
- Reservation `SHORT_EXPIRY`:
  - le hold-to-settlement produit environ un trade daily par jour et bloque
    souvent le marche via `market_already_open`;
  - les exits anticipes doivent permettre de liberer l'inventaire avant la
    derniere fenetre et de reserver une tranche de budget au moteur
    short-expiry sans augmenter le notional par trade.
- Passive maker / liquidity capture:
  - etudier un mode ALO/GTC autour de la fair probability au lieu de seulement
    traverser le spread;
  - rester en shadow tant que l'adverse selection, les partial fills et le
    risque d'inventaire ne sont pas mesures.
- Extension `priceBucket` / `namedOutcome`:
  - rester observation/paper tant que la resolution n'est pas replayable;
  - prioriser un dataset complet books/references/settlements avant toute
    execution reelle sur ces classes.

### Backlog Data Quality HIP-4 / Dirty Realtime Data

Verdict post "dirty websockets":

- Le diagnostic general est utile: un edge outcome peut etre detruit ou simule
  par des books stale, des references decalees, des snapshots caches ou une
  boucle trop lente.
- La recette Polymarket n'est pas transposable telle quelle a Hyperliquid:
  ne pas lancer `100-300` websockets par feed, ne pas utiliser de seuils fixes
  en cents, et ne pas considerer la latence comme preuve d'edge suffisante.
- Pour TRIDENT, le sujet concerne surtout `Pod B HIP-4 Outcome`. Pod A/Pod C
  utilisent deja un collecteur Hyperliquid shardé et rate-limit; il ne faut pas
  augmenter agressivement les connexions A/C sans preuve et sans respect des
  limites exchange.

Objectif avant toute promotion HIP-4 mainnet:

- Ajouter une couche `data_quality` appelee avant signal/risk/execution.
- Produire un verdict explicite par marche/fenetre:
  `tradable_window=true/false`, `quality_score`, `quality_reasons`.
- D'abord en observation/mainnet paper seulement; aucune execution mainnet
  reelle ne depend de cette couche tant qu'elle n'a pas ete replayee.
- Logger ces champs dans les artifacts HIP-4 et les exposer dans le status/UI.

Metriques minimales a logger:

- `book_age_ms` YES/NO et `max_book_age_ms`.
- `book_pair_skew_ms` entre les books YES et NO d'un meme marche.
- `reference_age_ms` par source quand disponible, et age de la reference
  agregee.
- `loop_total_ms`, `books_ms`, `reference_prices_ms`, `market_observation_ms`.
- `book_update_count_5s/15s` par coin outcome des que le streaming existe.
- `unique_book_count_5s/15s` apres deduplication.
- `price_jump_bps` sur l'underlying et variation absolue de probabilite book
  outcome (`book_probability_delta_abs`).
- `reference_divergence_bps`, sources rejetees, source count, anchor
  Hyperliquid.
- Spread, depth, empty/crossed book, missing bid/ask, et raison de rejet
  `data_quality_*`.

Regles candidates a tester en shadow puis replay:

- Warmup `SHORT_EXPIRY`: commencer la surveillance au moins `15s` avant la
  fenetre tradable, mesurer les `5s` finales, et skip la fenetre si les deux
  legs outcome n'ont pas assez d'updates propres.
- Rejeter une fenetre si un seul jump book/proba/reference depasse un seuil
  calibre en bps ou en probabilite outcome, jamais en cents hard-codes.
- En mode HTTP courant, utiliser l'age `time_ms` des `l2Book` YES/NO, le skew
  YES/NO et la latence de boucle comme proxy de fraicheur.
- En futur mode WebSocket, ignorer le premier tick/snapshot de chaque nouvelle
  connexion tant que la source n'a pas prouve sa fraicheur.
- Marquer une connexion comme stable seulement apres une periode de stabilisation
  d'environ `8s`.
- Stagger les subscriptions/reconnects sur environ `1s` pour eviter de voir le
  meme snapshot cache partout au meme instant.

Redondance prudente:

- Cible initiale: `1` stream primaire + `1` shadow stream pour les outcomes
  critiques ou les fenetres `SHORT_EXPIRY`; monter a `2-3` streams seulement si
  les logs prouvent un gain de fraicheur net.
- Deduplication par `coin`, `time_ms`, best bid/ask et top levels.
- Score `jitter_ema` par connexion: delai inter-message, variance, age max,
  erreurs, reconnects, snapshots identiques repetes.
- Cull les connexions les plus erratiques seulement apres stabilisation, avec
  caps explicites de respawn par minute et par cycle; respecter les limites
  `ws_connects_per_minute`/rate limiter.

Review et criteres de decision:

- Ajouter des buckets de review: PnL, PF, Brier, log-loss, edge decay et fill
  theorique par `quality_score`, `book_age_ms`, `book_pair_skew_ms`,
  `loop_total_ms` et `reference_divergence_bps`.
- Comparer opportunites acceptees vs opportunites qui auraient ete rejetees
  par `data_quality` sur le meme dataset mainnet paper.
- Verifier que la couche ne retire pas seulement quelques winners par hasard:
  exiger un effet stable par jour/expiry/underlying/side avant promotion.
- Mettre a jour `app/backtest/hip4_outcome_run_review.py`,
  `app/trident/hip4_outcome/analysis.py`, `scripts/trident_dry_run_review.sh`
  et `scripts/fetch_trident_data.sh` si de nouveaux fichiers de logs
  `data_quality` sont ajoutes.
- Nouveau pre-requis de promotion mainnet HIP-4: dataset mainnet paper avec
  `data_quality` complet, distributions de latence/fraicheur connues, impact
  replay positif ou neutre sur PF/Brier, et taux de fenetres skip acceptable.

### Backlog Nautilus Trader / Shadow Adapter HIP-4

Verdict post clipping `docs/new_idea.md`:

- Nautilus Trader est une piste d'infrastructure, pas une source d'edge.
- Le clipping est promotionnel; ne pas le traiter comme preuve de performance,
  de pricing ou de strategie exploitable.
- L'interet pour TRIDENT vient surtout de l'adapter Hyperliquid Nautilus:
  instruments normalises, WebSocket, order books, `allMids`, execution,
  reconciliation, HIP-3 builder perps et HIP-4 outcomes.
- L'usage cible est `TRIDENT-HIP4` seulement, en shadow/read-only d'abord.
- `Pod A` et `Pod C` ne doivent pas etre migres vers Nautilus tant que le burn-in
  A/C live/testnet et la preparation mainnet tiny-size ne sont pas termines.

Sources techniques a verifier avant implementation:

- Docs Nautilus overview:
  `https://nautilustrader.io/docs/latest/concepts/overview/`
- Getting started / contraintes Python:
  `https://nautilustrader.io/docs/latest/getting_started/`
- Integration Hyperliquid:
  `https://nautilustrader.io/docs/latest/integrations/hyperliquid/`
- Repo officiel:
  `https://github.com/nautechsystems/nautilus_trader`

Positionnement:

- Nautilus ne remplace pas le moteur TRIDENT au depart.
- Nautilus ne decide aucune entree/sortie et ne modifie aucun cap.
- Nautilus n'ecrit pas dans le state actif HIP-4.
- Nautilus ne doit pas envoyer d'ordre en `paper`, `testnet` ou `mainnet` pendant
  les phases shadow.
- Nautilus sert a comparer:
  - fraicheur des books YES/NO;
  - skew temporel entre legs d'un meme outcome;
  - parite symboles/instruments Hyperliquid;
  - profondeur/spread visibles;
  - `allMids` / references Hyperliquid;
  - fills et settlements observes, quand un replay ou un flux user read-only est
    disponible sans execution.

Contraintes d'environnement:

- Nautilus cible Python `3.12-3.14` dans sa doc courante; TRIDENT declare encore
  `requires-python = ">=3.11"` mais le Docker prod est `python:3.12-slim`.
- Ne pas ajouter Nautilus comme dependance prod globale dans une premiere passe.
- Preference initiale:
  - soit un extra optionnel `research` / `hip4-nautilus`;
  - soit un script isole lance uniquement dans l'app `TRIDENT-HIP4`;
  - soit un conteneur sidecar experimental desactive par defaut.
- Toute dependance native/Rust/PyO3 doit etre testee dans le Docker cible avant
  de modifier les scripts de deploiement.
- Si l'installation impose Python 3.12, les chemins fallback `python3.11` des
  scripts de review ne doivent pas devenir dependants de Nautilus.

Non-objectifs explicites:

- Pas de migration du `FullBotBacktestRunner`.
- Pas de remplacement de `LiveExecutionVenue` Pod A/C.
- Pas d'execution HIP-4 mainnet.
- Pas de mode testnet outcome automatique.
- Pas de Kelly, ML, maker live ou adaptive sizing via Nautilus avant calibration
  mainnet paper.
- Pas de changement de `config/trident.toml` pour activer Nautilus dans TRIDENT
  A/C.

Architecture cible phase 0:

- Nouveau module research/shadow, nom propose:
  `app/trident/hip4_outcome/nautilus_shadow.py`.
- Nouveau runner CLI, nom propose:
  `app/live/hip4_nautilus_shadow_runner.py`.
- Nouvelle config optionnelle, nom propose:
  `config/hip4_nautilus_shadow.toml`.
- Nouveaux tests unitaires, nom propose:
  `tests/test_hip4_nautilus_shadow.py`.
- Nouveau dossier de logs:
  `logs/hip4_nautilus_shadow/`.
- Nouveau state read-only/cache:
  `runtime/hip4_nautilus_shadow_state.json`.
- Le runner doit pouvoir tourner en `--once` et en boucle, comme
  `hip4_outcome_runner`.
- Tous les imports Nautilus doivent etre paresseux et produire un message clair
  si la dependance optionnelle n'est pas installee.

Config initiale proposee:

```toml
[hip4_nautilus_shadow]
enabled = false
mode = "shadow"
environment = "mainnet"
logs_dir = "./logs/hip4_nautilus_shadow"
state_path = "./runtime/hip4_nautilus_shadow_state.json"
loop_interval_seconds = 1
max_markets = 4
include_underlyings = ["BTC", "ETH", "SOL", "HYPE"]
include_outcome_products = true
include_hip3_products = false
subscribe_all_mids = true
subscribe_order_books = true
book_depth_levels = 10
warmup_seconds = 8
stagger_subscriptions_ms = 1000
max_ws_connects_per_minute = 6
write_shadow_books = true
write_shadow_quality = true
write_shadow_instruments = true
allow_orders = false
allow_private_user_stream = false
```

Artefacts a produire:

- `logs/hip4_nautilus_shadow/instruments.jsonl`
  - `ts`
  - `instrument_id`
  - `raw_symbol`
  - `product_type`
  - `underlying`
  - `expiry`
  - `quote_currency`
  - `tick_size`
  - `lot_size`
  - `source`
- `logs/hip4_nautilus_shadow/book_snapshots.jsonl`
  - `ts_event`
  - `ts_init`
  - `coin`
  - `instrument_id`
  - `market_id`
  - `side_name`
  - `best_bid`
  - `best_ask`
  - `bid_size`
  - `ask_size`
  - `bid_depth_10`
  - `ask_depth_10`
  - `spread`
  - `source_latency_ms`
- `logs/hip4_nautilus_shadow/data_quality.csv`
  - `ts`
  - `market_id`
  - `underlying`
  - `yes_coin`
  - `no_coin`
  - `yes_book_age_ms`
  - `no_book_age_ms`
  - `max_book_age_ms`
  - `book_pair_skew_ms`
  - `book_update_count_5s`
  - `book_update_count_15s`
  - `unique_book_count_5s`
  - `unique_book_count_15s`
  - `reference_age_ms`
  - `reference_divergence_bps`
  - `empty_book`
  - `crossed_book`
  - `quality_score`
  - `tradable_window`
  - `quality_reasons`
- `logs/hip4_nautilus_shadow/parity_compare.csv`
  - `ts`
  - `market_id`
  - `coin`
  - `trident_bid`
  - `trident_ask`
  - `nautilus_bid`
  - `nautilus_ask`
  - `bid_diff`
  - `ask_diff`
  - `trident_age_ms`
  - `nautilus_age_ms`
  - `verdict`
- `logs/hip4_nautilus_shadow/status.json`
  - resume operateur read-only;
  - compteur instruments;
  - compteur books;
  - dernier update par coin;
  - erreurs/reconnects;
  - decision `shadow_ready=true/false`.

Phase 1 - Shadow data local:

Implementation locale `2026-05-27`:

- Module/runner/config/tests ajoutes:
  `app/trident/hip4_outcome/nautilus_shadow.py`,
  `app/live/hip4_nautilus_shadow_runner.py`,
  `config/hip4_nautilus_shadow.toml`,
  `tests/test_hip4_nautilus_shadow.py`.
- Review HIP-4 branchee sur `logs/hip4_nautilus_shadow/data_quality.csv`
  avec statut non bloquant `nautilus_shadow_missing` si absent.
- Sidecar Docker/deploy/fetch ajoute en opt-in avec
  `--with-nautilus-shadow`; il reste desactive par defaut et read-only.
- Run local initial: `uv pip install nautilus_trader` a echoue car `clang`
  est absent dans l'environnement local; le runner expose donc
  `shadow_ready=false` et aucun artefact `data_quality.csv` exploitable.
- Suite `2026-05-27`: une image dediee `Dockerfile.hip4-nautilus` ajoute
  `clang`, `lld`, `libssl-dev`, `build-essential`, `pkg-config` et Rust
  uniquement pour le sidecar `hip4-nautilus-shadow`. L'image TRIDENT A/C et
  l'image HIP-4 principale restent sans toolchain Nautilus.
- Test local wheel binaire: `nautilus_trader==1.208.0` s'installe depuis
  l'index Nautilus mais l'import Hyperliquid echoue sur `libssl.so.1.1`; le
  sidecar Docker force donc une version recente `1.227.0` construite/installee
  dans son image dediee.
- Le build Docker n'a pas pu etre valide localement dans ce workspace car la
  commande `docker` est absente.
- Validation serveur `2026-05-27`: `./trident-hip4/deploy.sh --start
  --with-nautilus-shadow` a builde l'image dediee et demarre le sidecar.
  L'API `server-data/hip4/api/hip4-nautilus-shadow-2026-05-27_075744.json`
  expose `shadow_ready=true`, `reason=ok`, Nautilus `1.227.0` et
  `HyperliquidProductType.OUTCOME`.
- `server-data/hip4/logs/hip4_nautilus_shadow/data_quality.csv` existe apres
  fetch; snapshot final `2026-05-27T08:44Z`: `130` lignes qualite, `260`
  lignes parity, `260` snapshots books, `1` marche observe.
- La review latest voit Nautilus en `partial`: `row_count=130`,
  `market_count=1`, `matched_settlement_count=0`. Cela prouve la plomberie
  shadow et le CSV, pas encore un apport trading mesurable.
- `trident-hip4/fetch_data.sh` reutilise une connexion SSH multiplexee pour
  eviter de rater les artefacts Nautilus actifs lors d'un fetch complet.
- Etape 2 `2026-05-27`: le runner instancie maintenant
  `HyperliquidInstrumentProvider` + `HyperliquidWebSocketClient`, charge les
  instruments `OUTCOME`, souscrit les books des coins HIP-4 selectionnes, ecrit
  `book_snapshots.jsonl` depuis Nautilus et remplit `parity_compare.csv` avec
  `trident_bid/ask` vs `nautilus_bid/ask`.
- Validation serveur propre apres archive des lignes du bug de parse et
  redeploiement final: `shadow_ready=true`, `source=nautilus_hyperliquid_ws`,
  `snapshot_count=2`, `errors=[]`.
- La review latest expose maintenant aussi des buckets d'observation
  `quality_row_buckets` sur toutes les lignes shadow. Mainnet paper:
  `row_count=130`, `market_count=1`, `matched_settlement_count=0`,
  `avg_quality_score=0.8940`, `avg_max_book_age_ms=1262.4769`,
  `avg_book_pair_skew_ms=0.0`.
- Etape decision-time `2026-05-27`: la review joint chaque decision HIP-4 a la
  derniere ligne Nautilus du meme `market_id` anterieure a la decision et agee
  de moins de `300s`. Snapshot mainnet paper `08:44Z`: `68` decisions
  matchees, `4488` non matchees historiques, age moyen `7.41s`,
  `would_block_count=9` pour `reference_divergence_gt_50bps`. Aucune decision
  approuvee couverte pour l'instant, donc pas encore de conclusion PnL.
- Replay/review local date:
  `server-data/hip4/reviews/20260527T084458Z/hip4_outcome_run_review.md`.
  Verdict initial: pas d'apport mesurable Nautilus avant donnees shadow
  exploitables; a rejouer apres plusieurs settlements avec le CSV serveur.

- `hip4_nautilus_shadow_runner --once` existe; l'import Nautilus et la source
  order book Nautilus directe sont valides dans le sidecar serveur.
- Lire les marches courants depuis `outcomeMeta` via TRIDENT existant: fait
  dans le runner shadow.
- Souscrire seulement `max_markets` outcomes proches expiry: fait via les
  coins YES/NO des marches selectionnes.
- Ecrire les fichiers `instruments.jsonl`, `book_snapshots.jsonl` et
  `data_quality.csv`: fait cote sidecar serveur et rapatrie dans
  `server-data/hip4/logs/hip4_nautilus_shadow/`.
- Ne pas brancher le shadow runner au detector d'edge.
- Comparer les books Nautilus aux books TRIDENT HTTP sur le meme loop:
  - best bid/ask;
  - depth;
  - age;
  - skew YES/NO;
  - frequence d'updates.
- Sortie attendue:
  `server-data/replay_reports/hip4_nautilus_shadow_probe_<date>.md` ou `tmp/`
  si le run est purement local.

Phase 2 - Integration review HIP-4:

- Ajouter la lecture optionnelle de `logs/hip4_nautilus_shadow/data_quality.csv`
  dans `app/trident/hip4_outcome/analysis.py`.
- Ajouter des buckets au rapport:
  - PnL/PF/Brier par `quality_score`;
  - PnL/PF/Brier par `max_book_age_ms`;
  - PnL/PF/Brier par `book_pair_skew_ms`;
  - opportunites acceptees qui auraient ete rejetees par data quality;
  - opportunites rejetees par data quality qui auraient gagne/perdu au
    settlement.
- Ajouter une section markdown:
  `### Nautilus Shadow Data Quality`.
- Si le fichier est absent, la review doit rester verte avec statut
  `nautilus_shadow_missing`.
- Ne pas changer les verdicts `go/watch/park/kill` automatiquement tant que le
  shadow n'a pas plusieurs jours de donnees.

Phase 3 - Sidecar TRIDENT-HIP4 optionnel:

- Ajouter un service Docker desactive par defaut dans `docker-compose.hip4.yml`,
  nom propose: `hip4-nautilus-shadow`.
- Ajouter un flag deploy:
  `./trident-hip4/deploy.sh --with-nautilus-shadow`.
- Par defaut, `deploy.sh` ne doit pas lancer ce service.
- Le service doit monter les memes volumes `logs/` et `runtime/` que HIP-4.
- Le service doit utiliser `config/hip4_nautilus_shadow.toml`.
- Ajouter une route/status UI seulement si le status file existe:
  `/api/hip4-nautilus-shadow`.
- L'UI doit afficher clairement `shadow/read-only`, pas un executor.

Phase 4 - Decision d'adoption:

- Garder Nautilus si, sur au moins plusieurs jours mainnet paper:
  - il donne des books plus frais que le polling TRIDENT;
  - il reduit `book_pair_skew_ms`;
  - il explique des pertes ou faux signaux par data quality;
  - il ne cree pas d'instabilite websocket/rate-limit;
  - les artefacts sont replayables et utiles dans la review.
- Park Nautilus si:
  - installation fragile dans Docker;
  - pas de gain de fraicheur visible;
  - symbologie HIP-4 ou settlements incomplets dans la version disponible;
  - complexite superieure au benefice.
- Ne considerer une migration partielle que si le shadow prouve un gain net.

Chemin de migration partielle possible, non autorise au depart:

- Data path seulement:
  - remplacer certains fetches `l2Book`/`allMids` par un cache alimente par
    Nautilus;
  - garder `OutcomeEdgeDetector`, `OutcomeRiskManager`, capital guard, state et
    logs TRIDENT.
- Execution testnet/outcome seulement apres nouvelle validation:
  - ajouter un executor dedie `NautilusOutcomeExecutor`;
  - mode `testnet` uniquement;
  - caps minuscules;
  - reconciliation spot USDH et side tokens;
  - aucun mainnet sans mode `mainnet` explicite, preflight separe et
    confirmation operateur.
- Jamais de remplacement global Pod A/C sans nouveau plan.

Tests requis avant merge du shadow:

```bash
uv run python -m py_compile \
  app/trident/hip4_outcome/nautilus_shadow.py \
  app/live/hip4_nautilus_shadow_runner.py
```

```bash
uv run python -m unittest tests.test_hip4_nautilus_shadow
```

```bash
bash -n trident-hip4/deploy.sh trident-hip4/fetch_data.sh \
  scripts/trident_dry_run_review.sh scripts/fetch_trident_data.sh
```

```bash
uv run python -m app.live.hip4_nautilus_shadow_runner \
  --config config/hip4_nautilus_shadow.toml \
  --once
```

Impacts deploy/fetch/review traites ou a maintenir:

- `trident-hip4/deploy.sh`:
  - ajouter le flag opt-in `--with-nautilus-shadow`;
  - ne jamais l'activer par defaut;
  - afficher explicitement que le service est read-only.
- `docker-compose.hip4.yml`:
  - ajouter un service profile/opt-in;
  - verifier que les variables de secrets ne permettent pas l'execution.
- `trident-hip4/fetch_data.sh`:
  - rapatrier `logs/hip4_nautilus_shadow/`;
  - rapatrier le status et le state si presents;
  - ajouter un mode dry-run visible.
- `scripts/fetch_trident_data.sh`:
  - ne pas rapatrier Nautilus pour TRIDENT A/C par defaut;
  - garder une compat seulement si un ancien deploiement hybride expose encore
    ces logs.
- `scripts/trident_dry_run_review.sh`:
  - inclure les artefacts Nautilus seulement si le dossier existe;
  - ne pas faire echouer la review A/C si Nautilus est absent.
- `app/backtest/hip4_outcome_run_review.py` et
  `app/trident/hip4_outcome/analysis.py`:
  - ajouter ingestion et buckets optionnels;
  - status explicite `missing/partial/ok`.
- UI HIP-4:
  - afficher un badge `Nautilus shadow`;
  - montrer `shadow_ready`, age dernier book, reconnects, `quality_score`;
  - ne pas afficher de PnL Nautilus comme PnL de trading.

Risques a surveiller:

- Derive de symbologie entre `#E`, `+E`, token id, asset id et `InstrumentId`.
- Differents arrondis de prix/size entre TRIDENT et Nautilus.
- Global singleton state Nautilus: eviter plusieurs `TradingNode` dans le meme
  process; preferer un sidecar separe.
- Explosion de logs book si tous les outcomes sont suivis.
- Rate limits WebSocket ou reconnect storms.
- Confusion operateur entre `paper`, `shadow`, `testnet` et `mainnet`.
- Fausse impression de performance si le shadow voit un book plus frais mais
  non executable.

Critere de fin du shadow initial:

- Rapport experimental date avec:
  - version Nautilus;
  - version TRIDENT;
  - fenetre observee;
  - nombre de marches suivis;
  - distribution `max_book_age_ms`;
  - distribution `book_pair_skew_ms`;
  - comparaison bid/ask TRIDENT vs Nautilus;
  - erreurs/reconnects;
  - verdict `go/watch/park/kill`.
- Aucun changement de trading actif.
- Aucun changement des caps.
- Aucun changement de baseline officielle.
- Si le shadow initial est `go`, ouvrir un plan d'implementation separe pour
  une migration partielle.

### Backlog LLM Research Sidecar / TradingAgents

Verdict post TradingAgents / multi-agents LLM:

- Le papier et le framework sont interessants comme architecture de recherche:
  plusieurs agents jouent des roles d'analystes, debat bull/bear, trader,
  risk manager et portfolio manager.
- Ce n'est pas une preuve d'edge directement exploitable pour TRIDENT: le cadre
  vise surtout des actions et horizons plus lents, alors que `Pod B HIP-4`
  depend de books outcome, references, expiry proche, latence et calibration.
- Ne pas mettre un LLM dans la boucle d'execution: aucun agent ne doit ouvrir ou
  fermer une position, modifier les caps, changer le mode mainnet, editer une
  config active, ou promouvoir une regle sans replay et confirmation humaine.
- Usage cible: sidecar de review post-fetch et de recherche offline, apres
  collecte mainnet paper, pour accelerer l'analyse sans devenir autorite de
  trading.

Architecture cible:

- `DataQualityAnalyst`: lit `latency_stats.csv`, books, references, age/skew,
  divergences et futurs champs `data_quality`; propose des anomalies testables.
- `LossReviewAnalyst`: classe les pertes par stale book, spread, reference
  divergence, late expiry reversal, insufficient depth, model overconfidence,
  market already open ou sortie anticipee mal calibree.
- `BullResearcher` et `BearResearcher`: debattent une hypothese de guardrail ou
  de sizing, puis formulent un predicat entry-time concret et replayable.
- `RiskReviewer`: verifie caps, drawdown, exposition par underlying, slippage,
  sample size, biais de selection et impact sur Pod A/Pod C.
- `ReplayPlanner`: produit les commandes de replay/review a lancer et les
  slices minimales a comparer; ne change pas le code automatiquement.
- `OperatorReporter`: synthetise le rapport en francais avec verdict
  `go/watch/park/kill`, questions ouvertes et prochaines validations.

Inputs autorises:

- `server-data/logs/hip4_outcome_mainnet_paper/`:
  `opportunities`, `decisions`, `trades`, `settlements`, `latency_stats`,
  `edge_decay`, `short_expiry_features`, `market_observations`,
  `daily_summary`.
- Rapports `server-data/replay_reports/hip4_outcome_run_review_latest.*`.
- Configs HIP-4 et TRIDENT, uniquement en lecture, pour expliquer les seuils.
- Baselines full-bot Pod A/Pod C quand une hypothese pourrait toucher le
  portefeuille global.
- Futurs logs `data_quality` des qu'ils existent.

Outputs attendus:

- Un rapport experimental date dans `server-data/replay_reports/` ou `tmp/`,
  jamais en remplacement d'une baseline officielle sans demande explicite.
- Une liste courte de candidats testables, avec predicat entry-time,
  motivation, risques et commande de replay proposee.
- Des recommandations de collecte ou instrumentation, separees des
  recommandations de strategie.
- Aucun changement live, aucun ordre, aucune promotion et aucun changement de
  caps sans validation humaine.

Regles de promotion:

- Le sidecar LLM peut proposer, jamais decider.
- Tout candidat issu d'un debat LLM doit battre la baseline pertinente via
  replay comparable, avec PnL/PF/Brier/log-loss, sample suffisant et analyse
  par jour/expiry/underlying/side.
- Les conclusions doivent etre deterministes et replayables: si l'agent utilise
  du texte libre, il doit produire aussi des champs structures exploitables par
  les scripts de review.
- Priorite inferieure a `data_quality`: ne pas construire d'agents sophistiques
  tant que les books/references/fills/settlements ne sont pas propres et
  replayables.
- Si ce sidecar devient un service serveur ou ecrit de nouveaux logs a
  rapatrier, mettre a jour les scripts de deploiement, `scripts/fetch_trident_data.sh`
  et `scripts/trident_dry_run_review.sh`.

Cadrage API et couts indicatifs:

- Releve de prix API effectue le `2026-05-23`; a revalider avant tout budget
  engage, car les tarifs et noms de modeles evoluent regulierement.
- Fournisseurs envisageables: OpenAI API, Anthropic Claude API, Gemini API.
  Pas de fine-tuning requis au depart; utiliser une cle API dediee, stockee
  hors repo, et des appels offline/post-fetch uniquement.
- Ne pas envoyer les logs bruts au LLM. Le dossier
  `server-data/logs/hip4_outcome_mainnet_paper/` pese environ `678M` dans le
  workspace courant, principalement `market_observations.jsonl` et
  `decisions.jsonl`; le bon design est de pre-agreger localement puis de donner
  au LLM les rapports, tables agregees et echantillons cibles.
- Prix de reference releves le `2026-05-23`:
  - OpenAI `gpt-5.4-mini`: environ `$0.75/M` tokens input et `$4.50/M` output.
  - OpenAI `gpt-5.4`: environ `$2.50/M` input et `$15/M` output.
  - OpenAI `gpt-5.5`: environ `$5/M` input et `$30/M` output.
  - Anthropic Claude Sonnet 4.6: environ `$3/M` input et `$15/M` output.
  - Gemini 3.1 Flash-Lite: environ `$0.25/M` input et `$1.50/M` output.
- Ordres de grandeur par run quotidien, si les donnees sont agregees avant
  appel LLM:
  - review legere quotidienne (`~50k` input / `~5k` output): environ
    `$0.02/j` sur modele tres cheap, `$0.06/j` sur mini conseille, et
    `$0.20-$0.40/j` sur modele fort.
  - review HIP-4 approfondie (`~200k` input / `~10k` output): environ
    `$0.07/j` sur modele tres cheap, `$0.20/j` sur mini conseille, et
    `$0.65-$1.30/j` sur modele fort.
  - multi-agent `4-6` roles (`~800k-1.2M` input / `~40k-60k` output):
    environ `$0.26-$0.39/j` sur modele tres cheap, `$0.78-$1.17/j` sur mini
    conseille, et `$2.60-$7.80/j` sur modele fort.
  - envoi brut des logs HIP-4 courants (`~170M` tokens input estimes):
    environ `$43/j` sur modele tres cheap, `$128/j` sur mini conseille, et
    `$425-$850+/j` sur modele fort; a eviter.
- Ordres de grandeur mensuels si lance tous les jours:
  - review legere mini: environ `$2/mois`.
  - review approfondie mini: environ `$6/mois`.
  - multi-agent mini quotidien: environ `$25-$35/mois`.
  - multi-agent fort quotidien: environ `$80-$235/mois`.
- Recommandation courante: commencer par un seul rapporteur offline combinant
  `OperatorReporter` et `LossReviewAnalyst` sur un modele mini, puis reserver
  un modele fort uniquement pour relire une courte liste d'hypotheses avant
  replay comparable.

## Idees A Garder: Bot Prediction Market / Post Crypto_Jargon

Verdict:

- Le post "prediction market trading bot" n'est pas une preuve d'edge exploitable. Il melange des briques reelles de trading systematique avec des promesses non verifiees (`68.4% win rate`, `$300-$1,500/day`).
- La partie "Anthropic dropped" doit etre consideree comme marketing ou interpretation: la doc Anthropic consultee est un guide general de Skills Claude, pas une strategie officielle de trading.
- Les reponses indexees autour du post sont surtout sceptiques: edge qui disparait si tout le monde copie, manque de details sur fees/slippage/fills, dependance API/latence.

Ce que Pod B fait deja dans cet esprit:

- Scan de marches HIP-4 outcome.
- Estimation `p_model - p_market` et edge net frais/slippage.
- Garde-fous de budget, exposition, profondeur, spread, minimum ordre HL, reconciliation/fills/settlement.
- Logs decisionnels et UI pour analyser par coin, type d'edge, PnL, fees et settlements.

Ce que Pod B ne fait pas encore:

- Agent swarm Twitter/Reddit/RSS.
- Modele ML type XGBoost entraine sur historique de settlements.
- Fractional Kelly base sur probabilites calibrees.
- Auto-hedge cross-venue ou execution multi-CLOB.
- Auto-learning qui modifie la strategie tout seul.

Backlog utile, ordre recommande:

1. Dataset complet et replayable: snapshots book, reference prices, decisions, fills, fees, settlements, latence, edge decay.
2. Calibration: Brier score, log-loss, courbes de calibration, walk-forward par date, underlying, expiry horizon et type d'edge.
3. Sizing: fractional Kelly seulement apres calibration; garder hard caps par trade, coin, expiry, jour et drawdown.
4. Modele simple: logistic regression ou XGBoost seulement apres assez de settlements; baseline heuristique Pod B doit rester benchmark.
5. Loss review: classifier les pertes en stale price, spread, reference divergence,
   late expiry reversal, insufficient depth, model overconfidence.
6. Sentiment/news/LLM: a garder pour plus tard, surtout pour marches narratifs ou macro; priorite faible pour HIP-4 crypto 5m/15m ou la latence et la microstructure dominent.
7. Cross-venue/parity: interessant plus tard si on peut mesurer fills, slippage, inventory risk et settlement mismatch.

Regle:

- Ne pas implementer Kelly/ML/agents tant que Pod B n'a pas accumule un
  historique mainnet paper propre avec settlements exploitables.

## Validations Recentes

Resultat courant `2026-05-19`:

- Historique git relu depuis `2026-05-13`: les commits recents ont surtout
  porte sur le live hybride A/C, le support testnet separe, le remplacement Pod
  B par HIP-4 mainnet paper, la suppression de la piste trigger-liquidity, puis
  le durcissement de la reconciliation live.
- Tests locaux:
  - `python -m py_compile app/live/exchange_position_metrics.py app/live/reconciliation.py app/execution/live.py app/live/pod_a_live_runner.py app/live/pod_c_live_runner.py app/persistence/journal.py`: OK.
  - `.venv/bin/python -m unittest tests.test_live_readiness tests.test_pod_a_live_runner tests.test_journal tests.test_reporting tests.test_health`: `48` tests OK.
- Replay baseline officielle avec repo/config courants:
  - commande: `.venv/bin/python -m app.backtest.full_bot_replay --config config/trident.toml --input server-data/replay_inputs/external_reference_multisource_20260405_20260513_baseline.jsonl --report-output tmp/full_bot_baseline_current_20260519.json --summary-output tmp/full_bot_baseline_current_20260519.md`;
  - `40632` records, `301` timestamps dupliques ignores, memes dates que la
    reference archivee;
  - total `+872.74 USD` vs `+859.83 USD` archive (`+12.91`);
  - Pod A `+793.63` vs `+780.72`, `161` trades vs `155`;
  - Pod B `0.00`, Pod C `+79.11` inchange;
  - delta entierement explique par `6` trades `HYPE trend_pullback_long`
    reintroduits apres rollback du veto HYPE.
- Test serveur reel:
  - rebuild + restart uniquement `pod-a-live` et `pod-c-live` en
    `live/testnet`;
  - position BTC deja ouverte cote Hyperliquid reprise par Pod C;
  - Pod A classe BTC comme position externe connue;
  - `/api/report` remonte Pod C `position_count=1` et
    `total_unrealized_pnl_usd` depuis le status runtime;
  - logs post-restart A/C sans `Traceback`, sans `TypeError`, sans echec de
    reconciliation.
- Fixs valides:
  - journal JSONL compatible avec `Decimal` dans les fills live;
  - payload open positions priorise les valeurs Hyperliquid;
  - close live reduce-only utilise la taille exchange exacte;
  - cartes `Status > Pods` affichent `PnL realise` et `PnL latent`.
- Redeploiement propre `2026-05-21`:
  - ancien journal Pod A archive serveur dans
    `logs/archive/20260521T055645Z_redeploy_base/`;
  - `pod-a-live`, `pod-c-live`, `trident-api`, `hip4-outcome-dry-run` et
    `tradfi-funding-collector` redemarres en `live/testnet --without-funding`,
    `RestartCount=0` au demarrage de verification;
  - `/health`: `status=ok`, `mode=live`, `exchange_network=testnet`,
    `kill_switch_active=false`, version `3f56fd05 (2026-05-19 17:09)`;
  - `/api/state`: Pod A `ready=true`, `live_trading_paused=false`,
    `external_known_positions=["SOL"]`; Pod C `ready=true`,
    `live_trading_paused=false`, `open_positions=["SOL"]`; pas de positions
    inconnues/manquantes, pas d'ordres ouverts inconnus, pas de
    `trigger_orders` orphelins;
  - correction live validee: selection de close fills par timestamp/role
    plausible, conservation des metadonnees d'ordres pour les positions
    ouvertes non presentes dans la sauvegarde courante;
  - logs post-base rapatries: Pod A `negative_holds=0`, dernier close ETH
    `2026-05-21T06:00:18.384Z` apres open `2026-05-21T05:58:00Z`; Pod C
    `negative_holds=0`, dernier close BTC `2026-05-21T06:03:00Z` apres open
    `2026-05-21T05:49:00Z`.

Validation code HIP-4, observation embedded et integration UI/dry-run:

```bash
uv run python -m py_compile app/trident/hip4_outcome/models.py app/trident/hip4_outcome/parser.py app/trident/hip4_outcome/config.py app/trident/hip4_outcome/probability.py app/trident/hip4_outcome/edge.py app/trident/hip4_outcome/runner.py app/trident/hip4_outcome/logging.py app/trident/hip4_outcome/risk.py app/trident/hip4_outcome/analysis.py app/live/hip4_outcome_runner.py app/live/trident_dry_run_launcher.py app/observability/api.py
```

```bash
bash -n deploy.sh scripts/trident_server.sh scripts/trident_dry_run_review.sh scripts/fetch_trident_data.sh
```

```bash
uv run python -m unittest tests.test_hip4_outcome_pod tests.test_hip4_outcome_analysis tests.test_trident_dry_run_launcher tests.test_health
```

```bash
uv run python -m unittest tests.test_risk_gate tests.test_pod_a tests.test_health
```

Resultat courant `2026-05-17`:

- `uv run python -m unittest tests.test_hip4_outcome_pod tests.test_hip4_outcome_analysis tests.test_trident_dry_run_launcher tests.test_health`: `75` tests OK.
- `uv run python -m unittest tests.test_risk_gate`: `27` tests OK; couvre le
  fait que le veto HYPE est charge mais desactive et qu'un
  `HYPE trend_pullback_long` n'est plus rejete par cette regle.
- `uv run python -m unittest tests.test_risk_gate tests.test_pod_a tests.test_health`: `79` tests OK.
- `uv run python -m py_compile app/trident/hip4_outcome/models.py app/trident/hip4_outcome/parser.py app/trident/hip4_outcome/config.py app/trident/hip4_outcome/probability.py app/trident/hip4_outcome/edge.py app/trident/hip4_outcome/runner.py app/trident/hip4_outcome/logging.py app/trident/hip4_outcome/risk.py app/trident/hip4_outcome/analysis.py app/live/hip4_outcome_runner.py app/live/trident_dry_run_launcher.py app/observability/api.py`: OK.
- `bash -n deploy.sh scripts/trident_server.sh scripts/fetch_trident_data.sh scripts/trident_dry_run_review.sh`: OK.
- `config/hip4_outcome_testnet.toml`: testnet conserve `enable_model = false`,
  mais les blocages HYPE et `block_reference_divergence` sont retires
  (`blocked_opportunity_slices = []`, `reference_divergence_underlyings = []`).
- `server-data/replay_reports/hip4_outcome_run_review_latest.md`: review
  mainnet paper en place, statut `collect_more_data`, Brier `0.2695`, samples
  calibration `3/20`.
- `server-data/replay_reports/pod_a_c_shortlist_validation_latest_fetch_20260517.md`:
  rerun latest fetch complet OK, baseline `783.17` (`Pod A 715.59`,
  `Pod C 67.58`) et verdicts watch/rollback mis a jour dans la roadmap.
- `config/trident.toml`: rollback HYPE Pod A applique
  (`hype_trend_pullback_long_targeted.enabled = false`).
- `priceBucket` parse et modele paper/observer couverts; pas d'execution reelle.
- `Named Outcome` et classes inconnues logguees en observation, sans modele ni execution.
- API `/api/hip4-outcome` et alias Pod B conservent le chemin d'integration HIP-4.
- Fix important: `OutcomePosition.from_dict` et le state reload HIP-4 sont couverts par test de round-trip.

## Decisions Nettoyees

Ces pistes ne doivent plus apparaitre comme roadmap active. Elles restent seulement historiques si on relit les anciens rapports.

### Rejete / Non Promu

- Pod B Hyperps dynamique:
  - infra utile, mais pas de promotion.
  - raison: univers live courant insuffisant et TAO ne doit pas remplacer un Hyperp actif.
- Sleeve special symbols `TAO/XPL/BIO/PENGU`:
  - pas assez de couverture comparable, pas portefeuille-additif.
  - TAO reste bloque tradable.
- Crypto Regime V2 / `hybrid_moderate_a`:
  - interessant en shadow, pas promu.
  - trop de churn et faux positifs sur la fenetre recente.
- Pod B microstructure directionnel:
  - watchers `micro_liquidity_pull_trend_panic` et
    `micro_depth_refill_trend_panic` utiles en research/watch-only.
  - le replay d'integration `2026-04-23` garde la baseline full-bot inchangee;
    meme Pod B explicitement enabled produit `0` signal sur le fetch comparable.
- Squeeze / breakout via Pod B:
  - tests cibles a `0` trade ou non additifs.
- Shorts globaux Pod A:
  - rejetes.
  - `short_49_pivot_low_break_strong_flow` reste shadow seulement:
    standalone positif (`+153.73`, `216` trades), mais le full-bot promu degrade
    la reference (`571.67` total vs `859.83` officiel) et augmente fortement le
    nombre de trades Pod A (`408` vs `155`).
  - toute these short future doit battre le full-bot, pas seulement le standalone.
- Funding / liq / open interest comme pod principal:
  - pas de preuve replay comparable suffisante.
- Mean reversion generaliste:
  - recherche seulement, pas de pod live.
  - les recherches Hyperliquid top30/top50 ont transforme l'idee en verdicts:
    `range_mean_reversion` et `funding_reversion` sont surtout `kill/park`;
    quelques symboles restent candidats research, sans promotion transversale.
- Pod C `silver routing grace`, `gold routing grace`, equity/fx:
  - pas de promotion avec les donnees actuelles.
- Shorts Pod C:
  - rejeter les familles broad/oil/equity/silver/gold testees le `2026-05-17`.
  - seuls `pc_short_21_fx_breakdown_flow` (`+22.34`, `2` trades) et
    `pc_short_06_index_donchian_break_60` (`+0.93`, `4` trades) restent watch
    trop petits pour promotion.

### Garde En Watch / Research Seulement

- Microstructure `depth_refill_continuation` et
  `liquidity_pull_continuation`: watchers/research, pas execution. Le holdout
  `2026-04-23` reste positif (`depth_refill` `+1.3093 bps`, hit `0.5377`;
  `liquidity_pull` `+0.9681 bps`, hit `0.5504`) mais pas encore live gating.
- `funding_reversion`, `range_mean_reversion`, `stoch_cci_reversion`: research
  seulement; top50 ne justifie qu'un upgrade des pods existants, pas un nouveau
  pod mean-reversion.
- LTC/ZRO overextension: ZRO reste interessant en research
  `ema50_overextension_reversion`, LTC reste plutot `vwap_reclaim`/Pod A; aucun
  veto cible valide a promouvoir.
- `absorption` et `book_churn_flow_veto`: park/reformuler avant promotion.

## Roadmap Courante

### 1. Lancer Le Mode Hybride A/C Live + B Paper

- Deployer les changements via `./deploy.sh --start --mode live --without-funding`
  depuis le poste local, apres validation des credentials et du preflight.
- Lancer le bot complet sans `--without-pod-b`: Pod B doit rester HIP-4 paper,
  pas le legacy `pod-b-live`.
- Verifier que les services actifs sont `pod-a-live`, `pod-c-live` et
  `hip4-outcome-dry-run`.
- Verifier que le config runtime est
  `config/hip4_outcome_mainnet_paper.toml`, que le mode expose est `paper`, et
  que `allow_testnet_orders = false`.
- Verifier que `runtime/hyperliquid_rate_limits.json` est persistant entre les
  runs et que les compteurs private info / exchange action ne montrent pas de
  breaker ouvert.
- Verifier que `/api/hip4-outcome.blocked_opportunity_slices` est vide.
- Suivre `/hip4-outcome`:
  - edges par type
  - short-expiry features
  - edge decay
  - settlements estimes
  - PnL paper par underlying
  - bloc mainnet paper: markets, references, opportunities, replay
  - observations de classes HIP-4 non supportees et `priceBucket`
- Suivre aussi `/dashboard` et `/api/report`: Pod B doit pointer vers `pod_kind = hip4_outcome_edge_pod`.
- Ne pas conclure sur un seul signal; attendre plusieurs expiries.

### 2. Analyser Les Runs Pod B

Prerequis:

- fetch serveur complet apres quelques heures.
- logs mainnet paper et mainnet observer historique: `opportunities`,
  `decisions`, `trades`, `settlements`, `edge_decay`,
  `short_expiry_features`, `market_observations`, `daily_summary`.
- statut API et UI coherents.

Action:

- separer les vrais edges des artefacts de donnees: divergence de reference,
  book stale, absence de profondeur, settlement mismatch estime.
- calculer PnL net fees par coin, cote, type d'edge, horizon d'expiry et heure.
- mesurer win rate, profit factor, drawdown, edge decay et fill quality. Le win rate seul ne suffit pas.
- comparer mainnet paper vs mainnet observer: reference price, spread, depth,
  edge decay, et frequence des signaux.
- produire `hip4_outcome_run_review_latest.{json,md}` apres chaque fetch serveur complet.
- utiliser la section `Guardrail Candidates` pour choisir seulement des
  restrictions entry-time prouvees en mainnet paper.
- Ne pas ajouter de slice tant que la fenetre mainnet paper n'a pas confirme un
  Brier `<= 0.23` avec un volume encore exploitable.

### 3. Calibration Avant Sizing Dynamique

- Brier score, log-loss, buckets de calibration, loss review et guardrail
  simulation sont en place.
- Continuer la collecte: la derniere review mainnet paper depasse le minimum
  brut de settlements mais reste bloquee par PF `0.9106/1.15`, Brier
  `0.2405 > 0.23`, et absence de profil mainnet observer comparable.
- Faire du walk-forward par jour/expiry plutot que valider sur une seule fenetre.
- N'autoriser fractional Kelly ou XGBoost qu'apres historique suffisant et stable.
- Garder `max_position_usdc`, `max_total_outcome_exposure_usdc` et `max_per_underlying_outcome_exposure_usdc` comme hard caps meme si Kelly propose plus.

### 3b. Tester Les Sorties Anticipees HIP-4

- Objectif: sortir plus tot que settlement quand le carnet paie deja assez ou
  quand l'edge a disparu, afin de reduire la duree d'inventaire et d'augmenter
  le turnover paper sans augmenter le notional.
- Priorite d'implementation: `mainnet_paper` uniquement, avec journal dedie
  `early_exits.csv`.
- En parallele, garder des experiences shadow paper-only, sans changer les
  positions actives:
  - `shadow_exit_policies.csv`: hold-to-settlement, take-profit partiel
    +25/+35/+50%, sortie EV conservative, sortie defensive, sortie derniere
    fenetre 5/10/15 minutes;
  - `shadow_sizing.csv`: fractional Kelly virtuel/cappe pour estimer le sizing
    avant de toucher au notional actif;
  - `shadow_maker_quotes.csv`: quotes passive/maker virtuelles pour mesurer les
    cas ou le spread pourrait etre capture sans envoyer d'ordre reel.
- Regles initiales:
  - sortie partielle au bid sur ROI positif materialise;
  - sortie partielle + runner si le bid est superieur a l'EV conservative de
    hold;
  - sortie totale defensive si la probabilite de win conservative tombe sous
    seuil et que le bid recupere encore assez de valeur;
  - lock de re-entry sur le meme marche jusqu'au settlement apres sortie totale
    pour eviter le churn.
- Comparer apres plusieurs jours: hold-to-settlement historique vs early-exit
  paper, PnL, max drawdown, turnover, Brier/calibration et opportunites
  `SHORT_EXPIRY` debloquees.

### 4. Ameliorer La Latence HIP-4 Seulement Si Necessaire

Priorite apres plusieurs runs mainnet paper:

- remplacer le polling critique par streaming/WS si l'edge decay montre que les signaux disparaissent trop vite.
- ajouter book cache / allMids cache pour eviter de dependre de REST a chaque boucle.
- mesurer la latence dans `latency_stats.csv` avant d'optimiser.

### 5. Garder Pod A / Pod C Stables

- Pas de nouveau sweep massif tant que le Pod B HIP-4 est en exploration
  mainnet paper.
- Rejouer la baseline officielle seulement quand le fetch serveur change ou avant une promotion.
- Toute divergence live/replay doit etre analysee avec `collector + maintenance_refresh`.
- Shorts Pod A `2026-05-16`: `short_49` reste shadow, pas promotion, car le
  standalone positif ne survit pas au full-bot promu.
- Shorts Pod C `2026-05-17`: rejeter les familles larges; seulement deux
  micro-candidats FX/index restent watch trop petits.
- Latest shortlist full fetch `2026-05-17`:
  `server-data/replay_reports/pod_a_c_shortlist_validation_latest_fetch_20260517.md`.
- Resultats shortlist OOS `2026-05-05`:
  - Pod A HYPE veto: `reject`; rollback applique dans `config/trident.toml`
    (`enabled = false`) apres confirmation latest fetch (`-14.72`, `13`
    vetoes).
  - Pod A MTF vetoes: `reject` sur l'OOS recente mais `keep` sur latest fetch
    (`+32.97`, `94` vetoes); conserver actifs, ne pas etendre.
  - Pod A BTC/XRP overextension: `no_effect` sur OOS `2026-05-05`, mais
    `keep` sur latest fetch (`+26.20`, `3` vetoes); conserver actifs.
  - Pod C relaxed cluster-aware off: `reject` sur OOS et encore plus net sur
    latest fetch (`-353.32`, `408` trades); conserver la selectivite.
  - Pod C silver veto: `reject` sur OOS tres petit (`1` veto), mais `keep` sur
    latest fetch (`+18.52`, `13` vetoes); conserver actif.
  - Pod C gold vetoes: `gold_soft_extension_veto` watch tiny-sample
    (`+6.26`, `1` veto), `gold_strong_neutral_veto` sans effet,
    `gold_medium_neutral_veto` rejete (`-32.78`, `3` vetoes).
  - Pod C signal drought recent: `2026-05-02`, `2026-05-03` et `2026-05-05` sans signal; `2026-05-04` a `6` signaux mais `0` acceptes. Pas d'anomalie mecanique prouvee, plutot selectivite/regime.

### 6. Deploiement / Rollback

- S'assurer que le serveur utilise `config/trident.toml`.
- Chemin live hybride attendu: `./deploy.sh --start --mode live --without-funding`
  lance `pod-a-live` et `pod-c-live` en mode `live`, plus
  `hip4-outcome-dry-run` en `paper`.
- Le script serveur force `HIP4_OUTCOME_CONFIG=config/hip4_outcome_mainnet_paper.toml`,
  `HIP4_OUTCOME_MODE=paper` et `HIP4_OUTCOME_ALLOW_TESTNET_ORDERS=false` quand
  `TRIDENT_MODE=live`.
- Verifier que HIP-4 reste le Pod B paper, sans mode execution mainnet et sans
  `HIP4_OUTCOME_ALLOW_TESTNET_ORDERS=true`.

Critères de passage A/C en mainnet tiny-size:

- Fenetre minimale: `72h` propres apres le redeploiement stable du
  `2026-05-21T06:07:35Z`; prochaine reevaluation cible:
  `2026-05-24T06:07:35Z`.
- `pod-a-live`, `pod-c-live` et `trident-api` up en continu, sans crash loop et
  avec `RestartCount=0` depuis le dernier redeploiement, sauf restart manuel
  explicitement documente pour test de recovery.
- Runtime A/C frais en continu dans `/api/state` et `/api/report`:
  `runtime status fresh`, `healthy=true`, `live_trading_paused=false`.
- Reconciliation A/C propre: `ready=true`, `reasons=[]`,
  `unknown_exchange_positions=[]`, `missing_exchange_positions=[]`,
  `side_mismatches=[]`, `open_orders=[]` ou uniquement des orders connus par le
  state store.
- Au moins deux redemarrages/reconciliations propres avec positions exchange
  existantes, dont un cas ou Pod A voit une position connue par Pod C comme
  `external_known_positions`.
- Au moins un cycle reel `open -> close` sur Pod A et un cycle reel
  `open -> close` sur Pod C en testnet apres le fix `triggerPx`, avec state
  local persiste et PnL/fills coherents dans le pod et le superviseur.
- Les erreurs transitoires Hyperliquid testnet (`502`, websocket reconnect,
  timeout) doivent seulement pauser les entrees puis revenir a `ready=true`;
  aucun fill reel ne doit rester sans state local, et aucun ordre protecteur
  requis ne doit echouer sans emergency close.
- Fetch serveur post-burn-in complet: `/health`, `/api/state`, `/api/report`,
  `/api/metrics`, logs Docker, runtime states, snapshots et journals recuperes;
  review sans `Traceback`, sans `Decimal is not JSON serializable`, sans
  divergence A/C live vs state store.
- Rejouer la baseline/replay seulement si le fetch serveur ou la config ont
  change; sinon documenter que la promotion mainnet ne modifie pas la strategie,
  seulement le reseau et les caps.
- Mainnet uniquement tiny-size et manuel: config `config/trident.toml`,
  `--mode live --network mainnet --without-funding`, caps de notional live
  verifies, preflight Pod A et Pod C separes OK, confirmation operateur requise.
- Pod B HIP-4 reste `mainnet paper`; aucune execution HIP-4 mainnet n'est
  incluse dans cette promotion A/C.
- Garde-fous rate limit ajoutes pour le live A/C:
  - lectures privees HL cadencees par `private_info_requests_per_minute`;
  - actions `order/cancel` cadencees par `live_order_actions_per_minute`;
  - breaker partage sur signal 429/rate-limit.
- Garder un rollback simple:
  - couper le Pod B HIP-4 avec `--without-pod-b`
  - `--without-hip4-outcome` reste accepte comme alias historique
  - couper seulement le sidecar observateur mainnet avec `--without-hip4-mainnet-observer`
  - ou laisser `HIP4_OUTCOME_MODE=paper`

## Regles De Promotion

- Une idee validee seulement en candles/research ne passe pas en prod.
- Une idee positive en standalone mais negative en full-bot ne passe pas en prod.
- Une idee HIP-4 mainnet paper ne passe pas en execution mainnet sans dataset
  mainnet replayable, calibration, dry-run propre, preflight separe, caps
  tiny-size et confirmation manuelle.
- Une nouvelle logique doit etre lisible dans l'UI et dans les logs avant toute activation durable.
- Les documents historiques peuvent expliquer une decision, mais ne rouvrent pas automatiquement une piste rejetee.

## Documents Historiques / Non Canoniques

Ces documents peuvent etre consultes pour le contexte, mais ne sont plus la roadmap active:

- `hip4.md`
- `docs/hip4_outcome_testnet.md`
- `docs/new_podB.md`
- `docs/crypto_refonte_plan_20260417.md`
- `docs/pod_c_vs_pod_a_transfer_20260418.md`
- `docs/pod_c_research_protocol.md`
- `docs/pod_liq_data_feasibility.md`
- `docs/trident_plan/spec.md`
- `docs/trident_plan/status.md`
- `docs/trident_plan/stages.md`
