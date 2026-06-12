# Annexe 00 - Manifest du dossier d'audit externe TRIDENT

Date de generation: 2026-06-11

Cette annexe decrit le contenu a fournir a un outil d'audit externe qui n'a pas
acces au repo. Les fichiers doivent contenir les donnees elles-memes, pas
seulement des chemins locaux.

## 1. Fichiers Markdown du pack

Fichiers crees dans `docs/`:

- `docs/trident_project_audit_map.md`
- `docs/trident_audit_annexes/annex_00_manifest.md`
- `docs/trident_audit_annexes/annex_01_latest_fetch_digest_20260611.md`
- `docs/trident_audit_annexes/annex_02_pnl_audit_data_contract.md`
- `docs/trident_audit_annexes/annex_03_gap_and_improvement_register_20260611.md`
- `docs/trident_audit_annexes/annex_04_baseline_replays_20260611.md`

Outil d'export ajoute au repo:

- `scripts/export_trident_audit_pack.py`

Export compact local a joindre au pack:

- `server-data/audit_exports/20260611T135456Z/`

Etat de fraicheur:

- Un fetch global a ete lance le 2026-06-11 avant l'export.
- L'export a ete regenere avec `--fresh-fetch-run`.
- `manifest.json` indique `fresh_fetch_run=true` et `contains_secrets=false`.
- Derniere review TRIDENT A/C jointe: `2026-06-11T13:59:56.577550Z`.
- Dernier audit policy/market HIP4 joint: `2026-06-11T13:51:51.936041Z`.
- Derniere run review HIP4 jointe: `2026-06-05T14:13:23Z` environ; le fetch
  du 2026-06-11 a regenere l'audit policy/market mais pas cette run review.

## 2. Niveau d'audit autorise

Avec seulement les fichiers Markdown, l'auditeur peut faire:

- audit architecture/design;
- audit coherence des guardrails;
- audit des risques operationnels connus;
- lecture du digest des derniers fetchs;
- identification de donnees manquantes.

Avec les fichiers Markdown plus `server-data/audit_exports/20260611T135456Z/`,
l'auditeur peut aussi faire:

- analyse compacte des decisions A/C et HIP4;
- analyse des fills d'ouverture A/C;
- analyse closed-trade-level du PnL Pod A/C a partir de
  `trident_ac_closed_trades.csv`;
- analyse des trades/settlements HIP4 paper;
- comparaison des policy replays HIP4;
- analyse des rejets dominants et du pipeline d'opportunites;
- inspection des snapshots runtime/live-state Pod A/C.

Limites restantes meme avec l'export:

- Les logs A/C JSONL ne contiennent toujours pas de `close_fills`.
- Le serveur ne contient pas, dans les chemins inspectes, de fichier brut
  separe donnant tous les fills de fermeture exchange.
- `trident_ac_closed_trades.csv` permet une attribution PnL applicative par
  trade ferme, mais pas une reconciliation exchange fill-by-fill definitive.
- Les fees/funding/slippage exacts de sortie restent a confirmer avec des fills
  exchange si une decision PnL depend de ces details.
- Les replays HIP4 restent paper/shadow et les samples post-changement restent
  faibles.

Verdict attendu si l'export compact n'est pas fourni:

- Architecture: possible.
- Health courant: partiel.
- PnL A/C: `insufficient_data` au-dela des agregats.
- PnL HIP4: partiel si les tables HIP4 ne sont pas jointes.
- Amelioration trading: hypotheses seulement, sauf bug/risque evident.

## 3. Annexes minimales pour un audit complet

Pour TRIDENT A/C, inclure au minimum:

- `trident_ac_review_summary_latest.md`
- `trident_ac_review_summary_latest.json`
- `trident_ac_signal_decisions.jsonl`
- `trident_ac_fill_events.csv`
- `trident_ac_closed_trades.csv`
- `trident_ac_runtime_summary.json`
- `trident_ac_open_positions.json`
- `trident_ac_live_state_pod_a.json`
- `trident_ac_live_state_pod_c.json`

Priorite stricte pour completer encore l'audit A/C:

1. `trident_ac_exchange_fills.csv` brut ou normalise pour reconciler les
   positions fermees fill-by-fill.
2. Funding et fees reels par trade si absents ou approximatifs dans les closed
   trades applicatifs.
3. MFE/MAE par trade.
4. Snapshots marche entree/sortie si l'auditeur doit tester les exits.

Pour TRIDENT-HIP4, inclure au minimum:

- `hip4_outcome_run_review_latest.md`
- `hip4_policy_market_audit_latest.md`
- `hip4_outcome_run_review_latest.json`
- `hip4_policy_market_audit_latest.json`
- `hip4_runtime_statuses.json`
- `hip4_decisions.jsonl`
- `hip4_trades.csv`
- `hip4_settlements.csv`
- `hip4_shadow_exit_policies.csv`
- `hip4_policy_replay.csv`
- `hip4_policy_cutoff_replay.csv`

Pour recherche/amelioration, ajouter si disponible:

- baselines officielles referencees par le plan actif, jointes dans
  `annex_04_baseline_replays_20260611.md`;
- resultats replay par pod/setup/regime;
- changelog des modifications recentes avec timestamps;
- fenetres pre/post changement;
- liste des changements config actifs.

## 4. Metadata obligatoire par annexe de donnees

Chaque annexe de donnees doit commencer par:

```yaml
generated_at: YYYY-MM-DDTHH:MM:SSZ
source_window_start: YYYY-MM-DDTHH:MM:SSZ
source_window_end: YYYY-MM-DDTHH:MM:SSZ
app_kind: trident | trident-hip4
mode: observation | dry-run | live | paper | testnet | observer
network: mainnet | testnet | mixed | unknown
fresh_fetch_run: true | false
source_files:
  - name: ...
    original_path: ...
    mtime_utc: ...
contains_secrets: false
```

Si une valeur est inconnue, mettre `unknown` et expliquer pourquoi. Ne jamais
mettre de secret.

## 5. Regles de fraicheur

- Un digest sans fetch frais doit etre considere comme un snapshot historique.
- Pour audit operationnel "tout est OK", les donnees doivent venir du dernier
  fetch disponible et l'heure de fetch doit etre explicite.
- Pour audit PnL, la fraicheur est moins importante que l'exhaustivite, mais la
  fenetre analysee doit etre claire.
- Pour evaluer un changement recent, les annexes doivent contenir un cutoff et
  assez de donnees apres cutoff.

## 6. Format de reponse attendu de l'auditeur

L'auditeur externe doit rendre:

- verdict TRIDENT A/C;
- verdict HIP4 mainnet paper;
- verdict HIP4 observer;
- verdict PnL;
- donnees manquantes;
- risques critiques;
- hypotheses d'amelioration;
- tests/replays requis;
- actions qui demandent confirmation humaine.

## 7. Export compact local 2026-06-11

Commandes utilisees:

```bash
./scripts/fetch_all_data.sh
./scripts/fetch_trident_data.sh --logs-only
python scripts/export_trident_audit_pack.py --fresh-fetch-run --output server-data/audit_exports/20260611T135456Z
```

Note operationnelle:

- `fetch_all_data.sh` a termine avec code 0.
- La partie HIP4 a affiche une ligne ambigue
  `[ERROR] Fetch TRIDENT-HIP4 en erreur (code 0)`, mais les fichiers HIP4 ont
  bien ete rapatries et l'audit policy/market a ete regenere.
- `scripts/fetch_trident_data.sh` a ete etendu pour rapatrier aussi
  `runtime/trident/live_state_pod_a.json` et
  `runtime/trident/live_state_pod_c.json`.

Fichiers principaux generes:

| Fichier | Role | Contenu / limite |
| --- | --- | --- |
| `manifest.json` | Inventaire machine de l'export | `fresh_fetch_run=true`, `contains_secrets=false`. |
| `trident_ac_signal_decisions.jsonl` | Decisions/signaux/reviews A/C compacts | 193697 lignes. |
| `trident_ac_fill_events.csv` | Fills vus dans les logs A/C | 141 lignes data, ouvertures seulement; `close_fills=0`. |
| `trident_ac_closed_trades.csv` | Trades fermes A/C applicatifs | 31 trades: Pod A 25, Pod C 6. |
| `trident_ac_runtime_summary.json` | Status runtime Pod A/C | Snapshot de statut, reconciliation, PnL runtime. |
| `trident_ac_open_positions.json` | Positions ouvertes au snapshot | Pod A 1 position, Pod C 0 selon review. |
| `trident_ac_live_state_pod_a.json` | State store live Pod A | Positions, ordres, evenements runtime; pas de fills d'exit bruts. |
| `trident_ac_live_state_pod_c.json` | State store live Pod C | Positions, ordres, evenements runtime; pas de fills d'exit bruts. |
| `baseline_reference_status_20260513.md` | Statut des baselines replay | Definit la reference officielle courante. |
| `baseline_official_current_cli_20260513.md` | Rapport baseline full-bot courant | Total +859.83 USD, Pod A +780.72, Pod C +79.11. |
| `baseline_official_current_cli_20260513.json` | Rapport baseline machine | Contient les metriques detaillees et closed trades backtest. |
| `baseline_pod_a_evo11_comparison_20260513.md` | Comparaison promotion Pod A evo11 | Montre le delta +190.14 USD vs baseline corrigee. |
| `baseline_no_pod_c_20260513.md` | Replay Pod C off | Montre Pod A inchange et retire la contribution Pod C. |
| `hip4_decisions.jsonl` | Decisions HIP4 paper + observer compactes | 93610 lignes. |
| `hip4_trades.csv` | Trades HIP4 paper | 27 lignes data. |
| `hip4_settlements.csv` | Settlements/sorties HIP4 paper | 25 lignes data. |
| `hip4_shadow_exit_policies.csv` | Evenements shadow HIP4 | Support des policy replays. |
| `hip4_policy_replay.csv` | Resume policies shadow/active | 11 lignes data. |
| `hip4_policy_cutoff_replay.csv` | Replay par cutoff | 8 lignes data. |
| `hip4_runtime_statuses.json` | Status runtime HIP4 | Snapshot paper/observer/Nautilus si disponible. |

Stats `manifest.json`:

- A/C decisions/reviews: 193697.
- A/C fill rows: 141.
- A/C opened count: Pod A 118, Pod C 23.
- A/C close fill count: 0.
- A/C closed trades: Pod A 25, Pod C 6.
- A/C closed-trade PnL exporte: Pod A -5.90 USD, Pod C +0.46 USD.
- Baseline replay officielle A/C: total +859.83 USD, Pod A +780.72 USD,
  Pod B 0.00 USD, Pod C +79.11 USD.
- HIP4 decisions: 93610.
- HIP4 approved mainnet paper: 27.
- HIP4 decision rejects dominants: `observer_mode_signal_only`,
  `market_already_open`, `shock_guard_adverse_momentum`.

Conclusion manifest:

- A/C: PnL trade-level applicatif maintenant exploitable pour un premier audit,
  mais reconciliation exchange close-fill toujours incomplete.
- HIP4: audit PnL paper exploitable avec prudence sur le sample et les
  comparaisons shadow.
