"""German, Spanish and Russian names of the Arizona lower-desert species.

The common names in ``plant_species.json`` are translatable keys (context
``GalleryData``, binding ``bundled_names``). ``DE`` and ``ES`` are merged by
``fill_translations.py`` and ``fill_spanish_translations.py``; Russian has no fill
script, so ``python scripts/translations_species_az.py`` writes ``RU`` into the
Russian catalogue (run it after ``update_translations.py``).
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# source name: (German, Spanish, Russian)
_NAMES = {
    "Pomegranate": ("Granatapfel", "Granado", "Гранат"),
    "Date Palm": ("Dattelpalme", "Palmera datilera", "Финиковая пальма"),
    "Jujube": ("Jujube", "Azufaifo", "Зизифус (унаби)"),
    "Loquat": ("Wollmispel", "Níspero japonés", "Локва"),
    "Grapefruit Tree": ("Grapefruitbaum", "Toronjo", "Грейпфрутовое дерево"),
    "Lime Tree": ("Limettenbaum", "Limero", "Лаймовое дерево"),
    "Mandarin Tree": ("Mandarinenbaum", "Mandarino", "Мандариновое дерево"),
    "Apricot Tree": ("Aprikosenbaum", "Albaricoquero", "Абрикос"),
    "Grape Vine": ("Weinrebe", "Vid", "Виноградная лоза"),
    "Velvet Mesquite": ("Samt-Mesquite", "Mezquite aterciopelado", "Бархатный мескит"),
    "Prickly Pear": ("Feigenkaktus", "Chumbera", "Опунция"),
    "Saguaro": ("Saguaro", "Saguaro", "Сагуаро"),
    "Tipu Tree": ("Tipubaum", "Tipa", "Типуана"),
    "Orchid Tree": ("Orchideenbaum", "Árbol orquídea", "Орхидейное дерево"),
    "Oleander": ("Oleander", "Adelfa", "Олеандр"),
    "Blue Palo Verde": ("Blauer Palo Verde", "Palo verde azul", "Паловерде голубой"),
    "Desert Ironwood": ("Wüsten-Eisenholz", "Palo fierro", "Пустынное железное дерево"),
    "Sweet Acacia": ("Süße Akazie", "Acacia aromática", "Акация Фарнеза"),
    "Desert Willow": ("Wüstenweide", "Sauce del desierto", "Пустынная ива"),
    "Mexican Fan Palm": ("Mexikanische Fächerpalme", "Palmera mexicana de abanico", "Мексиканская веерная пальма"),
    "Texas Sage": ("Texas-Salbei", "Salvia de Texas", "Техасский лейкофиллум"),
    "Red Bird of Paradise": ("Roter Paradiesvogelstrauch", "Ave del paraíso roja", "Цезальпиния прекраснейшая"),
    "Yellow Bells": ("Gelbe Trompetenblume", "Tronadora", "Текома стойкая"),
    "Bougainvillea": ("Bougainvillea", "Bugambilia", "Бугенвиллея"),
    "Red Yucca": ("Rote Yucca", "Yuca roja", "Красная юкка"),
    "Century Plant": ("Amerikanische Agave", "Agave americana", "Агава американская"),
    "Parry Agave": ("Parrys Agave", "Agave de Parry", "Агава Парри"),
    "Aloe Vera": ("Echte Aloe", "Aloe vera", "Алоэ вера"),
    "Creosote Bush": ("Kreosotbusch", "Gobernadora", "Креозотовый куст"),
    "Jojoba": ("Jojoba", "Jojoba", "Жожоба"),
    "Brittlebush": ("Brittlebush", "Incienso", "Энцелия мучнистая"),
    "Golden Barrel Cactus": ("Goldkugelkaktus", "Cacto barril dorado", "Эхинокактус Грузона"),
    "Lantana": ("Wandelröschen", "Lantana", "Лантана"),
    "Moringa": ("Meerrettichbaum", "Moringa", "Моринга"),
    "Okra": ("Okra", "Okra", "Окра"),
    "Cowpea": ("Augenbohne", "Caupí", "Коровий горох"),
    "Tepary Bean": ("Tepary-Bohne", "Frijol tépari", "Фасоль тепари"),
    "Roselle": ("Roselle", "Flor de Jamaica", "Роселла"),
    "Jicama": ("Jicama", "Jícama", "Джикама"),
    "Mustard Greens": ("Blattsenf", "Hojas de mostaza", "Листовая горчица"),
    "Butternut Squash": ("Butternut-Kürbis", "Calabaza butternut", "Тыква баттернат"),
}

DE = {"GalleryData": {k: v[0] for k, v in _NAMES.items()}}
ES = {"GalleryData": {k: v[1] for k, v in _NAMES.items()}}
RU = {"GalleryData": {k: v[2] for k, v in _NAMES.items()}}

_RU_TS = (
    Path(__file__).resolve().parents[1]
    / "src/open_garden_planner/resources/translations/open_garden_planner_ru.ts"
)


def fill_russian(path: Path = _RU_TS) -> int:
    """Write ``RU`` into the Russian catalogue; return how many entries were set."""
    tree = ET.parse(path)
    filled = 0
    for context in tree.getroot().findall("context"):
        name = context.find("name")
        if name is None or name.text not in RU:
            continue
        table = RU[name.text]
        for message in context.findall("message"):
            source = message.find("source")
            translation = message.find("translation")
            if source is None or translation is None or source.text not in table:
                continue
            translation.text = table[source.text or ""]
            translation.attrib.pop("type", None)
            filled += 1
    ET.indent(tree, space="    ")
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE TS>\n'
        + ET.tostring(tree.getroot(), encoding="unicode")
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return filled


if __name__ == "__main__":
    print(f"filled {fill_russian()} Russian entries")
    sys.exit(0)
