import requests
import xml.etree.ElementTree as ET
from pathlib import Path
from collections import defaultdict

# Посилання на сирий файл PRIDE
SOURCE_URL = "https://prideservice.net/files_lk/file_read.php?file_name=pride0.xml"

# Куди кладемо готовий файл (GitHub Pages -> docs/)
OUTPUT_PATH = Path("docs/prideMotoChina_fixed.xml")


# ---------- НАЛАШТУВАННЯ ----------

# Ключові слова категорій, які треба ВИДАЛИТИ (по назві)
BLOCKED_CATEGORY_KEYWORDS = [
    "МОТОБЛОКИ",
    "БЕНЗОПИЛЫ, ТРИММЕРЫ",
]

# Явні ID категорій, які треба вирізати (і їх підкатегорії)
BLOCKED_CATEGORY_IDS = {"1000000007", "1000000009"}


# ---------- КАЛЬКУЛЯЦІЯ ЦІН ----------

def calc_price_usd(p):
    """
    Калькуляція націнки в USD (твоя логіка).
    """
    if p < 0.1:
        return p * 4.5
    elif p < 0.3:
        return p * 3.3
    elif p < 0.75:
        return p * 2.7
    elif p < 2:
        return p * 1.85
    elif p < 5:
        return p * 1.65
    elif p < 10:
        return p * 1.55
    elif p < 20:
        return p * 1.5
    elif p < 30:
        return p * 1.43
    elif p < 50:
        return p * 1.38
    elif p < 75:
        return p * 1.35
    elif p < 100:
        return p * 1.33
    else:
        return p * 1.3


def transform_availability(raw):
    """
    Наявність: в цьому файлі тільки "так/ні" по суті.
    Поки що просто:
      - якщо тег є -> залишаємо як є
      - якщо немає -> ставимо "false"
    """
    if raw is None:
        return "false"
    return raw.strip()


# ---------- РОБОТА З КАТЕГОРІЯМИ ----------

def build_category_maps(shop):
    """Збираємо categoryId -> (name, parentId)."""
    cat_elem = shop.find("categories")
    id_to_name = {}
    id_to_parent = {}
    if cat_elem is None:
        return id_to_name, id_to_parent

    for c in cat_elem.findall("category"):
        cid = c.attrib.get("id")
        if not cid:
            continue
        name = (c.text or "").strip()
        id_to_name[cid] = name
        parent = c.attrib.get("parentId")
        if parent:
            id_to_parent[cid] = parent
    return id_to_name, id_to_parent


def get_blocked_category_ids(id_to_name, id_to_parent):
    """
    Знаходимо всі категорії, які треба прибрати:
      - за явним списком ID
      - за ключовими словами в назві
      - плюс усі їхні дочірні категорії
    """
    blocked = set(BLOCKED_CATEGORY_IDS)

    # 1) за ключовими словами в назві
    for cid, name in id_to_name.items():
        upper = name.upper()
        if any(key in upper for key in BLOCKED_CATEGORY_KEYWORDS):
            blocked.add(cid)

    # 2) додаємо всіх нащадків заблокованих
    parent_to_children = defaultdict(list)
    for cid, parent in id_to_parent.items():
        parent_to_children[parent].append(cid)

    queue = list(blocked)
    while queue:
        cur = queue.pop()
        for child in parent_to_children.get(cur, []):
            if child not in blocked:
                blocked.add(child)
                queue.append(child)

    return blocked


# ---------- ОСНОВНА ОБРОБКА XML ----------

def fix_structure_and_filter(content):
    """
    1. Прибирає BOM
    2. Приводить до <yml_catalog><shop>...</shop></yml_catalog>
    3. Видаляє <categoriesUA>
    4. Вирізає товари з небажаних категорій
    5. Перераховує priceUSD + priceUAH по calc_price_usd
    6. Мінімально нормалізує наявність
    """
    txt = content.lstrip("\ufeff")
    root = ET.fromstring(txt)

    # Якщо вже yml_catalog – просто працюємо з ним
    if root.tag == "yml_catalog":
        yml = root
        shop = yml.find("shop")
        if shop is None:
            raise RuntimeError("Не знайшов <shop> всередині <yml_catalog>")
    elif root.tag == "shop":
        # Обгортаємо в yml_catalog
        date = root.attrib.pop("date", None)
        yml = ET.Element("yml_catalog")
        if date:
            # 2025-11-13T05:15:27 -> 2025-11-13 05:15
            if "T" in date:
                d, t = date.split("T", 1)
                t = t[:5]
                date = f"{d} {t}"
            yml.set("date", date)
        yml.append(root)
        shop = root
    else:
        raise RuntimeError("Неочікуваний корінь: <%s>" % root.tag)

    # Прибираємо categoriesUA
    for bad in list(shop.findall("categoriesUA")):
        shop.remove(bad)

    # Переставляємо name перед currencies (косметика, але хай буде)
    children = list(shop)
    name = shop.find("name")
    currencies = shop.find("currencies")
    ordered = []
    if name is not None:
        ordered.append(name)
    if currencies is not None:
        ordered.append(currencies)
    for ch in children:
        if ch not in (name, currencies):
            ordered.append(ch)
    shop[:] = ordered

    # Карта категорій
    id_to_name, id_to_parent = build_category_maps(shop)
    blocked_cids = get_blocked_category_ids(id_to_name, id_to_parent)

    # Обробка offers
    offers_parent = shop.find("offers")
    if offers_parent is None:
        raise RuntimeError("Не знайшов <offers> всередині <shop>")

    new_offers = []
    for offer in offers_parent.findall("offer"):
        # категорія товару
        cat_id_el = offer.find("categoryId")
        cid = (cat_id_el.text.strip() if cat_id_el is not None and cat_id_el.text else "")

        # якщо категорія заблокована – викидаємо товар
        if cid in blocked_cids:
            continue

        # ціни
        price_uah_el = offer.find("priceUAH")
        price_usd_el = offer.find("priceUSD")

        # читаємо USD
        price_usd = 0.0
        if price_usd_el is not None and price_usd_el.text:
            try:
                price_usd = float(price_usd_el.text.replace(",", "."))
            except Exception:
                price_usd = 0.0

        # читаємо UAH
        price_uah = 0.0
        if price_uah_el is not None and price_uah_el.text:
            try:
                price_uah = float(price_uah_el.text.replace(",", "."))
            except Exception:
                price_uah = 0.0

        # якщо USD є – рахуємо нові ціни
        if price_usd > 0:
            new_price_usd = calc_price_usd(price_usd)

            if price_uah > 0 and price_usd > 0:
                rate = price_uah / price_usd
            else:
                # запасний варіант, якщо в файлі немає UAH – припустимо курс 40
                rate = 40.0

            new_price_uah = round(new_price_usd * rate, 2)

            if price_usd_el is not None:
                price_usd_el.text = f"{new_price_usd:.2f}"
            if price_uah_el is not None:
                price_uah_el.text = f"{new_price_uah:.2f}"

        # Наявність
        avail_el = offer.find("available")
        raw_avail = avail_el.text if avail_el is not None else None
        new_avail = transform_availability(raw_avail)
        if avail_el is None:
            avail_el = ET.SubElement(offer, "available")
        avail_el.text = new_avail

        new_offers.append(offer)

    offers_parent[:] = new_offers

    return ET.ElementTree(yml)


def main():
    print("Скачую вихідний PRIDE XML...")
    resp = requests.get(SOURCE_URL, timeout=60)
    resp.raise_for_status()

    tree = fix_structure_and_filter(resp.content.decode("utf-8", errors="ignore"))

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tree.write(OUTPUT_PATH, encoding="utf-8", xml_declaration=True)
    print("Готовий файл збережено в", OUTPUT_PATH)


if __name__ == "__main__":
    main()
