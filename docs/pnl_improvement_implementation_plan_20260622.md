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
| A-PNL-01 | ready_review | P1-08 uniquement en sizing progressif demi-cap: `throttle=0.50`, `quarantine=0.50`, aucun blocage, avec logs `guard_state` et `live_action_changed`. Implementation 2026-06-22: code cap-only disponible via config `dynamic_symbol_guard_live_sizing_enabled=false` par defaut; la variante `quarantine=0.10` est rejetee. | Les donnees recentes montrent que les symboles en etat degrade concentrent des pertes; le demi-cap conserve l'activite et reduit legerement le drawdown sans supprimer les trades Pod A sur la fenetre live. | Replay dedie positif mais faible (`+2.91` A/C, PF Pod A `0.6879`): avant live, adapter l'audit `live_action_unchanged`, redeployer explicitement, puis review post-deploiement; aucune activation automatique. |
| A-PNL-02 | shadow | Echelle de notional par etat symbole implementee en dormant: stats rolling `symbol/setup` exposees, base `0.70`, partiel `0.85`, plein sizing seulement apres PF/expectancy rolling positifs. Flag `dynamic_symbol_guard_recovery_sizing_enabled=false` par defaut. | Les pertes recentes ne viennent pas d'un manque d'activite mais d'un mauvais payoff; reduire la taille dans les contextes mediocres ameliore l'esperance sans couper. | Replay full-window `p108_recovery_sizing_20260622`: positif vs courant (`+2.79`) mais inferieur a P1-08 `cap50/cap50` (`+2.91`) avec plus de reductions. Statut `research_only_no_live_change`; ne pas activer tel quel, garder dormant pour variantes futures. |
| A-PNL-03 | shadow | Cap headroom A-grade implemente en dormant: `a_grade_size_headroom_cap_enabled=false`; garde le label/exits A-grade mais limite le scale taille a la marge symbole et au risk budget initial si active. Le freeze strong P1-05 reste rejete. | Reduire la convexite des losers A-grade sans supprimer le signal ni les exits, tout en evitant de reproposer le freeze deja teste. | Replay live-window `p105_a_grade_headroom_cap_live_20260622`: positif mais non materiel (`+0.08` A/C, PF Pod A `0.6762` vs `0.6756`, DD `41.06` vs `41.14`). Statut `research_only_no_live_change`; ne pas activer tel quel. |
| A-PNL-04 | shadow | Audit P116 implemente pour `early_failure_exit`: replay per-trade sans EFE, jusqu'au stop/trailing/break-even/time-stop/cat-stop naturel, avec MFE/MAE post-sortie. | Les sorties precoces reduisent certaines pertes mais peuvent tuer des recoveries; l'audit mesure le cout d'opportunite sans reproposer le disable global deja couvert par P1-02. | Replay complet `p116_early_failure_post_exit_20260622`: sans EFE, les 41 trades EFE empirent de `-8.46` USD. `6` winners + `6` loss-cuts manques, mais `29` pertes evitees; garder EFE, ne pas promouvoir une relaxation globale. |
| A-PNL-05 | shadow | Score microstructure entree implemente en shadow/export: sous-scores spread, flow, microprice, depth, activite, range et churn. Replay P115 cap-only `<0.42` et `<0.56` ajoute, sans blocage ni flag live. | Les sources HFT indiquent que le desalignement prix mid vs micro-price/VAMP revele souvent l'adverse selection; utile en audit, mais la version cap-only testee ne separe pas assez les losers live. | Replay complet `p115_microstructure_entry_20260622`: baseline neutre, live negatif (`-1.02` poor50, `-0.13` weak50). Bucket `poor` live gagnant et pire bucket `strong`; garder shadow/audit, ne pas promouvoir le cap-only. |
| A-PNL-06 | shadow | Ajouter une reference crypto cross-exchange par symbole liquide: Binance/OKX/Bybit/Coinbase/Kraken selon disponibilite, avec premium HL et divergence momentum. | Si Hyperliquid est temporairement en avance ou en retard contre le marche large, le bot peut entrer sur un prix local defavorable. | Aucun effet trading au debut; verifier PnL par bucket de divergence et fraicheur reference. |
| A-PNL-07 | todo | Diagnostiquer les echecs IOC et la qualite de fill attendue: BBO age, depth, spread, price impact theorique, missed fill outcome. | Un bon signal peut devenir mauvais si l'execution paie le spread ou chase un carnet mince; filtrer ou repricer peut ameliorer le payoff moyen. | Rapport fill_quality: accepted, rejected, missed, adverse return 1/5/15m. |
| A-PNL-08 | todo | Remplacer toute blocklist statique par une probation a hysteresis: entree degradee apres cluster de losses, rehabilitation lente apres expectancy positive. | Evite le piege de `evo4_symbol_health` trop brutal: on reduit l'exposition au lieu d'effacer durablement des symboles qui peuvent redevenir bons. | Replay avec comparaison stricte contre P1-08 et baseline courante; mesurer winners perdus. |

### Pod C - tradfi builder-dex

| ID | Statut | Changement a faire | Pourquoi ca peut augmenter le PnL | Validation minimale |
| --- | --- | --- | --- | --- |
| C-PNL-01 | ready_review | Encadrer P1-09 oil short par un stoplight dedie: pas d'augmentation d'exposition tant que les positions fermees et latentes ne valident pas le edge. Implementation 2026-06-22: `fetch_trident_data.sh` expose `oil_stoplight`, closed PnL promu et latent oil ouvert dans `p109_oil_shadow_audit.*`. | Les premiers trades fermes ne suffisent pas; integrer l'unrealized evite de promouvoir un profil qui gagne seulement par hasard de timing. | Rapport quotidien oil: closed + open mark-to-market, PF, MAE, nombre de setups independants. |
| C-PNL-02 | shadow | Convertir P1-03 external reference en soft gate cap-only pour replay: baisse de taille quand premium/reference age/momentum sont defavorables. | Les validations recentes etaient prometteuses mais pas assez OOS; une version sizing limite le risque de tuer de bons trades. | Replay full-bot avec Yahoo/reference coverage explicite; aucune promotion sans baseline complete. |
| C-PNL-03 | shadow | Ajouter un filtre relative-value CL/BRENTOIL: trade oil seulement si les deux jambes confirment ou si le spread z-score soutient la direction. | Les repos Hyperliquid market making/pair trading montrent l'interet d'un prix juste relatif; Pod C a deja deux symboles oil exploitables. | Buckets PnL par accord/desaccord CL-BRENTOIL, spread z-score, session. |
| C-PNL-04 | todo | Session/liquidite calendar: min confidence et cap dynamiques selon US hours, futures active hours, overnight et fraicheur reference. | Beaucoup de faux signaux tradfi viennent de carnets moins actifs ou references lentes; adapter le seuil par session garde le bot actif mais plus selectif. | Rapport par session: WR, PF, slippage, stop_hit, routing_revoked. |
| C-PNL-05 | shadow | Audit maker/taker et cout d'execution builder-dex: spread paye, slippage, adverse return apres fill, simulation passive sans ordre live. | Si le cout d'execution domine le signal, le PnL peut s'ameliorer par timing/price limit plutot que par nouveau signal alpha. | Shadow uniquement; pas de maker live sans fill model. |
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
   A-PNL-01, A-PNL-02, A-PNL-03, C-PNL-02, C-PNL-03, C-PNL-04, H-PNL-01,
   H-PNL-04, H-PNL-05, H-PNL-08.

3. Changements cap-only ou paper-only apres preuve:
   A-PNL-01, A-PNL-02, C-PNL-02, C-PNL-04. Pour HIP4, rester paper tant que
   les seuils de readiness ne sont pas tenus par bucket.

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
  live, aucune activation live ni hausse de cap n'est incluse.
- HIP4: ne pas passer live maintenant. Continuer `prob_stop_full` en paper actif,
  enrichir Nautilus/observability et ne promouvoir le shadow que s'il prouve une
  amelioration nette sur settlements reels avec fills realistes.
