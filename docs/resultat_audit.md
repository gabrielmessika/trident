# Audit complet TRIDENT — 2026-06-11

Auditeur : revue externe trading/quant, architecture et sécurité opérationnelle.
Sources : `trident_audit_pack_light_20260611.zip` (état runtime/fetch du 2026-06-11, prioritaire) + repo public `github.com/gabrielmessika/trident` (clone `main`, commit `1d8ba57`).
Fichiers exclus du pack (assumé) : `trident_ac_signal_decisions.jsonl`, `hip4_decisions.jsonl`, `trident_ac_live_state_pod_a/c.json`. Toute conclusion dépendant des décisions brutes est marquée `needs_raw_decisions` ; toute conclusion dépendant des fills de sortie exchange est marquée `needs_exchange_reconciliation`.

---

## 1. Verdict global

| Périmètre | Verdict | Justification courte |
| --- | --- | --- |
| **TRIDENT A/C — opérationnel** | **PASS avec réserves** | Review `2026-06-11T13:59:56Z` : status PASS, mode live mainnet, reconciliation ready, 0 conflit ownership, cap 200 USD actif (fait observé : `trident_ac_review_summary_latest.md`). |
| **TRIDENT A/C — PnL** | **WARN / partiellement `insufficient_data`** | Fenêtre attribuable (31 trades fermés, 06-09 → 06-11) : Pod A **-5.90 USD** (PF 0.60, WR 24 %), Pod C **+0.46 USD**. Mais le PnL live cumulé runtime est **Pod A -134.27 USD / Pod C -14.21 USD** et seule la queue récente est attribuable : ~90 fermetures antérieures ne sont dans aucun export (`closed_trade_log` = buffer). Divergence majeure vs baseline replay (+780.72 / +79.11) **non expliquée à ce stade**. |
| **TRIDENT-HIP4 — mainnet paper** | **KO pour promotion, OK pour collecte** | 27 trades / 25 settlements, PnL **-47.84 USDC**, PF 0.71, Brier 0.2611 (> seuil 0.23 et > 0.25 d'un prédicteur naïf). Run review : `collect_more_data`. Les cutoffs récents (≥ 06-02) sont négatifs **pour toutes les policies**, y compris `prob_stop_full` shadow. |
| **TRIDENT-HIP4 — mainnet observer** | OK (signal-only) | 0 ordre, `observer_mode_signal_only` confirmé dans runtime statuses. |
| **Sécurité** | **KO — 2 findings critiques** | (1) Clé privée `HIP4_OUTCOME_SECRET_KEY` committée dans `.env.trident` du **repo public** (fichier git-tracked, vérifié sur le clone). (2) API HTTP sans authentification avec endpoint mutant `POST /api/routing/override`, publiée `3000:3000` sur toutes les interfaces, pilotant un bot **live mainnet**. |
| **Readiness opérationnelle** | WARN | Guardrails live bien conçus (live confirm, cap, protective orders, reconciliation stricte, `pending_position` durable post-incident ARB). Mais : exposition réseau de l'API, script fetch avec erreur ambiguë `code 0`, `reference_equity_usd=0.0` dans le report runtime, run review HIP4 non régénérée après fetch. |
| **Qualité des données** | WARN | Exploitable pour first-pass : closed trades A/C, trades/settlements/replays HIP4, baselines. Manquant : close fills exchange (0 ligne), historique complet des trades fermés, MFE/MAE, funding réel par trade, fees dans `fill_events` (≈0 après 05-24), `external_reference_*` vides côté Pod C. |

**Verdict en une phrase** : le système est techniquement sain et bien instrumenté pour un projet de cette maturité, mais (a) la sécurité du repo public doit être corrigée **avant toute autre action**, (b) le PnL live A/C cumulé (-148 USD vs +860 en replay) n'est attribuable que sur sa queue récente, ce qui interdit toute conclusion de promotion/augmentation de capital, et (c) HIP4 n'est pas promouvable.

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
- **Action** : publier `127.0.0.1:3000:3000` dans les compose (accès via tunnel SSH), et/ou ajouter un token d'auth sur tous les endpoints (au minimum sur le POST), et confirmer la règle firewall. Tester ensuite que `fetch_*_data.sh` fonctionne toujours.

### F-03 — PnL live cumulé A/C non attribuable (sévérité : **HAUTE / P0 data**)
- **Fait observé** : runtime Pod A `realized_pnl_usd=-134.27` (115 fills, WR 0.356), Pod C `-14.21` (23 fills, WR 0.261), alors que l'export ne contient que 31 trades fermés (-5.90 / +0.46) couvrant 06-09 → 06-11. `close_fills=0` partout. Les ~90 fermetures précédentes (depuis ~05-24) n'existent dans aucun fichier fourni.
- **Conséquence** : la divergence live (-148 USD cumulé) vs baseline replay (+859.83) est **le** sujet PnL n°1 du projet et il est aujourd'hui impossible de dire si elle vient du régime de marché, du modèle, du sizing, de l'exécution (slippage observé > hypothèse, cf. §3) ou des changements de config successifs (cap 933→500→250→200, blocklist élargie en cours de route — visibles dans `fill_events`). Statut : `needs_exchange_reconciliation` + `needs_data` (historique closed trades complet).

### F-04 — HIP4 : le delta shadow `prob_stop_full` (+237 USDC) ne survit pas aux cutoffs récents (sévérité : MOYENNE)
- **Fait observé** : sur la fenêtre complète, `prob_stop_full` shadow = +189.58 vs active -47.84 (delta +237.41). Mais cutoff ≥ 2026-06-02 : active -29.75, `prob_stop_full` -27.92, `hold_to_settlement` -71.72 — **toutes négatives**. Le runtime confirme que `active_policy=prob_stop_full` est déjà en place ; le -47.84 « active_paper » reflète largement l'ère de l'ancienne policy `bid_over_conservative_hold_ev` (14 exits, +40.90) qui coupait les gagnants. La perte récente est donc un problème de **modèle/edge**, pas seulement d'exit.

### F-05 — Pod A : le boost « strong A-grade » est contre-performant sur la fenêtre live attribuable (sévérité : MOYENNE, échantillon faible)
- **Fait observé** : strong A-grade = 10 trades, **-9.43 USD**, WR 10 % ; standard = 13 trades, +0.92 ; sans A-grade = 2 trades, +2.61 (2/2 gagnants). Le strong A-grade reçoit un size scale jusqu'à 1.40 et une stop grace 120 min — il amplifie donc précisément les trades qui perdent sur cette fenêtre. 25 trades = échantillon insuffisant pour conclure, mais c'est l'inverse exact de l'hypothèse evo11 promue en baseline (+190.14 en replay). Statut : `needs_replay` sur fenêtre récente + `needs_data`.

---

## 3. Audit PnL TRIDENT A/C

Source : `trident_ac_closed_trades.csv` (31 trades, 06-09 → 06-11), `trident_ac_fill_events.csv` (141 open fills, 05-24 → 06-11), `trident_ac_runtime_summary.json`, review du 2026-06-11. Attribution **applicative first-pass** ; tout ce qui dépend des prix exacts de sortie est `needs_exchange_reconciliation`.

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

**Stops planned vs actual** : sur les 5 `exchange_closed_stop_loss`, ratio perte réelle / perte planifiée entre 0.82 et 0.97 → **`within_plan`**, cohérent avec la review (actual -7.10 vs planned -8.06, excess +0.96 en faveur). Le slippage de sortie n'est donc pas le problème sur ces 5 trades. Une seule anomalie : le `stop_hit` BTC du 06-09 (-1.16 réel vs -0.90 planifié, ratio **1.29 = `mild_excess`**) — à reconcilier fill-by-fill (`needs_exchange_reconciliation`).

**Par confidence / A-grade / régime** :

| Bucket confidence | n | PnL | WR |
| --- | ---: | ---: | ---: |
| < 0.62 | 10 | -0.63 | 40 % |
| 0.62–0.70 | 8 | +3.91 | 25 % |
| 0.70–0.78 | 5 | **-7.15** | 0 % |
| ≥ 0.78 | 2 | -2.03 | 0 % |

La haute confiance (≥0.70 : 7 trades, -9.18, 0 % WR) est strictement perdante sur la fenêtre, et c'est elle qui reçoit la taille maximale (pas de quality-sizing réducteur + boost A-grade). Combiné au finding F-05 (strong A-grade -9.43), le mécanisme « plus de conviction → plus de taille » est inversé en live récent. Échantillon faible, mais le signal est cohérent sur deux axes indépendants.

Côté régime, 18/25 fermetures se font en `DeadZone` et 5 en `RangeAuction` : les entrées (faites en TrendExpansion d'après les `fill_events`) survivent rarement à la dégradation du régime — cohérent avec un marché récent sans suivi de tendance, et avec la performance négative de toutes les périodes courtes (PnL négatif sur <15m, 15-60m et 1-6h ; seul le trade >6h est gagnant).

**Coûts d'exécution (fait observé, `fill_events`)** : slippage d'ouverture Pod A moyenne **11.7 bps**, médiane 7.3, max **60.5 bps** — au-dessus de l'hypothèse de 8 bps open utilisée pour le live et les replays. Pod C : 3.25 bps en moyenne. Sur des entrées IOC taker sur alts peu liquides (PENGU, BIO, ZRO, STRK…), le coût d'entrée seul peut absorber une fraction significative de l'expectancy d'un setup à stop 45–130 bps. Par ailleurs `fee_usd≈0` dans les fill events après le 05-24 : la capture des fees dans l'export est défaillante (`needs_data`).

**Incohérences de config dans le temps (fait observé)** : les fill events montrent des notionals à 933/500/250 USD avant le 06-06 et des fills sur AVAX, AAVE, ONDO, HYPE, ICP (aujourd'hui bloqués). Le cap 200 et la blocklist actuelle ne s'appliquent que sur la fin de la fenêtre. Toute analyse du PnL cumulé -134.27 devra segmenter par époque de config — un changelog horodaté des changements de config est nécessaire (`needs_data`).

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

### 3.3 Ce qui reste bloqué sans close fills exchange
- Slippage et fees réels de sortie (l'attribution actuelle utilise les prix applicatifs).
- Confirmation du `mild_excess` BTC stop_hit.
- Décomposition gross/fees/funding du -134.27 cumulé.
- Vérification qu'aucune fermeture exchange n'a divergé du state store (post-incident ARB).

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
- **Points faibles** : API stdlib monolithique (~8 000+ lignes dans `api.py`) sans auth (F-02) ; dérive doc (README vs config réelle, ex. blocked_symbols Pod C) ; `closed_trade_log` non persistant au-delà du buffer runtime (F-03) ; le régime v2 existe mais inactif — la détection legacy (ADX 22 / structure 0.30) est l'unique gate de régime alors que la fenêtre récente montre des entrées TrendExpansion qui meurent en DeadZone.

---

## 7. Audit sécurité et exploitation

| # | Domaine | Constat | Sévérité |
| --- | --- | --- | --- |
| S-01 | Secrets | **Clé privée committée dans le repo public** (`.env.trident`, git-tracked) + `.gitignore` n'ignorant pas `.env.trident` (cf. F-01). Aucune trace de secret dans les logs/exports du pack (`contains_secrets=false`, vérifié par grep) ; le code ne logge pas les clés (lecture env uniquement, `private_state.py`). | **Critique** |
| S-02 | API/dashboard | Pas d'authentification, endpoint mutant `POST /api/routing/override`, publication `3000:3000`/`3001` toutes interfaces (cf. F-02). `log_message` désactivé → pas de trace d'accès non plus. | **Critique** |
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

### R-02 — Verrouiller l'API (bind 127.0.0.1 + auth + firewall) — **P0, `ready`**
- **Périmètre** : `docker-compose.trident.yml` / `docker-compose.hip4.yml` / `app/observability/api.py`.
- **Preuve** : F-02 (POST routing override non authentifié, publication toutes interfaces, bot live mainnet).
- **Impact PnL** : élimine un vecteur de fermetures forcées / manipulation de routing par un tiers.
- **Risque introduit** : casser `fetch_*_data.sh` si ces scripts interrogent l'API à distance → tester ; tunnel SSH à documenter.
- **Test requis** : fetch complet OK après changement ; scan externe du port 3000/3001 fermé.
- **Rollback** : revert compose (mais ne pas le faire sans auth).

### R-03 — Persistance des close fills exchange + historique complet des trades fermés — **P0, `needs_data` (c'est la donnée elle-même)**
- **Périmètre** : `app/execution/live.py` (journaliser les fills de fermeture comme les ouvertures), `export_trident_audit_pack.py`, fetch.
- **Preuve** : F-03 (`close_fills=0`, -134.27 cumulé inattribuable, `closed_trade_log` = buffer).
- **Impact PnL** : indirect mais maximal — c'est le prérequis pour expliquer la divergence live/replay (-148 vs +860) et toutes les décisions de sizing futures.
- **Risque introduit** : néant (instrumentation).
- **Test requis** : un cycle open/close live tiny vérifié fill-by-fill exchange vs state store vs closed trade ; backfill des fills historiques via l'API user fills HL si possible.
- **Rollback** : n/a.

### R-04 — Replay full-bot de la fenêtre live récente (24-05 → 11-06) avec la config courante — **P1, `needs_replay`**
- **Périmètre** : Pod A + Pod C, baseline comparée : live réel de la même fenêtre (et non +859.83, qui couvre une autre période).
- **Preuve** : §5.4 — trois hypothèses (régime, coûts, dérive config) non départagées.
- **Impact PnL** : si H2 (coûts) explique l'écart, l'action corrective (R-06) vaut plusieurs dizaines d'USD/mois au sizing actuel ; si H1 (régime), la bonne action est un gate de régime plus strict, pas un changement de setup.
- **Données manquantes** : input JSONL multisource couvrant la fenêtre (à collecter/assembler), changelog horodaté des changements de config live.
- **Test requis** : replay avec slippage 8 bps vs 12 bps vs slippage observé par symbole ; écart replay vs live < tolérance définie.
- **Rollback** : n/a (diagnostic).

### R-05 — Geler le boost de taille « strong A-grade » à 1.0 en live, conserver le label — **P1, `needs_replay`** (ne pas appliquer avant le replay)
- **Périmètre** : Pod A live sizing (`a_grade` size scale 1.25/1.40).
- **Preuve** : F-05 (strong A-grade 10 trades -9.43, WR 10 % ; confidence ≥0.70 : 0 % WR) — contre-performance là où la taille est maximale, sur la fenêtre attribuable.
- **Impact PnL attendu** : sur les 25 trades observés, ramener le scale strong à 1.0 aurait réduit la perte d'environ 2–3 USD (ordre de grandeur, attribution applicative) ; surtout, réduit la variance pendant le burn-in.
- **Risque introduit** : si le régime redevient TrendExpansion durable, on renonce au +190.14 que le boost a produit en replay avril–mai — d'où l'exigence de replay.
- **Données manquantes** : ≥ 50 trades live A-grade ; MFE/MAE.
- **Test requis** : replay full-bot fenêtre récente avec scale {1.0, 1.25, 1.40} comparé à la baseline officielle **et** à la fenêtre récente ; critère : le boost doit être ≥ neutre sur les deux.
- **Rollback** : restaurer 1.25/1.40 si le replay récent contredit le live (échantillon trop faible).

### R-06 — Réduire le coût d'entrée Pod A (slippage 11.7 bps observé vs 8 supposés) — **P1, `needs_replay` + `needs_exchange_reconciliation`**
- **Périmètre** : `LiveExecutionVenue` entrées IOC ; option : prix limite à mid+X bps borné, ou skip si spread > seuil au moment du fill, ou exclusion des alts à slippage récurrent > 20 bps (PENGU/BIO/STRK…).
- **Preuve** : `fill_events` — moyenne 11.7 bps, p75 15.2, max 60.5 sur 118 ouvertures Pod A ; stops 45–130 bps → 10–25 % du risque consommé à l'entrée.
- **Impact PnL attendu** : ~3–4 bps de coût moyen économisés ≈ 0.4–0.8 USD par tranche de 20 trades au sizing actuel ; surtout structurel à plus gros sizing.
- **Risque introduit** : fills manqués (IOC limite plus stricte) → moins de trades ; à mesurer.
- **Test requis** : replay avec modèle de coût recalibré ; A/B dry-run sur le taux de fill.
- **Rollback** : seuils de slippage relâchés si fill rate < 80 % des signaux acceptés.

### R-07 — `early_failure_exit` : replay avec/sans avant tout réglage — **P1, `needs_replay`**
- **Périmètre** : Pod A live (EFE pendant la stop grace).
- **Preuve** : 10/25 trades, -5.87, coupe à ~70 % du stop planifié ; impossible de savoir combien auraient récupéré.
- **Impact PnL** : borne haute si EFE ne coupait que des trades qui touchent le stop : +0.25 USD/trade EFE économisé (différence -0.59 vs -0.84) ; borne basse négative si beaucoup récupéraient.
- **Données manquantes** : MFE/MAE post-exit (P1 du registre des gaps).
- **Test requis** : replay full-bot avec EFE on/off sur la baseline officielle ET la fenêtre récente.
- **Rollback** : conserver EFE (réglage actuel) par défaut — c'est l'option défensive.

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
5. **Reconciliation exchange fill-by-fill** d'au moins un cycle live récent dès que R-03 est instrumenté ; backfill des user fills historiques pour expliquer le -134.27.
6. **Re-run multipliers Pod C** (dont `gold_070`) sur une fenêtre étendue incluant juin (R-09).
7. Tests d'exploitation : scan externe ports 3000/3001 après R-02 ; gitleaks sur l'historique complet après R-01 ; test crash/restart (kill du runner entre fill et protective order, vérifier la récupération via `pending_position`).

---

## 10. Données manquantes

| Donnée | Fichier attendu | Pourquoi bloquant | Sévérité | Comment la produire |
| --- | --- | --- | --- | --- |
| Fills exchange de fermeture A/C | `trident_ac_exchange_fills.csv` | Reconciliation fill-by-fill des sorties ; tout `needs_exchange_reconciliation` en dépend (mild_excess BTC, fees/slippage réels de sortie). | **P0** | Journaliser les close fills dans `LiveExecutionVenue` + backfill via endpoint user fills Hyperliquid. |
| Historique complet des trades fermés depuis le début du live | `trident_ac_closed_trades_full.csv` | -134.27 / -14.21 cumulés inattribuables ; `closed_trade_log` n'est qu'un buffer. | **P0** | Persister chaque trade fermé en append-only (JSONL/CSV) côté serveur ; reconstruire le passé depuis les user fills exchange. |
| Changelog horodaté des configs live | `config_changelog.md` | Le cap (933→500→250→200) et la blocklist ont changé pendant la fenêtre ; impossible de segmenter le PnL par époque. | P1 | Versionner chaque changement avec timestamp (git tag ou journal dédié). |
| MFE/MAE par trade | colonne dans closed trades | Indispensable pour juger EFE, time stops, trailing. | P1 | Tracker high/low depuis l'entrée dans le portfolio state. |
| Funding réel + fees réels par trade | colonnes closed trades / fill events | `fee_usd≈0` après 05-24 ; net PnL approximatif. | P1 | Corriger la capture des fees dans le journal de fills ; joindre funding payments exchange. |
| Références externes Pod C à l'entrée | `external_reference_*` peuplés | Tous à False/0 dans l'export : qualité du signal TradFi invérifiable. | P1 | Corriger la jointure dans `export_trident_audit_pack.py` ou vérifier la collecte. |
| Décisions brutes A/C et HIP4 | `*_signal_decisions.jsonl`, `hip4_decisions.jsonl` | Analyse fine des rejets (shock guard net effect, market_already_open opposite-side, pattern EFE-watchers) — exclus du pack léger. | P1 | Fournir dans un pack complet ; conclusions concernées marquées `needs_raw_decisions`. |
| Input replay fenêtre récente | `external_reference_multisource_20260524_20260611.jsonl` | Sans lui, R-04/R-05/R-07 impossibles. | P1 | Assembler depuis les snapshots live fetchés. |
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
8. Les actions R-01 et R-02 (rotation de clé, verrouillage API) sont les seules recommandées **immédiatement**, et elles n'envoient aucun ordre.

---

*Limites de cet audit : attribution PnL A/C applicative first-pass (close fills exchange absents) ; décisions brutes exclues du pack léger ; échantillons live faibles (25 trades Pod A, 6 Pod C, 25 settlements HIP4) — aucun chiffre de cette fenêtre ne doit être extrapolé sans replay. Baseline de comparaison utilisée partout : +859.83 USD (officielle 2026-05-13), le replay +872.74 du 2026-05-19 n'étant cité que pour la nuance HYPE.*

---
---

# ADDENDUM — 2026-06-11 (pack `trident_missing_pnl_data_20260611.zip`)

Cet addendum intègre les données complémentaires : décisions brutes A/C (193 697 lignes, 24-05 → 11-06), décisions HIP4 (93 610 lignes), live states A/C, statuts runtime, snapshots minute 24-05 → 11-06 (~2 Go), et `docs/trident_active_plan.md` (chronologie opérateur). Les sections ci-dessous **révisent** le rapport principal ; en cas de conflit, l'addendum prévaut.

## A. Le PnL cumulé -134.27 / -14.21 est maintenant expliqué à ~85 % — F-03 largement résolu

**Fait établi n°1 : le live mainnet A/C démarre le 2026-05-24.** Les décisions brutes commencent au `2026-05-24T16:05Z` (ligne 1 du journal pod A), les fill events comptent 118 ouvertures pod A + 23 pod C, et la review serveur affiche `total_fill_count` 115/23. Le cumul -134.27 / -14.21 USD couvre donc **exactement la fenêtre 24-05 → 11-06** dont nous avons désormais les entrées, les décisions et les prix minute. Il n'y a pas d'historique antérieur caché.

**Méthode de reconstruction.** Les close fills exchange restent absents (R-03 inchangé). J'ai donc reconstruit les sorties des 110 trades manquants en rejouant chaque position (long-only) contre les snapshots minute (prix + best bid), avec les règles de sortie **de l'époque de chaque trade** (voir ères ci-dessous), sortie au bid en cas de gap sous le stop. Calibration sur les 31 trades dont la vérité terrain existe : pod A réel -5.90 vs simulé -9.88 ; les raisons de sortie simulées correspondent trade par trade (trailing/BE/stop). La reconstruction est une **estimation**, pas une réconciliation : pod A estimé **-104.6** vs réel -134.27 (79 % de la perte expliquée trade par trade ; l'écart résiduel ≈ -30 USD est compatible avec les frais non capturés [~15-25 USD pour ~230 fills taker], le funding, le slippage intra-minute et l'absence d'EFE dans la simulation pré-09-06).

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
- **F-03 rétrogradé de P0-data à P1** : l'attribution est faite à ~85 % en estimation. R-03 (backfill des user fills Hyperliquid + journal append-only) reste nécessaire pour passer de l'estimation à la réconciliation exacte, mais ce n'est plus un trou noir.
- **F-05 (boost strong A-grade) : statut inchangé, vérification élargie impossible** — l'export de décisions ne contient pas les champs `a_grade_*` (nouveau gap, voir F-08). Le paradoxe A-grade reste établi uniquement sur les 25 trades de l'ère 3. En revanche, l'analyse par ère **renforce R-05/R-07 indirectement** : les correctifs du 09-06 (grace courte, cat stop plafonné, EFE) ont déjà réduit la perte moyenne par trade de ~-1.0 à -0.24 USD ; le problème résiduel de l'ère 3 est le WR (24 %) et la qualité d'entrée, plus la queue des stops.
- **R-06 (slippage)** : slippage d'entrée moyen par ère : 4.2 bps (ère 1) → **13.4 bps (ère 2)** → 9.8 bps (ère 3). La dégradation coïncide avec les caps plus hauts et le selloff ; l'hypothèse 8 bps du replay reste trop optimiste.

## B. Nouveaux findings

### F-06 — La référence externe Pod C est morte en live (sévérité : **HAUTE**)
`external_reference_available = False` sur **100 % des 79 744 enregistrements pod C** de la fenêtre (pas seulement les 6 trades fermés : ce n'est pas un bug d'export, le flux n'alimente pas le runtime). Or les baselines officielles (+79.11 pod C) ont été rejouées sur un input `external_reference_multisource_*` qui contient ces références. Double conséquence : (1) **parité live/replay rompue** — cause H4 à ajouter au triage de la divergence live/baseline, au même rang que H1/H2/H3 ; (2) **trou de sécurité fonctionnel** — Pod C trade des perps TradFi builder-dex sans garde-fou de prix externe (dislocation/staleness indétectable). À corriger avant tout élargissement Pod C ; les replays R-04/R-09 doivent être exécutés dans les deux modes (avec et sans référence) pour mesurer l'impact.

### F-07 — Le websocket `user_order_updates` se reconnecte à quasi chaque message (sévérité : MOYENNE)
Pod A : 2 816 reconnexions pour 2 819 messages ; Pod C : 2 809 / 2 829. Le flux de confirmations d'ordres est donc en pratique en mode reconnexion permanente. C'est un suspect direct pour la **non-capture des frais** dans les fill events (constat du rapport principal) et un risque de fills manqués au moment précis d'un stop — cohérent avec l'incident ARB du 07-06 (position fillée non persistée avant crash). À corriger avec R-03.

### F-08 — L'export de décisions n'emporte pas les champs A-grade/sizing (sévérité : BASSE, bloque l'analyse)
`setup_details` y est tronqué à un sous-ensemble de features ; `a_grade_active/level/score/size_scale` et le sizing qualité sont absents. Ajouter ces champs à `export_trident_audit_pack.py` (fusionner dans R-10) pour permettre l'analyse A-grade plein échantillon réclamée par R-05.

## C. HIP4 — décisions brutes : conclusions du rapport principal confirmées, rien d'inattendu
Les 93 610 décisions (34 030 paper / 59 580 observer) recoupent exactement les 27 trades paper : 27 approbations (15 BUY_NO / 12 BUY_YES), dont 7 le seul 24-05 (l'épisode de churn BTC_GT_76772 documenté), puis **cadence ~1/jour** après les garde-fous anti-churn. Modèle unique `lognormal_static_vol_v1` partout → confirme que R-08 vise le bon objet (le modèle de probabilité, pas les exits). Sizing 50 → 12 USDC visible au niveau décision à partir du 02-06 (gate Kelly `min_shadow_kelly_size_usdc=2`). `daily_summary` recoupe le -47.84 USDC au centime. Note de contexte : les strikes BTC passent de ~76.7k (24-05) à ~61.3k (11-06) — le selloff de ~20 % est le fond de tableau commun au bleed pod A (long-only) et à la dégradation du Brier HIP4. Verdict promotion : **inchangé (non)**.

## D. Snapshots et replay R-04 : consommables, avec une réserve
Les snapshots minute 24-05 → 11-06 sont au schéma du `SnapshotBuilder` (mêmes champs que ce que consomment les pods : prix, book, flux, régimes par cluster) → le replay full-bot de la fenêtre récente est **techniquement possible dès maintenant**. Réserve : ils ne contiennent pas les champs `external_reference_*` multisource du format baseline (cf. F-06) ; un replay « format identique au baseline » n'est pas possible pour la fenêtre récente. Recommandation pratique : lancer R-04 sur ces snapshots avec la config courante en acceptant cette limite, et l'annoter `no_external_reference` ; le différentiel attendu vs live est maintenant largement pré-expliqué par les ères (section A).

## E. Statuts mis à jour

| Item | Avant | Après |
|---|---|---|
| F-03 attribution PnL cumulé | P0 `needs_data` | **P1** — expliqué ~85 % par reconstruction ; réconciliation exacte toujours via R-03 |
| R-03 close fills + historique complet | P0 | **P0 inchangé** (estimation ≠ réconciliation ; ajouter le fix websocket F-07 au même chantier) |
| R-04 replay fenêtre récente | `needs_replay`, input à construire | **`ready`** — snapshots fournis et consommables (réserve F-06) |
| R-10 config changelog | « absent » | **existe** (`trident_active_plan.md`) ; reste à le verser au pack d'audit par défaut + ajouter champs A-grade à l'export (F-08) |
| Triage divergence live/replay | H1 régime / H2 coûts / H3 config | + **H4 : absence de référence externe en live (pod C)** ; H3 désormais documenté précisément (caps 100→250→500→250→200, grace 0→165→60/120) |
| Données encore manquantes | — | close fills exchange, frais/funding réels, MFE/MAE, run review HIP4 fraîche (inchangé, confirmé par le README du pack) |

## F. Plan de suivi priorisé — modifications et tests

Objectif : transformer les recommandations en file d'exécution traçable. Chaque étape ci-dessous est soit autosuffisante, soit rattachée explicitement aux findings/recommandations du rapport. Ne pas changer de cap live, de sizing, de stops ou activer de nouveaux ordres tant que les tests listés pour l'étape concernée ne sont pas verts.

### P0 — Sécurité et données bloquantes

- [x] **P0-01 — Secrets repo public : rotation, retrait et purge — statut 2026-06-12 : `OK_GIT_LOCAL_SCAN_VERT`**
  **Références** : F-01, R-01, S-07.
  **Modifs à faire** : révoquer/rotater la clé `HIP4_OUTCOME_SECRET_KEY` et l'API wallet associée ; retirer `.env.trident` du tracking git (`git rm --cached`) ; ajouter `.env.trident`, `.env.trident-hip4` et `.env.*` au `.gitignore` en gardant seulement les `*.example` ; purger l'historique public ou recréer un repo public nettoyé ; vérifier que les scripts de déploiement continuent de charger uniquement les secrets serveur.
  **Tests / preuves attendues** : `git ls-files` ne liste plus de fichier secret ; scan `gitleaks`/`trufflehog` sur tout l'historique ; redémarrage HIP4 paper/testnet avec secrets serveur uniquement ; aucun secret réel dans le nouveau pack d'audit.
  **Vérification locale 2026-06-12** : `git ls-files -- .env.trident .env.trident-hip4 '.env.*'` ne liste plus `.env.trident` ni `.env.trident-hip4` ; seuls `.env.trident.example` et `.env.tridentai.example` restent trackés. `.gitignore` ignore maintenant `.env`, `.env.trident`, `.env.trident-hip4`, `.env.tridentai` et `.env.*`, avec exception pour les `*.example`. `git grep -n -E '0x[0-9a-fA-F]{64}' -- ':!*.example' ':!docs/resultat_audit.md'` ne retourne rien.
  **Purge historique 2026-06-12** : historique local réécrit avec `git-filter-repo --path .env.trident --invert-paths`; `git log --all -- .env.trident` ne retourne plus rien après purge. Remote `origin` restauré après la réécriture automatique par `git-filter-repo`.
  **Scan secrets 2026-06-12** : `gitleaks 8.30.1` lancé sur 129 commits / 25.72 MB : `no leaks found`. Six faux positifs `generic-api-key` sur des identifiants de patterns de recherche (`app/research/pod_a_day_by_day_patterns.py`) sont ignorés via `.gitleaksignore` avec fingerprints exacts. `trufflehog 3.95.5` lancé sur le repo Git local : `verified_secrets=0`, `unverified_secrets=0`.
  **Preuve opérateur 2026-06-12** : clé compromise déclarée révoquée/rotatée par l'opérateur. Le redémarrage HIP4 avec secrets serveur uniquement reste à vérifier dans le flux de déploiement/review, mais ne bloque plus le volet Git/secrets du P0-01.
  **Terminé quand** : repo public nettoyé par force-push de l'historique réécrit, scan vert documenté, et prochaine review serveur confirmant le chargement des secrets serveur uniquement.

- [ ] **P0-02 — API : bind local + authentification + firewall**
  **Références** : F-02, R-02, S-02.
  **Modifs à faire** : binder les ports TRIDENT/HIP4 sur `127.0.0.1` dans les compose ; ajouter une authentification par token au minimum sur tous les `POST`, idéalement sur toute l'API observabilité ; journaliser les refus d'accès ; confirmer la règle firewall serveur ; documenter l'accès via tunnel SSH.
  **Tests / preuves attendues** : `curl` sans token retourne 401/403 ; `curl` avec token fonctionne via tunnel ; scan externe des ports 3000/3001 fermé ; `scripts/fetch_trident_data.sh` et `trident-hip4/fetch_data.sh` fonctionnent encore ; aucun endpoint mutant accessible sans auth.
  **Terminé quand** : l'API live mainnet n'est plus exposée publiquement et le fetch/review reste opérationnel.

- [ ] **P0-03 — PnL exact : close fills, fees, funding, historique append-only**
  **Références** : F-03, F-07, R-03, §3.3, §10, addendum A/E.
  **Modifs à faire** : persister chaque close fill exchange dans les `trade_close` append-only ; capturer `exchange_fee_usd`, `exchange_closed_pnl_usd`, `user_funding_history` et les paiements funding attribués par fenêtre `opened_at`/`closed_at` ; exporter ces champs dans `trident_ac_fill_events.csv` et `trident_ac_closed_trades.csv`; conserver l'historique complet des trades fermés au-delà du buffer runtime ; corriger le websocket `user_order_updates` qui se reconnecte quasi à chaque message.
  **Tests / preuves attendues** : unit tests parser `userFills` + `user_funding_history`; test d'un cycle live tiny open → close : ordre exchange, fill user, journal JSONL, state store et CSV d'export concordent ; `close_fill_count_by_pod > 0` dans le prochain audit pack ; fees/funding non nuls quand Hyperliquid les expose ; fetch A/C inchangé car les journaux `logs/pod_a_live.jsonl` et `logs/pod_c_live.jsonl` sont déjà rapatriés.
  **Terminé quand** : le prochain pack permet une reconciliation fill-by-fill du PnL net sans reconstruction au pas minute ; les anciens fills sont backfillés si l'API Hyperliquid le permet.

### P1 — Replays et corrections PnL avant tout réglage live

- [ ] **P1-01 — Replay full-bot fenêtre live récente avec config courante**
  **Références** : R-04, addendum A/D/E, hypothèses H1/H2/H3/H4, §5.4.
  **Modifs à faire** : ajouter ou stabiliser un runner de replay consommant les snapshots minute `2026-05-24 → 2026-06-11`; annoter explicitement la réserve `no_external_reference` tant que F-06 n'est pas corrigé ; produire un rapport dans `server-data/replay_reports/` sans écraser les baselines officielles.
  **Tests / preuves attendues** : replay trade-by-trade comparé au live reconstruit ; matrices slippage 8 bps / 12 bps / slippage observé par symbole ; segmentation par ère de config de l'addendum A ; écart expliqué ou listé comme résiduel.
  **Terminé quand** : on sait si l'edge courant survit à juin et quelles hypothèses expliquent l'écart live/replay. Aucun changement de sizing/stop ne doit précéder ce résultat.

- [ ] **P1-02 — Queue des stops et `early_failure_exit` : replay de sensibilité**
  **Références** : R-07, addendum A, levier PnL 1.
  **Modifs à faire** : paramétrer en replay le stop catastrophe dynamique, son plafond, la durée de grace 60/120 min et `early_failure_exit` on/off ; inclure l'ère 2 (cap 500 + grace 165) et l'ère 3 (correctifs du 09-06).
  **Tests / preuves attendues** : matrice replay `cat_stop_max_bps` × `EFE on/off` × `grace`; mesure excès perte réelle vs stop planifié ; comparaison contre baseline officielle et fenêtre récente ; aucun déploiement live sans preuve que la variante réduit la queue sans dégrader le PF.
  **Terminé quand** : une variante est clairement meilleure sur la fenêtre récente et au moins neutre sur la baseline, ou le réglage actuel est conservé.

- [ ] **P1-03 — Pod C : rétablir la référence externe live**
  **Références** : F-06, R-09, addendum B/D, levier PnL 4.
  **Modifs à faire** : restaurer l'alimentation `external_reference_*` dans les snapshots/runtime live Pod C ; exporter ces champs dans les décisions et closed trades ; ajouter un guardrail de stale/dislocation seulement après replay dédié, car il peut modifier les entrées.
  **Tests / preuves attendues** : sur un run live/dry-run, `external_reference_available` n'est plus False sur 100 % des enregistrements ; replay Pod C avec et sans référence externe ; `XYZ:SILVER` reste bloqué ; aucun ordre nouveau n'est activé par cette correction seule.
  **Terminé quand** : Pod C retrouve la parité data live/replay ou le rapport explique précisément l'écart restant.

- [ ] **P1-04 — Exécution Pod A : slippage et websocket**
  **Références** : R-06, F-07, addendum A, levier PnL 5.
  **Modifs à faire** : corriger la stabilité du websocket `user_order_updates`; ajouter dans l'audit des métriques slippage par symbole/setup/ère ; tester une entrée plus spread-aware ou un skip si spread/slippage attendu dépasse un seuil, uniquement en replay/dry-run avant live.
  **Tests / preuves attendues** : `reconnect_count` ne croît plus au rythme des messages ; fees réels capturés dans les fill events ; replay coûts 8/12/observé ; A/B dry-run sur taux de fill manqué vs PnL simulé.
  **Terminé quand** : le modèle de coût du replay reflète le live et toute règle de skip/limit prouve qu'elle améliore le net PnL sans tuer le fill rate.

- [ ] **P1-05 — A-grade / quality sizing : données d'abord, gel ensuite si confirmé**
  **Références** : F-05, F-08, R-05, addendum A/E, levier PnL 6.
  **Modifs à faire** : ajouter `a_grade_active`, `a_grade_level`, `a_grade_score`, `a_grade_size_scale` et les champs de quality sizing dans `export_trident_audit_pack.py`; rejouer les size scales `{1.0, 1.25, 1.40}` ; ne geler le boost strong à 1.0 en live que si le replay confirme la contre-performance.
  **Tests / preuves attendues** : prochain pack avec champs A-grade non vides ; replay baseline officielle + fenêtre récente pour chaque scale ; comparaison PnL, drawdown, PF, WR et concentration des pertes.
  **Terminé quand** : le boost est soit justifié par replay, soit gelé avec preuve et rollback documenté.

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

**Conclusion de l'addendum.** La perte live n'est pas un mystère statistique : c'est la combinaison documentée et désormais chiffrée (1) d'un bug de stop immédiat (ère 1, -20), (2) d'une fenêtre cap 500 + grace 165 min pendant un selloff de -20 % sur des longs alts (ère 2, -79, dont ~-103 d'excès de stops vs plan), et (3) d'un edge d'entrée réellement faible en régime DeadZone/Range (ère 3, WR 24 %). Les correctifs du 09-06 ont traité la queue (1)(2) ; le chantier restant est la qualité d'entrée et la parité live/replay (R-04 à lancer, F-06 à corriger), pas un nouveau réglage d'exits à chaud.

*Limites de l'addendum : reconstruction au pas minute (pas de wicks intra-minute), frais/funding estimés et non observés, paramètres silver_mode pod C approximés, EFE non simulé avant le 09-06. Aucun chiffre reconstruit ne doit servir de base à un réglage sans le replay R-04.*

---

Avant les leviers, un cadrage honnête, parce qu'il conditionne tout le reste : la question « comment améliorer drastiquement le PnL » suppose qu'il existe un edge à débloquer. L'audit laisse deux lectures possibles, et **rien dans les données ne les départage encore** :

- Lecture optimiste : le système a un edge (baseline +860 sur avril-mai), et c'est la queue des stops + un régime adverse qui ont mangé le live. On corrige → ça repasse positif.
- Lecture prudente : le baseline +860 est mesuré sur une période *trending*. En juin, marché baissier (-20 % sur BTC), un système **long-only** n'a peut-être structurellement pas d'edge, quels que soient les réglages d'exit.

Ce qui tranche entre les deux, c'est le replay R-04 (désormais `ready`). **C'est le plus gros levier, et ce n'est pas un réglage** : tant qu'il n'est pas lancé, tout réglage à chaud est un pari. Je le mets en préalable absolu.

Cela dit, voici les leviers par impact estimé sur la fenêtre que j'ai reconstruite.

**1. La queue des stops — le poste le plus chiffré (~-103 USD d'excès)**

C'est de loin le plus gros trou : 15 stops catastrophe ont coûté -120 à eux seuls, avec une perte moyenne de 2.4× le stop planifié. Les correctifs du 09-06 (grace ramenée à 60/120 min, cat stop dynamique plafonné, `early_failure_exit`) ont déjà divisé la perte/trade par ~4. La recommandation n'est pas d'inventer un nouveau réglage mais de **valider que ces correctifs tiennent sur l'ère 2 rejouée** (R-07 : EFE on/off), et probablement de **resserrer encore le cat stop** : sur des alts à 160 bps de stop, un stop catastrophe à 300 bps autorise une perte 2× le risque budgété. Réduire ce plafond est le geste à plus fort effet de levier mécanique.

**2. La discipline cap × régime — la leçon la plus claire de la fenêtre**

L'ère 2 raconte tout : cap monté à 500 le 02-06, puis selloff → -79, dont -82 sur la seule période cap-500/cap-redescendu. Le retour à 200 était la bonne décision. La règle qui en sort : **ne jamais remonter le cap sans régime favorable confirmé**, et idéalement lier le cap au régime de façon automatique plutôt qu'à un ajustement opérateur a posteriori. Un cap qui se contracte automatiquement quand le leader (BTC) passe en downtrend aurait évité l'essentiel du -44 du 05-06.

**3. La qualité d'entrée en régime dégradé — le vrai sujet de fond**

C'est le point le plus profond et le plus inconfortable. Le WR de l'ère 3 est de 24 %, et 18 closes sur 25 se font en DeadZone/RangeAuction alors que les entrées sont prises en TrendExpansion. Autrement dit : **le système entre sur un signal de tendance, puis le régime se dégrade sous lui.** Aucun réglage d'exit ne corrige un edge d'entrée faible. Les pistes à tester (par replay, pas en live) : un filtre qui invalide l'entrée si le régime du leader se dégrade dans les N minutes, ou un gate sur l'alignement BTC plus strict. C'est là que se trouve le « drastique » réel, mais c'est aussi le plus risqué à toucher.

**4. Pod C : ressusciter la référence externe + maintenir le blocage silver**

F-06 est à la fois un trou de PnL et de sécurité : la référence externe est morte sur 100 % des enregistrements live, donc Pod C trade sans garde-fou de prix et sans la donnée qui faisait son edge en backtest (+79). La rétablir est probablement le geste le plus rentable côté Pod C. Et le silver reste la quasi-totalité de la perte Pod C (~-24, 0 gagnant) — ne pas le réautoriser.

**5. Exécution / slippage**

13.4 bps observés en ère 2 vs 8 supposés. Sur ~140 entrées, l'écart se chiffre en dizaines de dollars. Entrée *spread-aware* (R-06) et résolution du websocket qui se reconnecte en boucle (F-07, qui fait aussi perdre la capture des frais).

**6. Le boost « strong A-grade » — à geler, échantillon faible**

Contre-performant sur les 25 trades attribuables (le boost ×1.4 amplifie les trades perdants), mais l'échantillon ne permet pas de conclure et l'export ne contient même pas les champs A-grade. Geler à 1.0 en attendant le replay R-05, sans détruire le label.

---

Si je devais ne retenir qu'une chose : **le levier le plus drastique n'est pas un meilleur exit, c'est de ne pas être long-only crypto dans un marché baissier.** La capacité short ou un hedge directionnel changerait l'ordre de grandeur — mais c'est précisément dans la liste des actions interdites sans validation, et pour de bonnes raisons. À court terme, la séquence rationnelle est : R-04 d'abord (l'edge existe-t-il encore ?), puis resserrer le cat stop et lier le cap au régime, puis attaquer le filtre d'entrée. Rien de tout ça ne se déploie sans le replay.

Je ne suis pas conseiller financier, et tous les chiffres ci-dessus reposent sur une reconstruction au pas minute, pas sur les fills exchange réels — donc à traiter comme des ordres de grandeur pour prioriser, pas comme des vérités à câbler en dur.
