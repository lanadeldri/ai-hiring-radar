"""
Normalize free-text job locations into cities and countries.

    parse_location("San Francisco, CA | New York City, NY")
    -> {"cities": ["San Francisco", "New York"], "countries": ["United States"], "remote": False}

Handles the formats job boards actually use: "City, ST", "City, State", "City, Country",
bare cities ("Stockholm"), bare countries/states, "Remote - US", "Remote-Friendly, United States",
and lists separated by "|", ";" or " / ".
"""
import re

US = "United States"

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}
US_STATE_NAMES = {v.lower() for v in US_STATES.values()}
CA_PROVINCES = {"on": "Ontario", "ontario": "Ontario", "bc": "British Columbia", "british columbia": "British Columbia",
                "qc": "Quebec", "quebec": "Quebec", "ab": "Alberta", "alberta": "Alberta"}

COUNTRIES = {
    # aliases -> canonical name
    "us": US, "usa": US, "u.s.": US, "u.s.a.": US, "united states": US, "united states of america": US, "america": US,
    "uk": "United Kingdom", "u.k.": "United Kingdom", "gb": "United Kingdom", "great britain": "United Kingdom",
    "united kingdom": "United Kingdom", "england": "United Kingdom", "scotland": "United Kingdom",
    "ca": "Canada", "can": "Canada", "canada": "Canada",
    "de": "Germany", "germany": "Germany", "deutschland": "Germany",
    "fr": "France", "france": "France", "nl": "Netherlands", "netherlands": "Netherlands", "the netherlands": "Netherlands",
    "ie": "Ireland", "ireland": "Ireland", "es": "Spain", "spain": "Spain", "it": "Italy", "italy": "Italy",
    "pt": "Portugal", "portugal": "Portugal", "ch": "Switzerland", "switzerland": "Switzerland",
    "se": "Sweden", "sweden": "Sweden", "dk": "Denmark", "denmark": "Denmark", "no": "Norway", "norway": "Norway",
    "fi": "Finland", "finland": "Finland", "pl": "Poland", "poland": "Poland", "cz": "Czechia", "czechia": "Czechia",
    "czech republic": "Czechia", "at": "Austria", "austria": "Austria", "be": "Belgium", "belgium": "Belgium",
    "gr": "Greece", "greece": "Greece", "ro": "Romania", "romania": "Romania", "hu": "Hungary", "hungary": "Hungary",
    "ee": "Estonia", "estonia": "Estonia", "lt": "Lithuania", "lithuania": "Lithuania", "ua": "Ukraine", "ukraine": "Ukraine",
    "rs": "Serbia", "serbia": "Serbia", "bg": "Bulgaria", "bulgaria": "Bulgaria", "hr": "Croatia", "croatia": "Croatia",
    "lu": "Luxembourg", "luxembourg": "Luxembourg", "tr": "Turkey", "turkey": "Turkey", "türkiye": "Turkey",
    "il": "Israel", "israel": "Israel", "ae": "United Arab Emirates", "uae": "United Arab Emirates",
    "united arab emirates": "United Arab Emirates", "sa": "Saudi Arabia", "saudi arabia": "Saudi Arabia", "qatar": "Qatar",
    "in": "India", "india": "India", "sg": "Singapore", "singapore": "Singapore", "jp": "Japan", "japan": "Japan",
    "kr": "South Korea", "korea": "South Korea", "south korea": "South Korea", "republic of korea": "South Korea",
    "cn": "China", "china": "China", "hk": "Hong Kong", "hong kong": "Hong Kong", "tw": "Taiwan", "taiwan": "Taiwan",
    "au": "Australia", "aus": "Australia", "australia": "Australia", "nz": "New Zealand", "new zealand": "New Zealand",
    "ph": "Philippines", "philippines": "Philippines", "vn": "Vietnam", "vietnam": "Vietnam", "id": "Indonesia",
    "indonesia": "Indonesia", "my": "Malaysia", "malaysia": "Malaysia", "th": "Thailand", "thailand": "Thailand",
    "br": "Brazil", "brazil": "Brazil", "brasil": "Brazil", "mx": "Mexico", "mexico": "Mexico", "méxico": "Mexico",
    "ar": "Argentina", "argentina": "Argentina", "co": "Colombia", "colombia": "Colombia", "cl": "Chile", "chile": "Chile",
    "pe": "Peru", "peru": "Peru", "uy": "Uruguay", "uruguay": "Uruguay", "cr": "Costa Rica", "costa rica": "Costa Rica",
    "za": "South Africa", "south africa": "South Africa", "ng": "Nigeria", "nigeria": "Nigeria", "ke": "Kenya",
    "kenya": "Kenya", "eg": "Egypt", "egypt": "Egypt", "pk": "Pakistan", "pakistan": "Pakistan",
}

# known cities -> (canonical city, country); covers bare "Tokyo", "Stockholm", aliases like "NYC", "Bengaluru"
CITIES = {
    "san francisco": ("San Francisco", US), "sf": ("San Francisco", US), "san francisco bay area": ("San Francisco", US),
    "sf bay area": ("San Francisco", US), "bay area": ("San Francisco", US),
    "new york": ("New York", US), "new york city": ("New York", US), "nyc": ("New York", US), "brooklyn": ("New York", US),
    "seattle": ("Seattle", US), "bellevue": ("Bellevue", US), "redmond": ("Redmond", US), "boston": ("Boston", US),
    "cambridge, ma": ("Cambridge", US), "austin": ("Austin", US), "dallas": ("Dallas", US), "houston": ("Houston", US),
    "chicago": ("Chicago", US), "los angeles": ("Los Angeles", US), "la": ("Los Angeles", US), "santa monica": ("Santa Monica", US),
    "san diego": ("San Diego", US), "san jose": ("San Jose", US), "palo alto": ("Palo Alto", US), "mountain view": ("Mountain View", US),
    "menlo park": ("Menlo Park", US), "sunnyvale": ("Sunnyvale", US), "san mateo": ("San Mateo", US), "foster city": ("Foster City", US),
    "redwood city": ("Redwood City", US), "san carlos": ("San Carlos", US), "oakland": ("Oakland", US), "berkeley": ("Berkeley", US),
    "denver": ("Denver", US), "boulder": ("Boulder", US), "atlanta": ("Atlanta", US), "miami": ("Miami", US),
    "washington, dc": ("Washington DC", US), "washington dc": ("Washington DC", US), "washington d.c.": ("Washington DC", US),
    "washington, d.c.": ("Washington DC", US), "dc": ("Washington DC", US), "district of columbia": ("Washington DC", US),
    "arlington": ("Arlington", US), "mclean": ("McLean", US), "reston": ("Reston", US), "philadelphia": ("Philadelphia", US),
    "pittsburgh": ("Pittsburgh", US), "salt lake city": ("Salt Lake City", US), "phoenix": ("Phoenix", US),
    "portland": ("Portland", US), "minneapolis": ("Minneapolis", US), "detroit": ("Detroit", US), "nashville": ("Nashville", US),
    "raleigh": ("Raleigh", US), "durham": ("Durham", US), "toronto": ("Toronto", "Canada"), "vancouver": ("Vancouver", "Canada"),
    "montreal": ("Montreal", "Canada"), "waterloo": ("Waterloo", "Canada"), "ottawa": ("Ottawa", "Canada"),
    "london": ("London", "United Kingdom"), "manchester": ("Manchester", "United Kingdom"), "edinburgh": ("Edinburgh", "United Kingdom"),
    "cambridge, uk": ("Cambridge", "United Kingdom"), "oxford": ("Oxford", "United Kingdom"), "knutsford": ("Knutsford", "United Kingdom"),
    "dublin": ("Dublin", "Ireland"), "paris": ("Paris", "France"), "lyon": ("Lyon", "France"),
    "berlin": ("Berlin", "Germany"), "munich": ("Munich", "Germany"), "münchen": ("Munich", "Germany"),
    "hamburg": ("Hamburg", "Germany"), "frankfurt": ("Frankfurt", "Germany"), "freiburg": ("Freiburg", "Germany"),
    "amsterdam": ("Amsterdam", "Netherlands"), "rotterdam": ("Rotterdam", "Netherlands"), "zurich": ("Zurich", "Switzerland"),
    "zürich": ("Zurich", "Switzerland"), "geneva": ("Geneva", "Switzerland"), "stockholm": ("Stockholm", "Sweden"),
    "copenhagen": ("Copenhagen", "Denmark"), "oslo": ("Oslo", "Norway"), "helsinki": ("Helsinki", "Finland"),
    "madrid": ("Madrid", "Spain"), "barcelona": ("Barcelona", "Spain"), "lisbon": ("Lisbon", "Portugal"),
    "milan": ("Milan", "Italy"), "rome": ("Rome", "Italy"), "warsaw": ("Warsaw", "Poland"), "krakow": ("Krakow", "Poland"),
    "prague": ("Prague", "Czechia"), "vienna": ("Vienna", "Austria"), "brussels": ("Brussels", "Belgium"),
    "tel aviv": ("Tel Aviv", "Israel"), "herzliya": ("Herzliya", "Israel"), "dubai": ("Dubai", "United Arab Emirates"),
    "abu dhabi": ("Abu Dhabi", "United Arab Emirates"), "bengaluru": ("Bengaluru", "India"), "bangalore": ("Bengaluru", "India"),
    "mumbai": ("Mumbai", "India"), "new delhi": ("Delhi", "India"), "delhi": ("Delhi", "India"), "gurgaon": ("Gurugram", "India"),
    "gurugram": ("Gurugram", "India"), "hyderabad": ("Hyderabad", "India"), "pune": ("Pune", "India"), "chennai": ("Chennai", "India"),
    "singapore": ("Singapore", "Singapore"), "tokyo": ("Tokyo", "Japan"), "osaka": ("Osaka", "Japan"),
    "seoul": ("Seoul", "South Korea"), "beijing": ("Beijing", "China"), "shanghai": ("Shanghai", "China"),
    "shenzhen": ("Shenzhen", "China"), "hong kong": ("Hong Kong", "Hong Kong"), "taipei": ("Taipei", "Taiwan"),
    "sydney": ("Sydney", "Australia"), "melbourne": ("Melbourne", "Australia"), "brisbane": ("Brisbane", "Australia"),
    "auckland": ("Auckland", "New Zealand"), "manila": ("Manila", "Philippines"), "jakarta": ("Jakarta", "Indonesia"),
    "são paulo": ("São Paulo", "Brazil"), "sao paulo": ("São Paulo", "Brazil"), "mexico city": ("Mexico City", "Mexico"),
    "buenos aires": ("Buenos Aires", "Argentina"), "bogota": ("Bogotá", "Colombia"), "bogotá": ("Bogotá", "Colombia"),
    "santiago": ("Santiago", "Chile"), "cape town": ("Cape Town", "South Africa"), "lagos": ("Lagos", "Nigeria"),
    "nairobi": ("Nairobi", "Kenya"), "belgrade": ("Belgrade", "Serbia"), "leiden": ("Leiden", "Netherlands"),
    "utrecht": ("Utrecht", "Netherlands"), "eindhoven": ("Eindhoven", "Netherlands"), "wichita": ("Wichita", US),
    "kyiv": ("Kyiv", "Ukraine"), "kiev": ("Kyiv", "Ukraine"), "lviv": ("Lviv", "Ukraine"), "montréal": ("Montreal", "Canada"),
    "cologne": ("Cologne", "Germany"), "köln": ("Cologne", "Germany"), "athens": ("Athens", "Greece"),
    "halifax": ("Halifax", "Canada"), "calgary": ("Calgary", "Canada"), "riyadh": ("Riyadh", "Saudi Arabia"),
    "borlänge": ("Borlänge", "Sweden"), "gothenburg": ("Gothenburg", "Sweden"), "malmö": ("Malmö", "Sweden"),
    "tallinn": ("Tallinn", "Estonia"), "bucharest": ("Bucharest", "Romania"), "budapest": ("Budapest", "Hungary"),
    "istanbul": ("Istanbul", "Turkey"), "doha": ("Doha", "Qatar"), "washington d.c": ("Washington DC", US),
    "ho chi minh city": ("Ho Chi Minh City", "Vietnam"), "hanoi": ("Hanoi", "Vietnam"), "kuala lumpur": ("Kuala Lumpur", "Malaysia"),
    "bangkok": ("Bangkok", "Thailand"), "cambridge": ("Cambridge", US), "new haven": ("New Haven", US),
    "ann arbor": ("Ann Arbor", US), "columbus": ("Columbus", US), "irvine": ("Irvine", US), "san antonio": ("San Antonio", US),
    "charlotte": ("Charlotte", US), "huntsville": ("Huntsville", US), "colorado springs": ("Colorado Springs", US),
}
CITY_ALIASES = {"new york city": "New York", "nyc": "New York", "bangalore": "Bengaluru", "münchen": "Munich",
                "zürich": "Zurich", "washington d.c.": "Washington DC", "washington, d.c.": "Washington DC"}

REMOTE_RE = re.compile(r"\b(remote|anywhere|distributed|work from home|wfh)\b", re.I)
NOISE_RE = re.compile(r"\((?:[^)]*)\)|\b(?:remote[- ]friendly|remote[- ]first|remote|hybrid|on[- ]?site|onsite|in[- ]office|"
                      r"travel[- ]required|anywhere|metro area|area|hq|headquarters|office|and)\b", re.I)
REGIONS = {"emea", "apac", "latam", "usca", "us/ca", "dach", "nordics", "benelux", "americas", "europe", "eu", "middle east", "asia", "north america", "global",
           "worldwide", "northeast", "southwest", "southeast", "northwest", "midwest", "west coast", "east coast"}


def _country(tok):
    return COUNTRIES.get(tok.strip(" .").lower()) if tok else None


def _state(tok):
    t = tok.strip(" .")
    if t.upper() in US_STATES and t.isupper():
        return US_STATES[t.upper()]
    if t.lower() in US_STATE_NAMES:
        return t.title()
    return None


def _city(name):
    key = name.strip().lower()
    if key in CITIES:
        return CITIES[key][0]
    name = name.strip()
    return CITY_ALIASES.get(key, name[:1].upper() + name[1:] if name else name)


def _segment(seg):
    """One location like 'Seattle, WA' -> (city or None, country or None)."""
    raw = seg.strip()
    low = raw.lower()
    if low in CITIES:                                    # whole thing is a known city ("washington, dc")
        return CITIES[low]
    s = NOISE_RE.sub(" ", raw)
    s = re.sub(r"\s*[-–—]\s*", ", ", s)                  # "Remote - US" -> ", US"
    parts = [p.strip(" .,") for p in s.split(",") if p.strip(" .,")]
    if not parts:
        return None, None
    if len(parts) == 1:
        p = parts[0]
        pl = p.lower()
        if pl in CITIES:
            return CITIES[pl]
        if pl in REGIONS:
            return None, None
        if c := _country(p):
            return None, c
        if _state(p):
            return None, US
        if pl in CA_PROVINCES:
            return None, "Canada"
        words = p.split()
        for k in (2, 1):                                 # "Bangalore India", "Toronto Canada"
            if len(words) > k and (c := _country(" ".join(words[-k:]))):
                head = " ".join(words[:-k])
                return (CITIES[head.lower()][0] if head.lower() in CITIES else _city(head)), c
        return _city(p), None                            # unknown bare place: keep as city
    city, last = parts[0], parts[-1]
    if last.lower() in REGIONS:
        last = parts[-2] if len(parts) > 2 else ""
    if (city + ", " + last).lower() in CITIES:            # "Cambridge, UK"
        return CITIES[(city + ", " + last).lower()]
    if _state(last) or (last.upper() in ("DC", "D.C.")):
        if (city.lower() in US_STATE_NAMES or _state(city)) and city.lower() not in CITIES:
            return None, US
        return _city(city), US
    if last.lower() in CA_PROVINCES:
        return (None if city.lower() in CA_PROVINCES else _city(city)), "Canada"
    if c := _country(last):
        if _country(city) or city.lower() in REGIONS or city.lower() in CA_PROVINCES or _state(city):
            return None, c
        return _city(city), c
    if city.lower() in CITIES:
        return CITIES[city.lower()]
    return _city(city), None


def parse_location(text):
    text = (text or "").strip()
    out = {"cities": [], "countries": [], "remote": bool(REMOTE_RE.search(text))}
    if not text:
        return out
    segs = [x for x in re.split(r"\s*[|;]\s*|\s+/\s+|\s+or\s+", text, flags=re.I) if x.strip()]
    bare_states = sum(1 for x in segs if NOISE_RE.sub(" ", x).strip(" -,.").lower() in US_STATE_NAMES)
    for seg in segs:
        bare = NOISE_RE.sub(" ", seg).strip(" -,.").lower()
        # "New York" / "Washington" alone means the state when it's remote or listed with other states
        if bare in ("new york", "washington") and (REMOTE_RE.search(seg) or bare_states > 1):
            if US not in out["countries"]:
                out["countries"].append(US)
            continue
        city, country = _segment(seg)
        if city and not country and city.lower() in CITIES:
            city, country = CITIES[city.lower()]
        if city and city not in out["cities"]:
            out["cities"].append(city)
        if country and country not in out["countries"]:
            out["countries"].append(country)
    return out
