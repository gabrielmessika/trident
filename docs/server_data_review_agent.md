# Agent review donnees serveur TRIDENT

## Mission

Utiliser ce guide quand l'utilisateur dit, en substance:

> J'ai fetche les data server sur les 2 bots. Verifie que tout est ok cote
> TRIDENT. Analyse les resultats paper et observation TRIDENT-HIP4.

L'objectif est de produire une review courte, factuelle et actionnable apres
avoir rafraichi les donnees locales des deux apps. Ne pas redeployer, ne pas
changer de config et ne pas activer d'execution live/mainnet sauf demande
explicite.

## Sources de verite

- Lire d'abord `docs/trident_active_plan.md`.
- Donnees TRIDENT A/C: `server-data/`.
- Donnees TRIDENT-HIP4: `server-data/hip4/`.
- Config TRIDENT A/C courante: `config/trident.toml`.
- Config HIP-4 paper courante: `config/hip4_outcome_mainnet_paper.toml`.
- Fetch global TRIDENT A/C + TRIDENT-HIP4: `./scripts/fetch_all_data.sh`.
- Fetch TRIDENT A/C: `./scripts/fetch_trident_data.sh`.
- Fetch HIP-4: `./trident-hip4/fetch_data.sh`.

En cas de contradiction entre un vieux rapport et `docs/trident_active_plan.md`,
le plan actif gagne.

## Regles de prudence

- Ne jamais afficher les secrets ni les valeurs de `.env.trident`.
- Ne pas activer de live trading/mainnet.
- Separer strictement `TRIDENT` A/C de `TRIDENT-HIP4`.
- Ne pas melanger observation mainnet HIP-4 et paper trading HIP-4.
- Quand une modification recente est analysee, isoler les lignes apres le
  redeploiement ou le timestamp de changement. Si ce timestamp n'est pas connu,
  le dire clairement.
- Ne pas conclure sur la performance d'un changement si aucun trade ou early
  exit frais n'a eu lieu apres ce changement.
- Ne pas conclure que Nautilus apporte un gain trading si le shadow ne couvre
  encore aucune decision approuvee ou aucun settlement.

## Fetch initial obligatoire

Au debut de l'analyse, lancer:

```bash
./scripts/fetch_all_data.sh
```

Ce fetch doit preceder la lecture des reviews et snapshots pour verifier que les
donnees TRIDENT A/C et TRIDENT-HIP4 sont a jour. Si la commande echoue,
continuer la review avec les donnees locales disponibles, mais donner un verdict
`WARN` ou `KO` selon l'impact et citer l'erreur de fetch. Ne pas relancer le
fetch en boucle; un seul essai suffit sauf demande explicite.

## Fichiers a ouvrir ensuite

Trouver les reviews les plus recentes. Preferer l'heure de modification ou le
chemin imprime par le fetch, pas un simple tri lexical: un dossier manuel du
type `local-recheck-*` peut trier apres les dossiers dates `YYYYMMDD...` sans
etre la review fraiche.

```bash
find server-data/reviews -mindepth 2 -maxdepth 2 -name review_summary.md -printf '%T@ %p\n' | sort -n | tail -n 1
find server-data/hip4/reviews -mindepth 2 -maxdepth 2 -name hip4_outcome_run_review.md -printf '%T@ %p\n' | sort -n | tail -n 1
find server-data/hip4/reviews -mindepth 2 -maxdepth 2 -name hip4_next_review_focus.md -printf '%T@ %p\n' | sort -n | tail -n 1
```

Trouver les derniers snapshots API:

```bash
find server-data/api -maxdepth 1 -type f -printf '%T@ %p\n' | sort -n | tail -n 20
find server-data/hip4/api -maxdepth 1 -type f -printf '%T@ %p\n' | sort -n | tail -n 30
```

Regarder aussi:

- `server-data/runtime/pod_a_live_status.json`
- `server-data/runtime/pod_c_live_status.json`
- `server-data/docker/trident-api.log`
- `server-data/docker/pod-a-live.log`
- `server-data/docker/pod-c-live.log`
- `server-data/hip4/runtime/hip4_outcome_status.json`
- `server-data/hip4/runtime/hip4_outcome_mainnet_status.json`
- `server-data/hip4/docker/hip4-api.log`
- `server-data/hip4/docker/hip4-outcome-paper.log`
- `server-data/hip4/docker/hip4-mainnet-observer.log`

## Review TRIDENT A/C

Donner un verdict `OK`, `WARN` ou `KO`.

Checks obligatoires:

- La derniere review `server-data/reviews/.../review_summary.md` existe.
- `/health` est `ok`.
- Les pods attendus sont Pod A et Pod C uniquement.
- Pod A et Pod C sont `healthy` dans `/api/report`.
- `live_trading_paused=false` ou bien expliquer pourquoi ce n'est pas le cas.
- `live_reconciliation.ready=true` pour Pod A et Pod C.
- Les listes de reconciliation sont vides:
  - `unknown_exchange_positions`
  - `missing_exchange_positions`
  - `side_mismatches`
  - `open_orders` inconnus
  - `trigger_orders` orphelins
- `ownership_conflict_count=0`.
- Pas de `Traceback` recent dans les logs docker.
- Pas d'erreur `Decimal is not JSON serializable`.
- Confirmer qu'aucun service HIP-4/Pod B n'est attendu dans TRIDENT A/C.
- Si `/api/state` contient encore une entree `pod_b`, verifier qu'elle est
  `enabled=false`; ne pas l'interpreter comme service attendu.
- Si le snapshot `/api/state` n'expose pas directement `exchange_network`,
  utiliser la review generee et les runtime statuses pour reporter mode/network.
- Pour les comptes Hyperliquid `unifiedAccount` / portfolio margin,
  `perp_account_value_usd=0` avec `spot_usdc_available>0` peut etre normal:
  TRIDENT doit alors utiliser `hl_available_usd` et la source
  `unified_spot_usdc`. Ne pas en faire un WARN collateral si
  `hl_available_usd>0`, `live_reconciliation.ready=true` et aucune raison de
  reconciliation n'est remontee.
- Si un pod a `accepted_count > 0` mais `opened_count = 0`, ne pas conclure
  "pas de signal": analyser les `signal.execution.skipped_open` dans
  `server-data/logs/pod_a_live.jsonl` / `pod_c_live.jsonl`.
- Pour Pod A, comparer les `signal.risk.target_notional_usd` acceptes avec
  `trident.execution.live_max_order_notional_usd`. Si tous les acceptes/skips
  sont au-dessus du cap live, classer `WARN`: la strategie accepte des plans que
  l'execution live bloque avant envoi d'ordre.
- Avec le sizing live cap-aware, les plans live Pod A/C au-dessus du cap doivent
  porter `setup_details.live_cap_active=true` et un
  `target_notional_usd <= live_max_order_notional_usd` avant risk gate. Le
  `target_notional_usd` peut etre sous le cap si le levier max symbole impose
  `margin_usd * max_leverage` plus bas. Si des skips `notional_above_live_cap`
  reapparaissent, c'est un bug de chemin live ou un add-on non cappe a
  investiguer.
- Pour Pod A, separer les periodes ou le regime/allocation donne
  `pod_a target_usd=0` (ex. `DeadZone`) des periodes ou des plans sont acceptes
  puis skips. Le premier cas est attendu, le second indique un mismatch
  sizing/cap ou un blocage execution.
- Pour Pod C, si les rejets sont `margin_below_min`, rappeler
  `pod_c.min_margin_usd` et `pod_c.size_multiplier`: les plans sont rejetes par
  risk gate avant execution.

Synthese a rendre:

- mode et network observes;
- etat Pod A / Pod C;
- positions ouvertes, ordres ouverts, fills, PnL realise et latent;
- incidents ou anomalies, avec fichier source;
- decision: rien a faire, surveiller, ou corriger.

## Review TRIDENT-HIP4

Donner un verdict separe pour:

- `mainnet_paper`: bot HIP-4 en paper, source des trades paper;
- `mainnet`: observer seul, source d'observations marche, sans PnL execute.

Checks status/config:

- Le dernier `server-data/hip4/api/hip4-outcome-*.json` hors
  `hip4-outcome-mainnet-*` indique `mode=paper` et `process_state=running`.
- `server-data/hip4/api/hip4-outcome-mainnet-*.json` indique un observer
  actif, sans execution.
- Le status expose les `pnl_levers`.
- Les derniers fichiers API doivent aussi etre choisis par timestamp/mtime, pas
  seulement par tri lexical.
- Apres le changement early-exit du `2026-05-25`, verifier si applicable:
  - `early_exit_ev_exit_fraction=0.5`;
  - `early_exit_reentry_lock_until_settlement=true`;
  - shadow policy `ev_plus_2pct_partial_runner` presente.

Metrics paper a extraire depuis la review HIP-4:

- opportunities;
- observations;
- approved;
- trades;
- settlements;
- PnL net;
- profit factor;
- win rate;
- worst loss;
- Brier score/calibration si disponible;
- nombre de marches/expiries couverts;
- raison du verdict readiness.

Analyse obligatoire des resultats paper:

- Ne pas se contenter du win rate.
- Regarder si quelques petits wins sont manges par un seul gros loss.
- Grouper les trades/settlements par `market_id`, expiry et side quand les
  colonnes existent.
- Comparer:
  - PnL net total;
  - somme des wins;
  - somme des losses;
  - plus grosse perte;
  - contribution du pire trade au PnL total;
  - nombre de positions ouvertes sur le meme market apres early exit.
- Si un early exit a libere du capital puis permis une re-entry perdante, le
  signaler explicitement.
- Distinguer ce qui aurait ete evite par une sortie tot et ce qui n'aurait
  jamais existe sans cette sortie tot.
- Pour `decisions.jsonl`, le schema courant met la decision dans
  `supervisor_decision`: lire `supervisor_decision.approved`,
  `supervisor_decision.reason` et `signal.market_id` / `signal.side` /
  `signal.edge_type`.
- Apres le cutoff d'un changement, compter explicitement:
  - trades approuves;
  - active early exits;
  - rejets `market_already_open`;
  - rejets `early_exit_reentry_lock`;
  - re-entry opposite-side sur le meme `market_id` avant settlement.
- Si les rejections post-cutoff sont surtout `market_already_open` apres une
  sortie partielle, c'est un signe que le runner bloque bien le churn; ce n'est
  pas une preuve de performance.

Analyse obligatoire des observations:

- Les donnees `mainnet` observer servent a valider couverture, flux et qualite
  d'observation, pas la performance executee.
- Comparer observations vs opportunities.
- Identifier si l'observer voit des marches que paper ne trade pas.
- Chercher erreurs, trous de collecte ou stale status.
- Ne pas promouvoir une regle sur observation seule.
- Les `ConnectionResetError` / `BrokenPipeError` dans `hip4-api.log` peuvent
  venir de clients HTTP qui coupent la connexion. Les classer en WARN log-noise
  si les statuses HIP-4 restent frais, `process_state=running` et
  `last_error=null`; les classer KO seulement si l'API/status devient stale ou
  si l'erreur touche le runner paper/observer.

Analyse obligatoire Nautilus Shadow HIP-4:

- Verifier si le shadow Nautilus est present via le dernier
  `server-data/hip4/api/hip4-nautilus-shadow-*.json` et
  `server-data/hip4/logs/hip4_nautilus_shadow/status.json`.
- Si present, extraire au minimum:
  - `shadow_ready`;
  - `reason`;
  - `errors`;
  - source des books Nautilus;
  - `snapshot_count`;
  - marche(s) selectionne(s).
- Verifier que `server-data/hip4/logs/hip4_nautilus_shadow/data_quality.csv`
  existe, est non vide, et que `parity_compare.csv` et `book_snapshots.jsonl`
  progressent aussi. Si `data_quality.csv` est absent ou vide, conclure que
  Nautilus est importable mais pas encore utile analytiquement.
- Lire la section `### Nautilus Shadow Data Quality` de la review HIP-4 et/ou
  le champ `nautilus_shadow` du JSON latest.
- Pour la partie decision-time, verifier explicitement:
  - `matched_decision_count`;
  - `unmatched_decision_count`;
  - `approved_count`;
  - `rejected_count`;
  - `would_block_count`;
  - `would_block_approved_count`;
  - `matched_settlement_count`.
- Le prochain signal utile est `approved_count > 0` puis
  `matched_settlement_count > 0`. Tant que ces deux compteurs restent a zero,
  ne pas conclure sur le PnL ou la performance trading de Nautilus.
- Si `would_block_count > 0`, separer les raisons de blocage:
  `reference_divergence_gt_50bps`, book age, skew YES/NO, ou quality score bas.
  Verifier surtout si ces blocages toucheraient des decisions approuvees
  (`would_block_approved_count`) ou seulement des decisions deja rejetees.
- Comparer les buckets suivants quand ils existent:
  - PnL/PF/Brier ou settlement outcome par `quality_score`;
  - PnL/PF/Brier ou settlement outcome par `max_book_age_ms`;
  - PnL/PF/Brier ou settlement outcome par `book_pair_skew_ms`;
  - PnL/PF/Brier ou settlement outcome par `reference_divergence_bps`.
- Si apres une expiry complete Nautilus couvre toujours zero decision approuvee,
  classer `WARN` data coverage et diagnostiquer:
  - nombre de marches observes vs `max_markets`;
  - underlyings couverts;
  - marches selectionnes vs marches trades par HIP-4;
  - age des rows qualite au moment des decisions;
  - erreurs de subscription ou de symbologie.
- Ne pas proposer de brancher Nautilus comme garde-fou actif tant qu'il n'a pas
  montre, sur plusieurs jours mainnet paper, qu'il aurait evite des faux
  signaux ou pertes sans bloquer de bons trades.

## Fichiers HIP-4 a examiner

Repertoire principal paper:

`server-data/hip4/logs/hip4_outcome_mainnet_paper/`

Fichiers utiles, selon presence:

- `trades.csv`
- `settlements.csv`
- `early_exits.csv`
- `decisions.jsonl`
- `opportunities.csv`
- `market_observations.jsonl`
- `shadow_exit_policies.csv`
- `shadow_sizing.csv`
- `latency_stats.csv`

Observer:

`server-data/hip4/logs/hip4_outcome_mainnet/`

Nautilus shadow, si present:

`server-data/hip4/logs/hip4_nautilus_shadow/`

- `status.json`
- `data_quality.csv`
- `parity_compare.csv`
- `book_snapshots.jsonl`
- `instruments.jsonl`

## Commandes d'aide

Resume rapide des derniers fichiers:

```bash
python3 - <<'PY'
from pathlib import Path

groups = [
    ("trident_review", list(Path(".").glob("server-data/reviews/*/review_summary.md"))),
    ("hip4_review", list(Path(".").glob("server-data/hip4/reviews/*/hip4_outcome_run_review.md"))),
    ("hip4_next_focus", list(Path(".").glob("server-data/hip4/reviews/*/hip4_next_review_focus.md"))),
    (
        "hip4_paper_api",
        [
            path
            for path in Path(".").glob("server-data/hip4/api/hip4-outcome-*.json")
            if not path.name.startswith("hip4-outcome-mainnet-")
        ],
    ),
    ("hip4_mainnet_observer_api", list(Path(".").glob("server-data/hip4/api/hip4-outcome-mainnet-*.json"))),
]
for label, files in groups:
    files = sorted(files, key=lambda path: path.stat().st_mtime)
    print(label, "=>", files[-1] if files else "MISSING")
PY
```

Resume post-changement HIP-4, avec un cutoff a adapter. Ce resume lit le schema
courant `decisions.jsonl` (`supervisor_decision` + `signal`):

```bash
python3 - <<'PY'
from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

CUTOFF = datetime.fromisoformat("2026-05-25T08:33:00+00:00")
BASE = Path("server-data/hip4/logs/hip4_outcome_mainnet_paper")

def parse_ts(value: str):
    if not value:
        return None
    value = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None

def csv_rows(path: Path):
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))

def jsonl_rows(path: Path):
    if not path.exists() or path.stat().st_size == 0:
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows

for name, loader in [
    ("trades.csv", csv_rows),
    ("settlements.csv", csv_rows),
    ("early_exits.csv", csv_rows),
    ("opportunities.csv", csv_rows),
    ("shadow_exit_policies.csv", csv_rows),
    ("shadow_sizing.csv", csv_rows),
    ("decisions.jsonl", jsonl_rows),
    ("market_observations.jsonl", jsonl_rows),
]:
    rows = loader(BASE / name)
    post = []
    max_ts = None
    for row in rows:
        ts = parse_ts(str(row.get("timestamp") or row.get("ts") or row.get("created_at") or ""))
        if ts:
            max_ts = ts if max_ts is None or ts > max_ts else max_ts
            if ts >= CUTOFF:
                post.append(row)
    print(f"{name}: total={len(rows)} post_cutoff={len(post)} max_ts={max_ts}")

decisions = jsonl_rows(BASE / "decisions.jsonl")
post_reasons = Counter()
post_edges = Counter()
post_sides = Counter()
post_market_reentry_blocks = Counter()
approved = 0
post_approved = 0
for row in decisions:
    ts = parse_ts(str(row.get("timestamp") or row.get("ts") or row.get("created_at") or ""))
    decision = row.get("supervisor_decision") or row.get("decision") or {}
    signal = row.get("signal") or {}
    if decision.get("approved") is True:
        approved += 1
    if ts and ts >= CUTOFF:
        reason = str(decision.get("reason") or row.get("reason") or "")
        post_reasons[reason] += 1
        post_edges[str(signal.get("edge_type") or "")] += 1
        post_sides[str(signal.get("side") or "")] += 1
        if decision.get("approved") is True:
            post_approved += 1
        if reason in {"market_already_open", "early_exit_reentry_lock"}:
            post_market_reentry_blocks[(reason, str(signal.get("market_id") or ""))] += 1

print(f"decisions approved_total={approved} post_approved={post_approved}")
print("post reasons:", post_reasons.most_common(12))
print("post edge mix:", post_edges.most_common(8))
print("post side mix:", post_sides.most_common(8))
print("post reentry blocks:", post_market_reentry_blocks.most_common(12))
PY
```

## Interpretation early-exit HIP-4

Apres le changement du `2026-05-25`, l'attendu est:

- Les sorties EV `bid_over_conservative_hold_ev` doivent etre des
  `partial_exit` a 50%, pas des full exits.
- Le runner restant doit empecher une re-entry immediate via
  `market_already_open`.
- Si une sortie full defensive arrive, une re-entry sur le meme
  market/expiry doit etre bloquee par `early_exit_reentry_lock` jusqu'au
  settlement.
- Les shadow policies doivent permettre de comparer:
  - `hold_to_settlement`;
  - `ev_plus_2pct_full`;
  - `ev_plus_2pct_partial_runner`.
- Si les lignes post-changement ne contiennent aucun trade ou aucun early exit
  actif, conclure: instrumentation OK, pas encore de preuve de performance.

## Sizing HIP-4

Attention aux minimums Hyperliquid:

- Ne pas proposer de passer automatiquement un sizing theorique sous le minimum
  au minimum executable.
- Si `shadow_sizing.csv` indique que le sizing Kelly/theorique est sous
  `min_order_value_usdc`, le verdict experimental est `skip` ou `shadow`, pas
  "arrondir au minimum".
- Mentionner si le sizing actif reste fixe faute de signal executable.

## Format de reponse attendu

Repondre en francais, avec cette structure:

1. Verdict global.
2. TRIDENT A/C: OK/WARN/KO, faits importants, anomalies.
3. TRIDENT-HIP4 paper: metrics, PnL, early exits, pertes/wins, conclusion.
4. TRIDENT-HIP4 observer: couverture/collecte, anomalies, utilite.
5. Ce qu'on peut conclure maintenant.
6. Ce qu'il faut checker a la prochaine review.

Etre explicite sur les limites:

- "On peut conclure X."
- "On ne peut pas encore conclure Y parce que ..."
- "Le prochain signal utile sera ..."
