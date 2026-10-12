"""Generate synthetic Amazon order records for every client in the corpus.

    ~/.venvs/anonymizer-redactor/bin/python -m eval.make_amazon CORPUS_ROOT

Each client - the ten households and the two discovery households - gets a
year of Amazon orders written four ways, each at least ten pages:

``Amazon_Your_Orders_<year>.pdf``
    The "Your Orders" page printed to PDF: an order card per order with the
    date placed, total, ship-to name and order number, then the items.
``Amazon_Order_Details_<year>.pdf``
    Order-details printouts, one order per page: shipping and billing
    addresses, "Visa ending in 1234", per-item sellers, gift messages.
``Amazon_Order_History_Export_<year>.pdf``
    The "Request My Data" order-history export, rendered as a landscape table.
``Amazon_Invoices_<year>.pdf``
    The printable invoice, "Final Details for Order #", one order a page, with
    digital (D01-) orders and refunds. Written after the fixes the first three
    layouts prompted, and scored before any change for it: the held-out check.

Each PDF gets a same-named JSON label in the corpus's own shape. The identity
comes from the client's existing labels - the same people, the same home, the
same card last-fours - so a run over a household sees one consistent client.
Each client also sends gifts to three to five people outside the household,
with their own names and addresses: who the client ships things to is exactly
what a financial declaration gets argued over. Everything is invented and
deterministic: the same corpus produces the same files.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import random
import re
from pathlib import Path

import pymupdf

# --------------------------------------------------------------------------
# who the clients are, read from their own labels
# --------------------------------------------------------------------------

# The two discovery households carry no statement holder block; their people
# and homes come from the pay stubs, property records and README of that set.
DISCOVERY = {
    "D-HH1": {"primary": "DANIEL R HOLBROOK", "secondary": "MEGAN L HOLBROOK",
              "address": ("1184 N CANYON CREST DR", "LEHI", "UT", "84043", "84043-2271"),
              "dir": "synthetic_household_discovery_2025"},
    "D-HH2": {"primary": "MARCUS A VALDEZ", "secondary": "ALYSSA M VALDEZ",
              "address": ("4417 W SAGEBRUSH LN", "SOUTH JORDAN", "UT", "84009", "84009-2210"),
              "dir": "synthetic_household_discovery_2025"},
}


def _client_of(path: str) -> str | None:
    m = re.search(r"/(HH\d\d)_[A-Z]+/", path)
    if m:
        return m.group(1)
    if "synthetic_household_discovery_2025" in path:
        m = re.search(r"(?:^|[/_])(HH[12])(?:[/_]|$)", path)
        return "D-" + m.group(1) if m else None
    return None


def clients(root: Path) -> dict[str, dict]:
    seen = collections.defaultdict(lambda: {"holders": collections.Counter(),
                                            "addr": collections.Counter(),
                                            "cards": collections.Counter(),
                                            "people": collections.Counter(),
                                            "years": collections.Counter(), "dir": None})
    for label in root.rglob("*.json"):
        if "amazon" in label.parts:
            continue
        client = _client_of("/" + str(label.relative_to(root)))
        if not client:
            continue
        try:
            data = json.loads(label.read_text())
        except (OSError, ValueError):
            continue
        entry = seen[client]
        parts = label.relative_to(root).parts
        if client.startswith("HH"):
            entry["dir"] = str(Path(*parts[:2]))
        holder = data.get("holder") if isinstance(data.get("holder"), dict) else {}
        if holder.get("primary"):
            entry["holders"][(holder.get("primary"), holder.get("secondary"))] += 1
            a = holder.get("address") or {}
            if a.get("line1"):
                entry["addr"][(a["line1"], a.get("city"), a.get("state"), a.get("zip"),
                               a.get("zip4"))] += 1
        for key in ("card4", "card4b"):
            value = data.get(key) or (data.get("data") or {}).get(key)
            if value:
                entry["cards"][str(value)] += 1
        for key in ("patient", "student", "employee"):
            value = data.get(key) or (data.get("data") or {}).get(key)
            if isinstance(value, str):
                entry["people"][value] += 1
        m = re.search(r"(20\d\d)-\d\d", label.name)
        if m:
            entry["years"][m.group(1)] += 1

    out: dict[str, dict] = {}
    for client, entry in sorted(seen.items()):
        if client in DISCOVERY:
            fixed = DISCOVERY[client]
            primary, secondary, address = fixed["primary"], fixed["secondary"], fixed["address"]
            folder = fixed["dir"]
        else:
            pairs = [p for p, _n in entry["holders"].most_common()]
            primary = pairs[0][0]
            secondary = next((s for p, s in pairs if s), None)
            address = entry["addr"].most_common(1)[0][0]
            folder = entry["dir"]
        adults = {primary, secondary}
        surnames = {n.split()[-1] for n in adults if n}
        kids = [p for p, _n in entry["people"].most_common()
                if p not in adults and p.split()[-1] in surnames and len(p.split()) == 2]
        cards = [c for c, _n in entry["cards"].most_common(3)] or ["4417", "9031"]
        year = int(entry["years"].most_common(1)[0][0]) if entry["years"] else 2024
        out[client] = {"primary": primary, "secondary": secondary, "address": address,
                       "kids": kids[:3], "cards": cards, "year": year, "dir": folder}
    return out


# --------------------------------------------------------------------------
# what they buy
# --------------------------------------------------------------------------

PRODUCTS = [
    ("Anker USB C Charger 20W, 511 Charger (Nano Pro)", "AnkerDirect", 15.99),
    ("Pampers Swaddlers Diapers Size 3, 168 Count", "Amazon.com", 54.99),
    ("Kirkland Signature Paper Towels, 12 Big Rolls", "Amazon.com", 23.49),
    ("LEGO Classic Large Creative Brick Box 10698", "Amazon.com", 47.99),
    ("Instant Pot Duo 7-in-1 Electric Pressure Cooker, 6 Quart", "Amazon.com", 89.95),
    ("Crayola Washable Markers, Broad Line, 40 Count", "Amazon.com", 11.97),
    ("Ninja Professional Blender 1000W, BL610", "SharkNinja Store", 89.99),
    ("Fire TV Stick 4K with Alexa Voice Remote", "Amazon.com", 49.99),
    ("Echo Dot (5th Gen) Smart Speaker with Alexa, Charcoal", "Amazon.com", 49.99),
    ("Hydro Flask Wide Mouth Bottle with Flex Cap, 32 oz", "Hydro Flask", 44.95),
    ("Carhartt Men's Loose Fit Heavyweight Short-Sleeve Pocket T-Shirt", "Carhartt", 22.99),
    ("Hanes Women's EcoSmart Crewneck Sweatshirt", "Amazon.com", 15.00),
    ("Melissa & Doug Wooden Building Blocks Set, 100 Pieces", "Melissa & Doug", 24.99),
    ("Scotch Heavy Duty Shipping Packaging Tape, 6 Rolls", "Amazon.com", 19.98),
    ("Energizer AA Batteries, Max Alkaline Double A Battery, 24 Count", "Amazon.com", 17.49),
    ("Tide PODS Laundry Detergent Pacs, Spring Meadow, 81 Count", "Amazon.com", 24.97),
    ("Purina Pro Plan Adult Dog Food, Salmon & Rice Formula, 35 lb", "Amazon.com", 76.48),
    ("Nintendo Switch Pro Controller", "Amazon.com", 69.99),
    ("Mario Kart 8 Deluxe - Nintendo Switch", "Amazon.com", 59.99),
    ("Ring Video Doorbell, Satin Nickel", "Amazon.com", 99.99),
    ("Amazon Basics Microfiber Sheet Set, Queen, Gray", "Amazon.com", 21.79),
    ("Coleman Sundome Camping Tent, 4 Person", "Coleman", 89.99),
    ("Osprey Daylite Plus Daypack", "Osprey Packs", 75.00),
    ("Stanley Quencher H2.0 FlowState Tumbler 40 oz", "Stanley 1913", 45.00),
    ("Dr. Seuss's Beginner Book Collection (Hardcover)", "Amazon.com", 27.99),
    ("The Very Hungry Caterpillar (Board Book)", "Amazon.com", 6.49),
    ("Rubbermaid Brilliance Food Storage Containers, 22 Piece", "Amazon.com", 39.99),
    ("Cuisinart 12-Piece Kitchen Knife Set with Blade Guards", "Cuisinart", 29.95),
    ("Philips Sonicare 4100 Power Toothbrush, Rechargeable", "Amazon.com", 49.96),
    ("Neutrogena Hydro Boost Water Gel Face Moisturizer, 1.7 oz", "Amazon.com", 19.99),
    ("Nature Made Vitamin D3 2000 IU, 250 Softgels", "Amazon.com", 12.48),
    ("Graco Extend2Fit Convertible Car Seat, Gotham", "Amazon.com", 199.99),
    ("Summer Infant Pop 'n Play Portable Playard", "Summer Infant", 79.99),
    ("Fisher-Price Baby Gym with Kick & Play Piano", "Amazon.com", 49.99),
    ("JanSport SuperBreak One Backpack, Black", "JanSport", 36.00),
    ("Five Star Spiral Notebooks, 1 Subject, College Ruled, 6 Pack", "Amazon.com", 14.98),
    ("TI-84 Plus CE Graphing Calculator, Black", "Amazon.com", 119.99),
    ("Apple AirPods (3rd Generation)", "Amazon.com", 169.00),
    ("SanDisk 128GB Extreme microSDXC UHS-I Memory Card", "Amazon.com", 17.99),
    ("Logitech MX Master 3S Wireless Performance Mouse", "Logitech", 99.99),
    ("Keurig K-Mini Single Serve Coffee Maker", "Amazon.com", 79.99),
    ("Starbucks Pike Place Roast K-Cup Pods, 72 Count", "Amazon.com", 42.99),
    ("Weber Original Kettle Premium Charcoal Grill, 22-Inch", "Weber", 199.00),
    ("Black+Decker 20V MAX Cordless Drill/Driver", "Amazon.com", 59.99),
    ("Gorilla Original Super Glue Gel, 2 Pack", "Amazon.com", 6.97),
    ("Command Picture Hanging Strips, 24 Pairs", "Amazon.com", 13.49),
    ("Yankee Candle Large Jar Candle, Balsam & Cedar", "Yankee Candle", 29.99),
    ("Funko Pop! Disney: Encanto - Mirabel", "Amazon.com", 11.99),
    ("Barbie Dreamhouse 2023, 75+ Pieces", "Amazon.com", 179.99),
    ("Hot Wheels Track Builder Unlimited Fuel Can Stunt Box", "Amazon.com", 34.99),
    ("Wilson NBA Authentic Indoor/Outdoor Basketball", "Wilson Sporting Goods", 29.95),
    ("Franklin Sports Kids Soccer Goal Set", "Franklin Sports", 39.99),
    ("Speedo Unisex-Child Swim Goggles Hydrospex", "Amazon.com", 9.99),
    ("Columbia Women's Benton Springs Full Zip Fleece Jacket", "Columbia Sportswear", 44.95),
    ("Levi's Men's 505 Regular Fit Jeans", "Amazon.com", 49.50),
    ("Skechers Women's Go Walk Joy Sneaker", "Amazon.com", 54.95),
    ("Crocs Unisex-Child Classic Clog", "Crocs", 34.99),
    ("Pyrex Basics 3 Quart Glass Oblong Baking Dish", "Amazon.com", 14.99),
    ("Ziploc Gallon Food Storage Freezer Bags, 120 Count", "Amazon.com", 17.38),
    ("Bounty Quick-Size Paper Towels, 12 Family Rolls", "Amazon.com", 32.99),
    ("Charmin Ultra Soft Toilet Paper, 24 Family Mega Rolls", "Amazon.com", 33.97),
    ("Clorox Disinfecting Wipes Value Pack, 225 Count", "Amazon.com", 15.99),
    ("Mrs. Meyer's Clean Day Liquid Dish Soap, Lemon Verbena, 3 Pack", "Amazon.com", 13.47),
    ("Burt's Bees Baby Bee Original Bath Set", "Amazon.com", 19.99),
    ("Owlet Dream Sock Smart Baby Monitor", "Owlet Baby Care", 299.00),
    ("Chicco KeyFit 35 Infant Car Seat Base", "Amazon.com", 99.99),
    ("Kindle Paperwhite (16 GB), Black", "Amazon.com", 149.99),
    ("Amazon Gift Card - Print - Happy Birthday", "Amazon.com", 50.00),
    ("Hallmark Birthday Card Assortment, 12 Cards", "Hallmark", 19.99),
    ("Hape Wooden Kitchen Playset", "Hape International", 129.99),
    ("Magna-Tiles Classic 100-Piece Set", "Magna-Tiles", 119.99),
    ("Osmo Genius Starter Kit for iPad", "Osmo", 79.99),
    ("Bose QuietComfort 45 Bluetooth Wireless Headphones", "Bose", 279.00),
    ("Samsung 55-Inch Class Crystal UHD 4K Smart TV", "Amazon.com", 397.99),
    ("Roku Express 4K+ Streaming Device", "Amazon.com", 39.00),
    ("Vitamix E310 Explorian Blender", "Vitamix", 349.95),
    ("Shark Navigator Lift-Away NV352 Upright Vacuum", "SharkNinja Store", 199.99),
    ("iRobot Roomba 694 Robot Vacuum", "iRobot", 274.99),
    ("Dyson V8 Cordless Vacuum Cleaner", "Dyson", 399.99),
    ("Lodge 10.25 Inch Cast Iron Skillet", "Lodge", 19.90),
    ("OXO Good Grips 3-Piece Mixing Bowl Set", "OXO", 34.99),
    ("Hamilton Beach 2-Slice Toaster", "Amazon.com", 24.99),
    ("Mainstays 5-Shelf Bookcase, Espresso", "Amazon.com", 49.88),
    ("Zinus 12 Inch Green Tea Memory Foam Mattress, Queen", "Zinus", 297.00),
    ("Linenspa 2 Inch Gel Infused Memory Foam Mattress Topper", "Linenspa", 49.99),
    ("Utopia Bedding Bed Pillows, Queen Size, Set of 2", "Utopia Bedding", 29.99),
    ("Amazon Basics Heavy Duty Storage Shelving Unit", "Amazon.com", 69.99),
    ("Sterilite 66 Quart Clear Storage Box, 6 Pack", "Amazon.com", 54.99),
    ("Hefty Strong Large Trash Bags, 30 Gallon, 74 Count", "Amazon.com", 15.99),
    ("Swiffer Sweeper Dry + Wet Starter Kit", "Amazon.com", 19.97),
    ("Dawn Platinum Dishwashing Liquid, Refreshing Rain, 3 Pack", "Amazon.com", 11.99),
    ("Cetaphil Gentle Skin Cleanser, 20 oz", "Amazon.com", 14.99),
    ("CeraVe Moisturizing Cream, 19 oz", "Amazon.com", 17.78),
    ("Gillette Fusion5 Men's Razor Blade Refills, 8 Count", "Amazon.com", 32.99),
    ("Olaplex No. 3 Hair Perfector", "Amazon.com", 30.00),
    ("Garmin Forerunner 55 GPS Running Watch", "Garmin", 199.99),
    ("Fitbit Charge 6 Fitness Tracker", "Amazon.com", 159.95),
    ("YETI Rambler 20 oz Tumbler, Stainless Steel", "YETI", 35.00),
    ("Bluey: The Beach (Hardcover Picture Book)", "Amazon.com", 8.99),
    ("Wonder Woman 1984 (Blu-ray + Digital)", "Amazon.com", 14.99),
    ("Monopoly Board Game, Family Game for Kids and Adults", "Amazon.com", 19.99),
    ("UNO Card Game, Family Card Game", "Amazon.com", 5.99),
    ("Catan Board Game (Base Game)", "Amazon.com", 43.97),
    ("Ticket to Ride Board Game", "Amazon.com", 44.99),
]

FIRSTS = ["Sarah", "Michael", "Jessica", "David", "Ashley", "Brandon", "Kaitlyn", "Tyler",
          "Megan", "Jordan", "Hailey", "Spencer", "Brooke", "Dallin", "McKenna", "Landon",
          "Whitney", "Trevor", "Camille", "Garrett", "Natalie", "Preston", "Kendall", "Bryce",
          "Alyssa", "Jaxon", "Savannah", "Colton", "Madison", "Kimball", "Ellie", "Porter"]
LASTS = ["Christensen", "Sorensen", "Petersen", "Larsen", "Bingham", "Hatch", "Pratt",
         "Stoddard", "Taggart", "Wheelwright", "Rasmussen", "Jeppson", "Nielson", "Hendricks",
         "Ostler", "Barlow", "Ashby", "Tolman", "Gardner", "Merrill", "Bateman", "Draper",
         "Furniss", "Lofgreen", "Montague", "Quarnberg", "Sandberg", "Vance", "Whitlock"]
STREETS = ["W CENTER ST", "N MAIN ST", "E 400 S", "S 1300 E", "W 3500 S", "N 200 W",
           "E STATE ST", "S REDWOOD RD", "W PIONEER RD", "N CANYON VIEW DR", "E MAPLE ST",
           "W PRAIRIE LN", "S HIGHLAND DR", "E BENCH RD", "N ORCHARD DR"]
CITIES = [("PROVO", "84604"), ("OREM", "84057"), ("SANDY", "84070"), ("DRAPER", "84020"),
          ("BOUNTIFUL", "84010"), ("LOGAN", "84321"), ("ST GEORGE", "84770"),
          ("MESA", "85201"), ("BOISE", "83702"), ("LAS VEGAS", "89123"), ("DENVER", "80220"),
          ("RENO", "89502"), ("TWIN FALLS", "83301"), ("CEDAR CITY", "84720")]
STATE_OF = {"MESA": "AZ", "BOISE": "ID", "LAS VEGAS": "NV", "DENVER": "CO", "RENO": "NV",
            "TWIN FALLS": "ID"}
RELATIONS = ["Mom", "Dad", "Grandma", "Grandpa", "Aunt", "Uncle", "friend", "sister", "brother"]
GIFT_MESSAGES = [
    "Happy birthday, {r}! Love you lots. - {s}",
    "Merry Christmas {r}! Hope you love it. Love, {s} and the kids",
    "Congratulations on the new baby, {r}! - {s}",
    "Thinking of you, {r}. Hang in there. {s}",
    "Happy anniversary {r}! xo {s}",
    "For your new place, {r}. Can't wait to visit! - {s}",
    "Good luck at school this year {r}! Love, {s}",
]


def _phone(rng) -> str:
    return f"({rng.choice(['801', '385', '435'])}) 555-01{rng.randint(10, 99)}"


def _order_id(rng) -> str:
    return f"{rng.choice(['111', '112', '113', '114'])}-{rng.randint(1000000, 9999999)}-{rng.randint(1000000, 9999999)}"


def _tracking(rng) -> str:
    if rng.random() < 0.7:
        return "TBA" + "".join(str(rng.randint(0, 9)) for _ in range(12))
    return "1Z" + "".join(rng.choice("0123456789ABCDEFGHJKLMNPRSTUVWXYZ") for _ in range(16))


def _title(name: str) -> str:
    return " ".join(part if len(part) == 1 else "-".join(p.capitalize() for p in part.split("-"))
                    for part in name.split())


def build_orders(client: str, who: dict) -> dict:
    rng = random.Random(f"amazon/{client}")
    primary, secondary = who["primary"], who["secondary"]
    line1, city, state, zip5, zip4 = who["address"]
    first = primary.split()[0].lower()
    last = primary.split()[-1].lower()
    email = f"{first}.{last}{rng.randint(1, 99)}@example.com"
    phone = _phone(rng)
    brands = ["Visa", "Mastercard", "Visa", "American Express"]
    cards = [{"brand": brands[i % len(brands)], "card4": c} for i, c in enumerate(who["cards"])]
    recipients = []
    for _ in range(rng.randint(3, 5)):
        rcity, rzip = rng.choice(CITIES)
        recipients.append({
            "name": f"{rng.choice(FIRSTS)} {rng.choice(LASTS)}",
            "relation": rng.choice(RELATIONS),
            "line1": f"{rng.randint(100, 9999)} {rng.choice(STREETS)}",
            "city": rcity, "state": STATE_OF.get(rcity, "UT"), "zip": rzip,
        })
    household = [primary] + ([secondary] if secondary else []) + who["kids"]
    orders = []
    day = dt.date(who["year"], 1, 1)
    for _ in range(110):
        day += dt.timedelta(days=rng.randint(1, 5))
        if day.year != who["year"]:
            day = dt.date(who["year"], 12, rng.randint(1, 28))
        items = []
        for _ in range(rng.choice([1, 1, 1, 2, 2, 3])):
            product, seller, price = rng.choice(PRODUCTS)
            qty = rng.choice([1, 1, 1, 2])
            items.append({"product": product, "seller": seller,
                          "price": round(price * rng.uniform(0.85, 1.1), 2), "qty": qty})
        subtotal = round(sum(i["price"] * i["qty"] for i in items), 2)
        shipping = 0.0 if subtotal > 35 or rng.random() < 0.7 else 5.99
        tax = round(subtotal * 0.0725, 2)
        gift = rng.random() < 0.18
        if gift:
            r = rng.choice(recipients)
            ship = {"name": r["name"].upper(), "line1": r["line1"], "city": r["city"],
                    "state": r["state"], "zip": r["zip"]}
            message = rng.choice(GIFT_MESSAGES).format(
                r=r["name"].split()[0] if rng.random() < 0.6 else r["relation"],
                s=_title(primary).split()[0])
        else:
            name = rng.choice(household[:2]) if rng.random() < 0.85 else rng.choice(household)
            ship = {"name": name, "line1": line1, "city": city, "state": state, "zip": zip5}
            message = ""
        pay = rng.choice(cards) if rng.random() < 0.9 else {"brand": "Amazon Gift Card", "card4": ""}
        orders.append({
            "order_id": _order_id(rng), "date": day.isoformat(),
            "subtotal": subtotal, "shipping": shipping, "tax": tax,
            "total": round(subtotal + shipping + tax, 2),
            "ship_to": ship, "gift_recipient": ship["name"] if gift else None,
            "gift_message": message, "payment": pay, "tracking": _tracking(rng),
            "delivered": (day + dt.timedelta(days=rng.randint(1, 5))).isoformat(),
            "items": items,
        })
    return {"email": email, "phone": phone, "cards": cards, "recipients": recipients,
            "household": household, "orders": orders}


# --------------------------------------------------------------------------
# drawing - every cell its own text call, as the real printouts are
# --------------------------------------------------------------------------

GREY = (0.94, 0.94, 0.94)
DARK = (0.13, 0.13, 0.13)
MUTED = (0.34, 0.34, 0.34)
LINK = (0.0, 0.44, 0.58)


def _text(page, x, y, text, size=8.0, bold=False, colour=DARK):
    page.insert_text((x, y), text, fontsize=size, fontname="hebo" if bold else "helv",
                     color=colour)


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _long_date(iso: str) -> str:
    d = dt.date.fromisoformat(iso)
    return d.strftime("%B ") + str(d.day) + d.strftime(", %Y")


def _addr_lines(ship: dict) -> list[str]:
    return [ship["name"], ship["line1"], f"{ship['city']}, {ship['state']} {ship['zip']}",
            "United States"]


def your_orders(path: Path, who: dict, data: dict) -> int:
    doc = pymupdf.open()
    page, y = None, 0.0
    for order in data["orders"]:
        height = 74 + 30 * len(order["items"])
        if page is None or y + height > 750:
            page = doc.new_page(width=612, height=792)
            _text(page, 40, 40, "amazon", 16, True)
            _text(page, 520, 40, f"Page {len(doc)}", 7, colour=MUTED)
            _text(page, 40, 70, "Your Orders", 15)
            _text(page, 40, 86, f"{len(data['orders'])} orders placed in {who['year']}", 8, colour=MUTED)
            y = 100.0
        page.draw_rect(pymupdf.Rect(40, y, 572, y + 34), color=(0.85, 0.85, 0.85), fill=GREY)
        _text(page, 50, y + 12, "ORDER PLACED", 6.5, colour=MUTED)
        _text(page, 50, y + 24, _long_date(order["date"]), 8)
        _text(page, 160, y + 12, "TOTAL", 6.5, colour=MUTED)
        _text(page, 160, y + 24, _money(order["total"]), 8)
        _text(page, 240, y + 12, "SHIP TO", 6.5, colour=MUTED)
        _text(page, 240, y + 24, _title(order["ship_to"]["name"]), 8, colour=LINK)
        _text(page, 410, y + 12, f"ORDER # {order['order_id']}", 6.5, colour=MUTED)
        _text(page, 410, y + 24, "View order details   Invoice", 7, colour=LINK)
        page.draw_rect(pymupdf.Rect(40, y + 34, 572, y + height - 6), color=(0.85, 0.85, 0.85))
        _text(page, 50, y + 50, "Delivered " + _long_date(order["delivered"]).rsplit(",", 1)[0], 9, True)
        iy = y + 66
        for item in order["items"]:
            _text(page, 60, iy, item["product"][:78], 8, colour=LINK)
            _text(page, 60, iy + 11, f"Sold by: {item['seller']}   Qty: {item['qty']}   "
                                     f"{_money(item['price'])}", 7, colour=MUTED)
            iy += 30
        if order["gift_recipient"]:
            _text(page, 400, y + 50, "Gift order", 7, True, colour=MUTED)
        y += height
    doc.save(path)
    pages = len(doc)
    doc.close()
    return pages


def order_details(path: Path, who: dict, data: dict, count: int = 14) -> int:
    doc = pymupdf.open()
    home = who["address"]
    for order in data["orders"][:count]:
        page = doc.new_page(width=612, height=792)
        _text(page, 40, 40, "amazon.com", 14, True)
        _text(page, 40, 66, "Order Details", 15)
        _text(page, 40, 84, f"Ordered on {_long_date(order['date'])}", 8)
        _text(page, 200, 84, f"Order# {order['order_id']}", 8)
        page.draw_rect(pymupdf.Rect(40, 96, 572, 222), color=(0.85, 0.85, 0.85))
        _text(page, 50, 112, "Shipping Address", 8, True)
        for i, line in enumerate(_addr_lines(order["ship_to"])):
            _text(page, 50, 126 + 11 * i, line, 8)
        _text(page, 220, 112, "Payment Method", 8, True)
        pay = order["payment"]
        _text(page, 220, 126, f"{pay['brand']} ending in {pay['card4']}" if pay["card4"]
              else pay["brand"], 8)
        _text(page, 220, 148, "Billing Address", 8, True)
        bill = {"name": who["primary"], "line1": home[0], "city": home[1], "state": home[2],
                "zip": home[3]}
        for i, line in enumerate(_addr_lines(bill)[:3]):
            _text(page, 220, 162 + 11 * i, line, 8)
        _text(page, 400, 112, "Order Summary", 8, True)
        rows = [("Item(s) Subtotal:", order["subtotal"]), ("Shipping & Handling:", order["shipping"]),
                ("Total before tax:", order["subtotal"] + order["shipping"]),
                ("Estimated tax to be collected:", order["tax"])]
        for i, (label, value) in enumerate(rows):
            _text(page, 400, 126 + 11 * i, label, 7.5)
            _text(page, 530, 126 + 11 * i, _money(value), 7.5)
        _text(page, 400, 178, "Grand Total:", 8, True)
        _text(page, 530, 178, _money(order["total"]), 8, True)
        _text(page, 40, 244, f"Delivered {_long_date(order['delivered'])}", 9, True)
        _text(page, 330, 244, f"Tracking ID: {order['tracking']}", 7.5, colour=MUTED)
        y = 264
        for item in order["items"]:
            _text(page, 50, y, f"{item['qty']} of: {item['product'][:80]}", 8, colour=LINK)
            _text(page, 50, y + 11, f"Sold by: {item['seller']}", 7, colour=MUTED)
            _text(page, 50, y + 22, "Condition: New", 7, colour=MUTED)
            _text(page, 520, y, _money(item["price"]), 8)
            y += 40
        if order["gift_message"]:
            _text(page, 50, y + 6, "Gift message:", 8, True)
            _text(page, 50, y + 18, order["gift_message"], 8)
            _text(page, 50, y + 30, f"Gift receipt sent to: {_title(order['ship_to']['name'])}", 7,
                  colour=MUTED)
        _text(page, 40, 740, f"Questions? Contact us. Account: {data['email']}  Phone on file: "
                             f"{data['phone']}", 6.5, colour=MUTED)
        _text(page, 520, 760, f"Page {len(doc)}", 7, colour=MUTED)
    doc.save(path)
    pages = len(doc)
    doc.close()
    return pages


EXPORT_COLUMNS = [("Order Date", 30), ("Order ID", 82), ("Title", 168), ("Seller", 300),
                  ("Qty", 372), ("Unit Price", 392), ("Total Owed", 432),
                  ("Payment Instrument Type", 476), ("Shipping Address", 554),
                  ("Billing Address", 640), ("Tracking", 726)]


def data_export(path: Path, who: dict, data: dict) -> int:
    doc = pymupdf.open()
    home = who["address"]
    rows = [(order, item) for order in data["orders"] for item in order["items"]]
    page, y = None, 0.0
    for order, item in rows:
        if page is None or y > 570:
            page = doc.new_page(width=792, height=612)
            _text(page, 30, 30, "Retail.OrderHistory.1.csv", 10, True)
            _text(page, 30, 44, f"Amazon - Your data export - {data['email']}", 7, colour=MUTED)
            page.draw_rect(pymupdf.Rect(28, 52, 764, 66), color=None, fill=GREY)
            for heading, x in EXPORT_COLUMNS:
                _text(page, x, 62, heading, 5.8, True)
            _text(page, 720, 600, f"Page {len(doc)}", 6.5, colour=MUTED)
            y = 78.0
        ship = order["ship_to"]
        pay = order["payment"]
        title = item["product"]
        cut = title.rfind(" ", 0, 34) if len(title) > 34 else len(title)
        _text(page, 168, y + 7, title[cut:].strip()[:34], 5.8)
        cells = [
            order["date"], order["order_id"], title[:cut], item["seller"][:16],
            str(item["qty"]), f"{item['price']:.2f}", f"{order['total']:.2f}",
            (f"{pay['brand']} - {pay['card4']}" if pay["card4"] else pay["brand"]),
        ]
        for (heading, x), cell in zip(EXPORT_COLUMNS, cells):
            _text(page, x, y, cell, 5.8)
        for i, line in enumerate((ship["name"], ship["line1"],
                                  f"{ship['city']} {ship['state']} {ship['zip']}")):
            _text(page, 554, y + 6.5 * i, line, 5.0)
        for i, line in enumerate((who["primary"], home[0], f"{home[1]} {home[2]} {home[3]}")):
            _text(page, 640, y + 6.5 * i, line, 5.0)
        _text(page, 726, y, order["tracking"], 5.0)
        page.draw_line((28, y + 12), (764, y + 12), color=(0.88, 0.88, 0.88), width=0.4)
        y += 26
    doc.save(path)
    pages = len(doc)
    doc.close()
    return pages


def invoice_orders(data: dict) -> list[dict]:
    """The invoice set: orders the details printout does not show, every fifth
    one a digital order with Amazon's D01- number."""
    out = []
    for index, order in enumerate(data["orders"][14:28]):
        order = dict(order)
        if index % 5 == 4:
            order["order_id"] = "D01-" + order["order_id"].split("-", 1)[1]
            order["digital"] = True
        out.append(order)
    return out


def invoices(path: Path, who: dict, data: dict) -> int:
    """The printable invoice - "Final Details for Order #" - one order a page.
    Written after the other three layouts' fixes, as a held-out check."""
    doc = pymupdf.open()
    line1, city, state, zip5, zip4 = who["address"]
    for order in invoice_orders(data):
        page = doc.new_page(width=612, height=792)
        pay = order["payment"]
        ship = order["ship_to"]
        _text(page, 40, 40, "amazon.com", 13, True)
        _text(page, 150, 64, f"Final Details for Order #{order['order_id']}", 12, True)
        _text(page, 230, 78, "Print this page for your records.", 7, colour=MUTED)
        _text(page, 40, 102, f"Order Placed: {_long_date(order['date'])}", 8)
        if order.get("digital"):
            _text(page, 40, 114, f"Digital Order: {order['order_id']}", 8)
            _text(page, 40, 126, f"Ordered By: {_title(who['primary'])}", 8)
        else:
            _text(page, 40, 114, f"Amazon.com order number: {order['order_id']}", 8)
        _text(page, 40, 138, f"Order Total: {_money(order['total'])}", 8, True)
        page.draw_rect(pymupdf.Rect(40, 148, 572, 400), color=(0.6, 0.6, 0.6))
        y = 164
        if order.get("digital"):
            _text(page, 50, y, "Digital Order Summary", 9, True)
        else:
            _text(page, 50, y, f"Shipped on {_long_date(order['delivered'])}", 9, True)
        _text(page, 50, y + 16, "Items Ordered", 8, True)
        _text(page, 520, y + 16, "Price", 8, True)
        iy = y + 30
        for item in order["items"]:
            title = item["product"] + (" Kindle Edition" if order.get("digital") else "")
            _text(page, 50, iy, f"{item['qty']} of: {title[:70]}", 8)
            _text(page, 60, iy + 10, f"Sold by: {item['seller']}", 7, colour=MUTED)
            _text(page, 520, iy, _money(item["price"]), 8)
            iy += 26
        if not order.get("digital"):
            _text(page, 50, 300, "Shipping Address:", 8, True)
            lines = [_title(ship["name"]), _title(ship["line1"]),
                     f"{_title(ship['city'])}, {ship['state']} "
                     + (zip4 if ship["line1"] == line1 else ship["zip"]),
                     "United States"]
            for i, line in enumerate(lines):
                _text(page, 50, 312 + 10 * i, line, 8)
            _text(page, 50, 360, "Shipping Speed:", 8, True)
            _text(page, 50, 370, "FREE Prime Delivery", 8)
            _text(page, 220, 300, "Carrier:", 8, True)
            _text(page, 220, 310, f"AMZN_US   Tracking #: {order['tracking']}", 8)
        if order["gift_message"]:
            import textwrap
            _text(page, 220, 330, "Gift Options:", 8, True)
            wrapped = textwrap.wrap("Gift message: " + order["gift_message"], 38)[:3]
            for i, line in enumerate(wrapped):
                _text(page, 220, 340 + 9 * i, line, 7.5)
            _text(page, 220, 342 + 9 * len(wrapped), f"Gift Recipient: {_title(ship['name'])}", 7.5)
        _text(page, 400, 300, f"Item(s) Subtotal: {_money(order['subtotal'])}", 7.5)
        _text(page, 400, 310, f"Shipping & Handling: {_money(order['shipping'])}", 7.5)
        _text(page, 400, 324, f"Total before tax: {_money(order['subtotal'] + order['shipping'])}", 7.5)
        _text(page, 400, 334, f"Sales Tax: {_money(order['tax'])}", 7.5)
        _text(page, 400, 348, f"Total for This Shipment: {_money(order['total'])}", 7.5, True)
        page.draw_rect(pymupdf.Rect(40, 410, 572, 560), color=(0.6, 0.6, 0.6))
        _text(page, 230, 426, "Payment information", 10, True)
        _text(page, 50, 446, "Payment Method:", 8, True)
        if pay["card4"]:
            _text(page, 50, 456, f"{pay['brand']} | Last digits: {pay['card4']}", 8)
        else:
            _text(page, 50, 456, "Gift Card Balance", 8)
        _text(page, 50, 476, "Billing address", 8, True)
        for i, line in enumerate([_title(who["primary"]), _title(line1),
                                  f"{_title(city)}, {state} {zip4}", "United States"]):
            _text(page, 50, 488 + 10 * i, line, 8)
        _text(page, 400, 446, f"Item(s) Subtotal: {_money(order['subtotal'])}", 7.5)
        _text(page, 400, 470, f"Grand Total: {_money(order['total'])}", 8, True)
        _text(page, 50, 540, "Credit Card transactions", 8, True)
        if pay["card4"]:
            _text(page, 220, 540, f"{pay['brand']} ending in {pay['card4']}: "
                                  f"{_long_date(order['delivered'])}: {_money(order['total'])}", 8)
        if order["total"] > 150:
            _text(page, 50, 580, f"Refund issued: {_money(order['items'][0]['price'])} to "
                                 + (f"{pay['brand']} ending in {pay['card4']}" if pay["card4"]
                                    else "Gift Card Balance")
                                 + f" on {_long_date(order['delivered'])}", 8)
        _text(page, 40, 760, "Conditions of Use | Privacy Notice (c) 1996-2025, Amazon.com, Inc. "
                             "or its affiliates", 6.5, colour=MUTED)
    doc.save(path)
    pages = len(doc)
    doc.close()
    return pages


def _label(kind: str, client: str, who: dict, data: dict, pdf: Path, pages: int,
           orders: list[dict]) -> dict:
    line1, city, state, zip5, zip4 = who["address"]
    return {
        "doc_type": kind, "household": client, "file": pdf.name, "pages": pages,
        "holder": {"primary": who["primary"], "secondary": who["secondary"],
                   "address": {"line1": line1, "city": city, "state": state, "zip": zip5,
                               "zip4": zip4}},
        "account_email": data["email"], "account_phone": data["phone"],
        "household_members": [{"ship_to_name": n} for n in data["household"]],
        "data": {"orders": [{
            "order_id": o["order_id"], "date": o["date"], "total": o["total"],
            "ship_to_name": o["ship_to"]["name"] if not o["gift_recipient"] else None,
            "gift_recipient": o["gift_recipient"],
            "ship_to": {"line1": o["ship_to"]["line1"], "city": o["ship_to"]["city"],
                        "state": o["ship_to"]["state"], "zip": o["ship_to"]["zip"]},
            "gift_message": o["gift_message"], "card4": o["payment"]["card4"],
            "payment_brand": o["payment"]["brand"], "tracking": o["tracking"],
            "items": [{"product": i["product"], "seller": i["seller"], "price": i["price"],
                       "qty": i["qty"]} for i in o["items"]],
        } for o in orders]},
        "note": "Synthetic. Every person, address, order number and card digit is invented; "
                "people and cards match the household's other documents.",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("root", type=Path)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    for client, who in clients(root).items():
        data = build_orders(client, who)
        folder = root / who["dir"] / "amazon" / (client.split("-")[-1] if client.startswith("D-") else "")
        folder.mkdir(parents=True, exist_ok=True)
        year = who["year"]
        made = []
        for kind, name, draw, orders in (
                ("amazon_your_orders", f"Amazon_Your_Orders_{year}.pdf", your_orders, data["orders"]),
                ("amazon_order_details", f"Amazon_Order_Details_{year}.pdf", order_details,
                 data["orders"][:14]),
                ("amazon_data_export", f"Amazon_Order_History_Export_{year}.pdf", data_export,
                 data["orders"]),
                ("amazon_invoice", f"Amazon_Invoices_{year}.pdf", invoices, invoice_orders(data))):
            pdf = folder / name
            pages = draw(pdf, who, data)
            pdf.with_suffix(".json").write_text(json.dumps(
                _label(kind, client, who, data, pdf, pages, orders), indent=1))
            made.append(f"{name} ({pages} pages)")
        print(f"{client}: {who['primary']} - {', '.join(made)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
