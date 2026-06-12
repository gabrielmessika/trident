# Audit complet TRIDENT — 2026-06-11

Auditeur : revue externe trading/quant, architecture et sécurité opérationnelle.
Sources : `trident_audit_pack_light_20260611.zip` (état runtime/fetch du 2026-06-11, prioritaire) + repo public `github.com/gabrielmessika/trident` (clone `main`, commit `1d8ba57`).
Fichiers exclus du pack initial (assumé) : `trident_ac_signal_decisions.jsonl`, `hip4_decisions.jsonl`, `trident_ac_live_state_pod_a/c.json`. Toute conclusion dépendant des décisions brutes reste marquée `needs_raw_decisions`. Les fills de sortie exchange A/C ont été ajoutés par la remédiation P0-03 du 2026-06-12.

---

## 1. Verdict global

| Périmètre | Verdict | Justification courte |
| --- | --- | --- |
| **TRIDENT A/C — opérationnel** | **PASS avec réserves** | Review `2026-06-11T13:59:56Z` : status PASS, mode live mainnet, reconciliation ready, 0 conflit ownership, cap 200 USD actif (fait observé : `trident_ac_review_summary_latest.md`). |
| **TRIDENT A/C — PnL** | **WARN / partiellement `insufficient_data`** | Fenêtre attribuable (31 trades fermés, 06-09 → 06-11) : Pod A **-5.90 USD** (PF 0.60, WR 24 %), Pod C **+0.46 USD**. Mais le PnL live cumulé runtime est **Pod A -134.27 USD / Pod C -14.21 USD** et seule la queue récente est attribuable : ~90 fermetures antérieures ne sont dans aucun export (`closed_trade_log` = buffer). Divergence majeure vs baseline replay (+780.72 / +79.11) **non expliquée à ce stade**. |
| **TRIDENT-HIP4 — mainnet paper** | **KO pour promotion, OK pour collecte** | 27 trades / 25 settlements, PnL **-47.84 USDC**, PF 0.71, Brier 0.2611 (> seuil 0.23 et > 0.25 d'un prédicteur naïf). Run review : `collect_more_data`. Les cutoffs récents (≥ 06-02) sont négatifs **pour toutes les policies**, y compris `prob_stop_full` shadow. |
| **TRIDENT-HIP4 — mainnet observer** | OK (signal-only) | 0 ordre, `observer_mode_signal_only` confirmé dans runtime statuses. |
| **Sécurité** | **Remédiation P0 clôturée le 2026-06-12** | Constats initiaux : clé privée committée et API HTTP non authentifiée avec endpoint mutant. Suivi : R-01 clôturé (`OK_GIT_REMOTE_PUSHED_SCAN_VERT`) ; R-02 clôturé (`OK_DEPLOY_AUTH_3000_3001`) avec Basic Auth sur `3000`/`3001` et `POST /api/routing/override` désactivé. Risque résiduel : HTTP public sans TLS par choix opérateur. |
| **Readiness opérationnelle** | WARN | Guardrails live bien conçus (live confirm, cap, protective orders, reconciliation stricte, `pending_position` durable post-incident ARB). Restent : risque résiduel HTTP public sans TLS, script fetch avec erreur ambiguë `code 0`, `reference_equity_usd=0.0` dans le report runtime, run review HIP4 non régénérée après fetch. |
| **Qualité des données** | WARN résiduel | Remédiation P0-03 clôturée le 2026-06-12 : close fills exchange, fees, funding et historique complet A/C sont backfillés et exportables. Restent hors P0-03 : MFE/MAE, checksums du pack, `external_reference_*` vides côté Pod C, et replay R-04. |

**Verdict en une phrase** : le système est techniquement sain et bien instrumenté pour un projet de cette maturité ; les remédiations P0 sécurité et données PnL sont clôturées au 2026-06-12, mais le replay R-04 reste nécessaire avant toute conclusion de promotion/augmentation de capital, et HIP4 n'est pas promouvable.

---

## 2. Findings critiques

### F-01 — Clé privée committée dans un repo public (sévérité : **CRITIQUE / P0**)
- **Fait observé** : le fichier `.env.trident` est **tracké par git** (vérifié : `git ls-files`) à la racine du repo public et contient `HIP4_OUTCOME_ACCOUNT_ADDRESS=0xfcC7a37d…2D90dC` et `HIP4_OUTCOME_SECRET_KEY=0xe883…e4d8` (clé privée 32 octets en clair, non reproduite intégralement ici volontairement). Le `.gitignore` ignore `.env` mais **pas** `.env.trident`.
- **Aggravant** : ce même fichier active `TRIDENT_ENABLE_HIP4_OUTCOME=true` et `HIP4_OUTCOME_ALLOW_TESTNET_ORDERS=true`. `scripts/trident_server.sh` charge automatiquement `.env.trident` s'il existe (`--env-file .env.trident`) : un simple `git clone` + démarrage utilise la clé fuitée avec envoi d'ordres testnet autorisé.
- **Inférence** : la clé semble être une clé testnet (mode `testnet` dans le fichier), mais (i) elle est dans l'historique git public de façon permanente, (ii) rien ne garantit qu'elle n'a jamais servi ailleurs, (iii) le pattern montre que des clés peuvent fuir par ce canal — la clé live mainnet utilise le même mécanisme de fichier.
- **Action immédiate** : révoquer/rotater la clé et l'API wallet associée côté Hyperliquid ; retirer le fichier (`git rm --cached`), corriger `.gitignore` (`.env.trident`, `.env.trident-hip4`, `.env.*` sauf `*.example`) ; **purger l'historique** (BFG / `git filter-repo`) ou, plus simple et plus sûr, passer le repo en privé puis recréer un repo public nettoyé si besoin ; lancer un scan complet (gitleaks/trufflehog) sur tout l'historique.

### F-02 — API non authentifiée avec endpoint mutant, exposée réseau (sévérité : **CRITIQUE / P0**)
- **Fait observé** : `app/observability/api.py` (HTTP stdlib) n'a **aucune authentification**. `do_POST` expose `/api/routing/override` qui permet à n'importe quel appelant de forcer/effacer l'ownership d'un symbole (`supervisor.set_runtime_symbol_override`). `docker-compose.trident.yml` lance l'API avec `--host 0.0.0.0` et publie `"3000:3000"` (toutes interfaces de l'hôte) ; idem HIP4 sur 3001.
- **Impact** : sur un bot **live mainnet**, un attaquant joignant le port 3000 peut (i) lire positions, PnL, équity, adresses de capital, (ii) modifier le routing — ce qui peut déclencher des `routing_revoked` et forcer des fermetures de positions réelles. La note `config/trident.toml` (`host=127.0.0.1`) est neutralisée par le compose.
- **Hypothèse** : un firewall Hetzner peut bloquer le port — non vérifiable depuis le pack. La défense en profondeur est absente dans tous les cas.
- **Action appliquée le 2026-06-12** : Basic Auth obligatoire sur UI/API `3000` et `3001` sauf `/health`, même login/password sur TRIDENT A/C et TRIDENT-HIP4, `POST /api/routing/override` désactivé par défaut. Les ports restent publics par choix opérateur (accès téléphone sans domaine), avec risque résiduel HTTP sans TLS accepté.

### F-03 — PnL live cumulé A/C non attribuable (sévérité initiale : **HAUTE / P0 data**, clôturé par P0-03)
- **Fait observé** : runtime Pod A `realized_pnl_usd=-134.27` (115 fills, WR 0.356), Pod C `-14.21` (23 fills, WR 0.261), alors que l'export ne contient que 31 trades fermés (-5.90 / +0.46) couvrant 06-09 → 06-11. `close_fills=0` partout. Les ~90 fermetures précédentes (depuis ~05-24) n'existent dans aucun fichier fourni.
- **Mise à jour 2026-06-12** : le trou de données est clôturé par P0-03 (`155/155` trades matchés aux fills exchange). La divergence live vs baseline reste **le** sujet PnL n°1, mais elle dépend désormais du replay R-04 et de la segmentation par régime/config, plus d'un manque de close fills.

### F-04 — HIP4 : le delta shadow `prob_stop_full` (+237 USDC) ne survit pas aux cutoffs récents (sévérité : MOYENNE)
- **Fait observé** : sur la fenêtre complète, `prob_stop_full` shadow = +189.58 vs active -47.84 (delta +237.41). Mais cutoff ≥ 2026-06-02 : active -29.75, `prob_stop_full` -27.92, `hold_to_settlement` -71.72 — **toutes négatives**. Le runtime confirme que `active_policy=prob_stop_full` est déjà en place ; le -47.84 « active_paper » reflète largement l'ère de l'ancienne policy `bid_over_conservative_hold_ev` (14 exits, +40.90) qui coupait les gagnants. La perte récente est donc un problème de **modèle/edge**, pas seulement d'exit.

### F-05 — Pod A : le boost « strong A-grade » est contre-performant sur la fenêtre live attribuable (sévérité : MOYENNE, échantillon faible)
- **Fait observé** : strong A-grade = 10 trades, **-9.43 USD**, WR 10 % ; standard = 13 trades, +0.92 ; sans A-grade = 2 trades, +2.61 (2/2 gagnants). Le strong A-grade reçoit un size scale jusqu'à 1.40 et une stop grace 120 min — il amplifie donc précisément les trades qui perdent sur cette fenêtre. 25 trades = échantillon insuffisant pour conclure, mais c'est l'inverse exact de l'hypothèse evo11 promue en baseline (+190.14 en replay). Statut : `needs_replay` sur fenêtre récente + `needs_data`.

---

## 3. Audit PnL TRIDENT A/C

Source initiale : `trident_ac_closed_trades.csv` (31 trades, 06-09 → 06-11), `trident_ac_fill_events.csv` (141 open fills, 05-24 → 06-11), `trident_ac_runtime_summary.json`, review du 2026-06-11. Cette section conserve l'attribution **applicative first-pass** du rapport initial ; la section 3.3 et P0-03 remplacent cette limite par le backfill exchange du 2026-06-12.

### 3.1 Pod A (25 trades, -5.90 USD, WR 24 %, PF 0.60)

**Par exit reason** :

| Exit | n | PnL | PnL moyen | Wins |
| --- | ---: | ---: | ---: | ---: |
| trailing_stop | 7 | **+8.72** | +1.25 | 6 |
| break_even_stop | 2 | -0.49 | -0.25 | 0 |
| early_failure_exit | 10 | **-5.87** | -0.59 | 0 |
| exchange_closed_stop_loss | 5 | **-7.10** | -1.42 | 0 |
| stop_hit | 1 | -1.16 | -1.16 | 0 |

- **Gains dominants** : TIA +4.23, ZRO +1.92, BTC +1.31 — tous via trailing stop. Le trailing est le seul mécanisme de sortie positif de la fenêtre.
- **Pertes dominantes** : SOL -2.51 (stop), SUI -1.99 (stop), DOGE -1.62 (stop), ZEC -1.92 (2 trades), PENGU -1.14. Les stops exchange concentrent 48 % des pertes brutes.

**`early_failure_exit` (10/25 trades, 100 % perdants, -5.87)** : perte moyenne réalisée -0.59 vs perte planifiée moyenne -0.84, soit ~70 % du stop planifié. La règle coupe donc bien avant le stop. Question ouverte non tranchée par le pack : combien de ces 10 trades auraient récupéré (BE/trailing) sans EFE ? Sans MFE/MAE post-exit et sans replay full-bot avec/sans EFE, impossible de dire si la règle est nette positive. Notons que les 10 EFE sont presque tous des entrées sizées à 0.50–0.64 (quality sizing déjà défensif), souvent avec watchers `vwap_weak`/`trend4h_flat` : la combinaison « entrée acceptée mais immédiatement annotée faible » est un pattern de sélection à creuser (`needs_raw_decisions`).

**Stops planned vs actual** : sur les 5 `exchange_closed_stop_loss`, ratio perte réelle / perte planifiée entre 0.82 et 0.97 → **`within_plan`**, cohérent avec la review (actual -7.10 vs planned -8.06, excess +0.96 en faveur). Le slippage de sortie n'est donc pas le problème sur ces 5 trades. Une seule anomalie : le `stop_hit` BTC du 06-09 (-1.16 réel vs -0.90 planifié, ratio **1.29 = `mild_excess`**) — réconciliable dans le backfill P0-03.

**Par confidence / A-grade / régime** :

| Bucket confidence | n | PnL | WR |
| --- | ---: | ---: | ---: |
| < 0.62 | 10 | -0.63 | 40 % |
| 0.62–0.70 | 8 | +3.91 | 25 % |
| 0.70–0.78 | 5 | **-7.15** | 0 % |
| ≥ 0.78 | 2 | -2.03 | 0 % |

La haute confiance (≥0.70 : 7 trades, -9.18, 0 % WR) est strictement perdante sur la fenêtre, et c'est elle qui reçoit la taille maximale (pas de quality-sizing réducteur + boost A-grade). Combiné au finding F-05 (strong A-grade -9.43), le mécanisme « plus de conviction → plus de taille » est inversé en live récent. Échantillon faible, mais le signal est cohérent sur deux axes indépendants.

Côté régime, 18/25 fermetures se font en `DeadZone` et 5 en `RangeAuction` : les entrées (faites en TrendExpansion d'après les `fill_events`) survivent rarement à la dégradation du régime — cohérent avec un marché récent sans suivi de tendance, et avec la performance négative de toutes les périodes courtes (PnL négatif sur <15m, 15-60m et 1-6h ; seul le trade >6h est gagnant).

**Coûts d'exécution (fait observé, `fill_events`)** : slippage d'ouverture Pod A moyenne **11.7 bps**, médiane 7.3, max **60.5 bps** — au-dessus de l'hypothèse de 8 bps open utilisée pour le live et les replays. Pod C : 3.25 bps en moyenne. Sur des entrées IOC taker sur alts peu liquides (PENGU, BIO, ZRO, STRK…), le coût d'entrée seul peut absorber une fraction significative de l'expectancy d'un setup à stop 45–130 bps. Le constat initial `fee_usd≈0` après le 05-24 est couvert par P0-03 ; le sujet restant est la modélisation coût/slippage dans R-06/R-04.

**Incohérences de config dans le temps (fait observé)** : les fill events montrent des notionals à 933/500/250 USD avant le 06-06 et des fills sur AVAX, AAVE, ONDO, HYPE, ICP (aujourd'hui bloqués). Le cap 200 et la blocklist actuelle ne s'appliquent que sur la fin de la fenêtre. Toute analyse du PnL cumulé devra segmenter par époque de config ; le changelog opérateur existe dans `docs/trident_active_plan.md`, et doit être utilisé par R-04.

### 3.2 Pod C (6 trades, +0.46 USD, WR 50 %)

| Cluster | n | PnL | Détail |
| --- | ---: | ---: | --- |
| index | 3 | **+3.57** | XYZ100 +0.83 (trailing) et +1.64 (time_stop 9h), SP500 +1.10 (time_stop 9h) |
| oil | 2 | **-2.58** | CL -1.17 et BRENTOIL -1.41, deux stops exchange `within_plan` (ratios 1.00 et 1.03) |
| gold | 1 | -0.53 | GOLD time_stop 6h |

- **`time_stop` (3/6)** : +2.21 au total. Deux des trois time-stops ferment **en profit** (SP500 +1.10, XYZ100 +1.64) sans avoir atteint TP (~120 bps) ni l'activation trailing (74–85 bps). Sur cette micro-fenêtre, le time_stop ne détruit pas l'expectancy ; la question est plutôt s'il coupe l'upside des trades index qui dérivent lentement dans le bon sens. `research_only` tant que l'échantillon < 20.
- **Oil** : 2/2 stops. Les deux entrées `oil_pullback_long` ont VWAP négatif à l'entrée (-1.19, -1.86) conformément à la branche v2 (VWAP entre -2.6 et -1.0) ; pertes dans le plan. Rien d'anormal côté exécution ; c'est la sélection oil récente qui est perdante (échantillon 2 : aucune conclusion).
- **`XYZ:SILVER`** : aucun trade silver dans les closed trades ni les fill events ✓ — le blocage est effectif sur la fenêtre observable.
- **Anomalie data** : `external_reference_available=False` et `external_reference_age_seconds=0` sur les 6 trades. Soit les références externes ne sont pas jointes à l'export, soit elles étaient réellement absentes au moment des entrées — dans ce second cas c'est un problème de qualité de signal TradFi. À trancher (`needs_raw_decisions`).
- **Divergence doc** : le README du repo indique `pod_c.blocked_symbols=["XYZ:GOLD"]` alors que le pack (autoritaire) indique `['XYZ:SILVER']` et que GOLD a tradé le 06-11. README obsolète — hygiène documentaire à corriger.

### 3.3 Mise à jour P0-03 — close fills exchange disponibles
Au 2026-06-12, le blocage initial "sans close fills exchange" est levé par le backfill `server-data/audit_backfills/20260612T161017Z_exchange_backfill/` et l'export compact `server-data/audit_exports/20260612T163311Z_p003_final/`.

- `155/155` trades fermés A/C matchés à des close fills exchange (`pod_a=128`, `pod_c=27`).
- `419` user fills exchange et `223` paiements funding importés en read-only depuis Hyperliquid.
- Close fills exchange matchés : `pod_a=152`, `pod_c=28`.
- PnL net exchange backfillé : Pod A `-142.557837`, Pod C `-15.145243` ; PnL journal applicatif : Pod A `-136.7`, Pod C `-15.05`.
- Validation fill-by-fill conservée : `server-data/audit_backfills/20260612T161017Z_exchange_backfill/fill_by_fill_validation.md` (cas BTC Pod A, open oid `466849105885`, close oid `466889637443`, JSONL `pod_a_live.jsonl:119237`, state store sans position BTC après close, CSV cohérents).

Reste hors P0-03 : MFE/MAE par trade, parité `external_reference_*` Pod C, et replay R-04 pour trancher l'edge live/régime.

---

## 4. Audit HIP4

Sources : `hip4_trades.csv` (27), `hip4_settlements.csv` (25), `hip4_policy_replay.csv`, `hip4_policy_cutoff_replay.csv`, `hip4_outcome_run_review_latest.md` (fenêtre 05-24 → 06-05), `hip4_policy_market_audit_latest.md` (frais, 06-11), `hip4_runtime_statuses.json`. Décisions brutes exclues → comptages de rejets repris du manifest (`needs_raw_decisions` pour toute analyse fine).

### 4.1 État réalisé mainnet paper
- 27 trades paper, 25 settlements, **PnL -47.84 USDC**, PF 0.71, WR 44 %. 100 % edge `MODEL`, BTC ultra-dominant (23/27 trades) ; premières positions non-BTC le 06-11 (SOL/ETH/HYPE, 1 chacune ; HYPE déjà settlée à -1.34, SOL et ETH encore ouvertes au snapshot).
- **Perte dominante** : -49.76 (BTC_GT_76772, BUY_NO settlé YES, 25-05). `worst_loss_share` ≈ 30 % des pertes brutes actives. La séquence du 24-05 sur ce marché est le cas d'école du churn : 5 entrées BUY_YES successives chacune sortie tôt par `bid_over_conservative_hold_ev` (+24.0 cumulés), puis **flip de side** en BUY_NO (2 entrées), la seconde tenue jusqu'au settlement = -49.76. Le `reentry_lock_until_settlement=true` actuel adresse exactement ce pattern — à conserver impérativement.
- **Sizing** : réduit de ~50 USDC à ~11.7 USDC par position autour du 03-06 (`max_position_usdc=12`). Bonne décision de de-risking pendant la phase de calibration ; les pertes récentes sont mécaniquement bornées à ~-11.9.
- Le runtime confirme `active_policy=prob_stop_full` (avec `conservative_probability_lte=0.35`, `exit_roi_gte=-0.2`). Les exits réalisés se répartissent : `bid_over_conservative_hold_ev` 14 (+40.90, ère legacy), `probability_stop` 7 (-9.84), `full_take_profit` 2 (+40.36). Les 7 probability_stops récents coupent de petites pertes (-0.18 à -3.31) — comportement conforme.

### 4.2 Replay policies : lecture correcte
- Fenêtre complète : `prob_stop_full` shadow +189.58 (PF 1.55), `ev_plus_2pct_partial_runner` +160.08, `hold_to_settlement` +105.41, active -47.84. Le delta +237.41 est réel **mais rétrospectif** : il mesure surtout le coût de l'ancienne policy qui vendait les gagnants (les 5 early exits du 24-05 ont laissé ~+74 de payoff potentiel sur la table, cf. best +74.03 des shadows).
- **Cutoffs** (le test qui compte) : entrées ≥ 06-02 → toutes les policies entre -27.9 et -71.7 ; entrées ≥ 06-05 → toutes entre -14.7 et -48.4. `prob_stop_full` est la moins mauvaise des shadows mais reste négative. **Conclusion : l'edge MODEL lui-même est négatif récemment ; aucune exit policy ne le sauve.** Le delta shadow ne doit en aucun cas être transformé en promotion (conforme à la contrainte d'audit).

### 4.3 Rejets, shock guard, depth, `market_already_open`
- `mainnet_paper:market_already_open` = 29 095 rejets pour 27 approbations. Inférence : le pipeline ré-émet la même opportunité à chaque boucle de 4 s tant que la position est ouverte ; c'est surtout du **bruit de log** (et un coût d'analyse), pas une perte d'opportunité — le reentry lock est la protection voulue. Vérifier qu'aucun de ces rejets ne masque une opportunité opposite-side légitime exige les décisions brutes (`needs_raw_decisions`).
- `shock_guard_adverse_momentum` : 4 661 paper / 16 089 observer. Le shock guard est très actif ; son apport net (pertes évitées vs gains bloqués) n'est pas mesurable depuis le pack (`needs_raw_decisions`).
- Depth rejects (`insufficient_yes/no_depth`) concentrés côté observer : la liquidité outcome reste l'une des contraintes structurelles ; cohérent avec `min_depth=12` et `max_position_usdc=12`.

### 4.4 Calibration et BTC vs non-BTC
- Calibration (run review, n=17, MODEL) : avg pred 0.457, win rate 0.588, **Brier 0.2611** (> seuil 0.23 ; pour mémoire un prédicteur constant 0.5 fait 0.25). Le gap pred/réalisé (+0.13) suggère un modèle sous-confiant sur cette tranche — mais n=17 ne permet rien de robuste, et le PnL négatif malgré WR>achat moyen montre que les pertes (positions tenues jusqu'à settlement défavorable) dominent les gains coupés.
- Non-BTC : ETH/HYPE/SOL sont désormais des candidats priceBinary tradables (confirmé paper + observer), avec 1 approbation paper chacun le 06-11. L'ancien diagnostic « BTC-only » est obsolète. C'est un axe de **couverture/collecte**, pas un edge prouvé (1 settlement non-BTC, perdant).
- Nautilus shadow : qualité moyenne 0.84, 1 trade « would-block » sur 8 joints — trop peu pour en faire une règle ; garder en watch comme le suggère le rapport (candidat `edge_decay_or_stale_book_context`).

### 4.5 Promotion justifiée ?
**Non, clairement.** Tous les seuils internes échouent (PF 0.71 < 1.15 ; Brier 0.2611 > 0.23 ; settlements post-changement de policy < 20 ; cutoffs récents négatifs). La seule chose « promue » de fait — la bascule `prob_stop_full` + reentry lock + sizing 12 USDC — est déjà active et constitue le bon réglage défensif. Décision recommandée : **continuer la collecte mainnet paper** avec la config courante, regénérer la run review après chaque fetch, et fixer un jalon explicite : ≥ 20 settlements **entrés sous `prob_stop_full`**, PF ≥ 1.15 et Brier ≤ 0.23 sur cette tranche, sinon réviser le modèle de probabilité (vol estimée, drift) plutôt que les exits.

---

## 5. Audit baseline / replays

### 5.1 Référence officielle
La baseline full-bot officielle (à utiliser pour toute comparaison) : config `config/trident.toml`, fenêtre `2026-04-05T19:45Z → 2026-05-13T07:56Z` (trous de collecte connus : 04-19, 04-28/29, 05-09→11), 40 632 records, 196 trades fermés :

| Total | Pod A | Pod B | Pod C |
| ---: | ---: | ---: | ---: |
| **+859.83 USD** | +780.72 (155 trades, WR 64.5 %) | 0.00 | +79.11 (41 trades, WR 58.5 %) |

C'est la variante promue `evo11_a_grade_boost_wider_exits` (delta +190.14 vs baseline pré-promotion +669.69). Fees directionnelles 133.18 USD.

### 5.2 Nuance avec le replay du 2026-05-19 (+872.74)
Le rejeu du **même input** avec le repo/config courants du 19-05 donne +872.74 (+12.91). L'écart provient **uniquement** de 6 trades HYPE `trend_pullback_long` Pod A réintroduits par le rollback du veto HYPE ; Pod C strictement inchangé (+79.11). Règle d'audit appliquée ici : **+859.83 reste la référence officielle** ; +872.74 ne sert que si la question porte explicitement sur l'état post-rollback HYPE. Toute nouvelle proposition doit annoncer laquelle des deux elle bat.

### 5.3 Replays disponibles et lecture
- `no_pod_c` : Pod A strictement identique (155 trades, +780.72) → pas de conflit full-bot Pod C → Pod A ; couper Pod C retirerait simplement +79.11.
- Multipliers Pod C (26-05) : `gold_070` est le levier le plus propre (+86.07 vs +79.11, même activité 41 trades) ; `global_070` monte à +105.56 mais avec 68 trades et +61 % de fees ; `silver_070`/`metals_070` rejetés (dégradent le global). Fenêtre récente (24→26-05, 4-6 trades) : tout est négatif, échantillon insignifiant.

### 5.4 Constat central
Le contraste **replay +859.83 (avril–mai) vs live cumulé ≈ -148 (fin mai–juin)** est la question prioritaire. Trois hypothèses non départagées par le pack : (H1) régime de marché différent (avril–mai trendait, juin est DeadZone/Range — cohérent avec 18/25 fermetures en DeadZone) ; (H2) coûts d'exécution réels > hypothèses de replay (slippage 11.7 bps observé vs 8 supposés, fees non capturées) ; (H3) dérive de config live (cap, blocklist, sizing) non répliquée dans la baseline. **Replays à relancer en priorité (§9)** : rejouer la config courante sur un input couvrant 2026-05-24 → 2026-06-11 et comparer trade par trade au live.

**Mise à jour P1-01 du 2026-06-12** : le replay full-bot récent est exécuté sur 53 557 snapshots (`2026-05-24 → 2026-06-11`) avec cap live A/C appliqué. Résultat : replay config courante `-20.27 USD` sur 66 trades (`pod_a=-14.11`, `pod_c=-6.16`) vs live exchange P0-03 `-157.88 USD` sur 142 trades. Overlay coûts : `-29.69 USD` avec slippage observé par symbole, `-46.67 USD` avec coût live config 8/12 bps. Conclusion : les coûts seuls n'expliquent pas le live ; la divergence résiduelle vient surtout des ères de config/historique live (142 trades live vs 66 replay) et de la parité Pod C `external_reference_*` absente. Artefacts : `server-data/replay_reports/p101_recent_full_bot_livecap_20260612T170415Z/`.

---

## 6. Audit architecture

### 6.1 Cartographie synthétique
Deux applications déployables strictement séparées (confirmé par compose + runtime) :

```
TRIDENT A/C (/opt/trident, port 3000, live mainnet, cap 200 USD)
  HL WS/REST → HyperliquidLiveCollector (sharding 10 coins/WS, backoff, rate limiter partagé)
    → SnapshotBuilder (bucket 60 s) → SymbolMarketSnapshot/RegimeSnapshot enrichis (funding/OI/clusters)
    → Supervisor (régime legacy actif, crypto_v2 off ; CapitalAllocator ; SymbolRouter hysteresis/cooldown)
    → Pod A (trend_pullback_long uniquement, long-only, A-grade/watchers/vetoes/loss-tax/correlation slots)
    → Pod C (tradfi continuation long, cluster-aware v2 : oil/gold/index actifs, silver bloqué)
    → risk gates → DirectionalExecutor → LiveExecutionVenue (IOC, protective orders, pending_position durable)
    → LiveStateStore / reconciliation / API-UI

TRIDENT-HIP4 (/opt/trident-hip4, port 3001, mainnet PAPER + observer)
  outcomeMeta/mids/l2Book → références externes multi-CEX → edge detector
  (MODEL dominant ; late_expiry/parity/price_bucket/named_basket ; short_expiry observe-only)
  → shock guard multi-fenêtres → risk manager → capital guard (budget 500, pos 12)
  → paper executor → state JSON → status/reports/API
```

- **Séparation A/C vs HIP4** : effective. Compose A/C force `TRIDENT_ENABLE_POD_B="false"` et `TRIDENT_ENABLE_HIP4_OUTCOME="false"` en dur ✓. Pod B legacy disabled ✓. L'alias `pod_b_live_status.json` pour HIP4 reste une source de confusion documentée mais maîtrisée.
- **Patterns de trading** : Pod A = pullback de continuation long crypto, multi-timeframe, gouverné en live par A-grade/quality sizing/loss tax/stop grace ; Pod C = continuation TradFi long cluster-aware ; HIP4 = arbitrage probabiliste sur tokens binaires (modèle lognormal vs prix YES/NO, net d'environ 3.7 % de coûts+marge).
- **Points forts** : pods producteurs de plans sans accès aux ordres ; venue unique pour l'exécution ; reconciliation qui refuse l'état ready sur positions inconnues ; `pending_position` durable écrit immédiatement après fill (correctif incident ARB 06-07) ; rounding `6 - szDecimals` corrigé ; séparation opening vs managed symbols qui évite les `routing_revoked` brutaux ; baselines versionnées avec statut explicite.
- **Points faibles** : API stdlib monolithique (~8 000+ lignes dans `api.py`, auth ajoutée après audit via R-02) ; dérive doc (README vs config réelle, ex. blocked_symbols Pod C) ; `closed_trade_log` non persistant au-delà du buffer runtime (F-03) ; le régime v2 existe mais inactif — la détection legacy (ADX 22 / structure 0.30) est l'unique gate de régime alors que la fenêtre récente montre des entrées TrendExpansion qui meurent en DeadZone.

---

## 7. Audit sécurité et exploitation

| # | Domaine | Constat | Sévérité |
| --- | --- | --- | --- |
| S-01 | Secrets | **Clé privée committée dans le repo public** (`.env.trident`, git-tracked) + `.gitignore` n'ignorant pas `.env.trident` (cf. F-01). Aucune trace de secret dans les logs/exports du pack (`contains_secrets=false`, vérifié par grep) ; le code ne logge pas les clés (lecture env uniquement, `private_state.py`). | **Critique** |
| S-02 | API/dashboard | Constat initial : pas d'authentification, endpoint mutant `POST /api/routing/override`, publication `3000:3000`/`3001` toutes interfaces (cf. F-02). Statut 2026-06-12 : corrigé par R-02 (`OK_DEPLOY_AUTH_3000_3001`) ; risque résiduel HTTP public sans TLS accepté. | **Critique clôturé / résiduel HTTP** |
| S-03 | Chemins vers ordres réels | Bien gardés : `live` exige `TRIDENT_LIVE_CONFIRM=I_UNDERSTAND_REAL_ORDERS` + clé 0x valide (`private_state.validate`) ; cap `live_max_order_notional_usd=200` appliqué dans la venue (`notional_above_live_cap`) ; protective orders requis avec emergency close si SL impossible ; HIP4 paper : `allow_testnet_orders=false`, executor testnet exige URL/secret/flag explicites ; observer signal-only confirmé runtime. Réserve : le `.env.trident` committé active l'envoi d'ordres testnet par défaut sur tout clone naïf (cf. F-01). | OK avec réserve |
| S-04 | Reconciliation / unknown positions | Conception saine : refuse ready si positions exchange inconnues, side mismatches, ordres inconnus ; récupération via metadata/`pending_position` ; état actuel ready=true des deux pods. L'override `TRIDENT_LIVE_ALLOW_UNKNOWN_POSITIONS` existe (vide actuellement) — à n'utiliser que sous intervention auditée, comme documenté. | OK |
| S-05 | State persistence / crash-restart | State stores JSON + `restart: unless-stopped` sur tous les services ; `pending_position` durable post-fill ; events/orders persistés. Non testé dans le pack : un kill -9 entre fill et écriture protective order (fenêtre courte mais réelle) — couvert en partie par la reconciliation au redémarrage. | OK |
| S-06 | Scripts fetch/deploy | `deploy.sh` exclut `.env.*` du rsync ✓ et vérifie la présence serveur du fichier ✓. Bug : `fetch_all_data.sh` affiche `[ERROR] Fetch TRIDENT-HIP4 en erreur (code 0)` alors que tout a réussi — gestion de code retour à corriger (risque d'alarmes ignorées à force). La run review HIP4 n'est pas régénérée par le fetch (06-05 vs fetch 06-11). | Moyenne |
| S-07 | Repo public | Au-delà de la clé : le repo expose l'intégralité des paramètres de stratégie (alpha leak : seuils, vetoes, sizing, caps), le naming d'infra (`trident-hetzner`, `/opt/trident`, user deploy, chemin de clé SSH) et les adresses de compte. Pour un bot mainnet avec capital réel, recommandation forte : **passer le repo en privé**. | Haute |
| S-08 | Supply chain | Empreinte minimale : `hyperliquid-python-sdk>=0.23.0` + `websockets>=15.0`, `uv.lock` committé ✓, HTTP stdlib (pas de framework web). Pas de CI visible ni d'épinglage par hash ; ajouter un audit dépendances (pip-audit/osv) dans le flux serait peu coûteux. | Faible |
| S-09 | Intégrité données d'audit | Export horodaté avec `manifest.json`, `fresh_fetch_run=true`, lignées de fichiers sources. Manque : checksums des fichiers exportés et signature ; les CSV sont modifiables sans détection. | Faible |
| S-10 | Divers | `reference_equity_usd=0.0` dans `/api/report` alors que la config dit 1000 (bug reporting → fausse les métriques en % d'equity) ; capital réel : perp equity ~10.8 USD par pod, 814 USDC spot unified — l'exposition réelle est bien tiny-size, cohérente avec le burn-in. | Faible |

---

## 8. Recommandations priorisées

### R-01 — Rotation de la clé fuitée + purge du repo public — **P0, `ready`**
- **Périmètre** : sécurité, repo GitHub + wallet HIP4.
- **Preuve** : `.env.trident` git-tracked dans le clone public, contient `HIP4_OUTCOME_SECRET_KEY` (F-01).
- **Impact PnL** : protège le capital (risque de vol total des fonds accessibles à la clé, et signal de compromission du processus secrets).
- **Risque introduit** : néant.
- **Données manquantes** : historique git complet (scan gitleaks) pour vérifier d'autres fuites passées.
- **Test requis** : après rotation, redémarrage HIP4 testnet OK ; scan gitleaks vert.
- **Rollback** : n/a.

### R-02 — Verrouiller l'API (auth UI/API + endpoint mutant désactivé) — **P0, `done`**
- **Périmètre** : `docker-compose.trident.yml` / `docker-compose.hip4.yml` / `app/observability/api.py`.
- **Preuve** : F-02 (POST routing override non authentifié, publication toutes interfaces, bot live mainnet).
- **Impact PnL** : élimine un vecteur de fermetures forcées / manipulation de routing par un tiers.
- **Statut** : clôturé le 2026-06-12. Choix opérateur retenu : ports publics `3000` (TRIDENT A/C) et `3001` (TRIDENT-HIP4) avec Basic Auth obligatoire sur UI/API sauf `/health`, même login/password sur les deux apps, et `POST /api/routing/override` désactivé par défaut.
- **Preuves conservées** : services redéployés et healthy ; `/health` retourne `200`; `/api/state` sans auth retourne `401`; `/api/state` avec auth retourne `200`; `POST /api/routing/override` avec auth retourne `403 routing_override_disabled`; scripts fetch/review adaptés pour sourcer les identifiants depuis `.env.trident` / `.env.trident-hip4`.
- **Risque résiduel** : accès en HTTP par IP publique, donc mot de passe non chiffré sur le réseau ; utiliser un mot de passe long, unique, et préférer ultérieurement un reverse proxy HTTPS ou VPN si l'ergonomie le permet.
- **Rollback** : remettre `TRIDENT_API_BIND=127.0.0.1` / `HIP4_OUTCOME_API_BIND=127.0.0.1` et recréer les conteneurs API si l'exposition publique doit être coupée.

### R-03 — Persistance des close fills exchange + historique complet des trades fermés — **P0, `done`**
- **Périmètre** : `app/execution/live.py`, `app/live/trade_audit.py`, `scripts/export_trident_audit_pack.py`, `scripts/backfill_trident_exchange_audit.py`, `app/live/user_stream.py`.
- **Preuve initiale** : F-03 (`close_fills=0`, -134.27 cumulé inattribuable, `closed_trade_log` = buffer) + F-07 (`user_order_updates` reconnect quasi permanent).
- **Statut** : clôturé le 2026-06-12 (`OK_P003_EXCHANGE_BACKFILL_AND_WS_DEPLOYED`).
- **Preuves conservées** : backfill `server-data/audit_backfills/20260612T161017Z_exchange_backfill/summary.json` (`155/155` trades matchés, `419` user fills, `223` funding payments, `exchange_close_fill_count_by_pod={"pod_a":152,"pod_c":28}`) ; validation fill-by-fill `fill_by_fill_validation.md` ; export compact final `server-data/audit_exports/20260612T163311Z_p003_final/` ; review serveur post-déploiement `server-data/reviews/20260612T163252Z/review_summary.md`.
- **Websocket** : `user_order_updates` ne reconnecte plus à chaque timeout/message. Preuve post-déploiement : Pod A `connected=true`, `timeout_count=5`, `pong_count=3`, `reconnect_count=1`; Pod C `connected=true`, `timeout_count=4`, `pong_count=3`, `reconnect_count=1`. À surveiller, mais la boucle F-07 est cassée.
- **Impact PnL** : le PnL net peut maintenant être audité fill-by-fill ; les décisions de sizing/replay peuvent utiliser fees/funding/closedPnl exchange au lieu d'une approximation minute.
- **Risque introduit** : néant sur les trades ; backfill et export sont read-only, la correction websocket ne modifie pas la logique d'entrée/sortie.
- **Rollback** : n/a pour les artefacts ; pour le websocket, revenir à la version précédente de `app/live/user_stream.py` si le flux Hyperliquid se comporte mal, en gardant la reconciliation REST comme garde-fou.

### R-04 — Replay full-bot de la fenêtre live récente (24-05 → 11-06) avec la config courante — **P1, exécuté P1-01**
- **Périmètre** : Pod A + Pod C, baseline comparée : live réel de la même fenêtre (et non +859.83, qui couvre une autre période).
- **Preuve** : P1-01 exécuté le 2026-06-12 : `server-data/replay_reports/p101_recent_full_bot_livecap_20260612T170415Z/p101_recent_replay_report.md`.
- **Résultat** : config courante + cap live A/C = `-20.27 USD` (`66` trades) vs live exchange `-157.88 USD` (`142` trades). Le replay standard sans cap live existe seulement comme contrôle (`server-data/replay_reports/p101_recent_full_bot_20260612T170300Z/`) et ne doit pas servir de comparaison principale.
- **Lecture PnL** : H2 coûts ne suffit pas (`-29.69` avec slippage observé, `-46.67` avec 8/12 bps) ; H3 ères de config/historique live reste dominante ; H4 Pod C `external_reference_*` reste une réserve de parité.
- **Suivi** : P1-02 pour rejouer les exits/stop grace/EFE, P1-03 pour rétablir la référence externe Pod C, P1-04 pour transformer l'overlay coût en règle d'exécution testée.
- **Rollback** : n/a (diagnostic).

### R-05 — Geler le boost de taille « strong A-grade » à 1.0 en live, conserver le label — **P1, `needs_replay`** (ne pas appliquer avant le replay)
- **Périmètre** : Pod A live sizing (`a_grade` size scale 1.25/1.40).
- **Preuve** : F-05 (strong A-grade 10 trades -9.43, WR 10 % ; confidence ≥0.70 : 0 % WR) — contre-performance là où la taille est maximale, sur la fenêtre attribuable.
- **Impact PnL attendu** : sur les 25 trades observés, ramener le scale strong à 1.0 aurait réduit la perte d'environ 2–3 USD (ordre de grandeur, attribution applicative) ; surtout, réduit la variance pendant le burn-in.
- **Risque introduit** : si le régime redevient TrendExpansion durable, on renonce au +190.14 que le boost a produit en replay avril–mai — d'où l'exigence de replay.
- **Données manquantes** : ≥ 50 trades live A-grade ; MFE/MAE.
- **Test requis** : replay full-bot fenêtre récente avec scale {1.0, 1.25, 1.40} comparé à la baseline officielle **et** à la fenêtre récente ; critère : le boost doit être ≥ neutre sur les deux.
- **Rollback** : restaurer 1.25/1.40 si le replay récent contredit le live (échantillon trop faible).

### R-06 — Réduire le coût d'entrée Pod A (slippage 11.7 bps observé vs 8 supposés) — **P1, `needs_replay`**
- **Périmètre** : `LiveExecutionVenue` entrées IOC ; option : prix limite à mid+X bps borné, ou skip si spread > seuil au moment du fill, ou exclusion des alts à slippage récurrent > 20 bps (PENGU/BIO/STRK…).
- **Preuve** : `fill_events` — moyenne 11.7 bps, p75 15.2, max 60.5 sur 118 ouvertures Pod A ; stops 45–130 bps → 10–25 % du risque consommé à l'entrée.
- **Impact PnL attendu** : ~3–4 bps de coût moyen économisés ≈ 0.4–0.8 USD par tranche de 20 trades au sizing actuel ; surtout structurel à plus gros sizing.
- **Risque introduit** : fills manqués (IOC limite plus stricte) → moins de trades ; à mesurer.
- **Test requis** : replay avec modèle de coût recalibré ; A/B dry-run sur le taux de fill.
- **Rollback** : seuils de slippage relâchés si fill rate < 80 % des signaux acceptés.

### R-07 — `early_failure_exit` : replay avec/sans avant tout réglage — **P1, exécuté P1-02**
- **Périmètre** : Pod A live (EFE pendant la stop grace).
- **Preuve P1-02 2026-06-12** : matrices récentes et baseline dans `server-data/replay_reports/p102_exit_sensitivity_recent_20260612T172220Z/` et `server-data/replay_reports/p102_exit_sensitivity_baseline_20260612T172334Z/`.
- **Résultat récent** : variante proche courante `grace60_cat160_efe_on` = `-12.51 USD`, soit `+1.60` vs replay original Pod A `-14.11`; EFE off à paramètres identiques tombe à `-32.88`.
- **Résultat baseline officielle** : le même `grace60_cat160_efe_on` tombe à `463.14 USD`, soit `-317.58` vs baseline Pod A `780.72`; EFE off préserve mieux la baseline (`553.62`) mais dégrade juin (`-32.88`).
- **Décision** : aucune modification live mécanique. EFE aide dans le régime récent adverse mais détruit trop d'upside sur la baseline trend. Prochaine étape : conditionner EFE/stop grace par régime/qualité d'entrée plutôt que changer les paramètres globaux.
- **Rollback** : n/a, aucun changement live appliqué.

### R-08 — HIP4 : pas de promotion ; jalon de collecte formalisé — **P1, `needs_data`**
- **Périmètre** : HIP4 mainnet paper.
- **Preuve** : §4 (PF 0.71, Brier 0.2611, cutoffs récents tous négatifs).
- **Impact PnL** : évite de déployer un edge actuellement négatif en réel.
- **Critère** : ≥ 20 settlements **entrés sous `prob_stop_full` + reentry lock + sizing 12**, PF ≥ 1.15, Brier ≤ 0.23 sur cette tranche ; sinon revoir le modèle de probabilité (vol/drift) — pas les exits.
- **Test requis** : run review régénérée à chaque fetch (corriger le pipeline, cf. S-06) ; replay cutoff au timestamp exact du changement de policy.
- **Rollback** : n/a (statu quo).

### R-09 — Pod C `gold_070` : candidat propre, à re-tester sur fenêtre étendue — **P2, `research_only`**
- **Préuve** : replay global +86.07 vs +79.11 à activité constante (41 trades) ; fenêtre récente sans changement.
- **Test requis** : nouveau replay full-bot incluant juin avant toute promotion ; comparer à +859.83.
- **Risque** : sur-apprentissage sur une fenêtre où gold trendait.

### R-10 — Hygiène exploitation/data — **P2, `ready`**
Corriger en lot : code retour `fetch_all_data.sh` (faux `[ERROR] code 0`) ; régénération automatique de la run review HIP4 post-fetch ; `reference_equity_usd=0.0` dans `/api/report` ; capture des `fee_usd` dans `fill_events` ; jointure `external_reference_*` Pod C dans l'export ; README repo aligné sur la config réelle (blocked_symbols Pod C) ; changelog horodaté des changements de config live (cap, blocklist, sizing) versionné ; checksums dans `manifest.json`.

---

## 9. Replays / tests à lancer (ordre de priorité)

1. **Replay full-bot fenêtre live 2026-05-24 → 2026-06-11, config courante** (R-04) — prérequis : assembler l'input JSONL de la fenêtre. Comparer trade-par-trade au live ; tester sensibilité slippage 8/12/observé.
2. **Replay A-grade size scale {1.0, 1.25, 1.40}** sur (a) la baseline officielle (+859.83 attendu pour 1.25/1.40) et (b) la fenêtre récente (R-05).
3. **Replay EFE on/off** sur les deux mêmes fenêtres (R-07).
4. **Replay cutoff HIP4 au timestamp exact d'activation de `prob_stop_full`** (≈ 2026-06-10 d'après l'annexe 03) + run review régénérée (R-08).
5. **Reconciliation exchange fill-by-fill** — **fait le 2026-06-12** via `server-data/audit_backfills/20260612T161017Z_exchange_backfill/` et `fill_by_fill_validation.md`. À relancer seulement après nouveaux incidents de fills ou changement de format exchange.
6. **Re-run multipliers Pod C** (dont `gold_070`) sur une fenêtre étendue incluant juin (R-09).
7. Tests d'exploitation : après R-02, vérifier `3000`/`3001` en `401` sans auth, `200` avec auth, `/health` en `200`, et `POST /api/routing/override` en `403 routing_override_disabled`; gitleaks sur l'historique complet après R-01 ; test crash/restart (kill du runner entre fill et protective order, vérifier la récupération via `pending_position`).

---

## 10. Données manquantes

| Donnée | Fichier attendu | Pourquoi bloquant | Sévérité | Comment la produire |
| --- | --- | --- | --- | --- |
| Fills exchange de fermeture A/C | `trident_ac_exchange_fills.csv` | **Résolu P0-03** : `server-data/audit_backfills/20260612T161017Z_exchange_backfill/trident_ac_exchange_fills.csv` contient `419` user fills, dont close fills matchés `pod_a=152`, `pod_c=28`. | clos | Regénérer avec `scripts/backfill_trident_exchange_audit.py` si besoin. |
| Historique complet des trades fermés depuis le début du live | `trident_ac_closed_trades_full.csv` | **Résolu P0-03** : `155/155` trades fermés A/C backfillés et matchés dans `server-data/audit_backfills/20260612T161017Z_exchange_backfill/trident_ac_closed_trades_full.csv`. | clos | Regénérer après fetch A/C si de nouveaux trades fermés doivent être audités. |
| Changelog horodaté des configs live | `config_changelog.md` | Le cap (933→500→250→200) et la blocklist ont changé pendant la fenêtre ; impossible de segmenter le PnL par époque. | P1 | Versionner chaque changement avec timestamp (git tag ou journal dédié). |
| MFE/MAE par trade | colonne dans closed trades | Indispensable pour juger EFE, time stops, trailing. | P1 | Tracker high/low depuis l'entrée dans le portfolio state. |
| Funding réel + fees réels par trade | colonnes closed trades / fill events | **Résolu P0-03 pour A/C** : `223` paiements funding importés ; fees/closedPnl exchange exportés par trade. | clos | Surveiller que les prochains `trade_close` gardent `exchange_fee_usd`, `exchange_closed_pnl_usd` et funding attribué. |
| Références externes Pod C à l'entrée | `external_reference_*` peuplés | Tous à False/0 dans l'export : qualité du signal TradFi invérifiable. | P1 | Corriger la jointure dans `export_trident_audit_pack.py` ou vérifier la collecte. |
| Décisions brutes A/C et HIP4 | `*_signal_decisions.jsonl`, `hip4_decisions.jsonl` | Analyse fine des rejets (shock guard net effect, market_already_open opposite-side, pattern EFE-watchers) — exclus du pack léger. | P1 | Fournir dans un pack complet ; conclusions concernées marquées `needs_raw_decisions`. |
| Input replay fenêtre récente | snapshots live + rapports P1 | **Couvert P1-01/P1-02** : `server-data/live_snapshots/` consommé par les replays récents ; reste seulement la réserve `external_reference_*` Pod C. | clos | Regénérer via `scripts/run_p101_recent_replay.py` puis `scripts/run_p102_exit_sensitivity.py` après nouveaux fetchs. |
| Run review HIP4 fraîche | régénérée post-fetch | La review structurée date du 06-05 alors que les données vont au 06-11. | P2 | Hook de régénération dans `fetch_data.sh`. |
| Checksums export | dans `manifest.json` | Intégrité du pack non vérifiable. | P2 | sha256 par fichier dans l'exporteur. |

---

## 11. Décisions interdites sans confirmation humaine explicite

1. **Toute activation live/mainnet supplémentaire** : passage de HIP4 paper → testnet ou mainnet réel ; réactivation Pod B legacy ; activation de setups Pod A désactivés ; activation shorts.
2. **Toute augmentation de capital ou de cap** : `live_max_order_notional_usd` > 200, `max_position_usdc` HIP4 > 12, levée des multiplicateurs quality sizing/loss tax — aucune preuve replay/burn-in suffisante n'existe aujourd'hui (PnL live négatif, divergence live/replay inexpliquée).
3. **Promotion d'une policy HIP4 sur la base du delta shadow +237 USDC** — explicitement contre-indiquée par les cutoffs récents.
4. **Usage de `TRIDENT_LIVE_ALLOW_UNKNOWN_POSITIONS=true`** ou de tout override de reconciliation — uniquement sous intervention auditée.
5. **Déblocage de `XYZ:SILVER`** ou de symboles de la blocklist Pod A.
6. **Promotion de `gold_070` ou `global_070`** sans nouveau replay full-bot incluant juin.
7. **Modification des protective orders / stop grace / catastrophic stop** sans tests de state, reconciliation et dry-run préalables.
8. Les actions R-01 et R-02 (rotation de clé, verrouillage API) étaient les seules recommandées **immédiatement** ; elles sont clôturées au 2026-06-12 et elles n'envoient aucun ordre.

---

*Limites de cet audit : le rapport initial était en attribution PnL applicative first-pass ; la remédiation P0-03 du 2026-06-12 fournit désormais les fills/fees/funding exchange A/C, mais les décisions brutes et replays restent nécessaires pour trancher l'edge. Échantillons live faibles (25 trades Pod A, 6 Pod C, 25 settlements HIP4) — aucun chiffre de cette fenêtre ne doit être extrapolé sans replay. Baseline de comparaison utilisée partout : +859.83 USD (officielle 2026-05-13), le replay +872.74 du 2026-05-19 n'étant cité que pour la nuance HYPE.*

---
---

# ADDENDUM — 2026-06-11 (pack `trident_missing_pnl_data_20260611.zip`)

Cet addendum intègre les données complémentaires : décisions brutes A/C (193 697 lignes, 24-05 → 11-06), décisions HIP4 (93 610 lignes), live states A/C, statuts runtime, snapshots minute 24-05 → 11-06 (~2 Go), et `docs/trident_active_plan.md` (chronologie opérateur). Les sections ci-dessous **révisent** le rapport principal au 2026-06-11 ; la mise à jour P0-03 du 2026-06-12 prévaut pour les close fills/fees/funding.

## A. Le PnL cumulé -134.27 / -14.21 est maintenant expliqué à ~85 % — F-03 largement résolu

**Fait établi n°1 : le live mainnet A/C démarre le 2026-05-24.** Les décisions brutes commencent au `2026-05-24T16:05Z` (ligne 1 du journal pod A), les fill events comptent 118 ouvertures pod A + 23 pod C, et la review serveur affiche `total_fill_count` 115/23. Le cumul -134.27 / -14.21 USD couvre donc **exactement la fenêtre 24-05 → 11-06** dont nous avons désormais les entrées, les décisions et les prix minute. Il n'y a pas d'historique antérieur caché.

**Méthode de reconstruction au 2026-06-11.** Les close fills exchange étaient encore absents dans ce pack, donc les sorties des 110 trades manquants ont été reconstruites en rejouant chaque position (long-only) contre les snapshots minute (prix + best bid), avec les règles de sortie **de l'époque de chaque trade** (voir ères ci-dessous), sortie au bid en cas de gap sous le stop. Calibration sur les 31 trades dont la vérité terrain existait : pod A réel -5.90 vs simulé -9.88 ; les raisons de sortie simulées correspondaient trade par trade (trailing/BE/stop). Cette reconstruction reste utile pour lire les ères de config, mais elle est remplacée pour le PnL net par le backfill exchange du 2026-06-12.

**Fait établi n°2 : la chronologie de configuration existe** (`trident_active_plan.md` — le « config changelog » réclamé en R-10 existait, il n'était pas dans le pack léger). Elle définit trois ères de gestion du stop, que la reconstruction confirme quantitativement :

| Ère | Période | Règles stop | n | PnL estimé | Lecture |
|---|---|---|---|---|---|
| 1 | 24-05 → 27-05 17:01Z | SL exchange **immédiat** (bug : pas de grace, vs 165 min en backtest) | 12 | **-19.6** | 9 trades du 27-05 tous stoppés → arrêt manuel des runners, incident documenté |
| 2 | 29-05 → 08-06 | grace **165 min** + SL catastrophe 300 bps pendant la grace | 80 | **-79.1** | le cœur de la perte |
| 3 | 09-06 → 11-06 | grace 60/120 min, cat stop dynamique plafonné, `early_failure_exit`, sizing qualité | 26 | **-5.9** (réel) | perte moyenne/trade divisée par ~4 |

**Fait établi n°3 : le mécanisme dominant de la perte est la queue des stops, pas le taux de réussite.** Sur les 93 trades reconstruits pod A : 37 trailing stops = **+120.0**, mais 15 stops catastrophe = **-119.9** (perte moyenne -8.0, soit ~2.4× la perte planifiée) et 32 stops = -84.3. L'excès cumulé « sortie sous le stop planifié » est estimé à **≈ -103 USD sur 39 trades stoppés** — c'est la quantification exacte du constat opérateur du 04-06 (« les stops réels peuvent sortir bien au-delà du stop planifié »). La courbe journalière reconstruite reproduit le récit opérateur de façon indépendante :

| Période | PnL estimé | Contexte (plan actif) |
|---|---|---|
| 29-05 → 02-06 | **+41.1** | recovery, cap 250 — « Pod A repasse positif » (02-06), cumul reconstruit +21.5 au 02-06 ✓ |
| 03-06 → 04-06 | **-37.8** | cap monté à 500 le 02-06 ; pires trades : TON -16.2 (plan -4.2), ZEC -13.8, ONDO -13.7 |
| 05-06 | **-44.3** | selloff BTC ~76k → ~60k ; cap redescendu à 200, 11 alts bloqués |
| 06-06 → 08-06 | **-38.1** | saignée résiduelle DeadZone/Range sous cap 200 |

Par symbole, les pertes estimées se concentrent sur TON (-24.5, 2 trades), ZEC (-21.7, 10 trades) et les alts bloqués le 05-06 (AVAX, AAVE, ADA, ONDO, XRP, VVV, PENDLE… ≈ -45 cumulés) : le blocklist du 05-06 cible bien les bons coupables, mais **après** la perte.

**Pod C : la perte vient quasi intégralement du silver.** 10 trades XYZ:SILVER (28-05 → 04-06, notional 249 puis **499**), 0 gagnant, ≈ **-24 estimés** sur un cumul réel de -14.21 (la simulation surestime un peu, paramètres silver_mode incertains) ; le reste du book pod C est ~flat à légèrement positif. Le blocage `XYZ:SILVER` du 04-06 est rétrospectivement la meilleure décision Pod C de la fenêtre. Ne pas réautoriser (action interdite n°5 inchangée).

**Conséquences sur les findings :**
- **F-03 au 2026-06-11** : l'attribution était faite à ~85 % en estimation. **Mise à jour 2026-06-12** : R-03 est clôturé par le backfill Hyperliquid (`155/155` trades matchés) ; l'estimation minute reste un contexte d'analyse des ères, pas la source de PnL net.
- **F-05 (boost strong A-grade) : statut inchangé, vérification élargie impossible** — l'export de décisions ne contient pas les champs `a_grade_*` (nouveau gap, voir F-08). Le paradoxe A-grade reste établi uniquement sur les 25 trades de l'ère 3. En revanche, l'analyse par ère **renforce R-05/R-07 indirectement** : les correctifs du 09-06 (grace courte, cat stop plafonné, EFE) ont déjà réduit la perte moyenne par trade de ~-1.0 à -0.24 USD ; le problème résiduel de l'ère 3 est le WR (24 %) et la qualité d'entrée, plus la queue des stops.
- **R-06 (slippage)** : slippage d'entrée moyen par ère : 4.2 bps (ère 1) → **13.4 bps (ère 2)** → 9.8 bps (ère 3). La dégradation coïncide avec les caps plus hauts et le selloff ; l'hypothèse 8 bps du replay reste trop optimiste.

## B. Nouveaux findings

### F-06 — La référence externe Pod C est morte en live (sévérité : **HAUTE**)
`external_reference_available = False` sur **100 % des 79 744 enregistrements pod C** de la fenêtre (pas seulement les 6 trades fermés : ce n'est pas un bug d'export, le flux n'alimente pas le runtime). Or les baselines officielles (+79.11 pod C) ont été rejouées sur un input `external_reference_multisource_*` qui contient ces références. Double conséquence : (1) **parité live/replay rompue** — cause H4 à ajouter au triage de la divergence live/baseline, au même rang que H1/H2/H3 ; (2) **trou de sécurité fonctionnel** — Pod C trade des perps TradFi builder-dex sans garde-fou de prix externe (dislocation/staleness indétectable). À corriger avant tout élargissement Pod C ; les replays R-04/R-09 doivent être exécutés dans les deux modes (avec et sans référence) pour mesurer l'impact.

### F-07 — Le websocket `user_order_updates` se reconnecte à quasi chaque message (sévérité : MOYENNE)
Pod A : 2 816 reconnexions pour 2 819 messages ; Pod C : 2 809 / 2 829. Le flux de confirmations d'ordres est donc en pratique en mode reconnexion permanente. C'est un suspect direct pour la **non-capture des frais** dans les fill events (constat du rapport principal) et un risque de fills manqués au moment précis d'un stop — cohérent avec l'incident ARB du 07-06 (position fillée non persistée avant crash). À corriger avec R-03.

**Mise à jour 2026-06-12** : corrigé et redéployé avec R-03. `user_order_updates` désactive le ping protocole websocket et envoie le ping JSON Hyperliquid sur idle, comme le collector principal. Preuve post-déploiement : Pod A `timeout_count=5`, `pong_count=3`, `reconnect_count=1`; Pod C `timeout_count=4`, `pong_count=3`, `reconnect_count=1`. La reconnexion résiduelle isolée reste à surveiller, mais la croissance quasi 1:1 est supprimée.

### F-08 — L'export de décisions n'emporte pas les champs A-grade/sizing (sévérité : BASSE, bloque l'analyse)
`setup_details` y est tronqué à un sous-ensemble de features ; `a_grade_active/level/score/size_scale` et le sizing qualité sont absents. Ajouter ces champs à `export_trident_audit_pack.py` (fusionner dans R-10) pour permettre l'analyse A-grade plein échantillon réclamée par R-05.

## C. HIP4 — décisions brutes : conclusions du rapport principal confirmées, rien d'inattendu
Les 93 610 décisions (34 030 paper / 59 580 observer) recoupent exactement les 27 trades paper : 27 approbations (15 BUY_NO / 12 BUY_YES), dont 7 le seul 24-05 (l'épisode de churn BTC_GT_76772 documenté), puis **cadence ~1/jour** après les garde-fous anti-churn. Modèle unique `lognormal_static_vol_v1` partout → confirme que R-08 vise le bon objet (le modèle de probabilité, pas les exits). Sizing 50 → 12 USDC visible au niveau décision à partir du 02-06 (gate Kelly `min_shadow_kelly_size_usdc=2`). `daily_summary` recoupe le -47.84 USDC au centime. Note de contexte : les strikes BTC passent de ~76.7k (24-05) à ~61.3k (11-06) — le selloff de ~20 % est le fond de tableau commun au bleed pod A (long-only) et à la dégradation du Brier HIP4. Verdict promotion : **inchangé (non)**.

## D. Snapshots et replay R-04 : consommables, avec une réserve
Les snapshots minute 24-05 → 11-06 sont au schéma du `SnapshotBuilder` (mêmes champs que ce que consomment les pods : prix, book, flux, régimes par cluster) et P1-01 les a consommés le 2026-06-12. Réserve maintenue : ils ne contiennent pas les champs `external_reference_*` multisource du format baseline (cf. F-06) ; un replay « format identique au baseline » n'est pas possible pour la fenêtre récente. Le replay P1-01 est donc annoté `no_external_reference` et cap live A/C appliqué.

## E. Statuts mis à jour

| Item | Avant | Après |
|---|---|---|
| F-03 attribution PnL cumulé | P0 `needs_data` | **Clôturé P0-03** — réconciliation exchange backfillée (`155/155` trades matchés), estimation minute conservée seulement comme contexte historique |
| R-03 close fills + historique complet | P0 | **`done`** — close fills/fees/funding/historique complet exportés, validation fill-by-fill conservée, websocket F-07 corrigé et redéployé |
| R-04 replay fenêtre récente | `needs_replay`, input à construire | **`done P1-01`** — replay récent exécuté avec cap live ; réserve F-06 maintenue |
| R-10 config changelog | « absent » | **existe** (`trident_active_plan.md`) ; reste à le verser au pack d'audit par défaut + ajouter champs A-grade à l'export (F-08) |
| Triage divergence live/replay | H1 régime / H2 coûts / H3 config | + **H4 : absence de référence externe en live (pod C)** ; H3 désormais documenté précisément (caps 100→250→500→250→200, grace 0→165→60/120) |
| Données encore manquantes | — | MFE/MAE, `external_reference_*` Pod C, checksums pack, run review HIP4 fraîche ; close fills/fees/funding ne sont plus bloquants |

## F. Plan de suivi priorisé — modifications et tests

Objectif : transformer les recommandations en file d'exécution traçable. Chaque étape ci-dessous est soit autosuffisante, soit rattachée explicitement aux findings/recommandations du rapport. Ne pas changer de cap live, de sizing, de stops ou activer de nouveaux ordres tant que les tests listés pour l'étape concernée ne sont pas verts.

### P0 — Sécurité et données bloquantes

- [x] **P0-01 — Secrets repo public : rotation, retrait et purge**
  **Références** : F-01, R-01, S-07.
  **Statut** : clôturé côté Git/secrets le 2026-06-12 (`OK_GIT_REMOTE_PUSHED_SCAN_VERT`).
  **Preuves conservées** : `.env.trident` / `.env.trident-hip4` ne sont plus trackés ; `.gitignore` couvre les `.env*` réels en gardant les `*.example`; historique public réécrit et force-pushé ; `gitleaks` et `trufflehog` verts ; clé compromise déclarée révoquée/rotatée par l'opérateur.
  **Suivi résiduel** : confirmer lors de la prochaine review serveur que HIP4 charge bien les secrets serveur uniquement.

- [x] **P0-02 — API : authentification UI/API et endpoint mutant verrouillé**
  **Références** : F-02, R-02, S-02.
  **Statut** : clôturé le 2026-06-12 (`OK_DEPLOY_AUTH_3000_3001`).
  **Preuves conservées** : TRIDENT A/C reste sur `3000`, TRIDENT-HIP4 reste sur `3001`; même Basic Auth sur les deux apps ; `/health` public en `200`; `/api/state` sans auth en `401`; `/api/state` avec auth en `200`; `POST /api/routing/override` avec auth en `403 routing_override_disabled`; services Docker healthy après redéploiement ; scripts fetch/review compatibles auth.
  **Risque résiduel accepté** : option opérateur = accès par IP publique en HTTP + login/password, sans domaine ni HTTPS ; mot de passe à garder long, unique, et à recréer les conteneurs API après toute modification de `.env.trident` / `.env.trident-hip4`.

- [x] **P0-03 — PnL exact : close fills, fees, funding, historique append-only**
  **Références** : F-03, F-07, R-03, §3.3, §10, addendum A/E.
  **Statut** : clôturé le 2026-06-12 (`OK_P003_EXCHANGE_BACKFILL_AND_WS_DEPLOYED`).
  **Preuves conservées** : `scripts/backfill_trident_exchange_audit.py` ; backfill `server-data/audit_backfills/20260612T161017Z_exchange_backfill/summary.json` (`155/155` trades matchés, `419` user fills, `223` funding payments, close fills exchange `pod_a=152` / `pod_c=28`) ; validation `fill_by_fill_validation.md` ; export compact `server-data/audit_exports/20260612T163311Z_p003_final/` ; review post-déploiement `server-data/reviews/20260612T163252Z/review_summary.md`.
  **Websocket** : F-07 corrigé dans `app/live/user_stream.py` et redéployé ; compteurs post-déploiement `reconnect_count=1` pour `timeout_count>=4`, plus de reconnexion quasi à chaque message/timeout.
  **Suivi résiduel** : continuer à surveiller `user_order_updates.reconnect_count` dans les prochaines reviews ; MFE/MAE et checksums restent en P2-03, pas bloquants P0-03.

### P1 — Replays et corrections PnL avant tout réglage live

- [x] **P1-01 — Replay full-bot fenêtre live récente avec config courante**
  **Références** : R-04, addendum A/D/E, hypothèses H1/H2/H3/H4, §5.4.
  **Statut** : exécuté le 2026-06-12 (`OK_P101_RECENT_REPLAY_LIVECAP`) ; étape clôturée car le livrable demandé était uniquement le replay, pas une modification prod.
  **Preuves conservées** : `scripts/run_p101_recent_replay.py`; `app.backtest.full_bot_replay --apply-live-notional-caps`; rapport principal `server-data/replay_reports/p101_recent_full_bot_livecap_20260612T170415Z/p101_recent_replay_report.md`; alignement trade-by-trade `trade_alignment.csv`; tests `tests/test_p101_recent_replay.py` et cap live dans `tests/test_full_bot_replay.py`.
  **Résultat** : live exchange P0-03 `-157.88 USD` / `142` trades ; replay config courante cap live `-20.27 USD` / `66` trades ; overlay slippage observé `-29.69`, overlay 8/12 bps `-46.67`. L'edge courant sur juin reste négatif mais bien moins que le live historique ; les coûts ne suffisent pas à expliquer la perte.
  **Suivi ouvert hors P1-01** : écart trade-count important (`142` live vs `66` replay) et match trade-by-trade faible (`16` matches) → traiter via P1-03/P1-04/P1-06 avant tout réglage live.

- [x] **P1-02 — Replay de sensibilité queue des stops et `early_failure_exit`**
  **Références** : R-07, addendum A, levier PnL 1.
  **Statut** : exécuté le 2026-06-12 (`OK_P102_EXIT_SENSITIVITY_NO_LIVE_CHANGE`) ; étape clôturée car le livrable demandé était le replay de sensibilité. Aucun changement prod n'a été appliqué et l'idée n'est pas abandonnée.
  **Preuves conservées** : `scripts/run_p102_exit_sensitivity.py`; rapport récent `server-data/replay_reports/p102_exit_sensitivity_recent_20260612T172220Z/p102_exit_sensitivity_report.md`; rapport baseline `server-data/replay_reports/p102_exit_sensitivity_baseline_20260612T172334Z/p102_exit_sensitivity_report.md`; tests `tests/test_p102_exit_sensitivity.py`.
  **Résultat** : EFE + cat stop plafonné aide la fenêtre récente (`grace60_cat160_efe_on` = `-12.51`, +`1.60` vs original ; `grace120/165_cat160_efe_on` = `-10.91`, +`3.20`) mais dégrade fortement la baseline (`grace60_cat160_efe_on` = `463.14`, -`317.58` vs `780.72`). EFE off protège mieux la baseline mais détériore juin.
  **Suivi ouvert hors P1-02** : le réglage actuel est conservé faute de variante globale robuste ; la piste restante est le gate régime/qualité en P1-06 pour activer le mode défensif seulement dans les régimes défavorables.

- [ ] **P1-03 — Pod C : rétablir la référence externe live**
  **Références** : F-06, R-09, addendum B/D, levier PnL 4.
  **Modifs à faire** : restaurer l'alimentation `external_reference_*` dans les snapshots/runtime live Pod C ; exporter ces champs dans les décisions et closed trades ; ajouter un guardrail de stale/dislocation seulement après replay dédié, car il peut modifier les entrées.
  **Tests / preuves attendues** : sur un run live/dry-run, `external_reference_available` n'est plus False sur 100 % des enregistrements ; replay Pod C avec et sans référence externe ; `XYZ:SILVER` reste bloqué ; aucun ordre nouveau n'est activé par cette correction seule.
  **Terminé quand** : Pod C retrouve la parité data live/replay ou le rapport explique précisément l'écart restant.

- [ ] **P1-04 — Exécution Pod A : slippage et coûts**
  **Références** : R-06, F-07, addendum A, levier PnL 5.
  **Modifs à faire** : ajouter dans l'audit des métriques slippage par symbole/setup/ère ; tester une entrée plus spread-aware ou un skip si spread/slippage attendu dépasse un seuil, uniquement en replay/dry-run avant live.
  **Tests / preuves attendues** : fees réels capturés dans les fill events ; replay coûts 8/12/observé ; A/B dry-run sur taux de fill manqué vs PnL simulé ; surveillance continue du websocket corrigé en P0-03.
  **Terminé quand** : le modèle de coût du replay reflète le live et toute règle de skip/limit prouve qu'elle améliore le net PnL sans tuer le fill rate.

- [ ] **P1-05 — A-grade / quality sizing : données d'abord, gel ensuite si confirmé**
  **Références** : F-05, F-08, R-05, addendum A/E, levier PnL 6.
  **Modifs à faire** : ajouter `a_grade_active`, `a_grade_level`, `a_grade_score`, `a_grade_size_scale` et les champs de quality sizing dans `export_trident_audit_pack.py`; rejouer les size scales `{1.0, 1.25, 1.40}` ; ne geler le boost strong à 1.0 en live que si le replay confirme la contre-performance.
  **Tests / preuves attendues** : prochain pack avec champs A-grade non vides ; replay baseline officielle + fenêtre récente pour chaque scale ; comparaison PnL, drawdown, PF, WR et concentration des pertes.
  **Terminé quand** : le boost est soit justifié par replay, soit gelé avec preuve et rollback documenté.

- [ ] **P1-06 — Régime haussier/baissier Pod A : gate long/short avant entrée**
  **Références** : §5.4, F-05, R-04/R-07, leviers PnL 2/3, `config/trident.toml` (`trend_pullback_long` seul autorisé, shorts désactivés).
  **Objectif** : définir une règle pré-entry qui identifie un régime haussier pour autoriser/renforcer les longs, un régime baissier pour bloquer/réduire les longs et tester les shorts en shadow, sans activer de nouveaux ordres ni modifier la config live tant que la validation n'est pas terminée.
  **Étape 1 — Constat initial réalisé** : scan opportunité pré-entry sur `BTC/ETH/SOL/HYPE` (`scripts/run_p106_bear_regime_research.py`) ; score bear basé uniquement sur informations disponibles au timestamp candidat (retours BTC 1h/4h, BTC vs EMA, breadth/structure/leader trend crypto, faiblesse locale 1h/4h) ; simulation long/short horizon 180m avec coût round-trip 16 bps ; replay Pod A seul avec `trend_pullback_short` réactivé puis short-only expérimental (`scripts/run_p106_pod_a_short_replay.py`) sur avril/mai et mai/juin.
  **Preuves conservées** : rapport principal `server-data/replay_reports/p106_bear_regime_short_research_20260612T183822Z/p106_bear_regime_report.md`; replay Pod A récent `server-data/replay_reports/p106_bear_regime_short_research_20260612T183822Z/pod_a_short_replay_recent/pod_a_short_replay.md`; replay Pod A baseline `server-data/replay_reports/p106_bear_regime_short_research_20260612T183822Z/pod_a_short_replay_baseline/pod_a_short_replay.md`; test `tests/test_p106_bear_regime_research.py`.
  **Résultat short** : sur mai/juin, le runner Pod A seul confirme que `trend_pullback_short` aurait aidé : config long-only `-94.27 USD` / `62` trades ; `trend_pullback_short_on` `-74.85` / `265` trades (`trend_pullback_short=+19.42`) ; short-only `+19.42` / `203` trades. Sur avril/mai, activation globale rejetée : long-only `+265.47` / `106` trades ; `trend_pullback_short_on` `-14.32` / `271` trades ; short-only `-279.79` / `165` trades.
  **Résultat régime initial** : mai/juin contient beaucoup plus de futures baisses BTC 6h (`371/1679`, 22.1 %) qu'avril/mai (`173/2505`, 6.9 %). Un score bear faible (`>=2`) capte une partie des phases adverses récentes mais reste peu précis ; un score strict (`>=4/5`) arrive trop souvent après le choc et ne doit pas déclencher un short mécanique. Le bon signal à tester est donc un gate régime pré-entry multi-conditions, pas un simple seuil unique.
  **Étape 2 — Dataset labellisé régime** : produire `regime_labels.csv` avec labels forward 3h/6h/24h : `bullish` si BTC et breadth crypto confirment une dérive positive nette, `bearish` si BTC et breadth/leader trend confirment une dérive négative nette, `neutral/transition` sinon. Les labels doivent être calculés hors features d'entrée pour éviter le lookahead.
  **Étape 3 — Features pré-entry à figer** : tester uniquement des variables disponibles avant décision : retours BTC 1h/4h/24h, prix BTC vs EMA fast/slow, pente EMA, breadth/alt participation, leader_trend_score, coherence/dispersion, structure_score, volatilité/compression, funding/OI si disponible, spread/liquidité et faiblesse/force locale du symbole.
  **Étape 4 — Règle candidate long/short** : calibrer une matrice simple `bull_gate`, `bear_gate`, `neutral_gate`. Exemple à valider, pas à déployer : longs autorisés seulement si `bull_score` élevé et `bear_score` faible ; shorts shadow seulement si `bear_score` élevé, `bull_score` faible, BTC sous EMA + retour 4h négatif + breadth dégradée ; neutral/transition = pas de nouveau directionnel ou taille réduite.
  **Étape 5 — Replay full-bot gated** : intégrer le gate en mode replay/shadow, avec colonnes `bull_regime_score`, `bear_regime_score`, `regime_gate_decision`, `would_allow_long`, `would_allow_short`. Comparer au minimum : config actuelle, longs filtrés par gate, longs filtrés + shorts shadow, sur baseline avril/mai et récent mai/juin, avec caps live et coûts observés.
  **Étape 6 — Validation out-of-sample** : faire des folds temporels rolling pour éviter de calibrer sur seulement deux régimes visibles. Critères minimaux : préserver l'essentiel de l'upside avril/mai, améliorer mai/juin net de coûts, réduire drawdown/queue de stops, et ne pas multiplier l'activité au-delà d'un seuil explicite.
  **Étape 7 — Shadow live sans ordre** : si le replay est positif, déployer uniquement la journalisation shadow du gate : chaque décision doit dire `live_action_unchanged`, `regime_gate_decision`, `would_block_long`, `would_open_short_shadow`, et mesurer pendant plusieurs jours/trades ce qui aurait été changé.
  **Étape 8 — Critères de promotion** : aucune activation short live sans confirmation explicite. Candidat promouvable seulement si le full-bot gated bat la config actuelle sur mai/juin, ne détruit pas avril/mai, garde un PF net > 1 sur les shorts shadow, limite le trade-count, et passe une review manuelle PnL/risque/corrélation.
  **Décision actuelle** : ne pas activer les shorts globalement en live. P1-06 reste ouvert jusqu'à obtention d'un gate régime long/short validé par replay full-bot, puis shadow live.
  **Terminé quand** : le rapport contient la règle pré-entry finale, ses seuils, ses résultats par régime, ses critères de promotion/rollback et la preuve shadow ; sinon le résultat reste `research_only`.

### P2 — Hygiène, recherche et audit continu

- [ ] **P2-01 — HIP4 : run review fraîche et cutoffs propres**
  **Références** : F-04, R-08, S-06, addendum C.
  **Modifs à faire** : régénérer automatiquement la run review HIP4 à chaque `trident-hip4/fetch_data.sh`; produire un replay cutoff au timestamp exact d'activation de `prob_stop_full`; garder HIP4 en mainnet paper.
  **Tests / preuves attendues** : review datée du dernier fetch ; métriques PF/Brier/calibration sur les settlements entrés sous policy courante ; aucune promotion tant que PF ≥ 1.15 et Brier ≤ 0.23 ne sont pas atteints sur une tranche suffisante.
  **Terminé quand** : le statut HIP4 est lisible sans ambiguïté à chaque audit pack.

- [ ] **P2-02 — Pod C research : `gold_070` et silver**
  **Références** : R-09, addendum A/B, action interdite n°5.
  **Modifs à faire** : relancer les multipliers Pod C (`gold_070`, `global_070`, variantes silver) sur une fenêtre étendue incluant juin ; conserver `XYZ:SILVER` bloqué tant que le replay ne prouve pas un edge robuste.
  **Tests / preuves attendues** : rapport comparé à la baseline officielle, activité équivalente, frais inclus, séparation par cluster ; aucune promotion si l'amélioration vient seulement d'un sur-apprentissage gold ou d'une hausse d'activité.
  **Terminé quand** : chaque cluster Pod C a un statut clair : promouvable, à surveiller ou bloqué.

- [ ] **P2-03 — MFE/MAE et intégrité d'audit**
  **Références** : §10 données manquantes, S-09, R-10.
  **Modifs à faire** : tracker MFE/MAE par trade dans le state/report ; ajouter des checksums SHA-256 par fichier dans `manifest.json`; inclure `docs/trident_active_plan.md` dans les packs d'audit par défaut ; aligner README et config réelle (`pod_c.blocked_symbols`).
  **Tests / preuves attendues** : closed trades avec colonnes MFE/MAE ; manifest vérifiable ; pack d'audit reproductible ; README sans divergence avec `config/trident.toml` et le plan actif.
  **Terminé quand** : les prochaines décisions EFE/time-stop/trailing reposent sur excursions observées, pas seulement sur PnL final.

- [ ] **P2-04 — Scripts fetch/deploy : fausses alertes et non-régression**
  **Références** : S-06, R-10, instructions repo sur scripts de déploiement/fetch.
  **Modifs à faire** : corriger le faux `[ERROR] Fetch TRIDENT-HIP4 en erreur (code 0)` ; vérifier que tout changement de journal/export est bien couvert par fetch ; documenter les commandes de review locale.
  **Tests / preuves attendues** : `./scripts/fetch_trident_data.sh --review-only` OK ; `./trident-hip4/fetch_data.sh` OK ; aucun nouveau fichier nécessaire à l'audit n'est absent de `server-data/`; tests shell ou smoke test documenté.
  **Terminé quand** : les fetchs ne produisent plus d'erreur ambiguë et les packs contiennent tous les artefacts requis par ce plan.

**Conclusion de l'addendum.** La perte live n'est pas un mystère statistique : c'est la combinaison documentée et désormais chiffrée (1) d'un bug de stop immédiat (ère 1, -20), (2) d'une fenêtre cap 500 + grace 165 min pendant un selloff de -20 % sur des longs alts (ère 2, -79, dont ~-103 d'excès de stops vs plan), et (3) d'un edge d'entrée réellement faible en régime DeadZone/Range (ère 3, WR 24 %). Les correctifs du 09-06 ont traité la queue (1)(2) ; P1-01 confirme que la config courante cap live perd beaucoup moins (`-20.27`) que le live historique (`-157.88`), mais reste négative sur juin. P1-02 est clôturé comme replay de sensibilité, pas comme changement prod ; les chantiers ouverts sont maintenant le gate régime/entrée (P1-06), la parité Pod C (P1-03) et le modèle coût/slippage (P1-04).

*Limites de l'addendum : reconstruction au pas minute (pas de wicks intra-minute), frais/funding estimés et non observés, paramètres silver_mode pod C approximés, EFE non simulé avant le 09-06. Aucun chiffre reconstruit ne doit servir de base à un réglage sans le replay R-04.*

---

Avant les leviers, un cadrage honnête, parce qu'il conditionne tout le reste : la question « comment améliorer drastiquement le PnL » suppose qu'il existe un edge à débloquer. Après P1-01, l'audit laisse deux lectures possibles, avec une première préférence prudente :

- Lecture optimiste : le système a un edge (baseline +860 sur avril-mai), et c'est la queue des stops + un régime adverse qui ont mangé le live. On corrige → ça repasse positif.
- Lecture prudente : le baseline +860 est mesuré sur une période *trending*. En juin, marché baissier (-20 % sur BTC), un système **long-only** n'a peut-être structurellement pas d'edge, quels que soient les réglages d'exit.

P1-01 tranche une partie du sujet : la config courante cap live n'est pas catastrophique comme le live historique, mais elle reste négative sur juin (`-20.27`, ou `-29.69` avec slippage observé). P1-02 a ensuite testé les exits sans trouver de variante globale promouvable. Le prochain préalable n'est plus un replay brut : c'est P1-06/P1-03/P1-04 pour savoir si on peut récupérer l'edge sans pari live.

Cela dit, voici les leviers par impact estimé sur la fenêtre que j'ai reconstruite.

**1. La queue des stops — le poste le plus chiffré (~-103 USD d'excès)**

C'est de loin le plus gros trou : 15 stops catastrophe ont coûté -120 à eux seuls, avec une perte moyenne de 2.4× le stop planifié. P1-02 montre toutefois que le remède global est piégeux : EFE + cat stop plafonné améliore juin mais détruit beaucoup d'upside sur la baseline trend. La recommandation devient donc : **ne pas toucher les paramètres globaux**, mais tester un gate régime/qualité qui active le mode défensif seulement quand le leader/régime se dégrade.

**2. La discipline cap × régime — la leçon la plus claire de la fenêtre**

L'ère 2 raconte tout : cap monté à 500 le 02-06, puis selloff → -79, dont -82 sur la seule période cap-500/cap-redescendu. Le retour à 200 était la bonne décision. La règle qui en sort : **ne jamais remonter le cap sans régime favorable confirmé**, et idéalement lier le cap au régime de façon automatique plutôt qu'à un ajustement opérateur a posteriori. Un cap qui se contracte automatiquement quand le leader (BTC) passe en downtrend aurait évité l'essentiel du -44 du 05-06.

**3. La qualité d'entrée en régime dégradé — le vrai sujet de fond**

C'est le point le plus profond et le plus inconfortable. Le WR de l'ère 3 est de 24 %, et 18 closes sur 25 se font en DeadZone/RangeAuction alors que les entrées sont prises en TrendExpansion. Autrement dit : **le système entre sur un signal de tendance, puis le régime se dégrade sous lui.** Aucun réglage d'exit ne corrige un edge d'entrée faible. P1-06 confirme que `trend_pullback_short` aurait aidé mai/juin (`+19.42 USD` en short-only Pod A), mais aurait détruit avril/mai (`-279.79 USD`) : le levier n'est donc pas "activer les shorts", c'est **détecter le régime baissier avant l'entrée** puis bloquer/réduire les longs et tester les shorts uniquement en shadow. C'est là que se trouve le « drastique » réel, mais c'est aussi le plus risqué à toucher.

**4. Pod C : ressusciter la référence externe + maintenir le blocage silver**

F-06 est à la fois un trou de PnL et de sécurité : la référence externe est morte sur 100 % des enregistrements live, donc Pod C trade sans garde-fou de prix et sans la donnée qui faisait son edge en backtest (+79). La rétablir est probablement le geste le plus rentable côté Pod C. Et le silver reste la quasi-totalité de la perte Pod C (~-24, 0 gagnant) — ne pas le réautoriser.

**5. Exécution / slippage**

13.4 bps observés en ère 2 vs 8 supposés. Sur ~140 entrées, l'écart se chiffre en dizaines de dollars. Entrée *spread-aware* (R-06), avec websocket F-07 déjà corrigé par P0-03 et fees/funding désormais backfillés.

**6. Le boost « strong A-grade » — à geler, échantillon faible**

Contre-performant sur les 25 trades attribuables (le boost ×1.4 amplifie les trades perdants), mais l'échantillon ne permet pas de conclure et l'export ne contient même pas les champs A-grade. Geler à 1.0 en attendant le replay R-05, sans détruire le label.

---

Si je devais ne retenir qu'une chose : **le levier le plus drastique n'est pas un meilleur exit, c'est de ne pas être long-only crypto dans un marché baissier.** P1-06 valide l'intuition sur juin mais rejette l'activation globale des shorts. À court terme, la séquence rationnelle est : garder le live inchangé, construire un gate `bear_regime_pre_entry` en shadow/dry-run, puis seulement après out-of-sample décider s'il bloque les longs, réduit le cap, ou autorise un short contrôlé.

Je ne suis pas conseiller financier, et tous les chiffres ci-dessus reposent sur une reconstruction au pas minute, pas sur les fills exchange réels — donc à traiter comme des ordres de grandeur pour prioriser, pas comme des vérités à câbler en dur.
