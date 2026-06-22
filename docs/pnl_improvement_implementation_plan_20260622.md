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
| A-PNL-02 | todo | Introduire une echelle de notional par etat symbole: base plus basse, retour progressif au cap seulement apres PF/expectancy rolling positifs. | Les pertes recentes ne viennent pas d'un manque d'activite mais d'un mauvais payoff; reduire la taille dans les contextes mediocres ameliore l'esperance sans couper. | Comparer PnL, PF, max loss/trade, trades conserves et missed upside par symbole. |
| A-PNL-03 | todo | Neutraliser ou capper le boost de taille A-grade en shadow/replay avant tout changement live. | Le bot semble payer cher ses convictions fortes quand elles se trompent; enlever le boost peut reduire les gros losers sans toucher au signal principal. | Replay full-bot `boost=1.0` vs courant, buckets A-grade par symbole/regime, puis paper shadow de 7 jours. |
| A-PNL-04 | shadow | Ajouter un journal MFE/MAE post-sortie pour `early_failure_exit`: suivre virtuellement le trade jusqu'au stop/time/trailing original. | Les sorties precoces reduisent certaines pertes mais peuvent tuer des recoveries; il faut mesurer le cout d'opportunite avant de durcir. | Rapport par exit_reason: pertes evitees, winners manques, delai moyen de recovery, PnL contrefactuel. |
| A-PNL-05 | shadow | Ajouter un score microstructure entree base sur micro-price/VAMP, profondeur BBO, order flow recent et age du carnet. | Les sources HFT indiquent que le desalignement prix mid vs micro-price/VAMP revele souvent l'adverse selection; utile pour eviter d'entrer juste avant un move adverse. | Shadow score attache a chaque signal, deciles de PnL par score, puis replay avec cap-only sur pires deciles. |
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
  confirmation live explicite et audit adapte.
- HIP4: ne pas passer live maintenant. Continuer `prob_stop_full` en paper actif,
  enrichir Nautilus/observability et ne promouvoir le shadow que s'il prouve une
  amelioration nette sur settlements reels avec fills realistes.
