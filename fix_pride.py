import os
import requests
import xml.etree.ElementTree as ET
from pathlib import Path
from collections import defaultdict

# URL для завантаження XML
FILE_URL = "https://prideservice.net/files_lk/file_read.php"

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

def calc_price_usd(p: float) -> float:
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


def transform_availability(raw: str | None) -> str:
    if raw is None:
        return "false"
    return raw.strip()


# ---------- РОБОТА З КАТЕГОРІЯМИ ----------

def build_category_maps(shop: ET.Element):
    cat_elem = shop.find("categories")
    id_to_name: dict[str, str] = {}
    id_to_parent: dict[str, str] = {}
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


def get_blocked_category_ids(id_to_name: dict[str, str],
                             id_to_parent: dict[str, str]) -> set[str]:
    blocked: set[str] = set(BLOCKED_CATEGORY_IDS)

    # по ключових словах
    for cid, name in id_to_name.items():
        upper = name.upper()
        if any(key in upper for key in BLOCKED_CATEGORY_KEYWORDS):
            blocked.add(cid)

    parent_to_children: dict[str, list[str]] = defaultdict(list)
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


# ---------- ОБРОБКА XML ----------

def fix_structure_and_filter(text: str) -> ET.ElementTree:
    txt = text.lstrip("\ufeff").strip()
    root = ET.fromstring(txt)

    if root.tag == "yml_catalog":
        yml = root
        shop = yml.find("shop")
        if shop is None:
            raise RuntimeError("Не знайшов <shop> всередині <yml_catalog>")
    elif root.tag == "shop":
        date = root.attrib.pop("date", None)
        yml = ET.Element("yml_catalog")
        if date:
            if "T" in date:
                d, t = date.split("T", 1)
                t = t[:5]
                date = f"{d} {t}"
            yml.set("date", date)
        yml.append(root)
        shop = root
    else:
        raise RuntimeError(f"Неочікуваний корінь: <{root.tag}>")

    # прибираємо categoriesUA
    for bad in list(shop.findall("categoriesUA")):
        shop.remove(bad)

    # name перед currencies
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

    # карти категорій
    id_to_name, id_to_parent = build_category_maps(shop)
    blocked_cids = get_blocked_category_ids(id_to_name, id_to_parent)

    offers_parent = shop.find("offers")
    if offers_parent is None:
        raise RuntimeError("Не знайшов <offers> всередині <shop>")

    new_offers = []
    for offer in offers_parent.findall("offer"):
        cat_id_el = offer.find("categoryId")
        cid = (cat_id_el.text.strip() if cat_id_el is not None and cat_id_el.text else "")

        if cid in blocked_cids:
            continue

        price_uah_el = offer.find("priceUAH")
        price_usd_el = offer.find("priceUSD")

        price_usd = 0.0
        if price_usd_el is not None and price_usd_el.text:
            try:
                price_usd = float(price_usd_el.text.replace(",", "."))
            except Exception:
                price_usd = 0.0

        price_uah = 0.0
        if price_uah_el is not None and price_uah_el.text:
            try:
                price_uah = float(price_uah_el.text.replace(",", "."))
            except Exception:
                price_uah = 0.0

        if price_usd > 0:
            new_price_usd = calc_price_usd(price_usd)

            if price_uah > 0 and price_usd > 0:
                rate = price_uah / price_usd
            else:
                rate = 40.0

            new_price_uah = round(new_price_usd * rate, 2)

            if price_usd_el is not None:
                price_usd_el.text = f"{new_price_usd:.2f}"
            if price_uah_el is not None:
                price_uah_el.text = f"{new_price_uah:.2f}"

        avail_el = offer.find("available")
        raw_avail = avail_el.text if avail_el is not None else None
        new_avail = transform_availability(raw_avail)
        if avail_el is None:
            avail_el = ET.SubElement(offer, "available")
        avail_el.text = new_avail

        new_offers.append(offer)

    offers_parent[:] = new_offers

    return ET.ElementTree(yml)


# ---------- ЗАВАНТАЖЕННЯ XML ЧЕРЕЗ API ----------

def download_pride_xml() -> str:
    """
    Тягнемо файл через file_read.php з параметрами.
    PRIDE_CLIENT_ID і PRIDE_API_KEY беремо з env (GitHub Secrets).
    """
    client_id = os.environ.get("PRIDE_CLIENT_ID")
    api_key = os.environ.get("PRIDE_API_KEY")

    if not api_key:
        raise SystemExit("Не задано PRIDE_API_KEY в env/Secrets")

    # ⚠️ ТУТ МІСЦЕ, ДЕ МОЖЛИВІ ВАРІАЦІЇ НАЗВ ПАРАМЕТРІВ
    params = {
        "file_name": "pride0.xml",
        "clientID": client_id,
        "api_key": api_key,   # якщо не працює — міняєш на 'apikey' або інше
    }

    print("GET", FILE_URL, "params:", params)

    resp = requests.get(FILE_URL, params=params, timeout=60)
    resp.raise_for_status()

    text = resp.content.decode("utf-8", errors="ignore").lstrip("\ufeff").strip()

    # Діагностика, якщо не XML
    if not (text.startswith("<shop") or text.startswith("<yml_catalog")):
        print("⚠ Отримано не XML. Перші 300 символів відповіді:")
        print(text[:300])
        raise SystemExit("PRIDE не повернув XML. Перевір назви параметрів у params.")

    return text


def main():
    print("Скачую вихідний PRIDE XML через API...")
    xml_text = download_pride_xml()
    tree = fix_structure_and_filter(xml_text)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tree.write(OUTPUT_PATH, encoding="utf-8", xml_declaration=True)
    print("✅ Готовий файл збережено в", OUTPUT_PATH)


if __name__ == "__main__":
    main()
