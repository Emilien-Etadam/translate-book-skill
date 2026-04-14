---
name: translate-book
description: Translate books via deterministic Python preprocessing (`prepare.py`) plus LLM-only tasks (glossary translation, style detection, chunk translation, optional consistency correction).
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, Agent, AskUserQuestion
metadata: {"openclaw":{"requires":{"bins":["python3","ebook-convert"],"anyBins":["calibre","ebook-convert"]}}}
---

# Book Translation Skill

Tu orchestres la traduction d’un livre en gardant toute logique conditionnelle complexe dans Python, pas dans ce fichier.

## 1) Paramètres

Récupérer depuis la demande utilisateur :
- `file_path` (requis)
- `target_lang` (défaut `zh`)
- `style` (`formal|literary|technical|conversational|auto`, défaut `auto`)
- `chunk_size` (défaut `6000`)
- `pdf_engine` (`auto|calibre|marker`, défaut `auto`)
- `preserve_svg` (`auto|always|never`, défaut `auto`)
- `num_samples` (défaut `5`)
- `concurrency` (défaut `8`)
- `custom_instructions` (optionnel)

## 2) Préparation déterministe (commande unique)

Exécuter :

```bash
python3 {baseDir}/scripts/prepare.py "<file_path>" --olang "<target_lang>" --chunk-size <chunk_size> --style "<style>" --pdf-engine "<pdf_engine>" --preserve-svg "<preserve_svg>" --num-samples <num_samples>
```

Déduire le temp dir attendu : `<dirname(file_path)>/<basename_sans_extension>_temp`.
Puis lire `<temp_dir>/pipeline_state.json` (le champ `temp_dir` dans ce fichier est la référence finale).

Important :
- Ne pas implémenter ici de logique PDF, dedup, SVG, chunking, parsing `config.txt`.
- Se baser uniquement sur les faits de `pipeline_state.json`.
- Avec `--pdf-engine auto` sur un PDF, le choix du moteur est déterminé côté Python : Marker est prioritaire quand `marker_single` est disponible, sinon fallback Calibre avec warning.

## 3) Glossaire (condition simple)

Si `pipeline_state.json.glossary_needed == true` :
- lire `glossary_candidates.txt`
- lancer un seul sub-agent pour produire `glossary.json` (objet JSON plat strict)

Sinon : passer.

Prompt glossaire (remplacer `{LANG}`) :

```
Tu reçois une liste de termes extraits d'un livre. Pour chaque terme, fournis la traduction la plus appropriée vers {LANG} dans le contexte d'un livre.
Format de sortie : JSON strict, objet plat {"source": "cible"}. Aucun autre texte.
Si un nom propre n'a pas de traduction conventionnelle dans la langue cible, le conserver tel quel.
```

## 4) Résumé du livre (systématique)

Lire `<temp_dir>/summary_prompt.txt`.

Lancer un seul sub-agent avec son contenu.

Le sub-agent retourne un résumé structuré (GENRE/SUJET/TON/ÉPOQUE/PERSONNAGES/RÉSUMÉ) et l’orchestrateur écrit la réponse dans `<temp_dir>/book_summary.json`.

## 5) Exemples few-shot (systématique)

Lire `<temp_dir>/fewshot_prompt.txt`.

Remplacer le placeholder :

`[contenu de book_summary.json une fois produit — ce champ est un placeholder, rempli par l'orchestrateur]`

par le contenu réel de `<temp_dir>/book_summary.json`.

Lancer un seul sub-agent avec le prompt final.

L’orchestrateur écrit la réponse dans `<temp_dir>/fewshot_examples.txt`.

## 6) Style (condition simple)

Si `pipeline_state.json.style_detection_needed == true` :
- lire `chunk0001.txt`, `chunk0002.txt`, `chunk0003.txt` quand présents
- lancer un seul sub-agent de détection
- résultat attendu : un seul mot parmi `formal|literary|technical|conversational`

Sinon :
- style effectif = `pipeline_state.json.style`

Prompt détection style :

```
Lis ces extraits d'un livre. Détermine le registre stylistique dominant. Réponds par un seul mot : formal, literary, technical, ou conversational. Aucun autre texte.
```

## 7) Traduction parallèle

Lire `pipeline_state.json.total_chunks`.

Traduire `chunk0001.txt` à `chunkNNNN.txt` par batchs de `concurrency` (défaut 8), un sub-agent par chunk.

Pour chaque sub-agent :
- assembler le message utilisateur dans cet ordre :
  1) instruction de registre (style résolu à l’étape 6)
  2) résumé du livre (`book_summary.json`) formaté en 2-3 lignes: `Tu traduis un [genre] sur [sujet]. Ton : [ton]. [résumé].`
  3) glossaire (`glossary.json`) uniquement s’il existe
  4) exemples few-shot (`fewshot_examples.txt`)
  5) contexte glissant (avant/après)
  6) chunk à traduire
- inclure contexte glissant : 5 lignes avant / 5 lignes après
- traduire uniquement `[CHUNK À TRADUIRE]`
- écrire `output_chunkNNNN.txt`
- valider : même nombre de lignes `Txxxx:` et mêmes ids, même ordre

Note coût: résumé + few-shot ajoutent en général ~500-800 tokens par sub-agent (coût marginal multiplié par le nombre de chunks), avec un impact significatif sur la cohérence globale et la justesse des choix lexicaux.

Prompt système traducteur (remplacer `{LANG}` et `{STYLE_INSTRUCTION}`) :

```
Segments numérotés d'un livre. Traduis chaque segment vers {LANG}.
Si un glossaire est fourni dans le message utilisateur, applique-le strictement.
{STYLE_INSTRUCTION}
Les sections [CONTEXTE PRÉCÉDENT] et [CONTEXTE SUIVANT] sont fournies pour la continuité. Ne les traduis pas. Ne les inclus pas dans ta sortie. Traduis uniquement les lignes de la section [CHUNK À TRADUIRE].
Les lignes commençant par # sont des commentaires de contexte. Ne les traduis pas. Ne les inclus pas dans ta sortie.
Garde chaque préfixe Txxxx: identique à l'entrée (T + 4 chiffres, «: », espace).
Interdit: fusionner, supprimer ou réordonner des segments. Le nombre de lignes sortie = entrée.
Conserve littéralement \n \r \\ (ne pas les interpréter).
Noms propres: ne pas traduire sauf usage courant dans {LANG}.
Texte dans balises HTML résiduelles (attributs, entités): ne pas traduire ce contenu.
Sortie: uniquement des lignes Txxxx:… Aucun préambule ni commentaire.
```

Mapping style :
- `formal` → `Traduis dans un registre formel et soutenu.`
- `literary` → `Traduis dans un registre littéraire, en préservant le rythme et les figures de style.`
- `technical` → `Traduis dans un registre technique précis. Privilégie la clarté et l'exactitude terminologique.`
- `conversational` → `Traduis dans un registre courant et naturel, comme une conversation orale.`

## 8) Post-traitement (commandes uniques)

Exécuter :

```bash
python3 {baseDir}/scripts/merge_and_build.py --temp-dir "<temp_dir>" --title "<translated_title>" --olang "<target_lang>"
```

Puis :

```bash
python3 {baseDir}/scripts/validate_consistency.py --temp-dir "<temp_dir>" --olang "<target_lang>"
```

## 9) Correction de cohérence (condition simple)

Si `consistency_report.txt` contient des problèmes :
- lancer un seul sub-agent de correction ciblée (`Txxxx` listés seulement)
- patcher uniquement ces lignes dans `output_chunk*.txt`
- relancer `merge_and_build.py`

Sinon : terminé.

Prompt correction :

```
Tu reçois un rapport d'incohérences terminologiques dans une traduction. Pour chaque segment listé, fournis la version corrigée. Format de sortie : une ligne par segment, `Txxxx: texte corrigé`. Ne corrige que les segments listés. Ne modifie rien d'autre.
```
