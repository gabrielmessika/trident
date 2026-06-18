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
  **Étape 1 — code local réalisé le 2026-06-14** : `app/live/pod_c_external_reference.py` ajoute un enrichisseur read-only Pod C avec cache et fetcher Yahoo injectable ; `config/trident.toml` mappe les sous-jacents `XYZ:*` (`CL`, Brent, SP500, XYZ100, Silver, Gold, JPY, TSLA, NVDA, CRCL) vers leurs références Yahoo ; `app/live/pod_c_live_runner.py` enrichit les snapshots avant annotation/journal/process et expose des compteurs `external_reference` dans `logs/pod_c_live_status.json`. Le fallback REST conserve les derniers champs externes connus. Garantie : aucune condition d'entrée/sortie Pod C ne lit ces champs ; ils restent diagnostic/audit seulement.
  **Étape 2 — export réalisé le 2026-06-14** : `scripts/export_trident_audit_pack.py` conserve maintenant le paquet complet dans décisions, symbol snapshots et closed trades : `external_reference_price`, `source_count`, `sources`, `symbol`, `time`, `age_seconds`, `max_deviation_bps`, `external_premium_bps`, `external_momentum_60s_bps`, `external_momentum_300s_bps`, `external_alignment_score`.
  **Étape 3 — fetch/review P1-03 ajouté le 2026-06-14** : `scripts/fetch_trident_data.sh` génère désormais `p103_external_reference_audit.md/json` dans chaque `server-data/reviews/<timestamp>/`, en plus de `review_summary.md/json`. Le diagnostic lit `logs/pod_c_live_status.json`, les dernières lignes de `logs/pod_c_live.jsonl` et les derniers snapshots rapatriés ; il reporte runtime `symbols_enriched`, couverture snapshot/journal, dernières références par symbole, erreurs de fetch externe et confirmation config `XYZ:SILVER` bloqué. Limites contrôlables : `TRIDENT_FETCH_P103_SNAPSHOT_FILES`, `TRIDENT_FETCH_P103_SNAPSHOT_TAIL_LINES`, `TRIDENT_FETCH_P103_JOURNAL_TAIL_LINES`.
  **Étape 4 — déployée en live mainnet le 2026-06-14** : `./deploy.sh --start --mode live --network mainnet` terminé OK. Preflight Pod A et Pod C `ready=true`, reconciliation vide (`unknown_exchange_positions=[]`, `missing_exchange_positions=[]`, `open_orders=[]`, `trigger_orders=[]`), `orderUpdates` OK, services actifs : `trident-api`, `pod-a-live`, `pod-c-live`, `tradfi-funding-collector`, `funding-collector`.
  **Preuves post-déploiement** : fetch `server-data/reviews/20260614T205447Z/` en `PASS`. Runtime Pod C `external_reference.enabled=true`, `records_seen=1`, `symbols_seen=5`, `symbols_enriched=5`, `symbols_missing_reference=0`, `fetch_error_count=0`. Dernier snapshot Pod C `2026-06-14T20:51:00Z` enrichi pour `XYZ:CL`, `XYZ:BRENTOIL`, `XYZ:SP500`, `XYZ:XYZ100`, `XYZ:SILVER` avec `external_reference_source_count=1`. Les âges Yahoo sont élevés (`~172k s`) car la preuve est hors séance/week-end, donc utilisables pour audit/stale mais pas pour un guardrail live sans replay. `XYZ:SILVER` reste bloqué par config.
  **Preuves locales** : smoke réseau read-only sur les `10/10` symboles Pod C configurés (`symbols_enriched=10`, `symbols_missing_reference=0`; âges élevés attendus hors séance/week-end) ; tests `uv run pytest tests/test_pod_c_external_reference.py tests/test_settings.py tests/test_reporting.py tests/test_pod_c.py -q` (`42 passed`) ; `py_compile` ; `bash -n scripts/fetch_trident_data.sh deploy.sh` ; `git diff --check`. Impact scripts : pas de nouveau fichier à fetcher, mais la review TRIDENT A/C inclut maintenant un artefact P1-03 dédié.
  **Étape 5 — preuve live/journal validée le 2026-06-15** : fetch global `./scripts/fetch_all_data.sh` terminé avec TRIDENT A/C frais dans `server-data/reviews/20260615T052911Z/`. `review_summary.md` et `p103_external_reference_audit.md/json` sont en `PASS` : runtime Pod C `external_reference.enabled=true`, `records_seen=518`, `symbols_seen=5166`, `symbols_enriched=5166`, `symbols_missing_reference=0`, `fetch_error_count=0`; snapshots `5166/44315` enrichis ; tail journal audit `1000/1000` setup records enrichis. Contrôle local post-déploiement (`>=2026-06-14T20:50Z`) : `1670/1670` setup records Pod C `XYZ:*` ont `external_reference_available=true` ou `external_reference_source_count>0`. Un trade Pod C fermé après déploiement (`XYZ:GOLD`, ouvert `2026-06-14T21:32Z`, fermé `2026-06-14T22:08:47Z`, `pnl_usd=1.67`) conserve aussi le paquet externe dans `setup_details` (`external_reference_available=true`, `external_reference_symbol=yahoo:GC=F`, `external_premium_bps=63.1079`). `XYZ:SILVER` reste bloqué par config ; aucun signal `symbol_blocked` silver n'apparaît dans le tail audité, donc le prochain fetch doit seulement confirmer ce cas si un candidat silver redevient éligible.
  **Étape 6 — validation stale/dislocation lancée le 2026-06-15** : `scripts/run_p103_pod_c_external_reference_validation.py` enrichit les trades fermés Pod C au timestamp d'entrée avec Yahoo 5m, puis teste des gates research `missing_or_stale_*`, `abs_premium_gt_*`, `counter_momentum_5m_6bps` et combos. Artefacts : `server-data/replay_reports/p103_pod_c_external_reference_validation_20260615/p103_pod_c_external_reference_validation.md/json`. Tests : `uv run pytest tests/test_p103_pod_c_external_reference_validation.py tests/test_external_reference_policy.py tests/test_pod_c_external_reference.py -q` (`8 passed`) puis test ciblé P1-03 (`2 passed`) ; `py_compile` OK. Résultat : fenêtre récente `2026-05-24 → 2026-06-11` couverte à `91.67%` (`22/24` trades Pod C), base `-66.63 USD`. Plusieurs gates améliorent cette fenêtre en counterfactual : `abs_premium_gt_50` passe à `-22.02` (`+44.61`), `candidate_default_5m` passe à `+13.46` (`+80.09`) mais bloque `15/24` trades, `counter_momentum_5m_6bps` gagne `+15.09`. Limite bloquante : la baseline avril/mai `2026-04-05 → 2026-05-13` n'a plus de couverture Yahoo intraday 5m disponible (`0/41` trades), donc ces gates ne sont pas validables out-of-sample contre le régime favorable qui faisait `+79.11 USD`.
  **Étape 7 — shadow P1-03 codé et déployé le 2026-06-15** : `app/trident/pod_c/external_reference_shadow.py` journalise en observation-only `external_reference_shadow_mode=observation_only`, `external_reference_shadow_live_action_unchanged=true`, `would_block_external_reference_abs_premium_gt_50`, `would_block_external_reference_abs_premium_gt_100`, `would_block_external_reference_counter_momentum_5m_6bps`, `would_block_external_reference_candidate_loose_5m`, `would_block_external_reference_candidate_default_5m` et `external_reference_shadow_reason`. `app/live/pod_c_live_runner.py` injecte ces champs dans les `TradePlan`, les previews journalisées et les signal reviews filtrées ; aucune décision live/risk/exécution ne lit ces champs. `scripts/fetch_trident_data.sh` compte maintenant `external_reference_shadow.records`, `with_shadow`, `by_gate`, `by_symbol` et échoue si `live_action_unchanged=false`; `scripts/export_trident_audit_pack.py` exporte aussi ces colonnes dans les packs d'audit. Tests : `uv run pytest tests/test_pod_c_external_reference_shadow.py tests/test_p103_pod_c_external_reference_validation.py tests/test_pod_c_external_reference.py tests/test_pod_c.py tests/test_reporting.py -q` (`46 passed`), `py_compile`, `bash -n scripts/fetch_trident_data.sh deploy.sh`, `git diff --check`.
  **Déploiement / preuve post-déploiement** : premier `./deploy.sh --start --mode live --network mainnet` stoppé par un `502 Bad Gateway` Hyperliquid sur le preflight Pod A, anciens services restés actifs. Second essai terminé OK : preflight Pod A/Pod C `ready=true`, websocket `orderUpdates` OK, services `trident-api`, `pod-a-live`, `pod-c-live`, `tradfi-funding-collector`, `funding-collector` démarrés. Fetch logs-only puis review-only `server-data/reviews/20260615T075322Z/` en `PASS` : runtime Pod C `external_reference.enabled=true`, `symbols_enriched=16`, `symbols_missing_reference=0`; journal `1000/1000` setup records avec référence ; shadow `2/1000`, `by_symbol={'XYZ:GOLD': 2}`, tous les `by_gate` à `0` pour ces deux premiers candidats et `live_action_unchanged_false=0`; `XYZ:SILVER` reste bloqué par config.
  **Décision P1-03 après derniers tests** : ne pas clôturer P1-03 si le périmètre inclut le guardrail stale/dislocation. La restauration data live est terminée et le shadow est en prod, mais l'idée guardrail n'est ni promue ni abandonnée : elle est prometteuse sur mai/juin et non testée sur avril/mai faute de rétention intraday. Aucune règle active ne doit être déployée maintenant.
  **Prochaine étape pour clôture** : attendre assez de nouveaux signaux/trades Pod C avec shadow, puis mesurer le PnL réel des trades que `would_block_external_reference_*` aurait bloqués. P1-03 pourra être clôturé seulement si ce shadow confirme une règle promouvable ou si l'on abandonne explicitement le guardrail et clôture P1-03 comme restauration data uniquement.
  **Tests / preuves attendues** : `external_reference_available` n'est plus False sur 100 % des nouveaux enregistrements live Pod C (validé le `2026-06-15`) ; `XYZ:SILVER` reste bloqué par config (validé, à confirmer sur prochain candidat silver `symbol_blocked`) ; replay stale/dislocation exécuté mais non conclusif OOS ; shadow live P1-03 déployé et audité par fetch (`shadow_coverage>0`, `live_action_unchanged_false=0`) ; aucun ordre nouveau ne doit être activé par une règle de référence externe tant qu'un shadow live n'est pas promu.
  **Terminé quand** : soit le shadow stale/dislocation Pod C valide une règle et elle est promue explicitement, soit le guardrail est abandonné explicitement et P1-03 est clôturé comme "data live restaurée, pas de règle active".

- [x] **P1-04 — Exécution Pod A : slippage et coûts**
  **Références** : R-06, F-07, addendum A, levier PnL 5.
  **Modifs à faire** : ajouter dans l'audit des métriques slippage par symbole/setup/ère ; tester une entrée plus spread-aware ou un skip si spread/slippage attendu dépasse un seuil, uniquement en replay/dry-run avant live.
  **Étape 1 — audit/replay P1-04 réalisé le 2026-06-15** : `scripts/run_p104_execution_cost_replay.py` produit un rapport research-only avec métriques slippage par pod/symbole/setup/ère depuis `trident_ac_fill_events.csv` et replay global A/C avec caps live. Artefacts : `server-data/replay_reports/p104_execution_cost_20260615T090601Z/p104_execution_cost_replay.md/json` et `slippage_by_pod_symbol_setup_era.csv`.
  **Résultat replay** : baseline avril/mai courante `+77.08 USD` A/C (`Pod A +56.72`, `Pod C +20.36`, `133` trades) ; fenêtre live post-baseline `-23.18 USD` A/C (`Pod A -15.35`, `Pod C -7.83`, `103` trades). Le filtre naïf `spread_lte_6bps` est rejeté : baseline `70.25` (`-6.83`) et live `-37.36` (`-14.18`) malgré `21` skips live. Les seuils `spread_lte_8bps`, `expected_entry_cost_lte_8bps` et `expected_entry_cost_lte_10bps` ne changent aucun trade sur ces fenêtres. Conclusion : le spread snapshot seul n'explique pas le slippage réel, et un skip spread direct tue plus de bons trades qu'il n'évite de pertes.
  **Métriques slippage utiles** : les pires buckets Pod A open restent concentrés sur l'ère 2/3 et les alts déjà identifiés (`AAVE` max `60.52bps`, `TON`, `ENA`, `ICP`, `PENDLE`, `PENGU`, `TIA`, etc.). Ces métriques doivent alimenter P1-08/P2-03 et un futur modèle par symbole/liquidité, pas une règle globale de spread.
  **Décision actuelle** : aucune règle live de skip/limit n'est promouvable. Garder l'exécution live inchangée ; continuer à collecter fees/fills et tester seulement des modèles plus fins, par symbole et état de marché, avec fill-rate explicite.
  **Clôture P1 le 2026-06-16** : P1-04 est clos comme levier P1, car le replay demandé existe et le seul changement live simple testé (`spread_lte_6bps`) est rejeté. Les coûts restent utiles comme feature d'audit continu et pour P1-08/P2-03, mais il n'y a pas de modification exécution à faire avant P2.
  **Tests / preuves attendues** : fees réels capturés dans les fill events ; replay coûts 8/12/observé ; A/B dry-run sur taux de fill manqué vs PnL simulé ; surveillance continue du websocket corrigé en P0-03.
  **Terminé** : aucune règle de skip/limit n'est promue ; la suite est un contrôle de qualité des données de coûts, pas un chantier P1 bloquant.

- [x] **P1-05 — A-grade / quality sizing : données d'abord, gel ensuite si confirmé**
  **Références** : F-05, F-08, R-05, addendum A/E, levier PnL 6.
  **Modifs à faire** : ajouter `a_grade_active`, `a_grade_level`, `a_grade_score`, `a_grade_size_scale` et les champs de quality sizing dans `export_trident_audit_pack.py`; rejouer les size scales `{1.0, 1.25, 1.40}` ; ne geler le boost strong à 1.0 en live que si le replay confirme la contre-performance.
  **Étape 1 — export/replay P1-05 réalisé le 2026-06-15** : `scripts/export_trident_audit_pack.py` exporte maintenant `a_grade_size_scale`, `a_grade_reason`, `live_quality_sizing_active`, `live_quality_original_target_notional_usd`, `live_quality_original_margin_usd`, `live_quality_original_risk_budget_usd` et `live_quality_original_expected_loss_usd` dans les closed trades et le compact `setup_details`. `scripts/run_p105_a_grade_replay.py` rejoue les scénarios `current`, `flat_scale_1p00`, `flat_scale_1p25`, `flat_scale_1p40` et `strong_frozen_1p00` sur baseline avril/mai et fenêtre live post-baseline avec cap live appliqué. Artefacts : `server-data/replay_reports/p105_a_grade_replay_20260615T123837Z/p105_a_grade_replay.md/json` et `scenario_summary.csv`.
  **Résultat replay** : baseline avril/mai inchangée pour tous les scénarios (`+77.08 USD` A/C, Pod A `+56.72`, `133` trades A/C, DD Pod A `24.32`). Fenêtre live post-baseline quasi inchangée : config courante `-30.96 USD` A/C (`Pod A -22.39`, `106` trades A/C), `flat_scale_1p00` `-30.88` (`+0.08`), `strong_frozen_1p00` delta `+0.00`. Les strong A-grade ne sont pas le trou PnL sous cap live : `37` trades strong en baseline pour `+30.67 USD`, `24` trades strong en live pour `+3.84 USD`.
  **Preuve export local** : pack `server-data/audit_exports/p105_a_grade_fields_20260615T1327Z/` généré sans fetch ; `trident_ac_closed_trades.csv` contient toutes les nouvelles colonnes, avec `134/141` closed trades Pod A ayant `a_grade_size_scale` non vide et `40/141` ayant `live_quality_sizing_multiplier` non vide. Les records `signal` restent pré-plan et ne contiennent pas `a_grade_*` dans le journal source ; l'analyse A-grade exploitable se fait donc sur les trades fermés tant qu'un journal post-plan dédié n'est pas ajouté.
  **Décision P1-05** : ne pas geler `a_grade_strong_boost_scale` à `1.0` et ne pas changer la config live. Le replay ne montre aucun gain matériel au freeze strong, et la perte récente vient davantage du régime/entrée que du scale A-grade. Si l'on veut auditer plus finement le quality sizing live, le faire via les closed trades exportés et P2-03 MFE/MAE, pas via une modification de sizing immédiate.
  **Tests / preuves attendues** : prochain pack avec champs A-grade non vides ; replay baseline officielle + fenêtre récente pour chaque scale ; comparaison PnL, drawdown, PF, WR et concentration des pertes.
  **Tests réalisés** : `uv run pytest tests/test_p105_a_grade_replay.py -q` (`3 passed`) ; `uv run python -m py_compile scripts/run_p105_a_grade_replay.py scripts/export_trident_audit_pack.py` ; export audit pack local P1-05 ; replay P1-05 complet.
  **Terminé** : boost strong conservé, sans changement live, car le replay ne confirme pas la contre-performance du scale A-grade sous cap live.

- [x] **P1-06 — Régime haussier/baissier Pod A : gate long/short avant entrée**
  **Références** : §5.4, F-05, R-04/R-07, leviers PnL 2/3, `config/trident.toml` (`trend_pullback_long` seul autorisé, shorts désactivés).
  **Objectif** : définir une règle pré-entry qui identifie un régime haussier pour autoriser/renforcer les longs, un régime baissier pour bloquer/réduire les longs et tester les shorts en shadow, sans activer de nouveaux ordres ni modifier la config live tant que la validation n'est pas terminée.
  **Étape 1 — Constat initial réalisé** : scan opportunité pré-entry sur `BTC/ETH/SOL/HYPE` (`scripts/run_p106_bear_regime_research.py`) ; score bear basé uniquement sur informations disponibles au timestamp candidat (retours BTC 1h/4h, BTC vs EMA, breadth/structure/leader trend crypto, faiblesse locale 1h/4h) ; simulation long/short horizon 180m avec coût round-trip 16 bps ; replay Pod A seul avec `trend_pullback_short` réactivé puis short-only expérimental (`scripts/run_p106_pod_a_short_replay.py`) sur avril/mai et mai/juin.
  **Preuves conservées** : rapport principal `server-data/replay_reports/p106_bear_regime_short_research_20260612T183822Z/p106_bear_regime_report.md`; replay Pod A récent `server-data/replay_reports/p106_bear_regime_short_research_20260612T183822Z/pod_a_short_replay_recent/pod_a_short_replay.md`; replay Pod A baseline `server-data/replay_reports/p106_bear_regime_short_research_20260612T183822Z/pod_a_short_replay_baseline/pod_a_short_replay.md`; test `tests/test_p106_bear_regime_research.py`.
  **Résultat short** : sur mai/juin, le runner Pod A seul confirme que `trend_pullback_short` aurait aidé : config long-only `-94.27 USD` / `62` trades ; `trend_pullback_short_on` `-74.85` / `265` trades (`trend_pullback_short=+19.42`) ; short-only `+19.42` / `203` trades. Sur avril/mai, activation globale rejetée : long-only `+265.47` / `106` trades ; `trend_pullback_short_on` `-14.32` / `271` trades ; short-only `-279.79` / `165` trades.
  **Résultat régime initial** : mai/juin contient beaucoup plus de futures baisses BTC 6h (`371/1679`, 22.1 %) qu'avril/mai (`173/2505`, 6.9 %). Un score bear faible (`>=2`) capte une partie des phases adverses récentes mais reste peu précis ; un score strict (`>=4/5`) arrive trop souvent après le choc et ne doit pas déclencher un short mécanique. Le bon signal à tester est donc un gate régime pré-entry multi-conditions, pas un simple seuil unique.
  **Étapes 2-6 — réalisées en replay le 2026-06-12** : `scripts/run_p106_regime_gate_replay.py` produit `regime_labels.csv`, fige les features pré-entry, applique plusieurs gates `bull_score`/`bear_score`, exporte les trades fermés par scénario et rejoue tout l'historique disponible avec cap live (`baseline_apr_may` 04-05 → 13-05, `58 752` records ; `live_post_baseline` 14-05 → 12-06, `36 947` records). Artefacts : `server-data/replay_reports/p106_regime_gate_full_20260612T2050Z/`.
  **Labels régime obtenus** : baseline avril/mai = `172/2503` labels 6h bearish et `108/2139` labels 24h bearish ; live post-baseline = `416/1990` labels 6h bearish et `728/1830` labels 24h bearish. Les données confirment deux régimes, mais les labels forward servent seulement à évaluer, jamais à décider.
  **Règle de score testée** : `bull_score`/`bear_score` utilisent seulement des features disponibles avant entrée : retours BTC 1h/4h/24h, BTC vs EMA fast/slow, structure/breadth/alt participation/leader trend/coherence/dispersion, et force/faiblesse locale du symbole. `bullish` = `bull_score>=4` et `bear_score<=2`; `bearish` = `bear_score>=4` et `bull_score<=2`; `defensive` = transition dégradée (`bear_score>=3`, pas assez propre pour être `bearish`).
  **Résultats replay live-cap** : config courante long-only = `+50.71 USD` total (`baseline +61.71`, live post-baseline `-11.00`). `long_not_bear` (bloquer longs si `bear_score>=4`) améliore peu mais proprement : `+54.54` total, live `-7.17`, baseline inchangée. Tous les shorts de régime `bearish` échouent : `short_only_global=-82.49`, `bear3_short_only=-76.41`, `bear4_short_only=-96.59`, et ils détruisent la baseline.
  **Candidat survivant** : le short n'a d'edge que dans le régime `defensive` (transition), pas en `bearish` plein. `defensive_short_only` = `+53.37` total (`baseline +2.22`, live `+51.15`, PF live `1.75`, max DD `18.73`). Le combo `long_not_bear_defensive_short` = `+107.91` total (`baseline +63.93`, live `+43.98`) vs courant `+50.71`, mais il double le trade-count (`304` vs `152`) et augmente le DD baseline (`32.88` vs `24.32`). Les gains live sont concentrés surtout les 04-05/06 (`+48.43` sur les shorts), donc pas de promotion directe.
  **Décision actuelle** : abandonner l'idée "ouvrir des shorts en régime bearish pur" ; conserver uniquement le candidat `defensive_short` en shadow. Ne pas activer les shorts globalement en live. Ne pas promouvoir `long_not_bear_defensive_short` sans shadow live, car le replay est positif mais l'activité et la concentration temporelle augmentent le risque.
  **Étape 7 — déployée en live mainnet le 2026-06-13** : `app/trident/pod_a/regime_shadow.py` calcule le score commun aux replays et au live ; `app/live/pod_a_live_runner.py` journalise `regime_shadow` dans les signaux Pod A et les revues filtrées, et ajoute dans `setup_details` `bull_regime_score`, `bear_regime_score`, `regime_gate_decision`, `would_block_long`, `would_open_defensive_short_shadow`, `live_action_unchanged`. Garantie : aucune décision live n'est filtrée par ce gate, aucun short n'est activé, les `risk_decisions` envoyées à l'exécuteur restent celles de la config courante. Tests : `uv run pytest tests/test_pod_a_regime_shadow.py tests/test_pod_a_live_runner.py tests/test_p106_regime_gate_replay.py tests/test_p106_bear_regime_research.py` (`17 passed`) + `py_compile`. Déploiement : `./deploy.sh --start --mode live --network mainnet`, preflight Pod A/Pod C OK, API healthy, Pod A/Pod C/funding up, module shadow importable dans `pod-a-live`. Impact scripts : pas de nouveau fichier à fetcher, les champs passent par `logs/pod_a_live.jsonl` déjà rapatrié par `scripts/fetch_trident_data.sh`; scripts de déploiement inchangés.
  **Étape 8 — audit shadow live réalisé le 2026-06-15** : fetch A/C frais `server-data/reviews/20260615T141358Z/`, puis `scripts/run_p106_regime_shadow_audit.py` lit `logs/pod_a_live.jsonl` et `live_snapshots`, mesure les champs observation-only, puis estime les candidats `defensive_short` avec un proxy fixed-horizon `180m`, coût `16 bps`, notionnel `200 USD`, dédupliqué `180m` par symbole. Artefacts : `server-data/replay_reports/p106_regime_shadow_audit_20260615T141411Z/p106_regime_shadow_audit.md/json`, `defensive_short_forward_returns_raw.csv` et `defensive_short_forward_returns_deduped.csv`. `scripts/export_trident_audit_pack.py` exporte aussi les champs P1-06 (`regime_shadow_mode`, `bull_regime_score`, `bear_regime_score`, `regime_gate_decision`, `would_block_long`, `would_open_defensive_short_shadow`, `live_action_unchanged` et retours/features associés) dans les décisions compactes et closed trades.
  **Résultat shadow live P1-06** : fenêtre locale `2026-06-13T12:59Z → dernier log fetché`, `4581` records avec shadow (`4280` signal reviews, `301` signals), gates `bullish=2564`, `constructive=615`, `neutral=18`, `defensive=810`, `bearish=574`, `would_block_long=226`, `would_open_defensive_short_shadow=23`, `live_action_unchanged_false=0`. Les `16` trades fermés avec shadow sont tous des longs en gate `bullish`, PnL `+2.37 USD`; le gate n'aurait donc bloqué aucun long réel fermé (`would_block_long_trades=0`). Le proxy defensive short est négatif : raw `20` candidats, PnL `-19.59 USD`, PF `0.32`; dédupliqué `17` candidats, PnL `-23.00 USD`, PF `0.20`.
  **Décision après audit shadow** : ne pas promouvoir `defensive_short`, ne pas activer de short live et ne pas activer `long_not_bear` en filtre live maintenant. Le replay historique restait intéressant, mais le premier out-of-sample shadow ne confirme pas les shorts défensifs ; les longs réellement pris étaient déjà en régime bullish. Garder P1-06 en observation et relancer l'audit après plus de closes et/ou un nouveau régime adverse.
  **Audit frais P1-06 du 2026-06-16** : après fetch A/C et review locale `server-data/reviews/20260616T130640Z/`, `scripts/run_p106_regime_shadow_audit.py` a produit `server-data/replay_reports/p106_regime_shadow_audit_20260616T130709Z/`. Couverture `7667` records avec shadow (`7170` signal reviews, `497` signals), gates `bullish=3621`, `constructive=1005`, `defensive=1488`, `bearish=1526`, `would_block_long=494`, `would_open_defensive_short_shadow=60`, `live_action_unchanged_false=0`. Les `23` trades fermés avec shadow restent tous en gate `bullish`, PnL `-2.12 USD`, aucun long réel fermé n'aurait été bloqué. Le proxy short défensif empire : raw `23` candidats, `-48.58 USD`, PF `0.16`; dédupliqué `18` candidats, `-32.91 USD`, PF `0.15`.
  **Décision finale P1-06** : classer `defensive_short`, `bearish_short` et `long_not_bear` en `research_only_no_live_change`. Ne pas activer de short Pod A, ne pas filtrer les longs par ce gate et ne pas modifier la config live. Le shadow peut rester journalisé comme diagnostic de régime, mais il ne porte plus de chantier P1 actif.
  **Prochaine action P1-06** : aucune modification. Réouvrir seulement si une nouvelle fenêtre adverse donne un proxy out-of-sample positif net et qu'un replay full-bot A/C confirme avant toute promotion.
  **Étape 9 — Critères de promotion** : aucune activation short live sans confirmation explicite. Candidat promouvable seulement si le shadow montre PF net > 1 sur `defensive_short`, drawdown stable, pas de concentration sur un seul événement, activité acceptable, et si le full-bot gated avec Pod C/routing confirme l'amélioration.
  **Terminé** : règle pré-entry non promue ; statut final `research_only_no_live_change`.

- [x] **P1-07 — Patterns chartistes multi-timeframe : H&S, EMA cross, order block, cup/handle**
  **Références** : demande opérateur du 2026-06-13, §6.1, P1-06, `config/trident.toml` (`allowed_setups=["trend_pullback_long"]`, patterns classiques non promus).
  **État actuel du bot** : pas de détection directe d'épaule-tête-épaule, épaule-tête-épaule inversé, tasse et anse, ni order block. Pod A utilise surtout `trend_pullback_long` avec alignement EMA fast/slow, VWAP/structure/flow, contexte 15m/1h/4h, Ichimoku/supertrend/RSI/CCI comme features/vetoes, et des setups codés mais désactivés (`bos_retest_*`, `liquidity_sweep_reclaim_*`, `vwap_reclaim_*`, `trend_pullback_short`). Les croisements EMA ne sont pas tradés comme événements de cross ; seules des conditions d'alignement/distance EMA et des vetoes MTF existent.
  **Étude réalisée** : script `scripts/run_p108_chart_pattern_research.py`, artefacts `server-data/replay_reports/p108_chart_patterns_20260613T000000Z/`. Scope : Pod A crypto uniquement, OHLC reconstruits depuis les snapshots minute, timeframes `15m/1h/4h`, coûts standalone `16 bps`, notional `200 USD`, stop `160 bps`, TP `260 bps`. Données exploitables : `baseline_apr_may` `2026-04-05T19:45Z → 2026-05-13T07:56Z` (`50 599` records sur `BTC/ETH/SOL/HYPE/DOGE/SUI/ENA/ZEC/BIO`) et `live_post_baseline` `2026-05-14T00:00Z → 2026-06-12T16:31Z` (`32 948` records). Limite : ce n'est pas trois mois calendaires complets ; le dataset local exploitable commence le 2026-04-05.
  **Résultat replay Pod A filtré** : baseline courante sur ce pool = `+20.62 USD` / `93` trades ; live post-baseline = `-12.72 USD` / `18` trades. Le filtre `veto_bearish_classic_any_tf` améliore fortement juin (`+2.74`, delta `+15.46`, seulement `4` trades, DD `2.08`) mais détruit une partie de la baseline (`+6.92`, delta `-13.70`). Le filtre le plus lisible est `veto_bearish_order_block_any_tf`, identique sur juin (`+2.74`, delta `+15.46`) mais encore négatif sur baseline (`+14.38`, delta `-6.24`). Les filtres de confirmation bullish améliorent peu juin ou réduisent trop l'activité : `require_bullish_ema_any_tf` passe juin à `-1.90` (delta `+10.82`) mais tombe à `+3.67` sur baseline ; `require_bullish_order_block_any_tf` monte baseline à `+28.22` (delta `+7.60`) mais reste négatif en juin (`-10.81`).
  **Résultat standalone par pattern** : les signaux doivent être régime-spécifiques. En avril/mai, les meilleurs longs sont `order_block_bull_retest` `1h/4h` (`+195.23` / `+147.57` simulés) et `cup_handle_breakout` `4h` (`+56.83`) ; les shorts équivalents détruisent la baseline (`order_block_bear_retest 1h=-753.48`, `inverse_cup_handle_breakdown 1h=-629.66`). En mai/juin, les meilleurs shorts sont `inverse_cup_handle_breakdown 1h` (`+502.72`), `order_block_bear_retest 1h` (`+210.97`), `ema_cross_bear 1h` (`+49.90`) ; les longs 15m/1h explosent à la baisse (`cup_handle_breakout 15m=-1702.50`, `order_block_bull_retest 15m=-1695.37`, `order_block_bull_retest 1h=-835.93`). H&S seul n'est pas robuste : `head_shoulders_breakdown 1h` est légèrement positif en juin (`+10.85`) mais négatif en baseline (`-57.93`) ; l'inversé est globalement faible/négatif.
  **P1-07b — replay régime + order block réalisé le 2026-06-14** : script `scripts/run_p107b_regime_order_block_replay.py`, artefacts `server-data/replay_reports/p107b_regime_order_block_20260614T000000Z/`. Scope : replay directionnel A/C, Pod B désactivé, caps live appliqués, full universe disponible, `order_block_bull_retest`/`order_block_bear_retest` uniquement en `1h/4h`. Baseline avril/mai courante = `+77.08 USD` A/C (`Pod A +56.72`, `Pod C +20.36`, `133` trades) ; live post-baseline courant = `-25.26 USD` A/C (`Pod A -17.15`, `Pod C -8.11`, `87` trades).
  **Résultats P1-07b** : `long_veto_defensive_bearish_ob_1h4h` ne dégrade pas avril/mai (`+77.08`, delta `+0.00`) et améliore mai/juin (`-20.88`, delta `+4.38`) en bloquant `9` longs récents. `long_constructive_bullish_ob_only_1h4h` est rejeté (`baseline -11.89`, live `-2.39` vs courant). Le short seul `defensive_short_bearish_ob_only_1h4h` est positif en Pod A (`baseline +4.50`, live `+35.60`) mais ne remplace pas les longs de baseline. Le meilleur candidat est le combo `long_veto_plus_defensive_short_ob_1h4h` : baseline `+81.58` A/C (delta `+4.50`, Pod A `+61.22`) et live `+14.72` A/C (delta `+39.98`, Pod A `+22.83`). Échantillon encore insuffisant pour prod : les shorts restent interdits en décision live.
  **Étape shadow locale ajoutée le 2026-06-14** : `app/trident/pod_a/order_block_shadow.py` reconstruit les order blocks `1h/4h` et `app/live/pod_a_live_runner.py` journalise `order_block_shadow` dans les signaux/revues filtrées avec `has_bearish_order_block_1h4h`, `would_block_long_order_block_shadow`, `would_open_defensive_short_order_block_shadow` et `live_action_unchanged=true`. Aucun filtre live n'est appliqué, aucun short n'est activé. Tests : `uv run pytest tests/test_pod_a_order_block_shadow.py tests/test_pod_a_live_runner.py tests/test_pod_a_regime_shadow.py` (`15 passed`) + `py_compile`. Impact scripts : pas de nouveau fichier à fetcher, les champs passent par `logs/pod_a_live.jsonl` déjà rapatrié par `scripts/fetch_trident_data.sh`; scripts de déploiement inchangés.
  **Étape audit shadow live réalisée le 2026-06-15** : après fetch A/C frais `server-data/reviews/20260615T141358Z/`, `scripts/run_p107_order_block_shadow_audit.py` lit `logs/pod_a_live.jsonl` et `live_snapshots`, mesure le shadow P1-07, puis valorise les candidats veto-long et defensive-short avec un proxy fixed-horizon `180m`, coût `16 bps`, notionnel `200 USD`, dédupliqué `180m` par symbole/type. Artefacts : `server-data/replay_reports/p107_order_block_shadow_audit_20260615T143232Z/p107_order_block_shadow_audit.md/json`, `long_veto_forward_returns_raw.csv`, `long_veto_forward_returns_deduped.csv`, `defensive_short_forward_returns_raw.csv` et `defensive_short_forward_returns_deduped.csv`.
  **Résultat shadow live P1-07** : fenêtre locale `2026-06-14T21:15Z → dernier log fetché`, `3488` records avec shadow (`228` signals, `3260` signal reviews), gates `bullish=2232`, `constructive=544`, `defensive=514`, `bearish=191`, `neutral=7`. `has_bearish_order_block_1h4h=95`, `would_block_long_order_block_shadow=9`, `would_open_defensive_short_order_block_shadow=0`, `live_action_unchanged_false=0`. Les `15` trades fermés avec shadow sont tous en gate `bullish`, PnL `+3.39 USD`; aucun trade réel fermé n'aurait été bloqué par P1-07 (`would_block_long_trades=0`). Le proxy veto-long est négatif : raw `8` candidats, decision value `-14.81 USD`, PF `0.03`; dédupliqué `3` candidats, decision value `-3.95 USD`, PF `0.00`. Aucun candidat defensive-short live n'a été observé.
  **Preuve export local** : `scripts/export_trident_audit_pack.py` exporte les champs P1-07 dans les décisions compactes et les closed trades. Pack vérifié : `server-data/audit_exports/p107_order_block_fields_20260615T1444Z/`; `trident_ac_signal_decisions.jsonl` contient `3488` décisions avec `order_block_shadow_mode=observation_only` (`228` signals, `3260` signal reviews), et `trident_ac_closed_trades.csv` contient les colonnes P1-07 avec `15` closed trades renseignés.
  **Décision après audit shadow** : ne pas promouvoir le veto long order-block et ne pas activer de short live. Le replay P1-07b historique restait intéressant, mais le premier shadow out-of-sample ne confirme ni les shorts défensifs ni le veto long : les longs réellement pris étaient déjà en régime bullish, et les candidats de veto auraient plutôt coûté du PnL en proxy. Les patterns 15m restent rejetés comme bruit ; le combo régime P1-06 + order block P1-07b reste observation-only.
  **Audit frais P1-07 du 2026-06-16** : après fetch A/C et review locale `server-data/reviews/20260616T130640Z/`, `scripts/run_p107_order_block_shadow_audit.py` a produit `server-data/replay_reports/p107_order_block_shadow_audit_20260616T130709Z/`. Couverture `6574` records avec shadow (`424` signals, `6150` signal reviews), `has_bearish_order_block_1h4h=795`, `would_block_long_order_block_shadow=117`, `would_open_defensive_short_order_block_shadow=3`, `live_action_unchanged_false=0`. Les `22` trades fermés avec shadow sont tous non bloqués par P1-07 ; le veto-long reste négatif en proxy (`raw=-15.09 USD`, `dedup=-4.22 USD`, PF `0.00`) et aucun short défensif maturé n'est disponible.
  **Décision finale P1-07** : abandonner le croisement régime + order block comme règle live actuelle. Ne pas promouvoir le veto long, ne pas activer de short et ne pas élargir les patterns 15m/H&S/cup-handle. Les champs shadow peuvent rester comme diagnostics, mais le chantier P1 est clos.
  **Prochaine action P1-07** : aucune modification. Réouvrir seulement si un nouvel audit shadow montre une valeur positive nette et qu'un replay full-bot A/C confirme sur baseline + live.
  **Terminé** : statut final `research_only_no_live_change`.

- [ ] **P1-08 — Guard dynamique par symbole / falling knife**
  **Références** : demande opérateur du 2026-06-15 sur les blocages manuels pendant la chute, ajustements opérateur du 2026-06-05 dans `docs/trident_active_plan.md`, `config/trident.toml` (`hyperliquid.tradable_blocked_symbols`, `pod_a.blocked_symbols`), P1-06 régime, P1-07 order block/patterns, P1-04 slippage/coûts et P2-03 MFE/MAE.
  **Problème à résoudre** : les blocages statiques ajoutés après la chute (`AAVE`, `ADA`, `AVAX`, `HYPE`, `ICP`, `NEAR`, `ONDO`, `PENDLE`, `TON`, `VVV`, `XRP`) ont probablement limité l'hémorragie, mais ils sont réactifs et non viables comme mécanisme de risque permanent. L'objectif n'est pas de décider qu'un coin est "mauvais", mais d'identifier quand un symbole devient temporairement dangereux à l'entrée long (`falling knife`) et quand il peut redevenir tradable sans intervention manuelle.
  **Objectif** : remplacer les blocages post-mortem par un guard dynamique Pod A en shadow d'abord, capable de classer chaque symbole en `normal`, `throttle`, `quarantine` ou `structural_block_candidate`. Les blocklists statiques doivent rester réservées aux exclusions structurelles prouvées : liquidité durablement insuffisante, données non fiables, comportement incompatible sur plusieurs régimes ou décision opérateur explicite.
  **Principe d'action graduée** : `normal` autorise la config courante ; `throttle` garde les entrées possibles mais réduit le cap/notional ou exige une qualité plus forte ; `quarantine` bloque seulement les nouvelles entrées du symbole pendant une durée limitée ; `structural_block_candidate` ne bloque pas automatiquement au début, mais signale qu'un symbole mérite une review manuelle si la contre-performance se répète hors simple régime baissier.
  **État machine à coder en shadow** : entrée en `throttle` si `falling_knife_score>=55` ou si le symbole sous-performe fortement BTC/ETH avec régime crypto défensif ; entrée en `quarantine` si `falling_knife_score>=75`, breakdown multi-timeframe confirmé, ou stop-outs récents concentrés sur le symbole ; maintien minimal `3h` en `throttle` et `6h` en `quarantine` ; sortie seulement si `falling_knife_score<=45` pendant `60m` et si le symbole repasse en force relative positive. Ces seuils sont des hypothèses de départ à rejouer, pas des règles prod.
  **Features pré-entry à utiliser sans lookahead** : `bear_regime_score`, `bull_regime_score` et `regime_gate_decision` de P1-06 ; retours BTC/ETH 1h/4h/24h ; retour local symbole 15m/1h/4h/24h ; force relative symbole vs BTC/ETH ; prix vs EMA/VWAP 15m/1h/4h ; pente EMA et distance EMA ; structure lower-high/lower-low ; breadth/leader trend crypto ; `has_bearish_order_block_1h4h` et signaux P1-07 ; volatilité/ATR et expansion de range ; spread/slippage attendu de P1-04 ; nombre de stops récents, perte réalisée récente, MFE/MAE défavorable et écart perte réelle vs stop planifié de P2-03.
  **Score initial à tester** : construire `falling_knife_score` sur 0-100 avec composantes lisibles plutôt qu'un modèle opaque : régime défensif/baissier (`0-20`), faiblesse relative locale (`0-20`), breakdown structure/EMA/VWAP (`0-20`), volatilité/spread/slippage (`0-15`), bearish order block ou pattern défavorable 1h/4h (`0-15`), stop-outs ou MAE récents du symbole (`0-10`). Exporter aussi les sous-scores pour comprendre chaque décision.
  **Actions shadow à journaliser** : `symbol_guard_shadow_mode=observation_only`, `symbol_guard_state`, `previous_symbol_guard_state`, `falling_knife_score`, `falling_knife_reason`, `would_throttle_dynamic_symbol_guard`, `would_block_dynamic_symbol_guard`, `would_reduce_cap_dynamic_symbol_guard`, `shadow_cap_multiplier`, `quarantine_until`, `quarantine_exit_reason`, `structural_block_candidate`, `symbol_guard_live_action_unchanged=true`. Aucune décision live/risk/exécution ne doit lire ces champs tant que P1-08 n'est pas promu explicitement.
  **Intégration code cible** : créer un module dédié Pod A, par exemple `app/trident/pod_a/dynamic_symbol_guard.py`, partagé entre replay et live ; l'appeler dans `app/live/pod_a_live_runner.py` au même niveau que `regime_shadow` et `order_block_shadow`, avant journalisation des signaux et `setup_details`. Le guard doit être déterministe, testable avec snapshots synthétiques, et ne pas dépendre d'état non persisté sauf pour l'historique rolling des décisions par symbole.
  **Persistance minimale** : conserver un petit état append-only ou JSON runtime par symbole avec `state`, `entered_at`, `last_score`, `reason`, `ttl_until`, `last_exit_check`, compteurs de transitions et derniers sous-scores. Au redémarrage, un symbole en `quarantine` ne doit pas redevenir `normal` par oubli de l'état ; il peut toutefois expirer automatiquement si le TTL et les conditions de sortie sont validés.
  **Replays à lancer avant tout déploiement shadow** : rejouer Pod A full-universe avec cap live sur avril/mai favorable et mai/juin défavorable ; comparer config courante, `throttle_only`, `quarantine_only`, `throttle_then_quarantine`, et variantes de seuils (`55/75`, `60/80`, TTL `3h/6h/12h`). Mesurer PnL net, PF, WR, drawdown, trade-count, winners manqués, pertes évitées, concentration par symbole/date, temps moyen en quarantaine et impact sur les symboles aujourd'hui bloqués.
  **Étape 1 — code local et replay P1-08 réalisés le 2026-06-15** : `app/trident/pod_a/dynamic_symbol_guard.py` implémente le score lisible 0-100 et l'état machine `normal/throttle/quarantine`; `app/live/pod_a_live_runner.py` ajoute les champs `dynamic_symbol_guard` et `symbol_guard_*` dans les signaux, revues filtrées et `setup_details` en observation-only. `scripts/run_p108_dynamic_symbol_guard_replay.py` rejoue les scénarios `current_ac`, `throttle_only_55_cap50`, `quarantine_only_75`, `throttle_then_quarantine_55_75` et `throttle_then_quarantine_60_80` avec caps live. Artefact frais : `server-data/replay_reports/p108_dynamic_symbol_guard_20260615T151221Z/p108_dynamic_symbol_guard_replay.md/json` et `scenario_summary.csv`.
  **Résultat replay P1-08** : baseline avril/mai inchangée sur tous les scénarios (`+77.08 USD` A/C, Pod A `+56.72`, `133` trades A/C, DD Pod A `24.32`) malgré `641` états throttle et jusqu'à `15` blocages counterfactual. Fenêtre live post-baseline `2026-05-14T00:00Z → 2026-06-15T14:12Z` : courant `-31.13 USD` A/C (`Pod A -22.56`, `106` trades A/C, DD Pod A `30.07`), `throttle_only_55_cap50` `-28.87` (delta `+2.26`, DD Pod A `26.93`, `3333` réductions de cap), `throttle_then_quarantine_55_75` identique `-28.87`, et `throttle_then_quarantine_60_80` `-30.02` (delta `+1.11`). `quarantine_only_75` n'améliore rien (`-31.13`) malgré `440` blocages counterfactual : il bloque surtout des candidats qui ne changent pas les trades fermés.
  **Décision après replay local** : ne pas promouvoir `quarantine` et ne pas activer de blocage dynamique live. Le throttle a un effet positif mais trop faible (`+2.26 USD`) pour une règle qui toucherait des milliers d'observations ; il peut seulement passer en shadow live observation-only pour mesurer pertes évitées vs winners manqués sur de vrais signaux, pas en réduction de cap prod.
  **Validation spécifique des coins bloqués le 2026-06-05** : sur `AAVE`, `ADA`, `AVAX`, `HYPE`, `ICP`, `NEAR`, `ONDO`, `PENDLE`, `TON`, `VVV`, `XRP`, vérifier si le guard aurait déclenché `throttle` ou `quarantine` avant les pertes, combien de temps il aurait maintenu le blocage, et à quel moment il aurait autorisé une réintégration. Un coin ne devient `structural_block_candidate` que si sa contribution reste négative après contrôle du régime, des coûts et du timing d'entrée.
  **Fetch / export à mettre à jour** : `scripts/fetch_trident_data.sh` doit agréger les champs `symbol_guard_*` depuis `logs/pod_a_live.jsonl` : couverture shadow, `by_state`, `by_symbol`, durées de quarantaine, `live_action_unchanged_false`, PnL réel des trades que le guard aurait bloqués, winners manqués et pertes évitées. `scripts/export_trident_audit_pack.py` doit exporter ces champs dans les décisions, snapshots et closed trades pour pouvoir refaire l'analyse sans relire les logs bruts.
  **État fetch/export au 2026-06-15** : `scripts/fetch_trident_data.sh` produit déjà `p108_dynamic_symbol_guard_audit.md/json` et `review_summary.md` avec couverture P1-08 ; il lit maintenant `setup_details`, `dynamic_symbol_guard` et l'alias éventuel `symbol_guard_shadow`, afin de capter les signaux, revues filtrées et trades fermés dès que le runner les journalise. `scripts/export_trident_audit_pack.py` exporte aussi `P108_DYNAMIC_SYMBOL_GUARD_FIELDS` dans les décisions compactes et les closed trades.
  **Étape 2 — déploiement shadow observation-only réalisé le 2026-06-15** : déploiement `./deploy.sh --start --mode live --network mainnet`, build Docker OK, preflight Pod A OK (`ready=true`, `unknown_exchange_positions=[]`, `trigger_orders=[]`), preflight Pod C OK (`ready=true`, positions Pod A classées `external_known_positions` côté Pod C), services `trident-api`, `pod-a-live`, `pod-c-live`, `tradfi-funding-collector` et `funding-collector` démarrés. Aucun filtre P1-08 n'est branché dans la décision live : le déploiement ajoute uniquement la journalisation shadow `dynamic_symbol_guard` / `symbol_guard_*`.
  **Fetch post-déploiement** : fetch `server-data/reviews/20260615T160041Z/` encore en `WARN` P1-08 (`0/2000`) car le dernier `pod_a_live.jsonl` rapatrié reste à `2026-06-15T15:54:00Z`, avant le redémarrage, et le status runtime juste après restart indique seulement `records_processed=2`, `signal_count=0`. Review-only locale `server-data/reviews/20260615T160141Z/` confirme que le rapport fetch mentionne désormais P1-08 dans `Next Review Focus`. Pack export vérifié : `server-data/audit_exports/p108_symbol_guard_fields_20260615T1602Z/` ; les colonnes P1-08 sont présentes dans `trident_ac_closed_trades.csv` et `trident_ac_signal_decisions.jsonl`, mais encore vides tant que le runner n'a pas journalisé de nouveaux signaux/revues post-déploiement. La prochaine analyse doit relancer `./scripts/fetch_trident_data.sh --days 1` après quelques cycles Pod A et vérifier que `p108_dynamic_symbol_guard_audit.md` passe à `with_shadow>0` et `live_action_unchanged_false=0`.
  **Fetch / contrôle P1-08 du 2026-06-16** : fetch global puis review-only locale `server-data/reviews/20260616T130640Z/` en `PASS`. `p108_dynamic_symbol_guard_audit.md` montre `2000/2000` records avec shadow dans le tail, `live_action_unchanged_false=0`, score moyen `28.752`, états `normal=1164`, `throttle=742`, `quarantine=94`, gates `would_throttle=836`, `would_block=94`, `structural_block_candidate=89`. Contrôle full-log local : `2586` records avec shadow, mais tous sont des `signal_review` et aucun vrai `signal` accepté n'est encore porteur du guard ; il y a donc assez de données pour valider l'instrumentation, pas assez pour décider `throttle`/`quarantine` en prod.
  **Critères de promotion possibles** : promouvoir d'abord une réduction de cap (`throttle`) avant un blocage complet si les deux améliorent le PnL ; promouvoir `quarantine` seulement si le shadow montre pertes évitées nettes après déduction des winners manqués, drawdown réduit, pas de sur-concentration sur un seul selloff, et retour automatique propre vers `normal`. Aucune promotion ne doit réautoriser automatiquement les shorts ; les shorts restent couverts par P1-06/P1-07.
  **Rollback / sécurité** : garder un kill switch config pour désactiver le guard ; limiter l'effet prod initial à Pod A ; ne jamais fermer une position existante par ce guard, uniquement agir sur les nouvelles entrées ; journaliser toute divergence avec `live_action_unchanged=false` comme erreur tant que le mode est shadow.
  **Tests / preuves attendues** : tests unitaires du score et de l'état machine ; tests de non-régression prouvant que le live reste inchangé en shadow ; replay full-bot sur les deux régimes ; fetch/review avec couverture `symbol_guard_shadow>0`; rapport comparant pertes évitées vs gains manqués ; preuve que les blocklists statiques restantes sont justifiées séparément ou remplacées par le guard dynamique.
  **Tests réalisés localement** : `uv run pytest tests/test_p108_dynamic_symbol_guard_replay.py tests/test_pod_a_dynamic_symbol_guard.py tests/test_pod_a_live_runner.py tests/test_p107_order_block_shadow_audit.py tests/test_p106_regime_shadow_audit.py tests/test_p105_a_grade_replay.py tests/test_reporting.py -q` (`41 passed` après ajout du test export P1-08) ; `uv run python -m py_compile app/trident/pod_a/dynamic_symbol_guard.py app/live/pod_a_live_runner.py scripts/run_p108_dynamic_symbol_guard_replay.py scripts/export_trident_audit_pack.py` ; `bash -n scripts/fetch_trident_data.sh deploy.sh` ; `./scripts/fetch_trident_data.sh --review-only` ; fetch serveur post-déploiement.
  **Prochaine action P1-08** : rester en observation-only et attendre de vrais `signal`/trades Pod A porteurs du guard. Ne pas promouvoir `throttle` ni `quarantine` tant que l'analyse ne mesure pas des pertes évitées nettes contre des winners manqués sur décisions réelles, pas seulement sur revues filtrées.
  **Terminé quand** : soit le guard dynamique est promu explicitement avec seuils, TTL, métriques de replay et shadow live, soit l'idée est abandonnée faute d'amélioration nette. La clôture ne doit pas se faire juste parce que la journalisation existe : il faut une décision sur `throttle`, `quarantine` et le statut des symboles bloqués après la chute.

- [ ] **P1-09 — Recherche factorielle from scratch multi-coins/timeframes**
  **Références** : addendum `2026-06-15` ci-dessous, demande opérateur d'ignorer les patterns existants et de repartir des données brutes, `server-data/live_snapshots/*.jsonl`, `server-data/logs/pod_a_live.jsonl`, `server-data/logs/pod_c_live.jsonl`, `server-data/hip4/logs/hip4_outcome_mainnet_paper`, `tmp/from_scratch_audit.py`, `tmp/from_scratch_audit_summary.json`, P1-03, P1-06, P1-07, P1-08, P2-01 et P2-02.
  **Objectif** : transformer les edges bruts détectés par coin, timeframe, régime, heure et jour de semaine en replay factoriel reproductible, sans toucher au live et sans réutiliser les patterns déjà codés comme hypothèse de départ.
  **Modifs à faire** : créer un script versionné, par exemple `scripts/run_p109_factor_research_replay.py`, qui consomme snapshots 5m, journaux décisions/trades A/C et settlements HIP-4 ; produire un rapport daté dans `server-data/replay_reports/p109_factor_research_<timestamp>/` avec PnL net, PF, WR, drawdown, frais/spread/slippage, trade-count, exposition max, corrélation portefeuille et résultat par mois/régime.
  **Variantes à tester en priorité** : `oil_short_4h_time_gate` (`XYZ:CL/BRENTOIL`, short 240m, régime `chop/mixed/high_vol`, fenêtre `07:00-10:00 UTC`) ; `crypto_alt_short_4h_weak_basket` (short 240m sur `PENGU/TIA/VVV/STRK/ZRO/ICP`, puis `SAGA/DYM` seulement si coûts nets acceptables) ; `crypto_high_vol_rebound_60m` (long court high-vol, sortie rapide, pas de grace longue) ; `gold_short_filter_4h` (d'abord filtre anti-long, puis paper short tiny si edge net) ; `hip4_buy_no_guard` (`BUY_YES only`, `skip BUY_NO`, `skip BUY_NO 6-18h`, gate data quality `book_age_ms/reference_divergence_bps`).
  **Contraintes méthodo** : features strictement pré-entry, séparation temporelle par sous-périodes, coûts réalistes, contrôle du lookahead, comparaison contre baseline full-bot A/C pertinente, limite d'exposition corrélée pour éviter 8 shorts alts dans le même mouvement, et rejet des règles positives uniquement sur une semaine ou sur 2-3 trades.
  **Fetch / export à mettre à jour** : aucun changement immédiat tant que P1-09 reste replay research. Si une variante passe en shadow, ajouter ses champs `p109_*` dans `logs/*`, `scripts/fetch_trident_data.sh` et `scripts/export_trident_audit_pack.py`, avec compteur `live_action_unchanged_false=0` obligatoire. Cette étape est désormais engagée uniquement pour `oil_short_4h_time_gate` en observation-only Pod C, sans action live.
  **Tests / preuves attendues** : tests unitaires du calcul des features/labels sans lookahead ; replay reproductible depuis `server-data/` ; rapport markdown/json ; `uv run pytest` ciblé ; `bash -n scripts/fetch_trident_data.sh deploy.sh` seulement si fetch/export ou deploy sont modifiés.
  **Étape 1 — replay factoriel from scratch réalisé le 2026-06-15** : `scripts/run_p109_factor_research_replay.py` consomme `server-data/live_snapshots`, les journaux fermés A/C et les CSV HIP-4 mainnet paper. Artefact final : `server-data/replay_reports/p109_factor_research_20260615T170016Z/` avec `p109_factor_research_replay.md/json`, `factor_variant_summary.csv`, `factor_trades.csv` et `hip4_policy_summary.csv`. Méthode : forwards snapshots 5m à horizon fixe, coût `16 bps + spread snapshot`, notionnel `200 USD`, déduplication par symbole sur l'horizon, cap d'exposition corrélée `3` positions, features strictement pré-entry. Ce n'est pas encore un replay full-bot A/C avec état d'exécution.
  **Résultats P1-09** : `oil_short_4h_time_gate` est le seul candidat `promouvable_shadow` au niveau research : `96` trades synthétiques, `+27.63 USD`, PF `1.42`, WR `41.7%`, DD `19.59`, max expo `400`, positif en mai (`+23.66`) et juin (`+10.99`) mais négatif en avril (`-7.01`). Les deux symboles contribuent (`XYZ:BRENTOIL +14.32`, `XYZ:CL +13.31`). Il ne peut pas passer shadow directement : il faut d'abord un replay full-bot A/C intégré Pod C qui vérifie l'impact sur la baseline, les conflits avec les longs existants, le cap live et les coûts réels builder-dex.
  **Pistes rejetées après coûts** : `crypto_alt_short_4h_weak_basket` est rejeté (`556` trades, `-46.09 USD`, PF `0.95`, positif seulement en mai et négatif avril/juin malgré cap corrélé) ; ne pas activer de sleeve short alts basket. `crypto_high_vol_rebound_60m` est rejeté (`598` trades, `-232.66 USD`, PF `0.74`, trois mois négatifs) ; les poches positives par symbole sont trop petites pour compenser le mode global. `gold_short_filter_4h` est rejeté (`259` trades, `-74.45 USD`, PF `0.46`, trois mois négatifs) ; ne pas utiliser cette règle comme filtre anti-long ni paper short.
  **HIP-4 P1-09** : policy courante mainnet paper rejetée (`36` settlements, `-26.88 USDC`, PF `0.88`). `BUY_YES only` / `skip BUY_NO` restent `research_only` (`16` settlements, `+65.06 USDC`, PF `2.06`) car le sample est trop faible. `skip BUY_NO 6-18h` reste aussi `research_only` (`30` settlements, `+55.43 USDC`, PF `1.40`) car le résultat n'est pas confirmé sur deux sous-périodes. Les gates data-quality sont positifs mais trop petits (`7` et `2` settlements). Conclusion : aucune promotion HIP-4 ; prolonger l'observation dans P2-01.
  **Clarification importante après challenge opérateur** : les 4 variantes ci-dessus n'étaient pas une recherche exhaustive. Elles étaient les hypothèses priorisées de l'addendum from-scratch. Un screener plus large a donc été ajouté en P1-09b.
  **P1-09b — screener exhaustif réalisé le 2026-06-15** : `scripts/run_p109b_exhaustive_factor_screen.py` scanne les `50` symboles et `546987` buckets snapshots sur horizons `5/15/30/60/120/240/480m`, dans les deux sens, avec coût `16 bps + spread snapshot`. Il croise symbole, cluster, régime, heure UTC, jour de semaine, et des équivalents locaux d'indicateurs publics classiques (`RSI`, `MACD`, moyennes, Bollinger, Donchian, momentum, force relative, VWAP, flow/book, compression et patterns chartistes approximés). Il ne consomme pas TradingView comme source externe : les indicateurs sont recalculés depuis nos snapshots 5m. Artefact : `server-data/replay_reports/p109b_exhaustive_factor_screen_20260615T173017Z/` avec `p109b_exhaustive_factor_screen.md/json`, `top_positive_edges.csv` et `top_all_edges.csv`.
  **Résultats P1-09b** : le screener trouve beaucoup plus de pistes que les 4 variantes initiales (`300` lignes positives top exportées, toutes classées `candidate_next_replay`, pas `promouvable_live`). Les familles les plus propres à rejouer sont : (1) `crypto high_vol short 480m` autour de `00:00-03:00 UTC` (`crypto|high_vol|h00` `+179.74 bps`, PF `3.45`, `1351` obs ; h01/h02/h03 aussi positives) ; (2) fade crypto après expansion extrême (`crypto|bollinger_width20|very_wide` short 480m `+42.46 bps`, PF `1.30`, `37476` obs ; `momentum60/sma20/ema20 deep_positive` short aussi positifs) ; (3) oil short plus large que la fenêtre P1-09 initiale (`oil|chop|h05` short 480m `+61.34 bps`, PF `2.44`, positif 3 mois ; `oil|momentum240|deep_positive` short `+49.62 bps`) ; (4) poches crypto long high-vol horaires (`crypto|high_vol|h19` long 120m `+94.82 bps`, PF `2.94`, et h10/h20 en 240m), ce qui invalide l'idée trop globale du rebound 60m mais garde une piste horaire ; (5) chart-patterns symbol-specific, surtout shorts alts (`TIA absorption_against_flow`, `DYM/SAGA relative_weakness_short`, `VVV/STRK rsi_overbought_fade`), à traiter comme hypothèses fragiles à cause du multiple testing.
  **A ne pas sur-interpréter** : beaucoup de top hits sont des effets `symbol_dow` ou `symbol_hour` (`TON d0 long`, `ONDO d4 short`, `VVV d2 short`, etc.). Ils peuvent capturer une structure réelle, mais aussi une coïncidence de calendrier sur seulement trois mois. Ils ne doivent pas devenir des règles directes. Le statut correct est `candidate_next_replay` : générateur d'hypothèses pour replay full-bot/walk-forward, pas edge validé. Gold ne ressort pas dans le top P1-09b ; silver a un signal Bollinger short mais reste interdit à rouvrir sans P2-02 dédié.
  **P1-09c — implémentation replay complet de chaque famille réalisée le 2026-06-15** : `scripts/run_p109c_pattern_full_replay.py` implémente les 4 patterns initiaux P1-09 et les familles P1-09b dans un replay A/C complet. Méthode : baseline Pod A/C rejouée dans la même passe, overlay synthétique P1-09c via `DirectionalExecutor`, cap live appliqué, skip si A/C ou overlay possède déjà le symbole, time-stop égal à l'horizon du pattern, aucun changement live. Artefact : `server-data/replay_reports/p109c_pattern_full_replay_20260615T180135Z/` avec `p109c_pattern_full_replay.md/json`, `scenario_summary.csv` et `pattern_decisions.csv`.
  **Décisions P1-09c** : seul `initial_oil_short_4h_time_gate` passe en `promouvable_shadow` : total overlay `+51.17 USD` sur `100` trades, baseline `+4.41` (`50` trades), live post-baseline `+46.76` (`50` trades), PF live `2.88`, DD live `7.56`, contribution live équilibrée (`XYZ:BRENTOIL +25.57`, `XYZ:CL +21.19`). Les autres familles ne sont pas promouvables : `crypto_high_vol_short_480` reste `research_only` (`+36.32` total, PF live `1.11`, DD live `134.97`) ; `crypto_high_vol_long_intraday` reste `research_only` (`baseline -14.79`, live `+31.48`, surtout h10) ; `initial_crypto_alt_short_4h_weak_basket` reste `research_only` (`+41.70` total mais `519` trades, PF live `1.04`, DD `143.45`) ; `initial_gold_short_filter_4h` reste `research_only` (`-5.42` total, baseline négative) ; `calendar_cluster` et `calendar_symbol_top_hits` restent `research_only` malgré leurs gains (`+188.59` et `+384.23`) car ce sont des règles pur calendrier/symbole à risque élevé de multiple testing. Sont rejetés : `initial_crypto_high_vol_rebound_60m` (`-313.13`, live `-144.71`), `crypto_expansion_fade_short_480` (`-348.42`), `chart_symbol_specific` (live `-74.98`), `all_non_calendar` (`-166.46`) et `all_candidates` (`-181.74`). Les variantes oil élargies P1-09b et silver n'ont pas produit de sample dans ce replay complet (`0` trade) : elles restent non statuables/research-only.
  **Challenge indicateurs TradingView top 50 — réponse du 2026-06-16** : non, le P1-09b initial ne couvrait pas les `50` indicateurs listés par l'opérateur. Il couvrait déjà une base utile (`RSI`, `MACD`, moyennes, Bollinger, Donchian, momentum/ROC, force relative, VWAP, flow/book, compression et quelques patterns), mais pas toute la liste : manquaient notamment `ATR`, Volume Profile, Fibonacci, Supertrend, Ichimoku complet, Pivot Points, `ADX/DMI`, Stoch RSI, Parabolic SAR, `OBV/MFI/CMF/A-D/PVT/Klinger`, `HMA/WMA/VWMA/ribbon`, `KAMA/ALMA/TEMA/TRIX/TSI/RVI/Vortex/EOM`, Net Volume, Volume Delta, Anchored VWAP et Technical Ratings.
  **P1-09b all-50 relancé le 2026-06-16** : `scripts/run_p109b_exhaustive_factor_screen.py` calcule maintenant des proxies locaux pour les `50` indicateurs TradingView fournis, exposés dans `tradingview_top50_coverage` du JSON. Les proxies volume/L2 restent explicitement basés sur les snapshots TRIDENT 5m (`bucket_notional_usd`, `volume_ratio`, `trade_flow_bias`, ranges 5m), sans consommation TradingView externe. Artefact final : `server-data/replay_reports/p109b_exhaustive_factor_screen_20260616T061841Z/`. Couverture : `548584` buckets, `50` symboles, `50/50` indicateurs marqués `used=true`. Les top hits restent dominés par calendrier/symbole et régime high-vol crypto ; les meilleurs nouveaux hits indicateurs sont `crypto|atr14|high` short 480m (`49584` obs, `+41.68 bps`, PF `1.30`), `crypto|trix|deep_negative` short 480m (`1579` obs, `+139.32 bps`, PF `1.92`), `crypto|bollinger_width20|very_wide`, plusieurs distances MA/adaptatives deep-positive, `crypto|pvt20|deep_positive`, et `equity|pivot_standard|above_r2`.
  **P1-09c all-50 relancé le 2026-06-16** : `scripts/run_p109c_pattern_full_replay.py` rejoue maintenant les nouvelles familles `all50_*` en plus des familles P1-09c précédentes. Artefact : `server-data/replay_reports/p109c_pattern_full_replay_20260616T055925Z/`. Verdict : les nouveaux candidats all-50 n'améliorent pas la décision. `p109c_all50_crypto_vol_trend_short_480` est positif live (`+24.40`, `25` trades, PF `2.34`) mais négatif baseline (`-28.24`, `21` trades), donc `research_only`. `p109c_all50_crypto_ma_pvt_exhaustion_short_480` est positif live (`+28.39`, PF `1.95`) mais négatif baseline (`-16.48`), donc `research_only`. Les pivots equity et le TRIX oil ne produisent aucun trade dans le replay intégré (`0` sample). `all_non_calendar` reste rejeté (`baseline -74.07`, live `-79.77`) et `all_candidates` reste rejeté (`baseline -157.98`, live `-3.48`). Le seul scénario `promouvable_shadow` demeure `initial_oil_short_4h_time_gate` (`+4.41` baseline, `+46.76` live, PF live `2.88`, DD live `7.56`).
  **Décision après relance all-50** : ne pas ouvrir de nouveau shadow crypto/indicator pour P1-09. Les nouveaux signaux all-50 sont utiles comme diagnostic de régime juin (volatilité crypto élevée, fades de MA/adaptive positifs), mais ils ne survivent pas au replay baseline + live intégré. La suite P1-09 reste inchangée : garder uniquement `oil_short_4h_time_gate` en shadow observation-only côté Pod C, et ne pas promouvoir les règles calendrier/symbole déjà rejetées par P1-10 long-historique.
  **Prochaine action P1-09 après replay complet** : ne pas brancher de live trading. Créer une étape shadow dédiée pour `oil_short_4h_time_gate` côté Pod C, en observation-only, avec champs `p109_oil_shadow_*` dans les logs, fetch et export, et compteur `live_action_unchanged_false=0`. Critère minimal avant promotion dry-run/live : au moins `30-50` signaux shadow out-of-sample, PF net > `1.15`, DD contenu, contribution positive sur `XYZ:CL` et `XYZ:BRENTOIL`, pas de conflit avec les longs Pod C existants, et replay full-bot mis à jour après collecte shadow. Les pistes crypto/calendar ne doivent pas passer shadow tant qu'une validation walk-forward séparée ne les isole pas proprement.
  **Étape 4 — shadow oil local préparé le 2026-06-15** : `app/trident/pod_c/oil_shadow.py` implémente le détecteur `oil_short_4h_time_gate` en observation-only pour `XYZ:CL` et `XYZ:BRENTOIL`, avec mapping local du régime Pod C vers les régimes research `chop/mixed/high_vol`. `app/live/pod_c_live_runner.py` ajoute les détails `p109_oil_shadow_*` aux plans, previews, signaux et revues filtrées, sans modifier les décisions, les caps, les ordres ni les positions existantes (`p109_oil_shadow_live_action_unchanged=true`). `scripts/fetch_trident_data.sh` agrège maintenant `p109_oil_shadow_audit.md/json`, et `scripts/export_trident_audit_pack.py` exporte les colonnes `p109_oil_*` dans les décisions compactes et closed trades.
  **Vérification fetch/export P1-09 oil shadow** : review-only locale `server-data/reviews/20260615T185653Z/` OK ; le rapport `p109_oil_shadow_audit.md` est en `WARN` attendu (`with_shadow=0`, `would_open=0`, `live_action_unchanged_false=0`) car le runner Pod C déployé n'a pas encore journalisé ces nouveaux champs. Pack export vérifié dans `server-data/audit_exports/p109_oil_shadow_fields_20260615Tlocal/` : les colonnes `p109_oil_shadow_mode`, `p109_oil_pattern`, `p109_oil_symbol`, `p109_oil_shadow_side`, `p109_oil_shadow_horizon_min`, `p109_oil_shadow_research_regime`, `p109_oil_shadow_hour_utc`, `p109_oil_shadow_score`, `p109_oil_shadow_reason`, `would_open_p109_oil_short_shadow` et `p109_oil_shadow_live_action_unchanged` sont présentes.
  **Étape 5 — déploiement shadow observation-only réalisé le 2026-06-15** : déploiement `./deploy.sh --start --mode live --network mainnet`, build Docker OK, preflight live Pod A OK (`ready=true`, `unknown_exchange_positions=[]`, `open_orders=[]`, `trigger_orders=[]`, `user_stream.ok=true`), preflight live Pod C OK avec les mêmes checks, puis redémarrage de `trident-api`, `pod-a-live`, `pod-c-live`, `tradfi-funding-collector` et `funding-collector`. Le déploiement ne branche aucun short oil live : P1-09 ajoute uniquement la journalisation shadow `p109_oil_shadow_*` en observation-only côté Pod C.
  **Fetch post-déploiement P1-09** : fetch serveur `./scripts/fetch_trident_data.sh --days 1` relancé après déploiement ; review finale `server-data/reviews/20260615T190740Z/review_summary.md` en `WARN` uniquement sur P1-09 oil shadow (`with_shadow=0/4000`, `would_open=0`, `live_action_unchanged_false=0`). L'état API post-déploiement confirme `mode=live`, `started_at=2026-06-15T19:05:43Z`, `enabled_pods=['pod_a','pod_c']`, Pod A/Pod C healthy et `ownership_conflict_count=0`. Le journal Pod C rapatrié s'arrête encore à `2026-06-15T19:04:00Z`, donc avant les premiers cycles post-redémarrage ; le warning P1-09 est attendu et doit disparaître après collecte de nouveaux signaux/revues Pod C.
  **Fetch / contrôle P1-09 du 2026-06-16** : fetch global puis review-only locale `server-data/reviews/20260616T130640Z/` en `PASS`. `p109_oil_shadow_audit.md` montre `877/4000` records avec shadow, `would_open=252`, `live_action_unchanged_false=0`, contribution équilibrée (`XYZ:CL=443`, `XYZ:BRENTOIL=434`) et heures clés couvertes (`07=119`, `08=120`, `09=68`, `10=119`). Contrôle full-log local : les `252` `would_open` sont encore des `signal_review`; proxy 4h short sur les prix Pod C disponible donne `198` candidats raw maturés très positifs, mais seulement `2` trades indépendants après déduplication `240m` par symbole (`+7.71 USD`, un par `XYZ:CL` et `XYZ:BRENTOIL`). Ce résultat valide la télémétrie et le sens du premier épisode, mais ne suffit pas à promouvoir dry-run/live.
  **Tests réalisés localement P1-09 initial** : `uv run pytest tests/test_p109_factor_research_replay.py -q` (`4 passed`) ; `uv run python -m py_compile scripts/run_p109_factor_research_replay.py tests/test_p109_factor_research_replay.py`.
  **Tests complémentaires P1-09b** : `uv run pytest tests/test_p109_factor_research_replay.py tests/test_p109b_exhaustive_factor_screen.py -q` (`8 passed`) ; `uv run python -m py_compile scripts/run_p109_factor_research_replay.py scripts/run_p109b_exhaustive_factor_screen.py tests/test_p109b_exhaustive_factor_screen.py`.
  **Tests complémentaires P1-09c** : `uv run pytest tests/test_p109c_pattern_full_replay.py tests/test_p109b_exhaustive_factor_screen.py tests/test_p109_factor_research_replay.py -q` (`18 passed`) ; `uv run python -m py_compile scripts/run_p109c_pattern_full_replay.py tests/test_p109c_pattern_full_replay.py`.
  **Tests complémentaires P1-09 all-50** : `uv run pytest tests/test_p109c_pattern_full_replay.py tests/test_p109b_exhaustive_factor_screen.py -q` (`19 passed`) ; `uv run python -m py_compile scripts/run_p109b_exhaustive_factor_screen.py scripts/run_p109c_pattern_full_replay.py tests/test_p109b_exhaustive_factor_screen.py tests/test_p109c_pattern_full_replay.py`.
  **Tests complémentaires P1-09 shadow oil** : `uv run pytest tests/test_pod_c_oil_shadow.py tests/test_pod_c_external_reference_shadow.py tests/test_pod_c.py::PodCTests::test_pod_c_live_runner_adds_external_reference_shadow_details tests/test_pod_c.py::PodCTests::test_pod_c_live_runner_adds_p109_oil_shadow_details tests/test_p109_factor_research_replay.py tests/test_p109b_exhaustive_factor_screen.py tests/test_p109c_pattern_full_replay.py tests/test_p110_hyperliquid_history_replay.py -q` (`32 passed`) ; `uv run python -m py_compile app/trident/pod_c/oil_shadow.py app/live/pod_c_live_runner.py scripts/export_trident_audit_pack.py tests/test_pod_c_oil_shadow.py tests/test_pod_c.py` ; `bash -n scripts/fetch_trident_data.sh` ; `./scripts/fetch_trident_data.sh --review-only` ; pack export smoke-test.
  **Prochaine action P1-09 shadow oil** : continuer la collecte jusqu'à au moins `30-50` signaux indépendants dédupliqués/maturés, pas seulement des revues minute corrélées. Ensuite produire un replay/proxy P1-09 oil dédié avec PF net, DD, contribution `XYZ:CL`/`XYZ:BRENTOIL` et conflits éventuels avec les longs Pod C avant toute promotion dry-run/live.
  **Terminé quand** : chaque piste est classée `promouvable_shadow`, `research_only` ou `rejetée`, avec seuils, coût net, sample, concentration, impact sur baseline A/C et décision explicite sur la suite. Aucune règle issue de P1-09 ne doit passer live sans étape shadow/dry-run séparée.

- [x] **P1-10 — Validation long-historique Hyperliquid API + S3 des patterns calendrier/symbole**
  **Références** : P1-09b/P1-09c, challenge opérateur sur les règles calendrier/symbole, API Hyperliquid `candleSnapshot`, archive S3 officielle `hyperliquid-archive` (`market_data/[date]/[hour]/[datatype]/[coin].lz4`, `asset_ctxs/[date].csv.lz4`) et limites documentées (`5000` candles max par intervalle API, S3 requester-pays et possiblement incomplet).
  **Objectif** : sortir les règles `symbol_dow` / `calendar_cluster` de la fenêtre locale de 3 mois et vérifier si elles survivent sur un historique public plus long, sans utiliser les features microstructure TRIDENT (`spread`, `book_imbalance`, `trade_flow_bias`) qui ne sont pas disponibles dans les candles historiques.
  **Implémentation réalisée le 2026-06-15** : `scripts/run_p110_hyperliquid_history_replay.py` récupère via API les candles `1h/4h/1d` pour les symboles P1-09 calendrier/symbole (`BTC/ETH/SOL/NEAR/TON/INJ/ZEC/HYPE/ONDO/VVV/SAGA/PENDLE/TIA/ZRO/STRK/DYM/ICP/PENGU`), tente un probe S3 requester-pays en best-effort, fige les règles P1-09 sans nouvelle optimisation, puis rejoue `symbol_dow_top_hits` et `calendar_cluster` avec coût fixe `16 bps`, notionnel `200 USD`.
  **Artefact P1-10** : `server-data/replay_reports/p110_hyperliquid_history_20260615T183123Z/` avec `api_manifest.json`, `p110_hyperliquid_history_replay.md/json`, `p110_rule_summary.csv`, `p110_rule_trades.csv` et `raw/api_candles/`. Couverture API : `1h` disponible sur `18/18` symboles mais limité à `~208` jours ; `4h` disponible sur `386→836` jours selon listing du symbole ; `1d` disponible sur `504→901` jours. S3 local : `unavailable`, car `aws` et `lz4` ne sont pas installés dans le workspace ; le script garde le chemin requester-pays pour relance sur une machine équipée.
  **Résultats P1-10** : les règles calendrier/symbole sont rejetées sur historique plus long. `symbol_dow_top_hits_1h_daily_open_8h` = `298` trades, `-113.72 USD`, PF `0.83`, pre-local `-190.32`, local P1-09 `+76.59`; `symbol_dow_top_hits_4h_every_bar_8h` = `6247` trades, `-1633.84`, PF `0.90`, pre-local `-2526.88`, local `+893.03`; `symbol_dow_top_hits_1d_1d_hold` = `1161` trades, `-408.74`, PF `0.92`. Même verdict pour les règles cluster : `calendar_cluster_1h_daily_open_8h` `-1307.63`, PF `0.64`; `calendar_cluster_4h_every_bar_8h` `-13295.13`, PF `0.84`; seul `calendar_cluster_1d_1d_hold` est légèrement positif total (`+28.80`) mais négatif pre-local (`-477.79`) et avec DD `2751.64`, donc `research_only` et non exploitable.
  **Décision P1-10** : les forts gains calendrier/symbole de P1-09c étaient très probablement une coïncidence de fenêtre locale / multiple testing. Ne pas passer ces règles en shadow. Garder `oil_short_4h_time_gate` comme seule piste P1-09 promouvable en shadow, car P1-10 ne la valide ni ne l'invalide : elle dépend de symboles builder-dex/oil et de régimes TRIDENT non disponibles dans les candles crypto API simples.
  **Tests réalisés localement** : `uv run pytest tests/test_p110_hyperliquid_history_replay.py -q` (`5 passed`) ; `uv run python -m py_compile scripts/run_p110_hyperliquid_history_replay.py tests/test_p110_hyperliquid_history_replay.py`. Pas de modification fetch/deploy : P1-10 est un replay research local, sans journalisation live.
  **Prochaine action P1-10** : pour exploiter S3 réellement, relancer le script sur une machine avec `aws` + `lz4`, credentials compatibles requester-pays, et `--s3-dates` couvrant plusieurs mois. Utiliser S3 seulement pour enrichir les features L2/asset_ctxs ; ne pas réhabiliter les règles calendrier/symbole tant que le verdict OHLCV long-historique reste négatif.
  **Clôture P1 le 2026-06-16** : P1-10 est clos côté décision P1, car les règles calendrier/symbole sont rejetées sur historique API long et ne doivent pas passer shadow. Une relance S3 reste une option de recherche future, mais elle n'est pas nécessaire pour bloquer ces règles maintenant.
  **Terminé** : règles calendrier/symbole rejetées ; aucune modification live, aucun shadow.

### P2 — Hygiène, recherche et audit continu

- [x] **P2-01 — HIP4 : run review fraîche et cutoffs propres**
  **Références** : F-04, R-08, S-06, addendum C.
  **Modifs à faire** : régénérer automatiquement la run review HIP4 à chaque `trident-hip4/fetch_data.sh`; produire un replay cutoff au timestamp exact d'activation de `prob_stop_full`; garder HIP4 en mainnet paper.
  **Tests / preuves attendues** : review datée du dernier fetch ; métriques PF/Brier/calibration sur les settlements entrés sous policy courante ; aucune promotion tant que PF ≥ 1.15 et Brier ≤ 0.23 ne sont pas atteints sur une tranche suffisante.
  **Terminé quand** : le statut HIP4 est lisible sans ambiguïté à chaque audit pack.
  **Statut 2026-06-16** : terminé côté outillage et décision. `trident-hip4/fetch_data.sh --review-only` produit une review fraîche `server-data/hip4/reviews/20260616T135524Z/hip4_outcome_run_review.md` et met à jour `hip4_outcome_run_review_latest.*`. La review réelle passe en `collect_more_data` : mainnet paper `36` settlements, PnL `-26.8823`, PF `0.8797`, Brier `0.2385`; donc aucune promotion. Le replay policy/market `server-data/hip4/replay_reports/hip4_policy_market_audit_20260616T135436Z.md` inclut maintenant le cutoff `2026-06-10T00:00:00Z` (`prob_stop_full`) : tranche post-cutoff `14` settlements, `+7.5708`, PF `1.1039`, encore sous le seuil `1.15` et échantillon trop court.

- [x] **P2-02 — Pod C research : `gold_070` et silver**
  **Références** : R-09, addendum A/B, action interdite n°5.
  **Modifs à faire** : relancer les multipliers Pod C (`gold_070`, `global_070`, variantes silver) sur une fenêtre étendue incluant juin ; conserver `XYZ:SILVER` bloqué tant que le replay ne prouve pas un edge robuste.
  **Tests / preuves attendues** : rapport comparé à la baseline officielle, activité équivalente, frais inclus, séparation par cluster ; aucune promotion si l'amélioration vient seulement d'un sur-apprentissage gold ou d'une hausse d'activité.
  **Terminé quand** : chaque cluster Pod C a un statut clair : promouvable, à surveiller ou bloqué.
  **Statut 2026-06-16** : terminé côté outillage et décision. Le runner `scripts/run_p202_pod_c_cluster_multiplier_replay.py` régénère les scénarios `current_live_blocked`, `baseline_055`, `global_065`, `global_070`, `gold_070`, `silver_070` et `metals_070`; il neutralise les overrides de routing live pour rester Pod C-only, conserve les frais, et écrit le dernier rapport dans `server-data/replay_reports/p202_pod_c_cluster_multiplier_20260616T150601Z/pod_c_cluster_multiplier_compare.md`. Replay étendu `2026-05-24 -> 2026-06-11` : `baseline_055` silver débloqué `-165.28` net / `61` trades / frais `37.99`; forme prod `current_live_blocked` `-144.45` / `47` trades / frais `34.92` mais toujours négative. Les variantes à promouvoir échouent toutes vs baseline : `global_065` `-191.79` (`68` trades), `global_070` `-209.50` (`94` trades), `gold_070` `-182.25` (`61` trades), `silver_070` `-167.81` (`79` trades) et `metals_070` `-184.78` (`79` trades). Statuts cluster : `gold`, `silver`, `index` et `oil` bloqués ; `equity`/`fx` à surveiller faute de trades. Décision : aucune promotion `gold_070`/`global_070`; `XYZ:SILVER` reste bloqué dans `config/trident.toml`.

- [x] **P2-03 — MFE/MAE et intégrité d'audit**
  **Références** : §10 données manquantes, S-09, R-10.
  **Modifs à faire** : tracker MFE/MAE par trade dans le state/report ; ajouter des checksums SHA-256 par fichier dans `manifest.json`; inclure `docs/trident_active_plan.md` dans les packs d'audit par défaut ; aligner README et config réelle (`pod_c.blocked_symbols`).
  **Tests / preuves attendues** : closed trades avec colonnes MFE/MAE ; manifest vérifiable ; pack d'audit reproductible ; README sans divergence avec `config/trident.toml` et le plan actif.
  **Terminé quand** : les prochaines décisions EFE/time-stop/trailing reposent sur excursions observées, pas seulement sur PnL final.
  **Statut 2026-06-16** : terminé côté instrumentation forward. `DirectionalPortfolioState` tracke désormais `best_price_seen`, `worst_price_seen`, `mfe_bps`, `mae_bps`; les live runners A/B/C les exposent dans `trade_close`, `closed_trade_log` et `open_positions` runtime. Les anciens trades exportés restent vides pour ces colonnes, normal car l'information n'existait pas encore. `scripts/export_trident_audit_pack.py` ajoute `trident_active_plan.md`, un `file_manifest` SHA-256 par fichier, les colonnes MFE/MAE dans `trident_ac_closed_trades.csv`, et tolère les runtime JSON HIP4 vides en les marquant `invalid_json`. Smoke pack OK : `server-data/audit_exports/p2_audit_integrity_20260616Tlocal/`. README aligné sur la config réelle : `pod_c.blocked_symbols = ["XYZ:SILVER"]`.

- [x] **P2-04 — Scripts fetch/deploy : fausses alertes et non-régression**
  **Références** : S-06, R-10, instructions repo sur scripts de déploiement/fetch.
  **Modifs à faire** : corriger le faux `[ERROR] Fetch TRIDENT-HIP4 en erreur (code 0)` ; vérifier que tout changement de journal/export est bien couvert par fetch ; documenter les commandes de review locale.
  **Tests / preuves attendues** : `./scripts/fetch_trident_data.sh --review-only` OK ; `./trident-hip4/fetch_data.sh` OK ; aucun nouveau fichier nécessaire à l'audit n'est absent de `server-data/`; tests shell ou smoke test documenté.
  **Terminé quand** : les fetchs ne produisent plus d'erreur ambiguë et les packs contiennent tous les artefacts requis par ce plan.
  **Statut 2026-06-16** : terminé. `scripts/fetch_all_data.sh` conserve maintenant le vrai code de sortie dans la branche d'erreur, sans faux `code 0`. `trident-hip4/fetch_data.sh` ignore les snapshots API zéro octet pour les raws de review, régénère la run review locale si des logs exploitables existent, ignore proprement les profils vides, et garde les commandes `--review-only` / `--skip-review` utilisables. Smoke wrapper OK : `./scripts/fetch_all_data.sh --review-only --skip-trident --skip-hip4-review`.

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

C'est le point le plus profond et le plus inconfortable. Le WR de l'ère 3 est de 24 %, et 18 closes sur 25 se font en DeadZone/RangeAuction alors que les entrées sont prises en TrendExpansion. Autrement dit : **le système entre sur un signal de tendance, puis le régime se dégrade sous lui.** Aucun réglage d'exit ne corrige un edge d'entrée faible. P1-06 confirme que les shorts globaux ou `bearish` purs détruisent l'edge, mais qu'un short de transition `defensive` est le seul candidat positif en replay live-cap. Le levier n'est donc pas "activer les shorts", c'est **détecter la transition défensive avant l'entrée** puis bloquer/réduire les longs et tester ces shorts uniquement en shadow. C'est là que se trouve le « drastique » réel, mais c'est aussi le plus risqué à toucher.

**4. Pod C : ressusciter la référence externe + maintenir le blocage silver**

F-06 est à la fois un trou de PnL et de sécurité : la référence externe est morte sur 100 % des enregistrements live, donc Pod C trade sans garde-fou de prix et sans la donnée qui faisait son edge en backtest (+79). La rétablir est probablement le geste le plus rentable côté Pod C. Et le silver reste la quasi-totalité de la perte Pod C (~-24, 0 gagnant) — ne pas le réautoriser.

**5. Exécution / slippage**

13.4 bps observés en ère 2 vs 8 supposés. Sur ~140 entrées, l'écart se chiffre en dizaines de dollars. Entrée *spread-aware* (R-06), avec websocket F-07 déjà corrigé par P0-03 et fees/funding désormais backfillés.

**6. Le boost « strong A-grade » — à geler, échantillon faible**

Contre-performant sur les 25 trades attribuables (le boost ×1.4 amplifie les trades perdants), mais l'échantillon ne permet pas de conclure et l'export ne contient même pas les champs A-grade. Geler à 1.0 en attendant le replay R-05, sans détruire le label.

---

Si je devais ne retenir qu'une chose : **le levier le plus drastique n'est pas un meilleur exit, c'est de ne pas être long-only crypto dans une transition défensive.** P1-06 valide l'intuition sur juin mais rejette l'activation globale des shorts et les shorts `bearish` purs. À court terme, la séquence rationnelle est : garder le live inchangé, journaliser un gate `defensive_short_shadow` + `long_not_bear` en shadow/dry-run, puis seulement après out-of-sample décider s'il bloque les longs, réduit le cap, ou autorise un short contrôlé.

Je ne suis pas conseiller financier, et tous les chiffres ci-dessus reposent sur une reconstruction au pas minute, pas sur les fills exchange réels — donc à traiter comme des ordres de grandeur pour prioriser, pas comme des vérités à câbler en dur.

---

# ADDENDUM — 2026-06-15 — Recherche from scratch multi-coins/timeframes

## Resultat audit from scratch

Date: 2026-06-15

## Synthese

J'ai ignore les patterns deja documentes et repris l'analyse depuis les donnees
de marche: snapshots 5 minutes, rendements futurs 15/60/240 minutes, trades A/C
fermes et settlements HIP-4.

Conclusion principale: les donnees ne soutiennent pas un simple renforcement du
long crypto existant. Les edges les plus nets sont plutot:

- short 4h sur une poche d'alts faibles;
- short 4h sur oil, surtout en regime chop/mixed et fenetre 07:00-10:00 UTC;
- short 4h gold en regime downtrend/mixed, edge plus petit mais propre;
- long crypto seulement en rebond court 60m sous high-vol, pas en hold 4h
  generalise;
- HIP-4: BUY_YES tient mieux que BUY_NO; BUY_NO explique la fragilite PnL.

Aucune recommandation ci-dessous ne doit passer live sans replay full-bot
comparable, couts inclus, puis paper/dry-run.

## Donnees et limites

Sources utilisees:

- `server-data/live_snapshots/*.jsonl`: 60 fichiers, du
  `2026-04-05T19:45Z` au `2026-06-15T09:30Z`.
- 543 676 buckets symbole en 5 minutes, collector-only, 50 symboles.
- Horizons testes: 15m, 60m, 240m.
- Clusters: crypto 408 126 buckets, equity 40 665, oil 27 110, index 27 110,
  gold/silver/fx 13 555 chacun.
- `server-data/logs/pod_a_live.jsonl` et `pod_c_live.jsonl`: 170 trades fermes.
- `server-data/hip4/logs/hip4_outcome_mainnet_paper`: 38 trades, 36 settlements,
  51 955 opportunities.

Limites importantes:

- La couverture n'est pas continue: trous notamment `2026-04-19`,
  `2026-04-28 -> 2026-04-29`, `2026-05-09 -> 2026-05-11`,
  `2026-05-17`, puis `2026-05-19 -> 2026-05-23`.
- Les rendements sont bruts; j'affiche l'avg spread observe, mais pas un modele
  complet de fees, slippage, liquidation path ou stop execution.
- Les stats 240m sont autocorrelees par construction; le t-like aide au tri,
  ce n'est pas une preuve IID.
- Le fetch global a produit une review A/C fraiche, mais la partie HIP-4 a ete
  interrompue apres plusieurs minutes sans progression visible. Les fichiers
  HIP-4 locaux recents etaient presents et ont ete analyses directement.

## Edges marche observes

### Crypto

Le signal crypto le plus stable est un biais short 4h sur une poche d'alts.
Les coins stables par mois:

| Coin | Side | Horizon | N | Moyenne brute | Hit | Avg spread | Periodes |
|---|---:|---:|---:|---:|---:|---:|---|
| SAGA | short | 240m | 6 944 | +37.15 bps | 57.3% | 12.18 bps | mai +41.39, juin +33.93 |
| DYM | short | 240m | 6 944 | +33.57 bps | 55.8% | 10.75 bps | mai +52.81, juin +18.98 |
| PENGU | short | 240m | 6 944 | +24.04 bps | 58.8% | 3.43 bps | mai +31.34, juin +18.51 |
| TIA | short | 240m | 6 944 | +20.10 bps | 54.3% | 3.67 bps | mai +29.32, juin +13.12 |
| VVV | short | 240m | 6 944 | +18.03 bps | 53.5% | 3.84 bps | mai +18.56, juin +17.63 |
| STRK | short | 240m | 6 944 | +12.26 bps | 55.0% | 5.76 bps | mai +20.58, juin +5.95 |
| ZRO | short | 240m | 14 344 | +10.37 bps | 54.4% | 3.07 bps | avr +0.44, mai +24.71, juin +4.08 |
| ICP | short | 240m | 6 944 | +8.34 bps | 53.3% | 4.83 bps | mai +8.14, juin +8.50 |

Lecture: le short alt 4h est la meilleure piste crypto from scratch, mais SAGA
et DYM ont un spread moyen eleve. Il faut les traiter en paper/replay net de
couts avant toute conclusion.

Regimes crypto:

- High-vol crypto: long 60m +7.28 bps, stable avril/mai/juin. C'est un rebond
  court, pas un argument pour tenir du long plusieurs heures.
- Mixed crypto: short 240m +11.75 bps, stable avril/mai/juin.
- Broad-up crypto: short 240m +7.97 bps, stable avril/mai/juin. Interpretation
  probable: fade de breadth apres expansion, pas momentum long.

Timing crypto:

- Short 240m fort autour de 00:00-03:00 UTC et 22:00 UTC.
- Long 240m ressort surtout vers 14:00 UTC.
- Par jour UTC: short mercredi/vendredi, long dimanche. A utiliser seulement
  comme variable de replay, pas comme regle autonome.

### Oil

Oil est le candidat le plus propre cote Pod C recherche:

| Symbole | Side | Horizon | N | Moyenne brute | Hit | Avg spread | Periodes |
|---|---:|---:|---:|---:|---:|---:|---|
| XYZ:CL | short | 240m | 13 070 | +11.01 bps | 52.5% | 0.82 bps | avr +9.51, mai +9.85, juin +13.87 |
| XYZ:BRENTOIL | short | 240m | 13 070 | +8.82 bps | 52.2% | 1.12 bps | avr +0.67, mai +10.73, juin +13.73 |

Regime/time:

- Oil chop short 240m: +13.85 bps, stable sur les 3 mois.
- Oil short 240m tres fort a 07:00-10:00 UTC.
- Jeudi/vendredi UTC short oil ressortent nettement.
- Oil 14:00 UTC long 60m existe, mais c'est moins stable que le short 4h.

Recommandation: construire un replay Pod C short oil separe, avec gate
`cluster in {chop,mixed,high_vol}`, fenetre 07:00-10:00 UTC d'abord, puis
extension controlee. Ne pas extrapoler a gold/silver/equity.

### Gold

Gold donne un edge short 4h modeste mais regulier:

- Gold downtrend short 240m: +5.54 bps, hit 54.8%, spread 0.24 bps.
- Gold mixed short 240m: +5.07 bps, hit 54.3%, spread 0.24 bps.
- Pattern generique flow/structure continuation short: +5.06 bps, stable
  avril/mai/juin.

Recommandation: utiliser d'abord comme filtre anti-long ou paper short tiny.
L'edge brut est petit; il peut disparaitre apres fees si l'execution n'est pas
propre.

### Equity / index / silver

Equity:

- Un pattern d'absorption contre flow en short 240m sort a +12.00 bps, mais le
  hit rate est seulement 49.9%. C'est probablement une distribution a queues,
  pas un edge confortable.
- Les horaires equity montrent long 04:00-07:00 UTC et short 10:00-12:00 UTC,
  mais il faut verifier si cela vient de CRCL/NVDA/TSLA et des horaires de
  reference externe.

Index:

- Index chop long 240m: +4.31 bps, hit 59.1%.
- Index hot-vol long 240m: +14.34 bps mais N=721 seulement.

Silver:

- Quelques signaux short 240m ressortent, mais le live recent et la review A/C
  gardent `XYZ:SILVER` bloque. Ne pas reouvrir silver sans replay dedie.

## Trades A/C observes

Depuis les journaux live:

- 170 trades fermes.
- PnL total: -150.09 USD.
- Pod A: -135.09 USD, 141 trades, win rate runtime 34.75%.
- Pod C: -15.00 USD, 29 trades, win rate runtime 27.59%.

Lecture:

- Pod A long crypto `trend_pullback_long`: moyenne -39.2 bps par notionnel.
- Les winners existent mais sont concentres dans les `trailing_stop`
  (+114.9 bps moyen, 49 trades, 93.9% hit).
- Les stops exchange detruisent le profil: `exchange_closed_stop_loss`
  Pod A = -192.1 bps moyen, 47 trades.
- Les pires pertes recentes touchent NEAR, TON, ONDO, ZEC, PENGU, ADA, ENA,
  PENDLE, TIA.

Conclusion execution: le long crypto actuel ne doit pas etre augmente. Si on
cherche de l'edge crypto, la donnees pointe plutot vers un sleeve short 4h sur
alts faibles et un mode long 60m high-vol beaucoup plus selectif.

## HIP-4

Mainnet paper direct CSV:

- 38 trades, 36 settlements.
- PnL net: -26.8823 USDC.
- Gains: +196.6478 USDC, pertes: -223.5301 USDC.
- Worst loss: -49.7638 USDC sur BTC BUY_NO.
- Best win: +31.3643 USDC sur BTC BUY_NO.

Par side:

- MODEL BUY_YES: 16 settlements, moyenne +4.066 USDC, hit 68.8%.
- MODEL BUY_NO: 20 settlements, moyenne -4.597 USDC, hit 25.0%.
- BUY_NO 6-18h avant expiry: moyenne -13.719 USDC, hit 16.7%.
- BUY_YES >18h avant expiry: moyenne +6.029 USDC, hit 66.7%.
- SOL BUY_YES: 3/3 wins, moyenne +14.833 USDC, echantillon trop petit.

Nautilus/data quality:

- `shadow_ready=true`.
- `data_quality.csv`: 149 706 lignes.
- Avg quality_score: 0.810.
- Avg max_book_age_ms: 28 502 ms.
- Raisons dominantes: `book_age_gt_1000ms`, puis divergence reference.

Conclusion HIP-4:

- Ne pas promouvoir mainnet.
- Tester un guardrail `skip BUY_NO` ou au minimum `skip BUY_NO 6-18h`.
- Le gros nombre d'opportunities a edge theorique ne se traduit pas en PnL:
  priorite a calibration + data quality, pas a plus de volume.
- BUY_YES peut rester en observation/paper, surtout pour verifier si SOL/HYPE
  sont de vrais sous-jacents additifs ou juste un mini-sample chanceux.

## Recommandations de recherche

Priorite 1 - Replay short oil Pod C:

- Candidat: `XYZ:CL`, puis `XYZ:BRENTOIL`.
- Horizon cible: 240m.
- Gate initial: oil `chop/mixed/high_vol`.
- Fenetre initiale: 07:00-10:00 UTC.
- Benchmark: baseline full-bot Pod A/C, pas replay isole seulement.
- Decision attendue: si positif net de fees et drawdown acceptable, passer
  paper/dry-run shadow. Pas live direct.

Priorite 2 - Replay crypto alt short 4h:

- Watchlist initiale: `PENGU`, `TIA`, `VVV`, `STRK`, `ZRO`, `ICP`, puis
  `SAGA`/`DYM` seulement si couts reels restent acceptables.
- Regimes candidats: crypto `mixed`, `broad_up` fade, high-vol continuation
  short 240m.
- Exclure les entrees si spread moyen ou instantane rend l'edge net negatif.
- Tester aussi une version portefeuille qui limite la correlation: pas 8 alts
  short ouvertes dans le meme move.

Priorite 3 - Crypto long high-vol 60m:

- Ce n'est pas le long actuel. C'est un mode rebond court.
- Gate: high-vol crypto, horizon 60m, sortie rapide, pas grace longue.
- Objectif: capter les rebounds sans accepter les stops catastrophe 4h.

Priorite 4 - Gold short filter:

- Utiliser comme filtre anti-long gold ou paper short micro.
- Edge brut faible; ne pas live avant preuve nette fees incluses.

Priorite 5 - HIP-4 side guard:

- Backtester `BUY_YES only`, `skip BUY_NO`, `skip BUY_NO 6-18h`, et gate par
  data quality (`book_age_ms`, `reference_divergence_bps`).
- Garder `prob_stop_full` actif en paper; ne pas reactiver une politique qui
  cree du churn/re-entry sans preuve.

## Evos a faire/tester par priorite

Correspondance plan priorise: **P1-09 — Recherche factorielle from scratch multi-coins/timeframes**.

Step 1 - Construire un replay de recherche factoriel:

- Objectif: transformer les pistes ci-dessus en hypotheses testables sans
  toucher au live.
- Input: snapshots 5m + journaux decisions/trades + settlements HIP-4.
- Output attendu: un rapport date dans `server-data/replay_reports/` avec, pour
  chaque variante, PnL net, PF, drawdown, fees, hit rate, nombre de trades,
  exposition max, et resultat par mois.
- Variantes a tester dans l'ordre:
  - `oil_short_4h_time_gate`: `XYZ:CL/BRENTOIL`, short, 240m,
    regime `chop/mixed/high_vol`, fenetre 07:00-10:00 UTC.
  - `crypto_alt_short_4h_weak_basket`: short 240m sur
    `PENGU/TIA/VVV/STRK/ZRO/ICP`, puis ajout conditionnel `SAGA/DYM` si le
    cout net reste positif.
  - `crypto_high_vol_rebound_60m`: long 60m uniquement en high-vol, sortie
    rapide, aucun stop grace long.
  - `gold_short_filter_4h`: d'abord filtre anti-long, puis paper short tiny si
    le replay reste positif net.
  - `hip4_buy_no_guard`: `BUY_YES only`, `skip BUY_NO`, `skip BUY_NO 6-18h`,
    et gate data quality.
- Critere de passage au step suivant: positif net de fees sur au moins deux
  sous-periodes, drawdown acceptable, pas de degradation de la baseline full-bot
  A/C, et sample suffisant pour eviter une decision sur 2-3 trades.
- Critere de rejet rapide: edge positif uniquement sur une seule semaine, ou
  profit absorbe par spread/fees, ou correlation trop forte entre positions.

## A ne pas faire maintenant

- Ne pas augmenter le sizing long crypto existant.
- Ne pas reactiver des shorts globaux sans replay full-bot.
- Ne pas rouvrir `XYZ:SILVER` live.
- Ne pas transformer les horaires/jours en regles directes: ce sont des
  variables de replay.
- Ne pas conclure que HIP-4 a un edge exploitable tant que BUY_NO et la data
  quality ne sont pas corriges.

## Artefacts

- Script d'audit temporaire: `tmp/from_scratch_audit.py`.
- Resume machine-readable: `tmp/from_scratch_audit_summary.json`.
- Review A/C fraiche consultee: `server-data/reviews/20260615T093431Z/review_summary.md`.
