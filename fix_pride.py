import os
import requests
import xml.etree.ElementTree as ET
from pathlib import Path
from collections import defaultdict

# URL-и API PRIDE
CHECK_API_URL = "https://lk.prideservice.net/db/check_api.php"
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


def transform_availability(raw: str | None) -> str:
    """
    Наявність: в цьому файлі по суті "так/ні".
    Поки що:
      - якщо тег є – повертаємо як є (обрізавши пробіли)
      - якщо немає – ставимо "false"
    """
    if raw is None:
        return "false"
    return raw.strip()


# ---------- РОБОТА З КАТЕГОРІЯМИ ----------

def build_category_maps(shop: ET.Element):
    """Збираємо categoryId -> name, parentId."""
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
    """
    Всі категорії, які треба прибрати:
      - явні ID
      - ті, де в назві є ключові слова
      - + усі їхні нащадки
    """
    blocked: set[str] = set(BLOCKED_CATEGORY_IDS)

    # 1) за ключовими словами в назві
    for cid, name in id_to_name.items():
        upper = name.upper()
        if any(key in upper for key in BLOCKED_CATEGORY_KEYWORDS):
            blocked.add(cid)

    # 2) додаємо всіх нащадків
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


# ---------- ОСНОВНА ОБРОБКА XML ----------

def fix_structure_and_filter(text: str) -> ET.ElementTree:
    """
    1. Прибирає BOM
    2. Приводить до <yml_catalog><shop>...</shop></yml_catalog>
    3. Видаляє <categoriesUA>
    4. Вирізає товари з небажаних категорій
    5. Перераховує priceUSD + priceUAH по calc_price_usd
    6. Мінімально нормалізує наявність
    """
    txt = text.lstrip("\ufeff").strip()
    root = ET.fromstring(txt)

    # Якщо вже yml_catalog – працюємо з ним
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
            if "T" in date:
                d, t = date.split("T", 1)
                t = t[:5]
                date = f"{d} {t}"
            yml.set("date", date)
        yml.append(root)
        shop = root
    else:
        raise RuntimeError(f"Неочікуваний корінь: <{root.tag}> (очікував <shop> або <yml_catalog>)")

    # Прибираємо categoriesUA
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

        # якщо категорія заблокована – викидаємо
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
                # запасний варіант, якщо UAH нема – припускаємо курс 40
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


def download_pride_xml() -> str:
    """
    1. Читає clientID та api_key з env (GitHub Secrets).
    2. Викликає check_api.php (чисто як перевірку, можна буде відключити).
    3. Тягне сам файл з file_read.php.
    Повертає текст XML.
    """
    client_id = os.environ.get("PRIDE_CLIENT_ID")
    api_key = os.environ.get("PRIDE_API_KEY")

    if not client_id or not api_key:
        raise SystemExit("Не задані PRIDE_CLIENT_ID / PRIDE_API_KEY в env")

    # 1. Перевірка API (може щось повертати типу OK/ERROR)
    try:
        resp_check = requests.post(
            CHECK_API_URL,
            data={
                "clientID": client_id,
                "api_key": api_key,  # якщо параметр називається інакше – підправиш тут
            },
            timeout=20,
        )
        print("check_api статус:", resp_check.status_code)
        print("check_api відповідь (обрізано):", resp_check.text[:200])
    except Exception as e:
        print("Помилка при зверненні до check_api.php:", e)

    # 2. Тягнемо сам XML
    resp = requests.get(
        FILE_URL,
        params={
            "file_name": "pride0.xml",
            "clientID": client_id,
            "api_key": api_key,  # якщо вони хочуть іншу назву – ти просто змінюєш ключ
        },
        timeout=60,
    )
    resp.raise_for_status()

    text = resp.content.decode("utf-8", errors="ignore")
    if not ("<shop" in text or "<yml_catalog" in text):
        print("⚠ Отримано не схоже на XML. Перші 300 символів:")
        print(text[:300])
        raise SystemExit("PRIDE не повернув XML (можливо, параметри API не ті).")

    return text


def main():
    print("Скачую вихідний PRIDE XML через API...")
    xml_text = download_pride_xml()

    tree = fix_structure_and_filter(xml_text)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tree.write(OUTPUT_PATH, encoding="utf-8", xml_declaration=True)
    print("Готовий файл збережено в", OUTPUT_PATH)


if __name__ == "__main__":
    main()
