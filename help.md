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

## Notes

- Formats supportés : PNG (avec alpha), JPEG, TIFF, BMP
- Le key est blanc plein si l'image n'a pas de canal alpha
