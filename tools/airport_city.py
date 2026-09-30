#!/usr/bin/env python3
"""Name airports by the city they serve, not the town they sit in.

OurAirports' municipality is where the field is: Kuala Lumpur's airport
is "Sepang", Athens "Spata-Artemida", Tokyo Narita "Narita", Bali
"Kuta, Badung"; for Italy it is the comune with its province in
brackets (Verona "Caselle (VR)"). The map, the cards and the app want
the city people fly to. Rewrites the name in web/assets/airports.json:
a hand list for the fields whose town is not the city (kept by rank:
the busy fields people search for), the Italian province suffix
dropped for the rest. A listed code renames its twin key (the ICAO one
at the same spot) too. Idempotent; run airports_map.py after.

    python3 tools/airport_city.py
"""
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AIRPORTS = os.path.join(ROOT, "web", "assets", "airports.json")

CITY = {
    "VRN": "Verona", "LIPX": "Verona",
    "TRN": "Torino", "LIMF": "Torino",
    "LIN": "Milano", "LIML": "Milano",
    "MXP": "Milano", "LIMC": "Milano",
    "BGY": "Bergamo", "LIME": "Bergamo",
    "AOI": "Ancona", "LIPY": "Ancona",
    "VBS": "Brescia", "LIPO": "Brescia",
    "CUF": "Cuneo", "LIMZ": "Cuneo",
    "CRV": "Crotone", "LIBC": "Crotone",
    "EBA": "Elba", "LIRJ": "Elba",
    # the rest of the world: the town the field is in -> the city served
    "KUL": "Kuala Lumpur", "ATH": "Athens", "NRT": "Tokyo", "TPE": "Taipei",
    "DPS": "Bali", "SAW": "Istanbul", "BRU": "Brussels", "NGO": "Nagoya",
    "ADB": "Izmir", "CGN": "Cologne", "EDI": "Edinburgh", "LYS": "Lyon",
    "MRS": "Marseille", "KRK": "Kraków", "KNO": "Medan", "CXR": "Nha Trang",
    "BSL": "Basel", "ACE": "Lanzarote", "MFM": "Macau", "OTP": "Bucharest",
    "TIA": "Tirana", "XNN": "Xining", "FUE": "Fuerteventura", "MCT": "Muscat",
    "EMA": "East Midlands", "COV": "Adana", "LXA": "Lhasa", "JHG": "Xishuangbanna",
    "LOP": "Lombok", "CRK": "Clark", "MAH": "Menorca", "BDJ": "Banjarmasin",
    "MRU": "Mauritius", "SDJ": "Sendai", "CFU": "Corfu", "GUM": "Guam",
    "ZAG": "Zagreb", "CHQ": "Chania", "VVO": "Vladivostok", "BJX": "León",
    "WMI": "Warsaw", "ILO": "Iloilo", "SKP": "Skopje", "MZG": "Penghu",
    "LEJ": "Leipzig", "NYO": "Stockholm", "PIE": "St. Petersburg",
    "RUN": "Réunion", "ISB": "Islamabad", "LIL": "Lille", "MVD": "Montevideo",
    "OVD": "Asturias", "DJE": "Djerba", "KCZ": "Kochi", "LJU": "Ljubljana",
    "FKB": "Karlsruhe", "FMO": "Münster", "UYU": "Uyuni", "DXN": "Noida",
    "KOS": "Sihanoukville", "IKU": "Issyk-Kul", "TRS": "Trieste",
    "GWD": "Gwadar", "CGB": "Cuiabá", "IAD": "Washington", "USM": "Koh Samui",
    "GOI": "Goa", "KBP": "Kyiv", "CEB": "Cebu", "HLP": "Jakarta",
    "PVD": "Providence", "SRQ": "Sarasota", "TYS": "Knoxville",
    "KOA": "Kona", "HNL": "Honolulu", "YUL": "Montréal", "SCU": "Santiago de Cuba",
    "STI": "Santiago", "BLA": "Barcelona (Venezuela)", "PMV": "Margarita",
    "SXM": "Sint Maarten", "PPT": "Tahiti", "TBU": "Tonga", "ECN": "North Nicosia",
    "MDC": "Manado", "SOC": "Solo", "DJJ": "Jayapura", "KMQ": "Komatsu",
    "PQC": "Phu Quoc", "HPH": "Haiphong", "SZB": "Kuala Lumpur (Subang)",
    "CTS": "Sapporo", "NAP": "Naples", "VCE": "Venice", "FLR": "Florence",
    "GOA": "Genoa", "PMO": "Palermo", "CIA": "Rome", "MED": "Medina",
    "DMM": "Dammam", "DHA": "Dhahran", "EBL": "Erbil", "AWZ": "Ahvaz",
}


def main():
    with open(AIRPORTS) as fh:
        data = json.load(fh)
    # a listed code's twin key (its ICAO, at the same spot) takes the name too
    city = dict(CITY)
    for code, name in CITY.items():
        entry = data.get(code)
        if not entry:
            continue
        for other, e in data.items():
            if other not in city and e[1:3] == entry[1:3]:
                city[other] = name
    changed = 0
    for code, entry in data.items():
        name = entry[0] or ""
        new = city.get(code) or re.sub(r"\s*\(\w\w\)$", "", name)
        if new != name:
            entry[0] = new
            changed += 1
    with open(AIRPORTS, "w") as fh:
        json.dump(data, fh, separators=(",", ":"), ensure_ascii=False)
    print("%d names changed" % changed)


if __name__ == "__main__":
    main()
