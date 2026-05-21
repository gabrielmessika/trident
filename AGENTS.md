# TRIDENT Agent Instructions

Ces instructions sont le contexte persistant du repo TRIDENT pour Codex et les
agents de code.

## Sources de verite

- Le plan actif est `docs/trident_active_plan.md`. Le lire en priorite pour
  toute question de roadmap, statut des pods, decisions de promotion, live,
  dry-run, backtest, deploy ou recherche. En cas de contradiction avec un ancien
  document, `docs/trident_active_plan.md` gagne.
- La config principale prod/dry-run est `config/trident.toml`.
- Les docs historiques dans `docs/`, `plan_trident.md` et les anciens rapports
  servent de contexte, pas de source de verite courante.

## Donnees serveur

- Le dossier local des donnees serveur est `server-data/` a la racine du repo
  (`/workspaces/trident/server-data` dans ce workspace).
- Si un prompt mentionne `/server-data`, verifier d'abord si ce chemin absolu
  existe. Sinon, interpreter la demande comme `server-data/` repo-local.
- `scripts/fetch_trident_data.sh` rapatrie par defaut les donnees dans
  `server-data/` et les reviews dans `server-data/reviews/<timestamp>`.
- Les miroirs historiques `data/server_archive/` et `data/gbot_archive/` sont
  utiles pour les anciens replays, mais ne remplacent pas les donnees courantes
  de `server-data/`.

## Etat fonctionnel a garder en tete

- TRIDENT orchestre des pods Hyperliquid. Ne supposer aucune evolution
  fonctionnelle recente sans relire `docs/trident_active_plan.md`.
- `Pod A` est le pod crypto core.
- `Pod B` courant designe la branche `HIP4OutcomeEdgePod` en mainnet paper;
  l'ancien Pod B directionnel est legacy et non demarre par defaut.
- `Pod C` est le pod Tradfi directionnel builder-dex.
- Toute decision live/mainnet, tout changement de caps, de risk gate, de
  reconciliation ou de promotion doit suivre les guardrails du plan actif.

## Commandes usuelles

- Installer/synchroniser: `uv sync`
- Tests: `uv run pytest` ou `make test`
- Lancer en dry-run: `make run-dry`
- Healthcheck: `make healthcheck`
- Fetch donnees serveur: `./scripts/fetch_trident_data.sh --days 3`
- Review locale seulement: `./scripts/fetch_trident_data.sh --review-only`

## Travail sur backtests et rapports

- Les references officielles de backtest sont listees dans
  `docs/trident_active_plan.md`.
- Ecrire les nouveaux rapports experimentaux dans `server-data/replay_reports/`
  ou `tmp/` avec un nom explicite/date, sans ecraser les baselines officielles
  sauf demande explicite.
- Pour une nouvelle regle de trading, comparer contre la baseline full-bot
  pertinente, pas seulement contre un test isole.

## Hygiene de changement

- Ne pas commiter de secrets ni afficher les valeurs de `.env.trident`.
- Ne pas activer de live trading/mainnet par simple refactor: demander une
  confirmation explicite si une action peut envoyer de vrais ordres.
- Respecter les changements non lies deja presents dans le worktree.
- Garder les docs projet en francais quand elles prolongent les docs existantes.
