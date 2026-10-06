import json
import logging
import math
import os
import re
import time
from datetime import date, timedelta
from functools import lru_cache

import requests

log = logging.getLogger(__name__)


def _load_env_file():
    """Read KEY=value lines from .env (project folder or current folder). No extra package needed."""
    here = os.path.dirname(os.path.abspath(__file__))
    for folder in (os.getcwd(), os.path.dirname(here), here):
        path = os.path.join(folder, ".env")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
        return path
    return None


ENV_FILE = _load_env_file()

TIMEOUT = 25
USER_AGENT = "EcoTravelAssistant/1.0 (student project)"

CLIMATIQ_KEY = os.getenv("CLIMATIQ_API_KEY")
DUFFEL_TOKEN = os.getenv("DUFFEL_ACCESS_TOKEN")
LITEAPI_KEY = os.getenv("LITEAPI_KEY")
DEFAULT_CURRENCY = os.getenv("DEFAULT_CURRENCY", "EUR")

if not ENV_FILE:
    log.warning("No .env file found — API keys must be set as environment variables.")


class ApiError(Exception):
    """Raised when a provider is not configured or returns an error."""


def _request(method, url, **kwargs):
    kwargs.setdefault("timeout", TIMEOUT)
    headers = kwargs.pop("headers", {})
    headers.setdefault("User-Agent", USER_AGENT)
    try:
        r = requests.request(method, url, headers=headers, **kwargs)
    except requests.RequestException as e:
        raise ApiError(f"Could not reach {url.split('/')[2]}: {e}") from e
    if r.status_code >= 400:
        raise ApiError(f"{url.split('/')[2]} returned {r.status_code}: {r.text[:300]}")
    return r.json()


def haversine_km(a, b):
    """Great-circle distance between two places with 'lat' and 'lon' keys."""
    d_lat, d_lon = math.radians(b["lat"] - a["lat"]), math.radians(b["lon"] - a["lon"])
    h = math.sin(d_lat / 2) ** 2 + math.cos(math.radians(a["lat"])) * math.cos(math.radians(b["lat"])) * math.sin(d_lon / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


# ---------------------------------------------------------------------------
# Geocoding — Open-Meteo (free, no key)
# ---------------------------------------------------------------------------
@lru_cache(maxsize=256)
def _geocode_cached(name, count):
    data = _request("GET", "https://geocoding-api.open-meteo.com/v1/search",
                    params={"name": name, "count": count, "language": "en", "format": "json"})
    return tuple(
        (r["name"], r.get("country", ""), r.get("country_code", ""), r.get("admin1", ""),
         r["latitude"], r["longitude"], r.get("population") or 0)
        for r in data.get("results", [])
    )


def geocode(name, count=5):
    """Return matching places: [{name, country, country_code, region, lat, lon, population}]."""
    name = (name or "").strip()
    if len(name) < 2:
        return []
    rows = _geocode_cached(name, count)
    return [dict(name=n, country=c, country_code=cc, region=a, lat=la, lon=lo, population=p) for n, c, cc, a, la, lo, p in rows]


# ---------------------------------------------------------------------------
# Landmarks — Wikipedia (free, no key)
# ---------------------------------------------------------------------------
WIKI = "https://en.wikipedia.org/w/api.php"
_SKIP_TITLES = re.compile(
    r"(station|airport|street|straat|road|list of|metro|tram stop|attack|bombing|riot|siege|battle|burning|"
    r"diocese|arrondissement|court of|datum|inauguration|\d{4})", re.I)


def nearby_landmarks(lat, lon, limit=8, city_name=None):
    """Well-known places within 10 km, most-viewed on Wikipedia first: [{name, lat, lon}]."""
    base = {"action": "query", "format": "json"}
    near = _request("GET", WIKI, params={**base, "list": "geosearch", "gscoord": f"{lat}|{lon}", "gsradius": 10000,
                                         "gslimit": 100, "gsprop": "type"}).get("query", {}).get("geosearch", [])
    # Keep places tagged as landmarks (or untagged); drop cities, events, regions, schools, etc.
    candidates = {p["title"]: p for p in near if p.get("type") in ("landmark", None) and not _SKIP_TITLES.search(p["title"])
                  and p["title"] != city_name}
    if not candidates:
        return []
    views = _request("GET", WIKI, params={**base, "generator": "geosearch", "ggscoord": f"{lat}|{lon}", "ggsradius": 10000,
                                          "ggslimit": 100, "prop": "pageviews", "pvipdays": 30}).get("query", {}).get("pages", {})
    popularity = {p["title"]: sum(v or 0 for v in (p.get("pageviews") or {}).values()) for p in views.values()}
    ranked = sorted(candidates.values(), key=lambda p: popularity.get(p["title"], 0), reverse=True)
    return [dict(name=p["title"], lat=p["lat"], lon=p["lon"]) for p in ranked[:limit]]


def find_landmark(query, city):
    """Look up a landmark the user typed. Returns {name, lat, lon} or None."""
    data = _request("GET", WIKI, params={
        "action": "query", "generator": "search", "gsrsearch": f"{query} {city}", "gsrlimit": 3,
        "prop": "coordinates", "format": "json",
    })
    pages = sorted(data.get("query", {}).get("pages", {}).values(), key=lambda p: p.get("index", 99))
    for p in pages:
        if p.get("coordinates"):
            c = p["coordinates"][0]
            return dict(name=p["title"], lat=c["lat"], lon=c["lon"])
    return None


# ---------------------------------------------------------------------------
# Emissions — Climatiq estimate API (works on the free plan)
# ---------------------------------------------------------------------------
CLIMATIQ = "https://api.climatiq.io"
CLIMATIQ_DATA_VERSION = "^37"

# Emission factors (activity IDs from Climatiq's database; UK BEIS/DESNZ unless noted)
FACTOR_RAIL_UK = "passenger_train-route_type_national_rail-fuel_source_na"
FACTOR_RAIL_INTERNATIONAL = "passenger_train-route_type_international_rail-fuel_source_na"
FACTOR_FLIGHT_DOMESTIC = "passenger_flight-route_type_domestic-aircraft_type_na-distance_na-class_na-rf_included-distance_uplift_included"
FACTOR_FLIGHT_SHORT = ("passenger_flight-route_type_international-aircraft_type_na-distance_short_haul_lt_3700km"
                       "-class_economy-rf_included-distance_uplift_included")
FACTOR_FLIGHT_LONG = ("passenger_flight-route_type_international-aircraft_type_na-distance_long_haul_gt_3700km"
                      "-class_economy-rf_included-distance_uplift_included")
FACTOR_COACH = "passenger_vehicle-vehicle_type_coach-fuel_source_na-distance_na-engine_size_na"
FACTOR_CAR_PETROL = "passenger_vehicle-vehicle_type_car-fuel_source_petrol-engine_size_na-vehicle_age_na-vehicle_weight_na"
FACTOR_CAR_ELECTRIC = "passenger_vehicle-vehicle_type_car-fuel_source_bev-engine_size_na-vehicle_age_na-vehicle_weight_na"
FACTOR_HOTEL = "accommodation-type_hotel_stay"


@lru_cache(maxsize=512)
def _climatiq_estimate(activity_id, params_json, region=None):
    if not CLIMATIQ_KEY:
        raise ApiError("CLIMATIQ_API_KEY is not set")
    factor = {"activity_id": activity_id, "data_version": CLIMATIQ_DATA_VERSION}
    if region:
        factor["region"] = region
    r = _request("POST", CLIMATIQ + "/data/v1/estimate", json={"emission_factor": factor, "parameters": json.loads(params_json)},
                 headers={"Authorization": f"Bearer {CLIMATIQ_KEY}"})
    ef = r.get("emission_factor") or {}
    return float(r["co2e"]), f"{ef.get('source', 'Climatiq')} {ef.get('year', '')}".strip()


def climatiq_estimate(activity_id, params, region=None):
    """kg CO2e for one factor. Tries the given region first, then Climatiq's default region."""
    params_json = json.dumps(params, sort_keys=True)
    if region:
        try:
            return _climatiq_estimate(activity_id, params_json, region)
        except ApiError:
            pass
    return _climatiq_estimate(activity_id, params_json, None)


def emissions_per_passenger(mode, distance_km, domestic=False, in_uk=False):
    """
    kg CO2e for ONE passenger travelling `distance_km` one way. Returns (kg, source).
    mode: "rail" | "flight" | "coach". For flights pass the straight-line distance:
    the factor already includes the usual uplift for real flight paths.
    """
    params = {"passengers": 1, "distance": round(distance_km, 1), "distance_unit": "km"}
    if mode == "rail":
        return climatiq_estimate(FACTOR_RAIL_UK if in_uk else FACTOR_RAIL_INTERNATIONAL, params)
    if mode == "coach":
        return climatiq_estimate(FACTOR_COACH, params)
    if mode == "flight":
        factor = FACTOR_FLIGHT_DOMESTIC if domestic else FACTOR_FLIGHT_SHORT if distance_km < 3700 else FACTOR_FLIGHT_LONG
        return climatiq_estimate(factor, params)
    raise ValueError(mode)


def emissions_per_vehicle(electric, distance_km, country_code=None):
    """kg CO2e for ONE car driving `distance_km`. Returns (kg, source)."""
    params = {"distance": round(distance_km, 1), "distance_unit": "km"}
    return climatiq_estimate(FACTOR_CAR_ELECTRIC if electric else FACTOR_CAR_PETROL, params, region=country_code)


def climatiq_hotel(place, room_nights):
    """kg CO2e for `room_nights` hotel room-nights in the place's country."""
    kg, _ = climatiq_estimate(FACTOR_HOTEL, {"number": room_nights}, region=place.get("country_code"))
    return kg


# ---------------------------------------------------------------------------
# Weather — Open-Meteo (free, no key)
# ---------------------------------------------------------------------------
WEATHER_CODES = {
    0: ("☀️", "Clear"), 1: ("🌤️", "Mostly clear"), 2: ("⛅", "Partly cloudy"), 3: ("☁️", "Cloudy"),
    45: ("🌫️", "Fog"), 48: ("🌫️", "Fog"), 51: ("🌦️", "Light drizzle"), 53: ("🌦️", "Drizzle"), 55: ("🌧️", "Heavy drizzle"),
    56: ("🌧️", "Freezing drizzle"), 57: ("🌧️", "Freezing drizzle"), 61: ("🌦️", "Light rain"), 63: ("🌧️", "Rain"),
    65: ("🌧️", "Heavy rain"), 66: ("🌧️", "Freezing rain"), 67: ("🌧️", "Freezing rain"), 71: ("🌨️", "Light snow"),
    73: ("🌨️", "Snow"), 75: ("❄️", "Heavy snow"), 77: ("🌨️", "Snow grains"), 80: ("🌦️", "Showers"),
    81: ("🌧️", "Showers"), 82: ("⛈️", "Heavy showers"), 85: ("🌨️", "Snow showers"), 86: ("🌨️", "Snow showers"),
    95: ("⛈️", "Thunderstorm"), 96: ("⛈️", "Thunderstorm"), 99: ("⛈️", "Thunderstorm"),
}


def weather_outlook(lat, lon, start, end):
    """
    Weather for the trip dates (ISO strings). Uses the forecast for trips starting within
    15 days; otherwise shows what the weather was on the same dates last year (clearly labelled).
    Returns {kind, days: [{date, emoji, label, t_max, t_min, rain}], summary, tip}.
    """
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    e = min(e, s + timedelta(days=6))  # show at most 7 days
    today = date.today()
    daily_base = "weather_code,temperature_2m_max,temperature_2m_min"
    if s <= today + timedelta(days=15):
        e = min(e, today + timedelta(days=15))
        data = _request("GET", "https://api.open-meteo.com/v1/forecast", params={
            "latitude": lat, "longitude": lon, "timezone": "auto", "start_date": s.isoformat(), "end_date": e.isoformat(),
            "daily": daily_base + ",precipitation_probability_max"})
        kind, rain_key = "forecast", "precipitation_probability_max"
    else:
        def last_year(d):
            try:
                return d.replace(year=d.year - 1)
            except ValueError:  # 29 February
                return d.replace(year=d.year - 1, day=28)
        data = _request("GET", "https://archive-api.open-meteo.com/v1/archive", params={
            "latitude": lat, "longitude": lon, "timezone": "auto",
            "start_date": last_year(s).isoformat(), "end_date": last_year(e).isoformat(),
            "daily": daily_base + ",precipitation_sum"})
        kind, rain_key = "last_year", "precipitation_sum"
    d = data.get("daily") or {}
    days = []
    for i, day in enumerate(d.get("time", [])):
        code = (d.get("weather_code") or [None])[i]
        emoji, label = WEATHER_CODES.get(code, ("🌡️", "Mixed"))
        days.append(dict(date=day, emoji=emoji, label=label, t_max=d["temperature_2m_max"][i], t_min=d["temperature_2m_min"][i],
                         rain=(d.get(rain_key) or [None])[i]))
    if not days:
        raise ApiError("No weather data for these dates")
    highs = [x["t_max"] for x in days if x["t_max"] is not None]
    lows = [x["t_min"] for x in days if x["t_min"] is not None]
    wet = sum(1 for x in days if x["rain"] is not None and (x["rain"] >= 50 if kind == "forecast" else x["rain"] >= 1))
    summary = f"{min(lows):.0f}–{max(highs):.0f}°C" if highs and lows else ""
    tips = []
    if wet:
        tips.append("pack a light rain jacket")
    if highs and max(highs) >= 26:
        tips.append("bring sun protection and a refillable water bottle")
    if lows and min(lows) <= 5:
        tips.append("pack warm layers")
    tip = ("Tip: " + ", and ".join(tips) + ".") if tips else "Looks like comfortable weather for exploring on foot or by bike."
    return dict(kind=kind, days=days, summary=summary, tip=tip)


# ---------------------------------------------------------------------------
# Flights — Duffel
# ---------------------------------------------------------------------------
DUFFEL = "https://api.duffel.com"


def duffel_is_test():
    return bool(DUFFEL_TOKEN) and DUFFEL_TOKEN.startswith("duffel_test")


def _duffel(method, path, **kwargs):
    if not DUFFEL_TOKEN:
        raise ApiError("DUFFEL_ACCESS_TOKEN is not set")
    headers = {"Authorization": f"Bearer {DUFFEL_TOKEN}", "Duffel-Version": "v2", "Accept": "application/json",
               "Content-Type": "application/json"}
    return _request(method, DUFFEL + path, headers=headers, **kwargs)


@lru_cache(maxsize=128)
def airport_code(name, country_code, lat, lon):
    """Best IATA city or airport code for a place, via Duffel place suggestions."""
    places = _duffel("GET", "/places/suggestions", params={"query": name}).get("data", [])
    same_country = [p for p in places if p.get("iata_country_code") == country_code] or places
    for p in same_country:
        if p.get("type") == "city" and p.get("iata_code"):
            return p["iata_code"]
    for p in same_country:
        if p.get("iata_code"):
            return p["iata_code"]
    # Nothing by name: try airports near the coordinates
    near = _duffel("GET", "/places/suggestions", params={"lat": lat, "lng": lon, "rad": 100000}).get("data", [])
    return next((p["iata_code"] for p in near if p.get("iata_code")), None)


def _iso_minutes(duration):
    """'PT2H35M' / 'P1DT2H' → minutes."""
    m = re.match(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?", duration or "")
    if not m:
        return None
    d, h, mi = (int(x or 0) for x in m.groups())
    return d * 1440 + h * 60 + mi


def search_flights(origin_code, dest_code, departure_date, adults):
    """Live one-way economy offers, cheapest first: [{airline, price_total, currency, depart, arrive, duration_min, stops, emissions_kg}]."""
    body = {"data": {
        "slices": [{"origin": origin_code, "destination": dest_code, "departure_date": departure_date}],
        "passengers": [{"type": "adult"} for _ in range(adults)],
        "cabin_class": "economy",
    }}
    data = _duffel("POST", "/air/offer_requests", params={"return_offers": "true", "supplier_timeout": 15000}, json=body)
    offers = []
    for o in data.get("data", {}).get("offers", []):
        s = o["slices"][0]
        segs = s["segments"]
        offers.append(dict(
            airline=o.get("owner", {}).get("name", "Airline"),
            price_total=float(o["total_amount"]),
            currency=o["total_currency"],
            depart=segs[0]["departing_at"][11:16],
            arrive=segs[-1]["arriving_at"][11:16],
            depart_date=segs[0]["departing_at"][:10],
            arrive_date=segs[-1]["arriving_at"][:10],
            duration_min=_iso_minutes(s.get("duration")),
            stops=len(segs) - 1,
            # Test-mode emission figures are random, so only keep them for live tokens
            emissions_kg=float(o["total_emissions_kg"]) if o.get("total_emissions_kg") and not duffel_is_test() else None,
            logo=o.get("owner", {}).get("logo_symbol_url"),
        ))
    offers.sort(key=lambda x: x["price_total"])
    return offers


# ---------------------------------------------------------------------------
# Hotels — LiteAPI (falls back to OpenStreetMap if unavailable)
# ---------------------------------------------------------------------------
LITEAPI = "https://api.liteapi.travel/v3.0"


def liteapi_is_sandbox():
    return bool(LITEAPI_KEY) and LITEAPI_KEY.startswith("sand_")


def _lite(method, path, **kwargs):
    if not LITEAPI_KEY:
        raise ApiError("LITEAPI_KEY is not set")
    return _request(method, LITEAPI + path, headers={"X-API-Key": LITEAPI_KEY, "Accept": "application/json"}, **kwargs)


def _plain_text(html, limit=220):
    text = re.sub(r"<[^>]+>", " ", html or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit].rsplit(" ", 1)[0] + "…" if len(text) > limit else text


def search_hotels(lat, lon, country_code, checkin=None, checkout=None, adults=2, currency=None, guest_nationality="GB", limit=12):
    """
    Places to stay near a point. Uses LiteAPI (photos, ratings, rates) when a key is set,
    otherwise — or if LiteAPI fails — OpenStreetMap (no prices, booking links instead).
    """
    if not LITEAPI_KEY:
        return search_hotels_osm(lat, lon, limit=limit)
    try:
        hotels = _search_hotels_liteapi(lat, lon, country_code, checkin, checkout, adults, currency, guest_nationality, limit)
        if hotels:
            return hotels
    except ApiError as e:
        log.warning("LiteAPI failed, using OpenStreetMap instead: %s", e)
    return search_hotels_osm(lat, lon, limit=limit)


def _search_hotels_liteapi(lat, lon, country_code, checkin, checkout, adults, currency, guest_nationality, limit):
    currency = currency or DEFAULT_CURRENCY
    listing = _lite("GET", "/data/hotels", params={
        "countryCode": country_code, "latitude": lat, "longitude": lon, "radius": 3000, "limit": 40,
    }).get("data", [])
    here = {"lat": lat, "lon": lon}
    hotels = {}
    for h in listing:
        if not (h.get("latitude") and h.get("longitude")):
            continue
        hotels[h["id"]] = dict(
            id=h["id"], name=h.get("name", "Hotel"), type="Hotel", address=h.get("address", ""),
            lat=h["latitude"], lon=h["longitude"], stars=int(h.get("stars") or 0),
            rating=h.get("rating") or 0, reviews=h.get("reviewCount") or 0,
            photo=h.get("main_photo") or h.get("thumbnail"), description=_plain_text(h.get("hotelDescription")),
            price_total=None, currency=currency, room_name=None, board=None, refundable=None,
            booking_url="https://www.booking.com/searchresults.html?ss=" + requests.utils.quote(f"{h.get('name', '')} {h.get('city', '')}"),
            source="LiteAPI sandbox" if liteapi_is_sandbox() else "LiteAPI",
        )
    if not hotels:
        return []
    # Closest 30 hotels get a rate check
    nearest = sorted(hotels.values(), key=lambda h: haversine_km(here, h))[:30]

    if checkin and checkout:
        per_room = [2] * (adults // 2) + ([1] if adults % 2 else [])
        rates = _lite("POST", "/hotels/rates", json={
            "hotelIds": [h["id"] for h in nearest], "checkin": checkin, "checkout": checkout, "currency": currency,
            "guestNationality": guest_nationality or "GB", "occupancies": [{"adults": a} for a in per_room],
            "maxRatesPerHotel": 1, "timeout": 10,
        }, timeout=45).get("data", [])
        for r in rates:
            h = hotels.get(r.get("hotelId"))
            room_types = r.get("roomTypes") or []
            if not (h and room_types):
                continue
            rt = room_types[0]
            price = rt.get("offerRetailRate") or {}
            if price.get("amount") is None:
                continue
            rate = (rt.get("rates") or [{}])[0]
            refundable_tag = (rate.get("cancellationPolicies") or {}).get("refundableTag")
            h.update(price_total=float(price["amount"]), currency=price.get("currency", currency),
                     room_name=rate.get("name"), board=rate.get("boardName"),
                     refundable=None if not refundable_tag else refundable_tag == "RFN")
        priced = [h for h in nearest if h["price_total"] is not None]
        if priced:
            return sorted(priced, key=lambda h: h["price_total"])[:limit]
    return sorted(nearest, key=lambda h: -(h["rating"] or 0))[:limit]


# ---------------------------------------------------------------------------
# Hotels — OpenStreetMap Overpass API (free, no key)
# ---------------------------------------------------------------------------
OVERPASS_MAIN = "https://overpass-api.de/api/interpreter"
OVERPASS_MIRROR = "https://maps.mail.ru/osm/tools/overpass/api/interpreter"  # used if the main server is busy
HOTEL_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hotel_cache.json")
STAY_TYPES = {"hotel": "Hotel", "guest_house": "Guesthouse", "hostel": "Hostel", "apartment": "Apartment", "motel": "Motel"}
AREA_RADIUS_M = 3000   # we fetch everything within 3 km of an area's centre ...
AREA_GRID = 0.02       # ... and areas are ~2 km grid squares, so nearby searches share one download


def _load_hotel_cache():
    try:
        with open(HOTEL_CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_hotel_cache(cache):
    try:
        with open(HOTEL_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except OSError as e:
        log.warning("Could not save hotel cache: %s", e)


def _overpass(query):
    """Ask Overpass. The free servers limit requests, so: main -> wait and retry -> mirror."""
    last_error = None
    for url, wait in [(OVERPASS_MAIN, 0), (OVERPASS_MAIN, 4), (OVERPASS_MIRROR, 0)]:
        if wait:
            time.sleep(wait)
        try:
            return _request("POST", url, data={"data": query}, timeout=40)
        except ApiError as e:
            log.info("Overpass attempt failed (%s): %s", url, e)
            last_error = e
    raise last_error


def _parse_osm_stays(elements):
    stays = []
    for el in elements:
        tags = el.get("tags", {})
        h_lat = el.get("lat") or el.get("center", {}).get("lat")
        h_lon = el.get("lon") or el.get("center", {}).get("lon")
        if not (h_lat and h_lon and tags.get("name")):
            continue
        stars = re.match(r"\d", tags.get("stars", ""))
        street = " ".join(x for x in [tags.get("addr:street"), tags.get("addr:housenumber")] if x)
        city = tags.get("addr:city", "")
        website = tags.get("website") or tags.get("contact:website")
        stays.append(dict(
            id=f"osm-{el['type']}-{el['id']}",
            name=tags["name"],
            type=STAY_TYPES.get(tags.get("tourism"), "Stay"),
            address=", ".join(x for x in [street, tags.get("addr:postcode"), city] if x),
            lat=h_lat, lon=h_lon,
            stars=int(stars.group()) if stars else 0,
            rating=0, reviews=0, photo=None,
            website=website,
            booking_url=website or "https://www.booking.com/searchresults.html?ss=" + requests.utils.quote(f"{tags['name']} {city}".strip()),
            wheelchair=tags.get("wheelchair") == "yes",
            price_total=None, currency=None,
            source="OpenStreetMap",
        ))
    return stays


def search_hotels_osm(lat, lon, limit=12):
    """
    Real places to stay near a point, from OpenStreetMap, nearest first.
    No prices (OSM has none), so each result carries a booking link instead.
    Results are saved in actions/hotel_cache.json, so repeat searches work offline.
    """
    cell_lat, cell_lon = round(round(lat / AREA_GRID) * AREA_GRID, 4), round(round(lon / AREA_GRID) * AREA_GRID, 4)
    key = f"{cell_lat},{cell_lon}"
    cache = _load_hotel_cache()
    if key not in cache:
        query = f"""
        [out:json][timeout:25];
        (
          node["tourism"~"^(hotel|guest_house|hostel|apartment|motel)$"]["name"](around:{AREA_RADIUS_M},{cell_lat},{cell_lon});
          way["tourism"~"^(hotel|guest_house|hostel|apartment|motel)$"]["name"](around:{AREA_RADIUS_M},{cell_lat},{cell_lon});
        );
        out center tags;
        """
        cache[key] = _parse_osm_stays(_overpass(query).get("elements", []))
        _save_hotel_cache(cache)
    here = {"lat": lat, "lon": lon}
    stays = sorted(cache[key], key=lambda h: haversine_km(here, h))
    return stays[:limit]