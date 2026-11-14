# Images fixes

Affiche une image fixe et la produit dans le pipeline MXL avec deux sorties : **Fill** (image) et **Key** (alpha canal). Idéal pour logos, bandeaux, sous-titres.

## À l'antenne et en preview

En haut de la page, deux vues côte à côte :

- **PROGRAMME** : l'image réellement à l'antenne (la vraie image de sortie, pas la vignette du fichier), avec son nom, sa définition, le cadrage appliqué et depuis quand elle est à l'antenne. Pendant un fondu, cette vue garde l'image d'avant et affiche « fondu en cours », puis passe à la nouvelle image à la fin du fondu : elle ne montre pas le fondu lui-même (pour le voir, **Stream**).
- **PREVIEW** : l'image choisie, rendue avec le **cadrage réglé** — ce que l'on voit est ce qui passera. Changer le cadrage ou la position met la preview à jour aussitôt. Une image transparente s'affiche sur un damier ; **Voir la découpe** montre l'alpha (le signal de découpe).

Les boutons **TAKE** (passe la preview à l'antenne, avec le fondu réglé) et **CUT** (sans fondu) sont entre les deux. Comme sur un mélangeur, les deux images **s'échangent** : l'image qui était à l'antenne passe en preview, et un second TAKE y revient. **Stream** ouvre la sortie en flux WebRTC dans une fenêtre du moniteur, pour voir un fondu en vrai (optionnel).

**Taille des écrans** : la poignée sous les deux vues les redimensionne (souris ou doigt, flèches au clavier) ; réduites, elles laissent la place à la bibliothèque. Double-clic sur la poignée : retour à la taille par défaut.

**PGM seul, à droite** : n'affiche que la vue PROGRAMME, dans une colonne à droite ; la bibliothèque prend alors toute la hauteur à gauche. La preview reste active : son nom s'affiche sous TAKE / CUT, et TAKE l'envoie à l'antenne. La poignée verticale entre les deux colonnes règle la largeur de la colonne PROGRAMME.

Ces deux réglages sont propres à votre navigateur : ils ne changent rien pour les autres utilisateurs.

## Choisir une image

Dans la bibliothèque (base des médias : bins, recherche, serveurs réseau, fiche Infos) : **double-clic** ou **Choisir** met l'image en **preview**, puis **TAKE** l'envoie à l'antenne. Avec **Take direct** coché (réglage du plugin, décoché par défaut), choisir une image la met directement à l'antenne, sans preview. Glisser une image sur un bin l'y range.

## Grilles (façon sampleur)

**Grille**, à côté de **Bibliothèque**, remplace la liste par des cases figées qui remplissent l'écran, sans défiler. Un clic sur une case met son image en preview, avec le cadrage de la case ; TAKE l'envoie à l'antenne. En Take direct, elle part aussitôt. Les cases en preview et à l'antenne sont marquées.

Une grille est une fiche de la base des médias, comme une playlist : elle se range dans les bins et les projets, se partage, s'exporte et s'importe. Chacun ouvre les siennes sur le panneau, en onglets (**Grilles ▾**) : pour une émission, préparez une grille par thème et passez de l'une à l'autre. Les onglets ouverts vous suivent d'un poste à l'autre.

**Préparer** : glissez des images de la bibliothèque sur les cases (plusieurs à la fois remplissent les cases vides), glissez une case sur une autre pour les échanger. Au doigt : choisissez l'image dans la bibliothèque, puis touchez la case. Le menu d'une case règle son cadrage (celui des réglages Cadrage et Position), son nom, ou la vide. Colonnes et rangées se choisissent en haut. L'antenne n'est pas touchée pendant la préparation, et tout est enregistré au fil des gestes.

## Image par défaut

**Définir** fait de l'image en preview, avec son cadrage, l'image par défaut. **Rappeler** (touche **D**) la met aussitôt à l'antenne, avec le fondu réglé ; l'image qui y était passe en preview. Elle est enregistrée avec le conteneur : un redéploiement la garde. Au démarrage, si aucune image n'est à l'antenne, c'est elle qui sort.

## Raccourcis clavier

| Touche | Geste |
|---|---|
| Espace | TAKE |
| Entrée | CUT |
| ← / → | Image précédente / suivante, dans l'ordre de la bibliothèque affichée, ou case voisine en vue Grille (elle passe en preview, ou à l'antenne en Take direct) |
| D | Rappeler l'image par défaut |
| Pavé numérique 1 à 9, 0 | Vue Grille : cases 1 à 10 |

Ils ne jouent pas pendant une saisie ni quand une fenêtre est ouverte.

## Éditer une image

Menu d'une image → **Éditer…** : recadrage, fond (dont l'image floutée), zones floutées ou pixelisées, bordure, ombre, texte, calques. L'éditeur propose d'emblée le **format de sortie de ce conteneur** (sa définition). L'enregistrement crée une nouvelle image à côté de l'originale, qui n'est jamais modifiée ; elle apparaît dans la bibliothèque et peut être mise en preview. Détails : article « Proxys de lecture et éditeur d'image » de l'Aide.

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

## Fondu

Le réglage **Fondu (ms)** donne la durée du fondu enchaîné au changement d'image (0 = coupe franche). Repositionner la même image reste une coupe.

## Macros

Actions : preview d'une image, Take, Cut, afficher une image, image suivante / précédente, rappeler et définir l'image par défaut ; réglage Take direct. L'état lisible indique l'image en preview, l'image à l'antenne et l'image par défaut. Une case d'une grille s'affiche par l'action « Afficher une case d'une grille » de la base des médias. Pour les macros, « suivante / précédente » suit l'ordre des noms dans le dossier de l'image en preview : une macro ne voit pas la bibliothèque affichée.

## Cas d'usage typique

Câbler **Fill** et **Key** sur les entrées DSK d'un mélangeur pour incruster un logo transparent.

## Sortie

- **Genlock** (activé par défaut) : Fill et Key sont calés sur la grille PTP, en phase avec le reste de la chaîne.
- **Sortie entrelacée native** (optionnelle : suit le balayage du format vidéo choisi à la création, par exemple un préréglage 1080i ; pas de réglage dans le panneau ⚙) : le générateur peut produire un flux entrelacé (émission champ par champ) pour être mixé avec des sources entrelacées. Progressif par défaut.

## Mode tranche (latence réduite)

Fill et Key peuvent être publiés **bande par bande** au lieu d'attendre l'image complète (mode tranche MXL) : l'aval en mode tranche démarre son traitement dès la première bande.

- **Interrupteur global** : **Réglages → MXL**, « Trancher toute la chaîne » (éteint par défaut) ; pas de réglage par conteneur dans l'interface. Prise d'effet au (re)déploiement.
- Sortie progressive uniquement ; en entrelacé, le chemin classique s'applique.

## Journal (diagnostic)

`log_level` (défaut « Événements ») : les événements marquants (changement d'image, repli sur un
chemin dégradé) sont toujours écrits ; passer en « Détaillé » ajoute un journal par trame — à
n'activer que le temps d'un diagnostic, le journal Docker n'est pas roté. Réglable à chaud
(`/log_level`, sans redéploiement) ou en persistant (config_schema, survit au redéploiement).

## Notes

- Formats supportés : PNG (avec alpha), JPEG, TIFF, BMP
- Le key est blanc plein si l'image n'a pas de canal alpha
