import os
import requests
import xml.etree.ElementTree as ET
from pathlib import Path
from collections import defaultdict

FILE_URL = "https://prideservice.net/files_lk/file_read.php"
OUTPUT_PATH = Path("docs/prideMotoChina_fixed.xml")

# ---------- НАЛАШТУВАННЯ ----------

BLOCKED_CATEGORY_KEYWORDS = [
    "МОТОБЛОКИ",
    "БЕНЗОПИЛЫ, ТРИММЕРЫ",
]

BLOCKED_CATEGORY_IDS = {"1000000007", "1000000009"}


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

    for bad in list(shop.findall("categoriesUA")):
        shop.remove(bad)

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


def download_pride_xml() -> str:
    client_id = os.environ.get("PRIDE_CLIENT_ID")
    api_key = os.environ.get("PRIDE_API_KEY")

    if not api_key:
        raise SystemExit("Не задано PRIDE_API_KEY в env/Secrets")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": "https://lk.prideservice.net/",
    }

    # різні варіанти параметрів, які могли придумати
    candidate_params = []

    # з clientID
    if client_id:
        candidate_params.extend([
            {"file_name": "pride0.xml", "clientID": client_id, "api_key": api_key},
            {"file_name": "pride0.xml", "clientID": client_id, "apikey": api_key},
            {"file_name": "pride0.xml", "clientID": client_id, "key": api_key},
        ])

    # без clientID
    candidate_params.extend([
        {"file_name": "pride0.xml", "api_key": api_key},
        {"file_name": "pride0.xml", "apikey": api_key},
        {"file_name": "pride0.xml", "key": api_key},
    ])

    last_text = ""
    for params in candidate_params:
        print("Пробую запит з params:", params)
        resp = requests.get(FILE_URL, params=params, headers=headers, timeout=60)
        print("Статус:", resp.status_code)
        text = resp.content.decode("utf-8", errors="ignore").lstrip("\ufeff").strip()
        last_text = text

        if text.startswith("<shop") or text.startswith("<yml_catalog"):
            print("✅ Знайшов XML з параметрами:", params)
            return text

        # якщо повернуло script/history.back — явно не воно, пробуємо далі
        print("Відповідь не схожа на XML, перші 200 символів:")
        print(text[:200])

    # якщо сюди дійшли – жоден варіант не дав XML
    print("❌ Жоден варіант параметрів не повернув XML. Остання відповідь:")
    print(last_text[:300])
    raise SystemExit("PRIDE не повернув XML ні з одним набором параметрів.")


def main():
    print("Скачую вихідний PRIDE XML через API (підбір параметрів)...")
    xml_text = download_pride_xml()
    tree = fix_structure_and_filter(xml_text)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tree.write(OUTPUT_PATH, encoding="utf-8", xml_declaration=True)
    print("✅ Готовий файл збережено в", OUTPUT_PATH)


if __name__ == "__main__":
    main()
