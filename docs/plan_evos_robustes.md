Oui. Le bon plan, maintenant, c’est d’arrêter les petites règles isolées et de construire des évolutions “robustes” comme un pipeline de décision: données propres, score combiné, validation OOS, shadow, puis live tiny.

**Objectif**
Améliorer le PnL sans couper le bot, en réduisant la taille ou l’exposition quand plusieurs signaux faibles se cumulent contre le trade. Pas de nouveau veto brutal sauf preuve très forte.

**Définition Robuste**
Une évolution est robuste seulement si elle:

- bat la baseline full-bot, pas seulement un sous-ensemble;
- reste positive sur au moins deux fenêtres temporelles;
- ne cappe pas trop de winners;
- inclut fees, spread, slippage, fills manqués, capital occupé;
- reste rollbackable par config;
- passe shadow/paper avant live.

**Phase 1: Données Et Baseline**
1. Construire un dataset unique `A/C decision journal` avec:
   - signaux acceptés, rejetés, acceptés non ouverts;
   - trades fermés;
   - PnL, MFE, MAE, close reason;
   - setup, symbol, session, regime;
   - spread/depth/cost/fill-quality;
   - external reference Pod C;
   - dynamic symbol guard state;
   - live sizing effectivement appliqué.

2. Refaire les splits:
   - baseline avril/mai;
   - live pre-split avant `2026-06-03`;
   - live post-split;
   - forward OOS quotidien après maintenant.

3. Créer une commande unique:
   - `scripts/run_pnl_robust_candidate_lab.py`
   - sortie: un rapport par candidat avec IS/OOS, winners cappés, losers cappés, PF, DD, concentration symbole.

**Phase 2: Pod A Score Combiné**
Construire un score Pod A qui combine les signaux déjà audités:

- `dynamic_symbol_guard_state`
- rolling `symbol/setup` PnL/PF
- fill-quality P117
- microstructure P115
- parent latent PnL pour scale-in P118
- stop/early-failure history
- session/regime

Évolution candidate:
- pas de block;
- sizing multiplicatif borné:
  - `1.00` normal;
  - `0.75` risque modéré;
  - `0.50` risque élevé;
  - `0.35` seulement si plusieurs signaux mauvais;
- jamais sous `min_notional`;
- log complet des raisons.

Validation:
- comparer contre P1-08 live actuel;
- exiger delta positif OOS;
- mesurer winners perdus;
- ne promouvoir que si le gain ne vient pas d’un seul symbole.

**Phase 3: Pod A Scale-In Sélectif**
Reprendre `parent_plus50`, mais le rendre robuste:

- add-on seulement si parent en gain latent `>=50 bps`;
- parent non `early_failure_exit`;
- fill-quality correcte;
- symbol/setup pas en probation;
- maximum 1 add-on par trade;
- cap add-on faible, genre `25%`.

Validation:
- hors INJ doit rester positif;
- post-split doit être positif;
- comparer capital-hour et drawdown;
- shadow d’abord, pas live direct.

**Phase 4: Pod C External Reference**
C’est probablement le meilleur vrai levier Pod C, mais il manque l’OOS.

Plan:

1. Restaurer une baseline avec coverage reference correcte.
2. Rejouer P103 sur baseline + live récent.
3. Tester uniquement cap-only, pas veto:
   - stale reference;
   - premium absolu;
   - momentum contre trade;
   - candidate default 5m.
4. Exiger que le cap marche sur baseline et live.

Candidate live possible:
- `C-PNL-02 external_reference_cap50`
- seulement si coverage baseline `>80%`;
- rollback par config;
- pas de changement sur Silver live sans preuve séparée.

**Phase 5: Pod C Oil Défensif**
Ne pas chercher à augmenter oil. Pour l’instant, l’amélioration PnL est défensive.

Plan:
- garder P1-09 stoplight;
- ajouter shadow `oil_pair_dedupe_240m`;
- ne prendre qu’un signal indépendant par symbole/fenêtre;
- conditionner à CL + BRENTOIL ou régime `high_vol/mixed`;
- réduire ou suspendre oil si closed+open PnL reste négatif.

Promotion possible seulement si:
- `closed_plus_open_pnl > 0`;
- au moins 10 trades promus;
- PF positif;
- open unrealized non négatif.

**Phase 6: Pod C Liquidity Floor**
Piste petite mais saine.

Plan:
- shadow `bucket_notional_lt100k_cap50`;
- ajouter slippage/fill model;
- vérifier que ce n’est pas seulement GOLD;
- combiner avec external reference, pas seul.

Pas de live tant que le gain reste `+1.56` live et GOLD-only.

**Phase 7: Process De Promotion**
Ordre proposé:

1. Monitoring live de `A-PNL-01` déjà promu.
2. Dataset robuste commun.
3. Pod C external reference OOS.
4. Pod A combined sizing score.
5. Pod A scale-in sélectif.
6. Oil defensive dedupe/stoplight.
7. Pod C liquidity floor.

**Règle Live**
Une seule promotion à la fois. Après chaque promotion:

- deploy;
- fetch/review;
- 24-48h observation;
- rapport PnL closed + open;
- rollback si `unexpected_live_action_changed>0`, reconciliation KO, ou drawdown anormal.

**Priorité Immédiate**
La prochaine vraie brique à coder devrait être le `pnl_robust_candidate_lab`: un harness commun qui empêche de se faire piéger par des règles jolies sur une fenêtre mais mauvaises ailleurs. Ensuite, on l’utilise pour sortir le premier candidat robuste: probablement `C-PNL-02 external reference cap-only` ou `Pod A combined sizing score`.

**Implémentation 2026-06-23: Robust Candidate Lab**

Statut: première brique codée et rejouée, strictement `research_only_no_live_change`.

Ajouts:

- `scripts/run_pnl_robust_candidate_lab.py`;
- `tests/test_pnl_robust_candidate_lab.py`;
- rapport généré: `server-data/replay_reports/pnl_robust_candidate_lab_20260623/pnl_robust_candidate_lab.md`;
- sorties machine: `candidate_summary.csv`, `period_summary.csv`, `pod_a_decision_journal.csv`, `pnl_robust_candidate_lab.json`.

Run initial:

- `27` candidats agrégés;
- `49` périodes de validation;
- `93` décisions/trades Pod A rejoués dans le journal de décision;
- `0` candidat `promotable_candidate`;
- aucun changement live, aucun changement config, aucun impact deploy/fetch.

Lecture du scoreboard:

- `pod_c_external_reference::cap50_candidate_default_5m`: meilleur signal shadow, `+40.05` USD sur la fenêtre couverte, mais baseline reference coverage insuffisante et une seule période couverte. C’est la priorité de recherche, pas un live direct.
- `pod_c_external_reference::cap50_abs_premium_gt_50`: shadow `+22.30` USD, même blocage coverage/OOS.
- `pod_c_external_reference::cap50_candidate_loose_5m`: shadow `+16.09` USD, même blocage coverage/OOS.
- `pod_c_execution_cost::cap_spread_gte_1_not_high_activity`: shadow `+6.96` USD sur deux périodes couvertes, mais une période est flat; à retester avec plus de données avant promotion.
- `pod_c_execution_cost::cap_bucket_notional_lt100k`: shadow `+6.25` USD, mais trop concentré symbole (`100%`).
- `pod_a_combined_sizing_v0`: rejeté, `-16.73` USD total, `pre_split -24.55`, `post_split +7.81`; la règle cappe plus de PnL gagnant (`413.50`) que de PnL perdant (`344.56`), donc elle n’est pas robuste.
- Oil deduped reste seulement un proxy positif (`+6.98`) sur une observation unique; les variantes raw restent fortement négatives.

**Implémentation 2026-06-23: C-PNL-02 Forward OOS**

Statut: étape solide réalisée, toujours `research_only_no_live_change`.

Ajouts:

- `scripts/run_p103_pod_c_external_reference_validation.py` accepte maintenant `--journal` pour lire les `trade_close` du journal live Pod C, utiliser les champs `external_reference_*` embarqués dans `setup_details`, et splitter les trades en fenêtres OOS avec `--journal-split`;
- `scripts/run_pnl_robust_candidate_lab.py` accepte maintenant plusieurs `--p103-report`, pour agréger le P103 historique et le forward OOS;
- tests étendus dans `tests/test_p103_pod_c_external_reference_validation.py`.

Run serveur frais:

- fetch A/C: `server-data/reviews/20260623T123504Z/review_summary.md`, statut `PASS`;
- P1-03: journal setup coverage `1000/1000`, shadow coverage `1000/1000`, `live_action_unchanged_false=0`;
- rapport forward: `server-data/replay_reports/p103_pod_c_external_reference_forward_oos_20260623/p103_pod_c_external_reference_validation.md`;
- lab agrégé: `server-data/replay_reports/pnl_robust_candidate_lab_20260623/pnl_robust_candidate_lab.md`.

Résultat forward OOS:

- fenêtre `2026-06-15_to_2026-06-21`: `11` trades, base `+1.50`, coverage `100%`;
- fenêtre `2026-06-22_to_2026-06-23`: `9` trades, base `-9.29`, coverage `100%`;
- `cap50_candidate_default_5m`: `-0.75` puis `+2.95`;
- `cap50_abs_premium_gt_50`: `-0.75` puis `+2.25`;
- `cap50_candidate_loose_5m`: `-0.75` puis `+1.40`;
- `cap50_counter_momentum_5m_6bps`: `-0.10` puis `+1.40`.

Lecture:

- Le signal récent reste fort, mais il n’est pas robuste: il a une fenêtre forward négative et la baseline avril/mai reste non couverte.
- Le problème principal de `candidate_default_5m` est le bloc `stale`: sur `2026-06-15_to_2026-06-21`, beaucoup de références très âgées/week-end auraient cappé des winners.
- Décision: aucune promotion C-PNL-02. Prochaine version à tester = `C-PNL-02 v2 fresh-only`, qui ignore les stale/missing comme signal de cap et ne cappe que les dislocations/momentum quand la référence est fraîche.

**Implémentation 2026-06-23: C-PNL-02 v2 fresh-only**

Statut: implémenté/rejoué, toujours `research_only_no_live_change`.

Ajouts:

- P103 expose maintenant quatre règles `fresh-only`: `fresh_abs_premium_gt_50`, `fresh_counter_momentum_5m_6bps`, `fresh_candidate_default_5m`, `fresh_candidate_loose_5m`;
- chaque règle existe aussi en outcome cap-only `50%` (`cap50_fresh_*`);
- une référence est utilisable seulement si elle est disponible et âgée de `<=900s`;
- les références `missing/stale` ne déclenchent plus de cap dans cette variante;
- correction de robustesse: les anciens payloads `setup_details` à zéro ne sont plus interprétés comme des références embarquées valides, donc les rapports historiques peuvent retomber sur l'enrichissement Yahoo au lieu de perdre la coverage.

Replays:

- historique P103: `server-data/replay_reports/p103_pod_c_external_reference_cap50_20260623/p103_pod_c_external_reference_validation.md`;
- forward OOS P103: `server-data/replay_reports/p103_pod_c_external_reference_forward_oos_20260623/p103_pod_c_external_reference_validation.md`;
- lab agrégé: `server-data/replay_reports/pnl_robust_candidate_lab_20260623/pnl_robust_candidate_lab.md`;
- lab agrégé mis à jour: `31` candidats, `77` périodes, décision globale `research_only_no_live_change`.

Résultat agrégé `fresh-only`:

- `cap50_fresh_candidate_default_5m`: `+34.28` USD, `3/0` périodes couvertes positives, concentration max `50%`, bloqué uniquement par `insufficient_coverage_periods=1`;
- `cap50_fresh_abs_premium_gt_50`: `+25.32` USD, `3/0` positives, mais concentration symbole `100%` sur une fenêtre forward;
- `cap50_fresh_counter_momentum_5m_6bps`: `+10.43` USD, `3/0` positives, concentration max `50%`;
- `cap50_fresh_candidate_loose_5m`: `+8.77` USD, `3/0` positives, concentration max `60%`.

Détail du meilleur candidat `cap50_fresh_candidate_default_5m`:

- baseline ancienne `2026-04-05_to_2026-05-13`: `+0.00`, coverage `0%`, donc non validable;
- historique récent `2026-05-24_to_2026-06-11`: `+29.85`, coverage `91.67%`;
- forward `2026-06-15_to_2026-06-21`: `+1.48`, coverage `100%`;
- forward `2026-06-22_to_2026-06-23`: `+2.95`, coverage `100%`.

Lecture:

- C'est le meilleur signal Pod C actuel: il enlève le problème du stale, reste positif sur les trois fenêtres couvertes, et ne dépend pas d'un seul symbole.
- Il ne passe pas encore en live car une fenêtre historique reste non couverte; le lab le classe donc `shadow_candidate`, pas `promotable_candidate`.
- Prochaine étape solide: garder `C-PNL-02 v2 fresh-only` en tête de liste, collecter plus de forward OOS avec coverage `100%`, puis préparer éventuellement un flag dormant/configurable seulement si une nouvelle fenêtre confirme le signal.
- Aucun changement live/config/deploy n'est effectué par cette implémentation.

**Implémentation 2026-06-23: C-PNL-02 v2 fresh-only shadow telemetry**

Statut: câblage observation-only, sans changement d'action live.

Ajouts:

- `app/trident/pod_c/external_reference_shadow.py` exporte maintenant les champs shadow `fresh-only`:
  `would_block_external_reference_fresh_abs_premium_gt_50`,
  `would_block_external_reference_fresh_counter_momentum_5m_6bps`,
  `would_block_external_reference_fresh_candidate_default_5m`,
  `would_block_external_reference_fresh_candidate_loose_5m`;
- ces champs restent strictement observation-only avec `external_reference_shadow_live_action_unchanged=true`;
- `scripts/fetch_trident_data.sh` compte ces nouveaux gates dans l'audit P1-03;
- `scripts/export_trident_audit_pack.py` les inclut dans le pack d'audit compact.

Lecture:

- Le prochain déploiement code-only ferait apparaître ces champs dans les journaux Pod C, mais ne changerait pas les tailles ni les entrées.
- Ce câblage permet de suivre `C-PNL-02 v2 fresh-only` en production comme shadow propre avant toute discussion de flag actif.

**Implémentation 2026-06-23: A-PNL-08/P119 v2 loss-probation**

Statut: code activable par config, désactivé par défaut, aucun ordre/live change.

Ajouts:

- `app/trident/pod_a/live_risk.py` sait maintenant appliquer une loss-probation rolling `symbol/setup` indépendante de la recovery sizing;
- nouveaux champs config Pod A:
  `dynamic_symbol_guard_loss_probation_sizing_enabled=false`,
  `dynamic_symbol_guard_loss_probation_multiplier=0.50`,
  `dynamic_symbol_guard_loss_probation_min_closed_trades=2`,
  `dynamic_symbol_guard_loss_probation_max_pnl_usd=-16.0`,
  `dynamic_symbol_guard_loss_probation_max_profit_factor=0.60`;
- `scripts/fetch_trident_data.sh` et `scripts/export_trident_audit_pack.py` exportent les compteurs/raisons `loss_probation`;
- tests ciblés ajoutés.

Replays:

- P119 v2: `server-data/replay_reports/p119_loss_probation_cap_v2_20260623/p119_loss_probation_cap_audit.md`;
- lab final: `server-data/replay_reports/pnl_robust_candidate_lab_20260623/pnl_robust_candidate_lab.md`;
- validation locale: `43` tests ciblés OK, review-only `server-data/reviews/20260623T131803Z/review_summary.md` en `PASS`.

Résultat:

- `pod_a_loss_probation::cap50_lb8_min2_pnl-16_pf0p6` est le seul `promotable_candidate` du lab;
- delta total `+30.95` USD, `2/0` périodes positives, concentration symbole max `29.55%`;
- all: `-60.05 -> -29.10`, PF `0.8078 -> 0.8834`;
- pre_split: `+11.80`, PF `1.0927 -> 1.2009`;
- post_split: `+19.14`, PF quasi flat `0.5634 -> 0.5635`;
- coût: cappe encore `+63.66` USD de winners, mais réduit `-125.56` USD de losers.

Lecture:

- C’est la première évolution PnL robuste vraiment promotable depuis le début du plan.
- Risque acceptable: cap-only, rollbackable, pas de blocage, pas de dépendance à un seul symbole.
- Promotion possible sans deux mois de shadow si on accepte une observation post-déploiement courte `24-48h` avec rollback strict.

**Implémentation 2026-06-23: C-PNL-02 fresh-only cap dormant**

Statut: code activable par config, désactivé par défaut, aucun ordre/live change.

Ajouts:

- `app/trident/pod_c/external_reference_sizing.py` applique un cap fresh-only configurable;
- nouveaux champs config Pod C:
  `external_reference_fresh_cap_sizing_enabled=false`,
  `external_reference_fresh_cap_gate="fresh_candidate_default_5m"`,
  `external_reference_fresh_cap_multiplier=0.50`;
- `PodCLiveRunner` applique la policy après l’annotation shadow et avant le cap notional live/risk gate;
- P1-03 distingue maintenant `expected_live_action_changed` et `unexpected_live_action_changed`, comme P1-08;
- audit pack compact étendu avec les champs `external_reference_fresh_cap_*`.

Résultat replay:

- `cap50_fresh_candidate_default_5m`: `+34.28` USD, `3/0` périodes couvertes positives, concentration max `50%`;
- bloqué par `insufficient_coverage_periods=1` car la baseline ancienne reste à `0%` de coverage;
- forward OOS couvert: `+1.48` puis `+2.95`;
- review locale actuelle: `P1-03 PASS`, `unexpected_live_action_changed=0`, `fresh_cap_sizing_active_records=0` avec flag off.

Lecture:

- Ce n’est pas aussi propre que P119, mais c’est le meilleur candidat Pod C.
- Si on veut prendre plus de risque, il peut être promu après P119, avec activation par config et surveillance immédiate, plutôt qu’attendre deux mois.

**Bilan D’Épuisement 2026-06-23**

Pistes robustes restantes:

- `A-PNL-08/P119 v2`: promotable.
- `C-PNL-02 fresh-only`: code-ready, shadow/risk-accepted candidate.

Pistes rejetées ou non prioritaires:

- `pod_a_combined_sizing_v0`: rejeté, `-16.73`, cappe trop de winners.
- `A-PNL-02 recovery sizing`: positif trop faible et inférieur à A-PNL-01.
- `A-PNL-03 A-grade headroom`: `+0.08`, non significatif.
- `A-PNL-04 early_failure_exit`: garder actif; le désactiver dégrade.
- `A-PNL-05 microstructure`: mauvais bucket live non monotone, rejet cap simple.
- `A-PNL-07 fill/cost/depth`: pas de seuil simple monotone.
- `P118 scale-in parent_plus50`: `+16.31`, mais une seule période et hors INJ tombe négatif.
- `P120 oil dedupe`: proxy `+6.98`, trop petit; oil live stoplight reste `hold_exposure`.
- `P121 session/liquidity` et `P122 execution_cost`: signaux petits, instables ou concentrés.

**Liste Évolutive De Promotion**

1. `A-PNL-08/P119 v2 loss_probation cap50_lb8_min2_pnl-16_pf0p6`: priorité 1 à promouvoir. Activer `dynamic_symbol_guard_loss_probation_sizing_enabled=true`, redéployer, fetch/review, puis surveiller `loss_probation_sizing_active_records`, PnL closed+open et `unexpected_live_action_changed=0`.
2. `C-PNL-02 fresh-only external_reference cap50_fresh_candidate_default_5m`: priorité 2, plus risquée mais défendable. Activer seulement après P119 ou si on accepte explicitement deux changements proches; vérifier `external_reference_fresh_cap_sizing_active_records`, `expected_live_action_changed`, et absence de surconcentration oil/GOLD.
3. `A-PNL-01 dynamic_symbol_guard cap50/cap50`: déjà live, continuer à monitorer; ne pas le retirer.
4. `P118 parent_plus50 scale-in`: garder en research; pas de live direct.
5. `Pod C execution_cost bucket_notional_lt100k` / `spread_not_high_activity`: shadow seulement.
6. `Oil dedupe/stoplight`: ne pas augmenter l’exposition tant que closed+open oil reste négatif.
