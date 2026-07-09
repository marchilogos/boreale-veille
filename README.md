# 🌌 Boréale — veille « saison de travail »

Outil de veille pour dénicher les bonnes offres de saison (hiver 2026-27, couple, Europe d'abord),
au-delà des plateformes. **Pas un tracker, pas un CRM** : une fois l'offre repérée, Jo prend la suite.

## Comment ça tourne

- **2 passages/jour** (≈ 7h et 17h Paris) via GitHub Actions — zéro serveur à gérer.
- Pipeline : collecte (`sources.yml`) → dédup (`data/seen.json`) → **mémoire des rejets**
  (`data/rejects.json`, une offre écartée ne revient jamais) → portes/règles → scoring IA
  (modèle léger en lot, modèle fort pour les tops) → `docs/data/offers.json`.
- Le site (cockpit) est servi par GitHub Pages depuis `docs/`.
- Sans clé API : repli heuristique, scores marqués « provisoires ».

## Recalibrer (sans coder)

Tout le comportement vit dans **`calibrage.yml`** (poids, portes, géo, journal des règles)
et **`sources.yml`** (le consortium). On les modifie en parlant à Fable — chaque changement
est un commit, donc traçable.

## Secrets attendus (Settings → Secrets and variables → Actions)

- `ANTHROPIC_API_KEY` — moteur de scoring (≈ 5-15 €/mois).

## Alertes

Si le pipeline casse, GitHub envoie un email automatiquement (Actions → échec).
Une source muette ≥ 4 passages remonte dans `meta.alerts`, affichée dans Coulisses.
