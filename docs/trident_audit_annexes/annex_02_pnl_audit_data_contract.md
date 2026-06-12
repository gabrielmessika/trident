# Annexe 02 - Data contract pour audit PnL TRIDENT

Date de generation: 2026-06-11

Cette annexe definit les exports a fournir a un outil externe pour un audit PnL
complet. Les noms de colonnes peuvent etre adaptes, mais les champs semantiques
doivent etre presents ou explicitement marques `unknown`.

## 1. Conventions communes

Formats acceptes:

- CSV pour tables plates.
- JSONL pour evenements imbriques.
- JSON pour snapshots d'etat.

Conventions:

- Horodatages en UTC ISO-8601.
- Montants USD en decimal string ou float.
- Bps en decimal float.
- Champs absents: `null`.
- Champs inconnus: `unknown`.
- Ne jamais inclure secrets, private keys, tokens ou contenu `.env.trident`.

Colonnes communes recommandees:

- `app_kind`: `trident` ou `trident-hip4`.
- `pod`: `pod_a`, `pod_c`, `hip4_outcome`, ou `observer`.
- `mode`: observation, dry-run, live, paper, testnet, observer.
- `network`: mainnet, testnet, mixed, unknown.
- `source_file`.
- `source_line` si disponible.
- `event_ts`.
- `generated_at`.

## 2. TRIDENT A/C - trades normalises

Fichier recommande:

- `trident_ac_closed_trades.csv` ou `trident_ac_closed_trades.jsonl`.
- Alias accepte: `trident_ac_trades.csv` si le fichier contient bien les
  positions fermees, pas seulement les fills.

Grain:

- Une ligne par position fermee.
- Ajouter une ligne separee pour position ouverte si besoin avec
  `is_open=true`.

Important:

- `trident_ac_fill_events.csv` ne remplace pas cette table.
- Des fills d'ouverture seuls ne permettent pas d'attribuer le PnL ferme.
- L'export 2026-06-11 produit `trident_ac_closed_trades.csv` depuis
  `closed_trade_log`; il permet une attribution PnL applicative first-pass.
- Si les closed trades manquent ou sont vides, le verdict PnL A/C doit rester
  `insufficient_data`.
- Meme avec les closed trades, une reconciliation exchange definitive requiert
  des fills de fermeture bruts ou un export exchange normalise.

Champs requis:

| Champ | Description |
| --- | --- |
| `position_id` | Identifiant stable position. |
| `pod` | `pod_a` ou `pod_c`. |
| `symbol` | Symbole exchange/logique. |
| `wire_symbol` | Symbole exchange si different. |
| `cluster` | crypto, index, gold, silver, oil, fx, equity. |
| `side` | long ou short. |
| `setup` | Setup d'entree. |
| `entry_ts` | Timestamp entree. |
| `exit_ts` | Timestamp sortie ou null. |
| `time_in_trade_sec` | Duree. |
| `entry_price` | Prix moyen entree. |
| `exit_price` | Prix moyen sortie. |
| `qty` | Taille position. |
| `entry_notional_usd` | Notional entree. |
| `exit_notional_usd` | Notional sortie. |
| `margin_usd` | Marge utilisee. |
| `leverage` | Levier. |
| `fees_usd` | Frais totaux. |
| `funding_usd` | Funding total. |
| `slippage_cost_usd` | Cout slippage estime ou reel. |
| `gross_pnl_usd` | PnL avant couts. |
| `net_pnl_usd` | PnL apres couts. |
| `unrealized_pnl_usd` | Pour positions ouvertes. |
| `exit_reason` | stop, trailing, TP, BE, time stop, etc. |
| `initial_stop_bps` | Stop planifie a l'entree. |
| `planned_risk_usd` | Perte planifiee. |
| `actual_loss_usd` | Perte reelle si trade perdant. |
| `r_multiple` | Net PnL / planned risk. |
| `mfe_bps` | Max favorable excursion. |
| `mae_bps` | Max adverse excursion. |
| `regime_entry` | Regime global entree. |
| `regime_exit` | Regime global sortie. |
| `cluster_regime_entry` | Regime cluster entree. |
| `local_regime_entry` | Regime local entree. |
| `routing_owner_entry` | Owner routing entree. |
| `allocation_target_usd` | Allocation symbole/pod a l'entree. |
| `confidence` | Confidence du plan. |
| `a_grade_score` | Pod A si applicable. |
| `watchers` | Liste JSON ou string jointe. |
| `vetoes` | Liste JSON ou string jointe. |
| `live_cap_usd` | Cap live applicable. |
| `target_notional_before_cap` | Notional avant cap. |
| `target_notional_after_cap` | Notional apres cap. |
| `protective_order_status` | ok, missing, failed, unknown. |
| `reconciliation_status_entry` | ready/not_ready/unknown. |

Champs fortement recommandes:

- `decision_mid_price`.
- `fill_price_entry`.
- `fill_price_exit`.
- `execution_shortfall_bps`.
- `order_ids`.
- `stop_order_id`.
- `take_profit_order_id`.
- `catastrophic_stop_used`.
- `stop_grace_active`.
- `early_failure_exit_active`.
- `loss_tax_active`.
- `correlation_group`.
- `correlation_slot`.
- `external_reference_age_sec` pour Pod C.

Champs absents ou partiels dans l'export 2026-06-11:

- `position_id` stable.
- `wire_symbol`.
- `qty` et notionals de sortie detailles.
- funding reel par trade.
- MFE/MAE.
- prix/fills exchange bruts de fermeture.
- slippage de sortie.

## 3. TRIDENT A/C - signal decisions

Fichier recommande:

- `trident_ac_signal_decisions.jsonl`.

Grain:

- Une ligne par signal preview, risk decision, executor skip ou open attempt.

Champs requis:

| Champ | Description |
| --- | --- |
| `event_ts` | Timestamp decision. |
| `event_type` | preview, risk_decision, executor_open, executor_skip, close. |
| `pod` | pod_a ou pod_c. |
| `symbol` | Symbole. |
| `setup` | Setup. |
| `side` | Side. |
| `confidence` | Confidence. |
| `approved` | true/false/null. |
| `reason` | Raison accept/reject/skip. |
| `target_notional_usd` | Notional courant. |
| `target_notional_before_cap` | Avant cap si disponible. |
| `live_cap_active` | true/false. |
| `margin_usd` | Marge. |
| `leverage` | Levier. |
| `stop_bps` | Stop. |
| `risk_budget_usd` | Budget risque. |
| `expected_loss_usd` | Perte attendue. |
| `regime` | Regime global. |
| `cluster_regime` | Regime cluster. |
| `local_regime` | Regime local. |
| `routing_owner` | Owner. |
| `routing_reason` | Raison routing. |
| `allocation_target_usd` | Allocation. |
| `watchers` | Watchers. |
| `vetoes` | Vetoes. |
| `setup_details` | JSON details. |

Raisons a conserver telles quelles:

- `symbol_blocked`.
- `regime_blocked`.
- `setup_disabled`.
- `margin_below_min`.
- `notional_below_min`.
- `notional_above_live_cap`.
- `live_reconciliation_not_ready`.
- `user_stream_unhealthy`.
- `routing_revoked`.
- `market_already_open`.

## 3 bis. TRIDENT A/C - fill events

Fichier recommande:

- `trident_ac_fill_events.csv`.

Role:

- Reconciler les executions brutes avec les signaux.
- Mesurer slippage, fees et presence des protective order ids.
- Verifier si un fill est ouverture ou fermeture.

Limite:

- Si le fichier ne contient que des `action=open`, il n'est pas suffisant pour
  calculer le PnL ferme.

Champs requis:

| Champ | Description |
| --- | --- |
| `event_ts` | Timestamp du signal/execution. |
| `pod` | Pod A ou Pod C. |
| `symbol` | Symbole. |
| `side` | long/short. |
| `setup` | Setup. |
| `action` | open/close. |
| `fill_ts` | Timestamp du fill. |
| `price` | Prix fill. |
| `notional_usd` | Notional fill. |
| `fee_usd` | Frais fill. |
| `slippage_bps` | Slippage. |
| `filled_size` | Quantite exchange. |
| `oid` | Order id. |
| `cloid` | Client order id. |
| `risk_accepted` | Decision risk. |
| `risk_reason` | Raison risk. |
| `confidence` | Confidence. |
| `regime` | Regime. |

## 4. TRIDENT A/C - snapshots health/state

Fichiers recommandes:

- `trident_ac_health_latest.json`.
- `trident_ac_state_latest.json`.
- `trident_ac_report_latest.json`.
- `trident_ac_metrics_latest.json`.
- `trident_ac_pod_a_status_latest.json`.
- `trident_ac_pod_c_status_latest.json`.
- `trident_ac_reconciliation_latest.json`.

Champs critiques:

- mode;
- exchange network;
- process state par pod;
- live trading paused;
- reconciliation ready;
- unknown exchange positions;
- missing exchange positions;
- side mismatches;
- open orders inconnus;
- trigger orders orphelins;
- ownership conflict count;
- positions ouvertes;
- realized/unrealized PnL;
- fill count;
- win rate;
- live max order notional;
- stop grace config exposee.

## 5. HIP4 - decisions

Fichier recommande:

- `hip4_decisions.jsonl`.

Grain:

- Une ligne par opportunite/decision.

Champs requis:

| Champ | Description |
| --- | --- |
| `event_ts` | Timestamp decision. |
| `profile` | mainnet_paper, mainnet, testnet, shadow. |
| `mode` | paper, observer, testnet. |
| `market_id` | Market id outcome. |
| `underlying` | Sous-jacent. |
| `expiry_ts` | Expiry. |
| `edge_type` | MODEL, LATE_EXPIRY, etc. |
| `side` | BUY_YES, BUY_NO, etc. |
| `probability_yes` | Probabilite estimee. |
| `probability_no` | Si disponible. |
| `yes_ask` | Prix ask YES. |
| `no_ask` | Prix ask NO. |
| `yes_bid` | Prix bid YES. |
| `no_bid` | Prix bid NO. |
| `gross_edge` | Edge brut. |
| `net_edge` | Edge net. |
| `approved` | true/false. |
| `reason` | Raison decision. |
| `size_usdc` | Taille proposee/approuvee. |
| `reference_price` | Prix reference. |
| `reference_source_count` | Nombre sources. |
| `reference_divergence_bps` | Divergence. |
| `shock_guard_status` | ok/reject/unknown. |
| `reentry_lock_active` | true/false. |
| `active_policy` | Policy active. |
| `shadow_policy_outputs` | JSON si disponible. |

## 6. HIP4 - trades et settlements

Fichiers recommandes:

- `hip4_trades.csv`.
- `hip4_settlements.csv`.

Champs trades:

- `trade_id`.
- `market_id`.
- `profile`.
- `entry_ts`.
- `exit_ts`.
- `underlying`.
- `expiry_ts`.
- `edge_type`.
- `side`.
- `entry_price`.
- `exit_price`.
- `size_usdc`.
- `fees_usd`.
- `slippage_usd`.
- `net_pnl_usd`.
- `exit_reason`.
- `probability_entry`.
- `probability_exit`.
- `active_policy`.
- `reentry_after_early_exit`.
- `same_market_position_count_before_settlement`.
- `nautilus_quality_bucket`.

Champs settlements:

- `market_id`.
- `settlement_ts`.
- `underlying`.
- `expiry_ts`.
- `actual_outcome`.
- `predicted_probability_entry`.
- `brier_component`.
- `settlement_pnl_usd`.
- `position_count`.
- `best_shadow_policy`.
- `active_policy_pnl`.
- `hold_to_settlement_pnl`.
- `prob_stop_full_pnl`.

## 7. HIP4 - policy replay

Fichier recommande:

- `hip4_policy_replay.csv`.

Champs:

- `policy`.
- `source`.
- `settlements`.
- `exits`.
- `pnl_usd`.
- `delta_vs_active_usd`.
- `profit_factor`.
- `win_rate`.
- `worst_loss_usd`.
- `best_win_usd`.
- `cutoff`.
- `notes`.

Regle d'interpretation:

- Une policy shadow ne doit pas etre promue sans assez de settlements, sans
  test post-cutoff et sans analyse du churn/reentry.

## 8. Calculs obligatoires

TRIDENT A/C:

- PnL total et par pod.
- PnL par symbole, cluster, setup, regime, exit reason.
- Profit factor, win rate, expectancy USD, expectancy R.
- Worst trade et concentration des pertes.
- Realized vs unrealized.
- Stop actual vs planned.
- Slippage/funding/fees.
- Accepted signals vs opened trades.
- Risk rejects vs executor skips.
- Live cap utilization.

HIP4:

- PnL net total paper.
- Settlements count.
- Profit factor.
- Win rate.
- Brier score.
- Calibration par slice.
- Pire perte et contribution.
- Policy active vs shadow.
- Reentry/churn.
- Observer coverage vs paper trades.
- Nautilus data quality et would-block analysis.

## 9. Sortie machine recommandee

L'auditeur peut produire un JSON final avec:

```json
{
  "verdicts": {
    "trident_ac": "OK|WARN|KO|insufficient_data",
    "hip4_mainnet_paper": "OK|WARN|KO|insufficient_data",
    "hip4_observer": "OK|WARN|KO|insufficient_data",
    "pnl": "OK|WARN|KO|insufficient_data"
  },
  "pnl_summary": {},
  "top_findings": [],
  "missing_data": [],
  "recommended_changes": [],
  "required_tests": [],
  "human_confirmations_required": []
}
```
