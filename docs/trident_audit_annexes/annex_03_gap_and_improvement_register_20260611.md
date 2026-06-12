# Annexe 03 - Registre des gaps et axes d'amelioration

Date de generation: 2026-06-11

Cette annexe integre le fetch frais, l'export compact regenere et l'inspection
read-only du serveur. Le verdict initial "architecture suffisante, PnL
insuffisant" doit etre nuance: le PnL A/C est maintenant exploitable au niveau
trade ferme, mais pas encore au niveau fill exchange complet.

## 1. Verdict corrige

| Perimetre | Donnees actuelles | Verdict |
| --- | --- | --- |
| Architecture TRIDENT A/C | Carte + annexes | Suffisant. |
| Architecture HIP4 | Carte + annexes | Suffisant. |
| Health recent A/C | Fetch frais + review `2026-06-11T13:59:56Z` | Suffisant pour statut operationnel ponctuel. |
| PnL Pod A/C trade-level | `trident_ac_closed_trades.csv` 31 trades | Exploitable pour attribution applicative first-pass. |
| Reconciliation Pod A/C fill-level | Open fills seulement, `close_fills=0` | Incomplet; exchange fills manquants. |
| PnL HIP4 paper | Trades/settlements/replay exportes | Exploitable avec prudence sample faible. |
| HIP4 readiness promotion | Run review ancienne + policy audit frais | `collect_more_data`; pas de promotion. |
| Axes d'amelioration chiffres A/C | Closed trades + decisions exportes | Possible, avec label `needs_exchange_reconciliation` si l'axe depend des exits fills. |
| Axes d'amelioration HIP4 | Decisions/trades/settlements/policy replay | Possible, avec prudence shadow/post-change. |

Conclusion courte:

- Oui, le pack permet de comprendre le projet sans sources.
- Oui, il permet maintenant un premier audit PnL A/C au niveau trade ferme.
- Non, il ne permet pas encore une reconciliation exchange complete des sorties
  A/C, car les `close_fills` bruts ne sont pas presents.
- Oui, il isole des axes d'amelioration concrets, mais toute promotion
  live/mainnet reste soumise aux guardrails et a confirmation humaine.

## 2. Export compact genere

Commandes:

```bash
./scripts/fetch_all_data.sh
./scripts/fetch_trident_data.sh --logs-only
python scripts/export_trident_audit_pack.py --fresh-fetch-run --output server-data/audit_exports/20260611T135456Z
```

Manifest:

- `generated_at=2026-06-11T14:11:47.397559+00:00`
- `fresh_fetch_run=true`
- `contains_secrets=false`
- Source root: `server-data`

Inventaire:

| Fichier | Taille approx | Lignes/rows | Usage |
| --- | ---: | ---: | --- |
| `trident_ac_signal_decisions.jsonl` | 292 MB | 193697 | Decisions, rejets, reviews A/C. |
| `trident_ac_fill_events.csv` | 35 KB | 141 data | Fills A/C vus dans logs, ouvertures seulement. |
| `trident_ac_closed_trades.csv` | 14 KB | 31 data | Trades fermes Pod A/C depuis `closed_trade_log`. |
| `trident_ac_runtime_summary.json` | 162 KB | n/a | Status Pod A/C. |
| `trident_ac_open_positions.json` | 974 B | n/a | Positions ouvertes au snapshot. |
| `trident_ac_live_state_pod_a.json` | 100 KB | n/a | State store Pod A: positions/orders/events. |
| `trident_ac_live_state_pod_c.json` | 20 KB | n/a | State store Pod C: positions/orders/events. |
| `baseline_official_current_cli_20260513.md` | 1.2 KB | n/a | Baseline full-bot officielle courante. |
| `baseline_official_current_cli_20260513.json` | 818 KB | n/a | Rapport baseline machine. |
| `baseline_reference_status_20260513.md` | 2.1 KB | n/a | Statut des references de backtest. |
| `hip4_decisions.jsonl` | 80 MB | 93610 | Decisions HIP4 paper + observer. |
| `hip4_trades.csv` | 3.2 KB | 27 data | Trades paper HIP4. |
| `hip4_settlements.csv` | 4.9 KB | 25 data | Settlements/sorties paper HIP4. |
| `hip4_shadow_exit_policies.csv` | 82 KB | n/a | Evenements shadow exits. |
| `hip4_policy_replay.csv` | 1.5 KB | 11 data | Policies active/shadow. |
| `hip4_policy_cutoff_replay.csv` | 1.2 KB | 8 data | Policies par cutoff. |
| `hip4_runtime_statuses.json` | 462 KB | n/a | Status HIP4 runtime. |
| `manifest.json` | 8.7 KB | n/a | Inventaire machine. |

Stats manifest:

- A/C decisions/reviews: 193697.
- A/C fill rows: 141.
- A/C opened count: Pod A 118, Pod C 23.
- A/C close fill count: 0.
- A/C closed trades: Pod A 25, Pod C 6.
- A/C closed-trade PnL: Pod A -5.90 USD, Pod C +0.46 USD.
- Baseline replay officielle: total +859.83 USD, Pod A +780.72 USD, Pod B
  0.00 USD, Pod C +79.11 USD, 196 trades fermes.
- HIP4 decisions: 93610.
- HIP4 approved mainnet paper: 27.

Inspection serveur:

- Les logs remote contiennent les `open_fills`, mais pas de `close_fills`.
- Les status runtime contiennent `closed_trade_log`: Pod A 25, Pod C 6.
- Les live states contiennent positions, orders et events, pas les fills de
  fermeture exchange bruts.
- Aucun fichier separe de close fills n'a ete trouve dans les chemins distants
  inspectes `/opt/trident/logs`, `/opt/trident/runtime`, `/opt/trident/data`.

## 3. Gaps PnL prioritaires

Priorite P0:

- Exporter `trident_ac_exchange_fills.csv` ou equivalent pour reconciler les
  exits fill-by-fill.
- Ajouter une persistence explicite des `close_fills` dans les logs
  d'execution, ou un fetch fiable des fills exchange historiques.
- Verifier que `closed_trade_log` conserve toutes les positions fermees sur la
  fenetre souhaitee, et pas seulement un buffer runtime.

Priorite P1:

- Ajouter MFE/MAE par trade A/C.
- Ajouter funding reel par trade.
- Ajouter slippage decision mid vs fill price pour entree et sortie.
- Ajouter user stream/reconciliation status au moment de chaque open/close.
- Ajouter un identifiant stable de position pour joindre decisions, fills,
  state store et closed trades.

Priorite P2:

- Ajouter un export par position des watchers, vetoes, A-grade et loss tax.
- Ajouter un join market snapshot entree/sortie.
- Ajouter un join routing/allocation au moment de l'entree et de la sortie.
- Ajouter une run review HIP4 regeneree automatiquement apres fetch complet.

## 4. Axes d'amelioration detectables maintenant

### A/C closed-trade attribution

Observation:

- L'export contient 31 trades fermes: Pod A 25, Pod C 6.
- Le PnL ferme exporte est Pod A -5.90 USD et Pod C +0.46 USD.
- Les exits dominants sont `early_failure_exit`, `trailing_stop`,
  `exchange_closed_stop_loss` pour Pod A, et `time_stop` pour Pod C.

Interpretation:

- L'auditeur peut enfin faire des buckets PnL par symbole, setup, confidence,
  exit reason et features presentes dans `setup_details`.
- Les conclusions qui dependent du prix exact de chaque fill de sortie doivent
  rester marquees `needs_exchange_reconciliation`.

Action:

- Grouper `trident_ac_closed_trades.csv` par pod/symbole/exit reason.
- Comparer `expected_loss_usd`, `stop_bps`, `pnl_usd` et `close_reason`.
- Identifier si les pertes sont dues au modele, aux exits ou a l'execution.

### Pod A early_failure_exit

Observation:

- `early_failure_exit` represente 10 des 25 trades fermes Pod A.

Interpretation:

- C'est un axe PnL concret: il faut mesurer si cette sortie limite les pertes ou
  coupe des trades qui recuperent ensuite.

Action:

- Backtester ou rejouer les trades Pod A avec et sans early failure exit.
- Ajouter MFE/MAE post-exit si possible.
- Comparer le resultat au full-bot baseline, pas seulement a ces 10 trades.

### Pod A stops exchange

Observation:

- Pod A a 5 `exchange_closed_stop_loss`.
- Le dernier review indique stop actual vs planned: count=5, actual=-7.10,
  planned=-8.06, excess=0.96.

Interpretation:

- Les stops semblent globalement dans le plan sur cette fenetre, mais il faut
  verifier chaque trade avec fill de sortie si la precision compte.

Action:

- Marquer chaque stop `within_plan`, `mild_excess` ou `severe_excess`.
- Chercher les cas ou la perte reelle depasse le plan au-dela de la tolerance.

### Pod C time_stop et selection

Observation:

- Pod C a 6 trades fermes, PnL +0.46 USD.
- `time_stop` represente 3 exits.
- `XYZ:SILVER` est bloque dans la config courante.

Interpretation:

- Sample faible: ne pas augmenter le capital sur cette seule fenetre.
- Le sujet le plus utile est la qualite des exits et des references externes.

Action:

- Segmenter Pod C par symbole/cluster, `external_reference_age_seconds` et exit
  reason.
- Verifier qu'aucune ouverture `XYZ:SILVER` n'existe apres le blocage effectif.
- Mesurer si `time_stop` ameliore le drawdown ou retire l'upside.

### HIP4 policy ambiguity

Observation:

- La carte indique `prob_stop_full` comme policy active courante.
- Le replay policy frais montre `active_paper` a -47.8386 USDC et
  `prob_stop_full` shadow a +189.5755 USDC.
- Le delta shadow vs active est +237.4141 USDC.

Interpretation:

- Axe PnL le plus tangible cote HIP4.
- Il faut verifier si `active_paper` reflete une policy runtime legacy ou la
  policy active au moment des trades.
- Les cutoffs recents restent faibles et negatifs, donc pas de promotion
  automatique.

Action:

- Comparer trades/settlements avant et apres le changement du 2026-06-10.
- Verifier dans le status runtime que `active_policy=prob_stop_full`.
- Generer une run review apres nouveaux settlements post-change.

### HIP4 market_already_open

Observation:

- Export compact: 29095 rejets `mainnet_paper:market_already_open`.
- Approved mainnet paper: 27.

Interpretation:

- Peut signaler un pipeline qui genere beaucoup d'opportunites redondantes.
- Peut aussi etre une protection anti-churn efficace.

Action:

- Grouper les rejets par `market_id`, side et minute.
- Mesurer combien d'opportunites redondantes etaient equivalentes a une
  position deja ouverte.
- Chercher les cas re-entry opposite-side avant settlement.

### HIP4 non-BTC

Observation:

- L'audit non-BTC frais indique des candidats tradables ETH, HYPE et SOL.
- `mainnet_paper` a 1 approbation sur chacun de ETH, HYPE et SOL dans la
  fenetre observee.

Interpretation:

- L'ancien diagnostic "BTC-only" est obsolete.
- C'est un axe de couverture et collecte, pas encore une preuve PnL.

Action:

- Suivre calibration/PnL par underlying.
- Ne pas augmenter le risque non-BTC avant un sample suffisant.

## 5. Recommandations a appliquer au pack

Le pack externe final doit inclure:

1. `docs/trident_project_audit_map.md`.
2. `docs/trident_audit_annexes/`.
3. `server-data/audit_exports/20260611T135456Z/`.
4. Un export futur `trident_ac_exchange_fills.csv` si une reconciliation
   exchange complete est requise.
5. Une run review HIP4 regeneree apres nouveaux settlements si la question porte
   sur promotion ou rollback d'exit policy.
6. `annex_04_baseline_replays_20260611.md` pour toute proposition
   d'amelioration PnL qui doit etre comparee a la baseline.

Regles de verdict:

- Sans `trident_ac_closed_trades.csv`, tout axe A/C doit rester `needs_data`.
- Avec `trident_ac_closed_trades.csv`, les axes A/C peuvent etre analyses au
  niveau applicatif.
- Sans close fills/exchange fills, les conclusions A/C qui dependent des prix
  exacts de sortie doivent rester `needs_exchange_reconciliation`.
- Avec HIP4 trades/settlements/replay, les axes HIP4 peuvent etre analyses, mais
  les promotions restent bloquees par sample faible et post-change insuffisant.

## 6. Modifications de support effectuees

- `scripts/fetch_trident_data.sh` rapatrie maintenant les live states Pod A/C
  depuis `runtime/trident/live_state_pod_a.json` et
  `runtime/trident/live_state_pod_c.json`.
- `scripts/export_trident_audit_pack.py` exporte maintenant:
  - `trident_ac_closed_trades.csv`;
  - `trident_ac_live_state_pod_a.json`;
  - `trident_ac_live_state_pod_c.json`.
- Le README de l'export distingue l'attribution closed-trade disponible et la
  reconciliation close-fill encore manquante.
