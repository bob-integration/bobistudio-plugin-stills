# Images fixes

Affiche une image fixe et la produit dans le pipeline MXL avec deux sorties : **Fill** (image) et **Key** (alpha canal). Idéal pour logos, bandeaux, sous-titres.

## Choisir une image

Dans **Médias → Images fixes** : choisir un dossier, les vignettes s'affichent. Cliquer une vignette pour la mettre en sortie.

## Sorties

| Sortie | Description |
|--------|-------------|
| `<hostname>` | Fill — l'image cadrée |
| `<hostname>_key` | Key — l'alpha en niveaux de gris (blanc = opaque, noir = transparent) |

## Cadrage

| Mode | Comportement |
|------|-------------|
| Pixel à pixel | Image à sa taille native, centrée |
| Au plus grand | Agrandie sans déformer (bandes transparentes) |
| Plein cadre | Étirée pour remplir (peut déformer) |

La section **Position & taille** ajoute un réglage X/Y/Taille en % du cadre (indépendant de la résolution).

## Cas d'usage typique

Câbler **Fill** et **Key** sur les entrées DSK d'un mélangeur pour incruster un logo transparent.

## Sortie

- **Genlock** (activé par défaut) : Fill et Key sont calés sur la grille PTP, en phase avec le reste de la chaîne.
- **Sortie entrelacée native** (optionnelle, réglage `scan`) : le générateur peut produire un flux entrelacé (émission champ par champ) pour être mixé avec des sources entrelacées. Progressif par défaut.

## Mode tranche (latence réduite)

Fill et Key peuvent être publiés **bande par bande** au lieu d'attendre l'image complète (mode tranche MXL) : l'aval en mode tranche démarre son traitement dès la première bande.

- **Opt-in** : paramètre `slice_mode` au déploiement (désactivé par défaut — comportement inchangé).
- Sortie progressive uniquement ; en entrelacé, le chemin classique s'applique.

## Journal (diagnostic)

`log_level` (défaut « Événements ») : les événements marquants (changement d'image, repli sur un
chemin dégradé) sont toujours écrits ; passer en « Détaillé » ajoute un journal par trame — à
n'activer que le temps d'un diagnostic, le journal Docker n'est pas roté. Réglable à chaud
(`/log_level`, sans redéploiement) ou en persistant (config_schema, survit au redéploiement).

## Notes

- Formats supportés : PNG (avec alpha), JPEG, TIFF, BMP
- Le key est blanc plein si l'image n'a pas de canal alpha
