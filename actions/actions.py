import copy
import json
import logging
import math
import os
import re
from datetime import date, timedelta
from typing import Any, Dict, List, Text

import requests
from rasa_sdk import Action, Tracker
from rasa_sdk.events import SlotSet
from rasa_sdk.executor import CollectingDispatcher

from . import travel_apis as api
from .travel_apis import ApiError, haversine_km

log = logging.getLogger(__name__)

HANDOFF_WEBHOOK_URL = os.getenv("HANDOFF_WEBHOOK_URL")  # e.g. your CRM / helpdesk webhook
ADVISOR_EMAIL = os.getenv("ADVISOR_EMAIL")

# Fallback factors (kg CO2e per passenger-km, cars per vehicle-km), used only when
# Climatiq is unavailable. UK DESNZ conversion factors 2024, rounded.
FALLBACK_FACTORS = {"rail": 0.0355, "air_short": 0.1510, "air_long": 0.1479, "car": 0.1645, "ev": 0.0468}
COACH_FACTOR = 0.0272  # DESNZ 2024 coach, kg CO2e per passenger-km (fallback only)
UPLIFT = {"rail": 1.2, "road": 1.25, "air": 1.08}
CAR_SEATS = 5

# Countries joined by land or rail tunnel, where train / coach / car trips are realistic.
LAND_EUROPE = {"GB", "FR", "BE", "NL", "LU", "DE", "CH", "AT", "IT", "ES", "PT", "DK", "SE", "NO", "FI", "PL", "CZ", "SK",
               "HU", "SI", "HR", "BA", "RS", "ME", "MK", "AL", "GR", "BG", "RO", "LT", "LV", "EE", "LI", "MC", "AD", "SM"}

INTENT_OPTS = [("planning", "✈️ I'm planning a trip"), ("exploring", "🌍 Just exploring"), ("inspiration", "💡 Looking for inspiration")]
COMPANION_OPTS = [("solo", "🧍 Solo"), ("couple", "❤️ Couple"), ("family", "👨‍👩‍👧 Family"), ("friends", "👫 Friends"), ("group", "👥 Group")]
PREF_OPTS = [("carbon", "🌱 Lowest carbon footprint"), ("budget", "💰 Budget-friendly"), ("fast", "⚡ Fastest journey"),
             ("comfort", "🛋️ Comfort"), ("scenic", "🚆 Scenic routes"), ("balance", "⚖️ A balance of everything")]
FLEX_OPTS = [("2", "A weekend (2 nights)"), ("3", "3 nights"), ("5", "5 nights"), ("7", "A week"), ("3", "Not sure yet")]
EDIT_OPTS = [("destination", "📍 Destination"), ("origin", "🏁 Starting point"), ("dates", "📅 Dates"),
             ("travellers", "👥 Travellers"), ("preferences", "🎯 Preferences"), ("cancel", "↩️ Never mind")]
EXPLORE_OPTS = [("transport", "🚆 Change transport"), ("hotel", "🏨 Change where I stay"), ("dates", "📅 Change dates"),
                ("destination", "📍 Change destination"), ("cancel", "↩️ Back to my plan")]
VIBE_OPTS = [("city", "🏙️ City break"), ("nature", "🌲 Nature"), ("coast", "🌊 Coast"), ("surprise", "🎲 Surprise me")]

# Curated ideas for the "just exploring" path (editorial content, not live data)
IDEAS = [
    ("Amsterdam", "city", "Flat, compact and made for bikes. Direct Eurostar from London.", "🚆 Great by train"),
    ("Edinburgh", "city", "Castles, closes and hills. About 4½ hours by train from London.", "🚆 Great by train"),
    ("Copenhagen", "coast", "Harbour swims and a city where most locals get around by bike.", "🚲 Car-free friendly"),
    ("Zurich", "nature", "Lakes, mountains and one of Europe's best rail networks.", "🏔️ Alps by rail"),
    ("Vienna", "city", "Coffee houses and palaces, linked by night trains across Europe.", "🌙 Night-train hub"),
    ("Barcelona", "coast", "Beaches and Gaudí. High-speed trains from Paris in about 6½ hours.", "🚆 Great by train"),
    ("Oslo", "nature", "Fjords on the doorstep and the scenic Bergen line nearby.", "🌲 Nature close by"),
    ("Florence", "city", "Renaissance art, about 1½ hours from Rome by high-speed train.", "🎨 Walkable"),
    ("Lisbon", "coast", "Trams, sunshine and Atlantic beaches a short train ride away.", "🚋 Trams & beaches"),
    ("Munich", "nature", "Alpine lakes and mountains within easy reach by regional train.", "🏞️ Lakes by train"),
]

MODE_META = {
    "rail": ("🚆", "Train"), "coach": ("🚌", "Coach"), "ev": ("⚡", "Electric car"),
    "car": ("🚗", "Petrol car"), "flight": ("✈️", "Flight"),
}


def new_trip():
    return dict(
        welcomed=False, name=None, intent=None, origin=None, destination=None, stops=[], route_confirmed=False,
        start=None, end=None, nights=None, flexible=False, companions=None, travellers=None, preferences=[],
        budget=None, summary_confirmed=False, editing=False, options=[], transport=None, notices=[],
        attraction=None, landmarks=[], hotels=[], stay_kg=None, hotel_choice=None, hotel=None,
        handoff=None, completed=False, awaiting=None, last_prompt=None, idea_page=0, weather=None,
    )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def payload(cmd, value=""):
    return "/inform" + json.dumps({"cmd": cmd, "value": str(value)})


def buttons(options, cmd):
    return [{"title": label, "payload": payload(cmd, oid)} for oid, label in options]


def nights_text(n):
    return "a day trip" if n == 0 else f"{n} night{'s' if n != 1 else ''}"


def fmt_date(s):
    d = date.fromisoformat(s)
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"


def dates_text(t):
    if t["start"] and t["end"]:
        return f"{fmt_date(t['start'])} → {fmt_date(t['end'])}"
    return "Flexible" if t["flexible"] else "—"


def route_text(t):
    names = [p["name"] for p in [t["origin"], *t["stops"], t["destination"]] if p]
    return " → ".join(names)


def label_of(options, oid):
    return next((label for i, label in options if i == oid), oid)


def clean_name(raw):
    s = re.sub(r"^(hi|hello|hey)[,!.\s]+", "", raw.strip(), flags=re.I)
    s = re.sub(r"^(my name is|my name's|i am|i'm|im|it's|its|call me|this is)\s+", "", s, flags=re.I)
    s = "".join(ch for ch in s if ch.isalpha() or ch in " '-").strip()
    words = s.split()[:2]
    if not words or len(" ".join(words)) > 32:
        return ""
    return " ".join(w[:1].upper() + w[1:] for w in words)


def is_handoff(text):
    t = text.lower()
    if re.search(r"\b(human|real person|actual person|advisor|adviser|representative|live agent|customer service)\b", t):
        return True
    return bool(re.search(r"\b(someone|somebody|a person)\b", t) and re.search(r"\b(help|speak|talk|book|call|contact)\b", t))


WORDNUM = dict(a=1, an=1, one=1, two=2, three=3, four=4, five=5, six=6, seven=7, eight=8, nine=9, ten=10)
NUMW = r"(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten)"


def to_num(s):
    return int(s) if s.isdigit() else WORDNUM.get(s)


def extract_details(text):
    """Dates, trip length, companions, traveller count and budget from free text."""
    t = text.lower()
    out = {}
    today = date.today()
    dow = today.weekday()
    fri = today + timedelta(days=(4 - dow) % 7)
    if "next weekend" in t:
        s = fri if dow >= 5 else fri + timedelta(days=7)
        out["start"], out["end"] = s, s + timedelta(days=2)
    elif "this weekend" in t:
        out["start"], out["end"] = (today, today + timedelta(days=1)) if dow >= 5 else (fri, fri + timedelta(days=2))
    elif re.search(r"\btomorrow\b", t):
        out["start"] = today + timedelta(days=1)
    m = re.search(rf"\b{NUMW}\s*(nights?|days?)\b", t)
    if m and to_num(m.group(1)):
        v = to_num(m.group(1))
        out["nights"] = max(1, v - 1) if m.group(2).startswith("day") else v
    elif re.search(r"\b(a|one) week\b", t):
        out["nights"] = 7
    if "start" in out and "end" not in out:
        out["end"] = out["start"] + timedelta(days=out.get("nights", 2))
    for k in ("start", "end"):
        if k in out:
            out[k] = out[k].isoformat()

    if re.search(r"\b(partner|girlfriend|boyfriend|wife|husband|spouse|couple|honeymoon)\b", t):
        out["companions"], out["travellers"] = "couple", 2
    elif re.search(r"\b(family|kids|children|son|daughter)\b", t):
        out["companions"] = "family"
    elif re.search(r"\b(friends?|mates)\b", t):
        out["companions"] = "friends"
    elif re.search(r"\b(solo|alone|by myself|on my own|just me)\b", t):
        out["companions"], out["travellers"] = "solo", 1
    m = re.search(rf"\b{NUMW}\s*(people|persons|travell?ers|adults|of us|guests)\b", t)
    if m and to_num(m.group(1)):
        out["travellers"] = to_num(m.group(1))
    m = re.search(rf"\bwith (my )?{NUMW}\s*(friends|others|people|kids|children)\b", t)
    if m and to_num(m.group(2)):
        out["travellers"] = to_num(m.group(2)) + 1
    m = re.search(r"(budget|under|max(?:imum)?|less than|up to|no more than)\D{0,10}(\d{2,5})", t) or re.search(r"[£€$]\s?(\d{2,5})", t)
    if m:
        out["budget"] = int(m.groups()[-1])
    return out


def city_mentions(text, city_entities):
    """[(name, is_origin)] from NLU `city` entities, with a regex fallback for 'to X' / 'from X'."""
    found = []
    for e in city_entities:
        before = text[max(0, e.get("start", 0) - 12):e.get("start", 0)].lower()
        found.append((str(e["value"]), bool(re.search(r"(from|leaving|starting in|based in)\s*$", before))))
    if not found:
        stop = r"(?=\s+(?:from|to|next|this|on|in|with|for|and)\b|[,.!?]|$)"
        for m in re.finditer(rf"\b(from|to)\s+([A-Z][\w'\-]+(?:\s+[A-Z][\w'\-]+)*){stop}", text):
            found.append((m.group(2), m.group(1).lower() == "from"))
    return found


def money(amount, currency):
    symbol = {"GBP": "£", "EUR": "€", "USD": "$"}.get(currency, f"{currency} ")
    return f"{symbol}{amount:,.0f}"


# ---------------------------------------------------------------------------
# Output helper
# ---------------------------------------------------------------------------
class Out:
    def __init__(self, dispatcher, trip):
        self.d = dispatcher
        self.t = trip

    def say(self, text, btns=None):
        self.d.utter_message(text=text, buttons=btns or [])

    def ui(self, type_, btns=None, **data):
        # Buttons travel inside the custom payload so the card can draw them in place.
        self.d.utter_message(json_message={"type": type_, "buttons": btns or [], **data})

    def prompt(self, awaiting, text=None, type_=None, btns=None, **data):
        """Ask a question and remember it so we can re-ask after a side-conversation."""
        self.t["awaiting"] = awaiting
        self.t["last_prompt"] = dict(text=text, type=type_, btns=btns or [], data=data)
        if text or (btns and not type_):
            self.say(text or "", None if type_ else btns)
        if type_:
            self.ui(type_, btns, **data)

    def reprompt(self):
        lp = self.t.get("last_prompt")
        if not lp:
            return
        if lp["type"]:
            self.ui(lp["type"], lp["btns"], **lp["data"])
        elif lp["btns"]:
            self.say(lp["text"] or "Pick one:", lp["btns"])


# ---------------------------------------------------------------------------
# The planner
# ---------------------------------------------------------------------------
class Planner:
    def __init__(self, trip, out, tracker):
        self.t, self.o, self.tracker = trip, out, tracker

    # ----- entry point -----
    def handle(self, intent, ents, city_ents, text):
        t = self.t
        if "cmd" in ents:
            return self.command(str(ents["cmd"]), str(ents.get("value", "")))
        if intent == "request_human" or is_handoff(text):
            return self.handoff()
        if intent == "start_over":
            return self.ask_restart()
        if t["welcomed"] and (intent == "ask_weather" or re.search(r"\b(weather|forecast|temperature|rain)\b", text.lower())):
            return self.weather_question()
        if not t["welcomed"]:
            self.welcome()
            if intent == "greet" or not text or text.startswith("/"):
                return
        elif intent == "greet" and not text.startswith("/"):
            self.o.say(f"Hello{', ' + t['name'] if t['name'] else ''}! 👋")
            return self.o.reprompt()
        elif intent == "greet":
            return self.o.reprompt()
        self.free_text(intent, text, city_ents)

    def welcome(self):
        t = self.t
        t["welcomed"] = True
        if t["name"]:
            self.o.say(f"Welcome back, {t['name']}! Ready for a fresh adventure? 🌍")
        else:
            self.o.say("Welcome! I help plan trips that are a bit kinder to the planet. 🌍")
            self.o.say("Before we start dreaming about your next adventure, I'd love to get to know you!")
        self.advance()

    # ----- the next question -----
    def advance(self):
        t, o = self.t, self.o
        if not t["name"]:
            return o.prompt("name", "First things first — what should I call you?", "name_input")
        if not t["intent"]:
            return o.prompt("intent", "Are you planning a trip soon, or just exploring ideas for now?", btns=buttons(INTENT_OPTS, "trip_intent"))
        if t["intent"] != "planning":
            return
        if not t["destination"]:
            return o.prompt("destination", "Exciting! Where are you thinking of heading?", "city_input", field="destination", multi=True,
                            suggestions=["Amsterdam", "Paris", "Edinburgh", "Barcelona", "Copenhagen", "Vienna"])
        if not t["start"] and not t["flexible"]:
            return o.prompt("dates", "When are you planning to escape?", "date_range")
        if t["flexible"] and t["nights"] is None:
            return o.prompt("flex_nights", "No problem at all! Roughly how long are you thinking?", btns=buttons(FLEX_OPTS, "flex_nights"))
        if not t["origin"]:
            return o.prompt("origin", "Where will your adventure begin?", "city_input", field="origin", multi=False, suggestions=[])
        if not t["route_confirmed"]:
            return o.prompt("route", "And where would you like to go? I've filled in your destination. Add any stops along the way.", "route_editor",
                            origin=t["origin"]["name"], destination=t["destination"]["name"], stops=[s["name"] for s in t["stops"]])
        if not t["companions"]:
            return o.prompt("companions", "Who is joining you on this adventure?", btns=buttons(COMPANION_OPTS, "companions"))
        if t["travellers"] is None:
            return o.prompt("travellers", "Lovely! How many people will be travelling in total?", "number_picker")
        if not t["preferences"]:
            return o.prompt("preferences", "One last thing before we find your best travel options — what matters most to you?",
                            "multi_select", options=[{"id": i, "label": l} for i, l in PREF_OPTS], cmd="preferences")
        if not t["summary_confirmed"]:
            t["editing"] = False
            return o.prompt("summary", None, "trip_summary",
                            btns=[{"title": "Looks good!", "payload": payload("summary", "ok")},
                                  {"title": "Edit my trip", "payload": payload("summary", "edit")}],
                            trip=self.view())
        if not t["transport"]:
            return self.show_transport()
        if t["hotel_choice"] is None:
            if t["nights"] == 0:
                t["hotel_choice"] = "none"
                o.say("As it's a day trip, you won't need a bed for the night. 🌙")
                return self.advance()
            return o.prompt("hotel_ask", "Now that we've sorted your journey, shall we find you a lovely place to stay near your attractions?",
                            btns=[{"title": "🏨 Yes, find me a hotel", "payload": payload("hotel_ask", "yes")},
                                  {"title": "⏭️ Skip for now", "payload": payload("hotel_ask", "skip")}])
        self.show_final()

    def view(self):
        """A plain summary of the trip for the front end."""
        t = self.t
        return dict(
            name=t["name"], origin=t["origin"]["name"] if t["origin"] else None,
            destination=t["destination"]["name"] if t["destination"] else None,
            stops=[s["name"] for s in t["stops"]], dates=dates_text(t),
            nights=t["nights"], flexible=t["flexible"], travellers=t["travellers"],
            companions=label_of(COMPANION_OPTS, t["companions"]) if t["companions"] else None,
            preferences=[label_of(PREF_OPTS, p) for p in t["preferences"]],
            budget=t["budget"], route=route_text(t),
        )

    # ----- button payloads -----
    def command(self, cmd, value):
        t, o = self.t, self.o
        if cmd == "name":
            return self.set_name(value)
        if cmd == "trip_intent":
            return self.set_intent(value)
        if cmd == "vibe":
            o.say({"city": "City breaks it is! These are compact, walkable and well connected by rail:",
                   "nature": "Fresh air, coming right up. 🌲 These put nature within easy reach by train:",
                   "coast": "Sea breeze mode activated. 🌊 A few coastal favourites:"}.get(value, "Feeling adventurous! Here's a mixed bag:"))
            return self.show_ideas(value)
        if cmd == "more_ideas":
            return self.show_ideas(value or None)
        if cmd == "plan_idea":
            t["intent"] = "planning"
            return self.set_destinations([value])
        if cmd == "destination":
            return self.set_destinations([v for v in value.split("|") if v.strip()])
        if cmd == "dates":
            s, e = value.split("|")
            return self.set_dates(s, e)
        if cmd == "flexible":
            t.update(flexible=True, start=None, end=None, nights=None)
            return self.advance()
        if cmd == "flex_nights":
            n = int(value)
            t.update(flexible=True, nights=n)
            o.say(f"Great, I'll plan around roughly {nights_text(n)}. You can pin down exact dates later.")
            return self.advance()
        if cmd == "origin":
            return self.set_origin(value)
        if cmd == "route":
            dest, _, stops = value.partition("|")
            return self.set_route(dest, [s for s in stops.split(";") if s.strip()])
        if cmd == "companions":
            return self.set_companions(value)
        if cmd == "travellers":
            return self.set_travellers(int(value))
        if cmd == "preferences":
            return self.set_preferences([v for v in value.split(",") if v])
        if cmd == "summary":
            return self.summary(value)
        if cmd == "edit":
            return self.edit(value)
        if cmd == "transport":
            return self.choose_transport(value)
        if cmd == "hotel_ask":
            return self.hotel_ask(value)
        if cmd == "attraction":
            return self.set_attraction(value)
        if cmd == "hotel":
            return self.choose_hotel(value)
        if cmd == "final":
            return self.final(value)
        if cmd == "connect":
            return self.connect_advisor()
        if cmd == "after_handoff":
            if value == "again":
                return self.restart()
            t["handoff"] = None
            o.say("Happy to keep going! Here's where we left off.")
            return self.advance()
        if cmd == "restart":
            if value == "yes":
                return self.restart()
            o.say("Keeping everything as it is. 👍")
            return o.reprompt() if t["completed"] else self.advance()
        o.say("Sorry, I didn't understand that button.")

    # ----- setters -----
    def set_name(self, raw):
        name = clean_name(raw)
        if not name:
            self.o.say("I didn't quite catch a name there. What should I call you?")
            return self.o.reprompt()
        self.t["name"] = name
        self.o.say(f"Lovely to meet you, {name}! 🌱 Let's plan something wonderful.")
        self.advance()

    def set_intent(self, value):
        t, o = self.t, self.o
        t["intent"] = value
        if value == "planning":
            if t["destination"]:
                o.say(f"You mentioned {t['destination']['name']}. Great pick!")
            return self.advance()
        if value == "exploring":
            o.say("Love that. No pressure at all! Here are a few places that are easy to reach without flying from much of Europe:")
            return self.show_ideas(None)
        o.prompt("vibe", "Ooh, let's find you something special! ✨ What kind of trip is calling you?", btns=buttons(VIBE_OPTS, "vibe"))

    def show_ideas(self, vibe):
        t = self.t
        pool = [i for i in IDEAS if not vibe or vibe == "surprise" or i[1] == vibe]
        start = (t["idea_page"] * 4) % len(pool)
        t["idea_page"] += 1
        picks = (pool[start:] + pool[:start])[:4]
        self.o.prompt("ideas", None, "ideas", vibe=vibe or "",
                      ideas=[dict(city=c, why=w, tag=g) for c, _, w, g in picks])

    def resolve(self, name):
        """Look a place up with Open-Meteo. Returns a place dict or None (and tells the user why)."""
        try:
            hits = api.geocode(name)
        except ApiError as e:
            log.warning(e)
            self.o.say("I couldn't look places up just now. Please try again in a moment.")
            return None
        if not hits:
            self.o.say(f"I couldn't find a place called “{name}”. Could you check the spelling or try a nearby city?")
            return None
        h = hits[0]
        return dict(name=h["name"], country=h["country"], country_code=h["country_code"], lat=h["lat"], lon=h["lon"])

    def set_destinations(self, names):
        t, o = self.t, self.o
        places = []
        for n in names:
            p = self.resolve(n)
            if not p:
                return o.reprompt()
            places.append(p)
        if not places:
            return o.reprompt()
        if t["origin"] and places[-1]["name"] == t["origin"]["name"]:
            o.say("That's where you're starting from! Where would you like to go?")
            return o.reprompt()
        changed = not t["destination"] or places[-1]["name"] != t["destination"]["name"]
        t.update(destination=places[-1], stops=places[:-1], intent="planning", route_confirmed=t["editing"] and bool(t["origin"]))
        if changed:
            t.update(transport=None, options=[], hotel=None, hotel_choice=None, attraction=None, hotels=[])
        dest = places[-1]
        o.say(f"A multi-stop adventure: {' → '.join(p['name'] for p in places)}! 🗺️" if len(places) > 1
              else f"{dest['name']}, {dest['country']} sounds lovely! ✨")
        self.advance()

    def set_dates(self, start, end):
        n = (date.fromisoformat(end) - date.fromisoformat(start)).days
        if n < 0:
            self.o.say("Your return date is before your departure date. Could you pick them again?")
            return self.o.reprompt()
        self.t.update(start=start, end=end, nights=n, flexible=False)
        self.o.say("Perfect! A day trip it is. Let's keep planning!" if n == 0 else f"Perfect! I've got you down for {nights_text(n)}. Let's keep planning!")
        self.advance()

    def set_origin(self, name):
        t, o = self.t, self.o
        p = self.resolve(name)
        if not p:
            return o.reprompt()
        if t["destination"] and p["name"] == t["destination"]["name"]:
            o.say("That's where you're heading! Where will you be starting from?")
            return o.reprompt()
        t.update(origin=p, transport=None, options=[], route_confirmed=True if t["editing"] else t["route_confirmed"])
        o.say(f"Starting from {p['name']}, {p['country']}. Got it!")
        self.advance()

    def set_route(self, dest_name, stop_names):
        t, o = self.t, self.o
        dest = t["destination"] if t["destination"] and dest_name == t["destination"]["name"] else self.resolve(dest_name)
        if not dest:
            return o.reprompt()
        stops = []
        for s in stop_names:
            p = next((x for x in t["stops"] if x["name"] == s), None) or self.resolve(s)
            if not p:
                return o.reprompt()
            stops.append(p)
        if dest["name"] != t["destination"]["name"]:
            t.update(hotel=None, hotel_choice=None, attraction=None, hotels=[])
        t.update(destination=dest, stops=stops, route_confirmed=True, transport=None, options=[])
        o.say(f"Route locked in: {route_text(t)}. 🗺️")
        self.advance()

    def set_companions(self, value):
        t = self.t
        t.update(companions=value, travellers=1 if value == "solo" else 2 if value == "couple" else None, transport=None, options=[])
        if value == "solo":
            self.o.say("Solo adventures are the best kind of freedom. 🧭")
        elif value == "couple":
            self.o.say("A trip for two. How lovely! ❤️")
        self.advance()

    def set_travellers(self, n):
        if not 1 <= n <= 60:
            self.o.say("Could you give me a number between 1 and 60?")
            return self.o.reprompt()
        self.t.update(travellers=n, transport=None, options=[])
        self.o.say(f"{n} traveller{'s' if n != 1 else ''}. Noted! I'll use that for prices and emissions.")
        self.advance()

    def set_preferences(self, ids):
        if not ids:
            return self.o.reprompt()
        self.t.update(preferences=ids, transport=None, options=[])
        self.o.say("A planet-first traveller. My favourite kind! 🌱" if "carbon" in ids
                   else "A bit of everything. I'll find you a sensible all-rounder. ⚖️" if "balance" in ids
                   else "Got it. I'll keep that front of mind.")
        self.advance()

    # ----- summary & edits -----
    def summary(self, value):
        t, o = self.t, self.o
        if value == "ok":
            t.update(summary_confirmed=True, editing=False)
            if not t["transport"]:
                self.show_weather()
                o.say("Brilliant! Now let's find a way to get you there without leaving too big a footprint.")
            return self.advance()
        o.prompt("edit", "Sure! What would you like to change?", btns=buttons(EDIT_OPTS, "edit"))

    def edit(self, field):
        t, o = self.t, self.o
        if field == "transport":
            t["transport"] = None
            o.say("Sure, let's take another look at your options.")
            return self.advance()
        if field == "hotel":
            t.update(hotel=None, hotel_choice=None, attraction=None, hotels=[])
            return self.advance()
        if field == "cancel":
            t["summary_confirmed"] = bool(t["summary_confirmed"] and t["transport"])
            o.say("No changes made. Here's where we are.")
            return self.advance()
        clears = dict(
            destination=dict(destination=None, stops=[], transport=None, options=[], hotel=None, hotel_choice=None, attraction=None, hotels=[]),
            origin=dict(origin=None, transport=None, options=[]),
            dates=dict(start=None, end=None, nights=None, flexible=False, transport=None, options=[], hotels=[], hotel=None, hotel_choice=None),
            travellers=dict(companions=None, travellers=None, transport=None, options=[]),
            preferences=dict(preferences=[], transport=None, options=[]),
        )
        t.update(**clears[field], intent="planning", editing=True, summary_confirmed=False)
        self.advance()

    # ----- weather -----
    def show_weather(self, force=False):
        """Weather card for the destination and dates (Open-Meteo). Quietly skipped if unavailable."""
        t, o = self.t, self.o
        if not (t["destination"] and t["start"] and t["end"]):
            return False
        try:
            w = api.weather_outlook(t["destination"]["lat"], t["destination"]["lon"], t["start"], t["end"])
        except (ApiError, ValueError) as e:
            log.info("Weather unavailable: %s", e)
            if force:
                o.say("I couldn't get the weather just now. Please try again in a moment.")
            return False
        t["weather"] = w
        o.ui("weather", city=t["destination"]["name"], **w)
        return True

    def weather_question(self):
        t, o = self.t, self.o
        if not t["destination"]:
            o.say("Tell me where you're heading first, and I'll check the weather for your dates. 🌤️")
        elif not (t["start"] and t["end"]):
            o.say(f"Pick your travel dates and I'll check the weather in {t['destination']['name']} for them. 📅")
        else:
            self.show_weather(force=True)
        o.reprompt()

    # ----- transport -----
    def show_transport(self):
        t, o = self.t, self.o
        options, notices = build_transport_options(t)
        t["options"], t["notices"] = options, notices
        if not options:
            o.say(f"I couldn't find a practical way to estimate a journey from {t['origin']['name']} to {t['destination']['name']}. "
                  "Want to try a different starting point or destination?")
            return o.prompt("edit", None, btns=buttons([x for x in EDIT_OPTS if x[0] in ("origin", "destination", "cancel")], "edit"))
        ground = any(x["mode"] in ("rail", "coach") for x in options)
        who = f"{t['travellers']} traveller{'s' if t['travellers'] != 1 else ''}"
        if not ground:
            intro = (f"There's no practical train or coach route from {t['origin']['name']} to {t['destination']['name']}, "
                     "so I'll show you the flight estimate so you know what to expect.")
        else:
            intro = f"Here's how you can get from {t['origin']['name']} to {t['destination']['name']} for {who}, one way."
        if t["budget"]:
            intro += " I've flagged anything over your per-person budget."
        o.say(intro)
        o.prompt("transport", None, "transport_options", options=options, notices=notices,
                 headline=carbon_headline(options), route_link=rome2rio_link(t))

    def choose_transport(self, option_id):
        t, o = self.t, self.o
        opt = next((x for x in t["options"] if x["id"] == option_id), None)
        if not opt:
            o.say("That option has expired. Here are your options again.")
            return self.show_transport()
        t["transport"] = opt
        flight = next((x for x in t["options"] if x["mode"] == "flight"), None)
        lowest = min(t["options"], key=lambda x: x["kg_pp"])
        name = t["name"]
        if opt["mode"] == "flight" and lowest["id"] != opt["id"]:
            pct = round((1 - lowest["kg_pp"] / opt["kg_pp"]) * 100)
            o.say(f"Got it, {name}, the flight is the quickest way there. ✈️ It's estimated at {opt['kg_pp']:.0f} kg CO₂e per person. "
                  f"If plans change, the {lowest['title'].lower()} would cut that by about {pct}%.")
        elif flight and opt["kg_pp"] < flight["kg_pp"] * 0.9:
            o.say(f"Great call, {name}! 🌱 You've chosen the {opt['title'].lower()}. Based on our estimates, this option has a much "
                  "lower carbon footprint than flying for your journey.")
        else:
            o.say(f"Great choice, {name}! The {opt['title'].lower()} it is.")
        saving = (flight["kg_total"] - opt["kg_total"]) if flight and opt["mode"] != "flight" else None
        o.ui("selected_transport", option=opt, saving_vs_flight_kg=saving if saving and saving > 0 else None, travellers=t["travellers"])
        self.advance()

    # ----- hotels -----
    def hotel_ask(self, value):
        t, o = self.t, self.o
        if value != "yes":
            t["hotel_choice"] = "skipped"
            o.say("No problem! You can add a place to stay any time. 👍")
            return self.advance()
        dest = t["destination"]
        try:
            t["landmarks"] = api.nearby_landmarks(dest["lat"], dest["lon"], city_name=dest["name"])
        except ApiError as e:
            log.warning(e)
            t["landmarks"] = []
        o.prompt("attraction", "Do you have a particular area or attraction you'd like to stay close to?", "attraction_picker",
                 city=dest["name"], landmarks=[x["name"] for x in t["landmarks"]])

    def set_attraction(self, query):
        t, o = self.t, self.o
        dest = t["destination"]
        place = None
        if query:
            place = next((x for x in t["landmarks"] if x["name"].lower() == query.lower()), None)
            if not place:
                try:
                    place = api.find_landmark(query, dest["name"])
                except ApiError as e:
                    log.warning(e)
            if not place:
                o.say(f"I couldn't find “{query}” on the map, so I'll measure distances from the centre of {dest['name']}.")
        t["attraction"] = place or dict(name=f"{dest['name']} centre", lat=dest["lat"], lon=dest["lon"])
        self.show_hotels()

    def show_hotels(self):
        t, o = self.t, self.o
        a, n = t["attraction"], t["travellers"] or 1
        try:
            hotels = api.search_hotels(a["lat"], a["lon"], t["destination"]["country_code"], t["start"], t["end"], adults=n,
                                       currency=currency_for(t), guest_nationality=t["origin"]["country_code"] or "GB")
        except ApiError as e:
            log.warning(e)
            o.say("I can't search hotels right now (the hotel service isn't connected or didn't answer). "
                  "You can skip this step and add a stay later.")
            return o.prompt("hotel_ask", None, btns=[{"title": "⏭️ Skip for now", "payload": payload("hotel_ask", "skip")},
                                                     {"title": "🔄 Try again", "payload": payload("hotel_ask", "yes")}])
        if not hotels:
            o.say(f"I couldn't find hotels near {a['name']}. Want to try a different area?")
            return o.prompt("attraction", None, "attraction_picker", city=t["destination"]["name"], landmarks=[x["name"] for x in t["landmarks"]])
        rooms = max(1, math.ceil(n / 2))
        nights = t["nights"] or 1
        for h in hotels:
            h["distance_km"] = round(haversine_km(h, a), 1)
        try:
            t["stay_kg"] = api.climatiq_hotel(t["destination"], nights * rooms)
        except ApiError as e:
            log.warning(e)
            t["stay_kg"] = None
        t["hotels"] = hotels
        priced = any(h["price_total"] is not None for h in hotels)
        if priced and hotels[0].get("source") == "LiteAPI sandbox":
            note = " Prices come from the LiteAPI sandbox, so treat them as examples rather than bookable rates."
        elif priced:
            note = ""
        elif hotels[0].get("source") == "OpenStreetMap":
            note = " These are real places from OpenStreetMap. Prices aren't included, so use “Book / website” to check rates."
        else:
            note = " Live prices need exact dates, so pick dates to see rates."
        o.say(f"Here are places to stay near {a['name']}.{note}")
        o.prompt("hotels", None, "hotel_options", hotels=hotels, attraction=a["name"], nights=nights, rooms=rooms,
                 stay_kg=t["stay_kg"], flexible=t["flexible"])

    def choose_hotel(self, hotel_id):
        t, o = self.t, self.o
        h = next((x for x in t["hotels"] if str(x["id"]) == hotel_id), None)
        if not h:
            return o.reprompt()
        t.update(hotel=h, hotel_choice="selected")
        o.say(f"Lovely choice! {h['name']} is {h['distance_km']} km from {t['attraction']['name']}. 🛏️ Just so you know, this is a saved "
              "recommendation, not a booking. You'd confirm and pay with the hotel or a booking site.")
        self.advance()

    # ----- wrap-up -----
    def show_final(self):
        t = self.t
        opt = t["transport"]
        flight = next((x for x in t["options"] if x["mode"] == "flight"), None)
        round_trip = opt["kg_total"] * 2
        stay = t["stay_kg"] if t["hotel"] else None
        self.o.prompt("final", None, "final_summary",
                      btns=[{"title": "I'm happy with my plan!", "payload": payload("final", "happy")},
                            {"title": "Review my trip", "payload": payload("final", "review")},
                            {"title": "Explore more options", "payload": payload("final", "explore")}],
                      trip=self.view(), transport=opt, options=t["options"], hotel=t["hotel"], weather=t["weather"],
                      transport_round_trip_kg=round_trip, stay_kg=stay,
                      total_kg=round_trip + (stay or 0),
                      saving_vs_flight_kg=(flight["kg_total"] - opt["kg_total"]) * 2 if flight and opt["mode"] != "flight" else None)

    def final(self, value):
        t, o = self.t, self.o
        if value == "happy":
            t["completed"] = True
            o.say(f"That's everything sorted, {t['name']}! 🌍✈️")
            o.say("Enjoy your trip and safe travels! Thank you for making thoughtful choices for our planet.")
            return o.prompt("done", None, "success", btns=[{"title": "Plan another adventure", "payload": payload("restart", "yes")}],
                            trip=self.view(), transport=t["transport"], hotel=t["hotel"])
        if value == "review":
            t["summary_confirmed"] = False
            return self.advance()
        o.prompt("edit", "Of course! What would you like to look at again?", btns=buttons(EXPLORE_OPTS, "edit"))

    # ----- human handoff -----
    def handoff(self):
        t, o = self.t, self.o
        t["handoff"] = "requested"
        name = f", {t['name']}" if t["name"] else ""
        o.say(f"Of course{name}! 😊 Sometimes it's nice to have a real person help with the details. I'll connect you with one of our travel advisors.")
        o.say("Meet Alex Green, your travel advisor! Alex will be happy to help you with your travel plans.")
        o.prompt("handoff", None, "handoff", advisor="Alex Green", role="Sustainable Travel Advisor",
                 btns=[{"title": "Connect with Alex", "payload": payload("connect", "yes")}])

    def connect_advisor(self):
        t, o = self.t, self.o
        status = "not_configured"
        if HANDOFF_WEBHOOK_URL:
            transcript = [{"from": e["event"], "text": e.get("text")} for e in self.tracker.events if e.get("event") in ("user", "bot") and e.get("text")]
            try:
                r = requests.post(HANDOFF_WEBHOOK_URL, json={"trip": self.view(), "transport": t["transport"], "hotel": t["hotel"],
                                                             "transcript": transcript[-60:]}, timeout=15)
                status = "sent" if r.ok else "failed"
            except requests.RequestException:
                status = "failed"
        t["handoff"] = status
        o.ui("handoff_status", status=status, email=ADVISOR_EMAIL, trip=self.view(),
             transport=t["transport"]["title"] if t["transport"] else None, hotel=t["hotel"]["name"] if t["hotel"] else None)
        name = f", {t['name']}" if t["name"] else ""
        o.say(f"Thanks for chatting with me{name}! Wishing you a wonderful adventure. Until next time — keep travelling thoughtfully! 🌱👋")
        o.prompt("after_handoff", None, btns=[{"title": "🌍 Plan another adventure", "payload": payload("after_handoff", "again")},
                                              {"title": "💬 Keep planning with me", "payload": payload("after_handoff", "keep")}])

    # ----- restart -----
    def ask_restart(self):
        self.o.say("Start a fresh trip? Your current plan will be cleared.",
                   [{"title": "✨ Yes, start fresh", "payload": payload("restart", "yes")},
                    {"title": "↩️ Keep my trip", "payload": payload("restart", "no")}])

    def restart(self):
        name = self.t["name"]
        self.t.clear()
        self.t.update(new_trip(), name=name)
        self.welcome()

    # ----- free text -----
    def free_text(self, intent, text, city_ents):
        t, o = self.t, self.o
        aw = t["awaiting"]
        low = text.lower().strip()
        details = extract_details(text)
        cities = city_mentions(text, city_ents)

        if aw == "name":
            if (cities or details) and len(text.split()) > 3:
                parts = self.apply_details(cities, details)
                if parts:
                    o.say(f"{', '.join(parts)}. Love it! Before we dig in, what should I call you?")
                    return o.reprompt()
            return self.set_name(text)
        if intent == "affirm":
            if aw == "summary":
                return self.summary("ok")
            if aw == "final":
                return self.final("happy")
            if aw == "hotel_ask":
                return self.hotel_ask("yes")
            if aw == "intent":
                return self.set_intent("planning")
        if intent == "deny":
            if aw == "hotel_ask":
                return self.hotel_ask("skip")
            if aw == "summary":
                return self.summary("edit")
        if aw == "travellers":
            m = re.search(r"\d+", low)
            if m:
                return self.set_travellers(int(m.group()))
        if aw == "dates" and re.search(r"(not sure|flexible|don'?t know|undecided|no idea)", low):
            t.update(flexible=True)
            return self.advance()
        if aw == "origin" and (cities or len(text.split()) <= 3):
            return self.set_origin(cities[0][0] if cities else text)
        if aw in ("destination", "route") and not cities and not details and len(text.split()) <= 3:
            return self.set_destinations([text])
        if aw == "attraction" and not cities:
            return self.set_attraction("" if re.search(r"(anywhere|central|centre|center)", low) else text)
        if intent == "thanks":
            o.say(f"You're very welcome{', ' + t['name'] if t['name'] else ''}! 🌱")
            return o.reprompt()
        if intent == "goodbye":
            return o.say("Bye for now! Your trip is saved in this chat if you come back. 👋")

        # "change my dates" style edits
        if re.search(r"\b(change|edit|update|different|switch)\b", low) and not cities and not details and t["name"]:
            field = ("dates" if re.search(r"(date|when)", low) else "destination" if re.search(r"(destination|where|city)", low)
                     else "origin" if re.search(r"(origin|start|from|leaving)", low) else "travellers" if re.search(r"(people|travell|who)", low)
                     else "preferences" if re.search(r"(preference|priorit)", low) else "transport" if re.search(r"(transport|train|flight)", low)
                     else "hotel" if re.search(r"(hotel|stay|accommodation)", low) else None)
            if field:
                return self.edit(field)

        parts = self.apply_details(cities, details)
        if parts:
            o.say(f"Got it: {', '.join(parts)}.")
            return self.advance()
        o.say("Hmm, I'm not sure I followed that. You can use the options below, or try something like "
              "“change my dates”, “we're 4 people” or “talk to a human”.")
        o.reprompt()

    def apply_details(self, cities, details):
        """Update the trip from free text. Returns a list of what was understood."""
        t = self.t
        parts = []
        changed_route = changed_people = False
        for name, is_origin in cities:
            p = self.resolve(name)
            if not p:
                continue
            if is_origin:
                t["origin"] = p
                parts.append(f"starting from {p['name']}")
            else:
                if t["destination"] and p["name"] != t["destination"]["name"]:
                    t.update(hotel=None, hotel_choice=None, attraction=None, hotels=[])
                t.update(destination=p, stops=[])
                parts.append(p["name"])
            changed_route = True
        if changed_route and t["origin"] and t["destination"]:
            t["route_confirmed"] = True
        if details.get("start"):
            n = (date.fromisoformat(details["end"]) - date.fromisoformat(details["start"])).days
            t.update(start=details["start"], end=details["end"], nights=n, flexible=False)
            parts.append(f"{fmt_date(details['start'])} → {fmt_date(details['end'])} ({nights_text(n)})")
        elif details.get("nights") is not None:
            if t["start"]:
                t.update(end=(date.fromisoformat(t["start"]) + timedelta(days=details["nights"])).isoformat(), nights=details["nights"])
            else:
                t.update(flexible=True, nights=details["nights"])
            parts.append(nights_text(details["nights"]))
        if details.get("companions") and details["companions"] != t["companions"]:
            t["companions"] = details["companions"]
            t["travellers"] = details.get("travellers")
            parts.append({"solo": "travelling solo", "couple": "travelling as a couple"}.get(details["companions"], f"travelling with {details['companions']}"))
            changed_people = True
        if details.get("travellers") and details["travellers"] != t["travellers"]:
            t["travellers"] = details["travellers"]
            t["companions"] = t["companions"] or ("solo" if details["travellers"] == 1 else "couple" if details["travellers"] == 2 else "group")
            if details.get("companions") not in ("solo", "couple"):
                parts.append(f"{details['travellers']} travellers")
            changed_people = True
        if details.get("budget"):
            t["budget"] = details["budget"]
            parts.append(f"a budget of about {details['budget']} per person")
        if parts:
            if not t["intent"]:
                t["intent"] = "planning"
            if changed_route or changed_people or details.get("budget"):
                t.update(transport=None, options=[])
            if t["summary_confirmed"]:
                t["summary_confirmed"] = False
        return parts


# ---------------------------------------------------------------------------
# Transport options (real APIs, with transparent fallbacks)
# ---------------------------------------------------------------------------
def currency_for(t):
    cc = (t["origin"] or {}).get("country_code")
    return "GBP" if cc == "GB" else "USD" if cc == "US" else api.DEFAULT_CURRENCY


def ground_possible(a, b):
    return a["country_code"] == b["country_code"] or (a["country_code"] in LAND_EUROPE and b["country_code"] in LAND_EUROPE)


def rome2rio_link(t):
    slug = lambda p: p["name"].replace(" ", "-")
    return f"https://www.rome2rio.com/map/{slug(t['origin'])}/{slug(t['destination'])}"


def leg_emissions(mode, a, b, n):
    """
    (group kg CO2e, distance km, source) for one leg.
    Distances: straight line for flights (the Climatiq factor includes the flight-path uplift),
    straight line x uplift for rail and road. Emissions: Climatiq; built-in DESNZ factors if it is unavailable.
    """
    cars = math.ceil(n / CAR_SEATS)
    gc = haversine_km(a, b)
    km = gc if mode == "flight" else gc * (UPLIFT["rail"] if mode == "rail" else UPLIFT["road"])
    both_uk = a["country_code"] == b["country_code"] == "GB"
    try:
        if mode in ("rail", "coach"):
            kg, src = api.emissions_per_passenger(mode, km, in_uk=both_uk)
            return kg * n, km, f"Climatiq ({src})"
        if mode == "flight":
            kg, src = api.emissions_per_passenger("flight", km, domestic=a["country_code"] == b["country_code"])
            return kg * n, km * UPLIFT["air"], f"Climatiq ({src})"
        if mode in ("car", "ev"):
            kg, src = api.emissions_per_vehicle(mode == "ev", km, a["country_code"])
            return kg * cars, km, f"Climatiq ({src})"
    except ApiError as e:
        log.info("Climatiq unavailable, using fallback factors: %s", e)
    if mode == "flight":
        return gc * UPLIFT["air"] * (FALLBACK_FACTORS["air_short"] if gc < 3700 else FALLBACK_FACTORS["air_long"]) * n, gc * UPLIFT["air"], "DESNZ factors (fallback)"
    if mode == "rail":
        return km * FALLBACK_FACTORS["rail"] * n, km, "DESNZ factors (fallback)"
    if mode == "coach":
        return km * COACH_FACTOR * n, km, "DESNZ factors (fallback)"
    return km * FALLBACK_FACTORS[mode] * cars, km, "DESNZ factors (fallback)"


def build_transport_options(t):
    """Compare transport modes for the whole route. Returns (options, notices)."""
    points = [t["origin"], *t["stops"], t["destination"]]
    legs = list(zip(points, points[1:]))
    n = t["travellers"] or 1
    notices = []
    straight = [haversine_km(a, b) for a, b in legs]
    ground = all(ground_possible(a, b) for a, b in legs)
    in_europe = all(p["country_code"] in LAND_EUROPE for p in points)
    max_rail = 2000 if in_europe else 1000

    modes = []
    if ground:
        if sum(straight) * UPLIFT["rail"] <= max_rail:
            modes.append("rail")
        if sum(straight) * UPLIFT["road"] <= 1800:
            modes.append("coach")
        if sum(straight) * UPLIFT["road"] <= 1500:
            modes += ["ev", "car"]
    if not ground or min(straight) >= 250:
        modes.append("flight")

    options = []
    sources = set()
    for mode in modes:
        kg_total = km = 0.0
        for a, b in legs:
            k, d, src = leg_emissions(mode, a, b, n)
            kg_total += k
            km += d
            sources.add(src)
        emoji, short = MODE_META[mode]
        opt = dict(id=mode, mode=mode, emoji=emoji, title=short, operator="", depart=None, arrive=None,
                   duration_min=None, duration_estimated=True, door_to_door_min=None,
                   price_pp=None, price_total=None, currency=None, price_note="", transfers=None,
                   distance_km=round(km), kg_pp=kg_total / n, kg_total=kg_total, notes=[])
        if mode == "rail":
            opt.update(operator="Rail operators", duration_min=km / (140 if km > 300 else 100) * 60 + 30,
                       price_note="See live fares")
        elif mode == "coach":
            opt.update(operator="Long-distance coach operators", duration_min=km / 65 * 60 + (km // 400) * 30, price_note="See live fares")
        elif mode in ("car", "ev"):
            cars = math.ceil(n / CAR_SEATS)
            opt.update(operator="Your own or a hire car", duration_min=km / 85 * 60 + (km // 250) * (35 if mode == "ev" else 20),
                       price_note="Fuel/charging costs vary")
            opt["notes"].append(f"{cars} car{'s' if cars > 1 else ''}, up to {min(n, CAR_SEATS)} people each")
            if mode == "ev":
                opt["notes"].append("Emissions depend on the local electricity mix")
        elif mode == "flight":
            opt.update(operator="Airlines", duration_min=km / 780 * 60 + 40, price_note="Live fares unavailable")
            opt["door_to_door_min"] = opt["duration_min"] + 150
            add_live_flight(opt, t, legs, n, notices)
        options.append(opt)

    if not options:
        return [], notices

    # Badges, only where the estimates support the comparison
    flight = next((x for x in options if x["mode"] == "flight"), None)
    car = next((x for x in options if x["mode"] == "car"), None)
    min_kg = min(x["kg_pp"] for x in options)
    for x in options:
        x["lowest"] = x["kg_pp"] == min_kg and len(options) > 1
        ref = (flight, "flying") if flight and x is not flight else (car, "driving") if not flight and car and x is not car else None
        x["badge"] = None
        if ref and ref[0]["kg_pp"] > 0:
            pct = round((1 - x["kg_pp"] / ref[0]["kg_pp"]) * 100)
            if pct >= 10:
                x["badge"] = f"🌱 {pct}% less CO₂e than {ref[1]}"
        x["over_budget"] = bool(t["budget"] and x["price_pp"] and x["price_pp"] > t["budget"])

    rank_options(options, t["preferences"])
    notices.append("Emissions: " + ", ".join(sorted(sources)) + ". All figures are estimates for one-way travel.")
    if any(x["duration_estimated"] for x in options):
        notices.append("Train, coach and car times are estimated from distance. Check live timetables before booking.")
    return options, notices


def add_live_flight(opt, t, legs, n, notices):
    """Fill a flight option with the cheapest live Duffel offer, when possible."""
    if len(legs) > 1:
        notices.append("Live fares are only fetched for single-leg trips.")
        return
    if not t["start"]:
        notices.append("Pick exact dates to see live flight fares.")
        return
    a, b = legs[0]
    try:
        oc = api.airport_code(a["name"], a["country_code"], a["lat"], a["lon"])
        dc = api.airport_code(b["name"], b["country_code"], b["lat"], b["lon"])
        if not (oc and dc):
            notices.append("No airport found for this route.")
            return
        offers = api.search_flights(oc, dc, t["start"], n)
    except ApiError as e:
        log.info("Duffel unavailable: %s", e)
        notices.append("Live flight fares aren't connected (Duffel), so only emissions are shown for flights.")
        return
    if not offers:
        notices.append(f"No flights found from {oc} to {dc} on {fmt_date(t['start'])}.")
        return
    best = offers[0]
    opt.update(title="Direct flight" if best["stops"] == 0 else f"Flight · {best['stops']} stop{'s' if best['stops'] > 1 else ''}",
               operator=f"{best['airline']} · {oc} → {dc}", depart=best["depart"], arrive=best["arrive"],
               duration_min=best["duration_min"] or opt["duration_min"], duration_estimated=False,
               door_to_door_min=(best["duration_min"] or opt["duration_min"]) + 150,
               price_total=best["price_total"], price_pp=best["price_total"] / n, currency=best["currency"],
               price_note="Test fare (Duffel sandbox)" if api.duffel_is_test() else "Live fare (Duffel)",
               transfers=best["stops"], logo=best.get("logo"))
    if best["emissions_kg"]:
        opt["notes"].append(f"Airline data estimate for this itinerary: {best['emissions_kg']:.0f} kg CO₂e")
    if api.duffel_is_test():
        notices.append("Flight fares come from Duffel TEST mode (sandbox airline 'Duffel Airways'), not real fares. Use a live token for real prices.")


def rank_options(options, prefs):
    prefs = prefs or ["balance"]
    comfort = {"rail": 5, "flight": 3, "car": 3, "ev": 3, "coach": 2}
    scenic = {"rail": 5, "car": 3, "ev": 3, "coach": 3, "flight": 1}
    w = dict(co2=0.0, price=0.0, time=0.0, comfort=0.0, scenic=0.0)
    for p, k in [("carbon", "co2"), ("budget", "price"), ("fast", "time"), ("comfort", "comfort"), ("scenic", "scenic")]:
        if p in prefs:
            w[k] += 1
    if "balance" in prefs:
        w.update(co2=w["co2"] + 0.8, price=w["price"] + 0.4, time=w["time"] + 0.6, comfort=w["comfort"] + 0.4, scenic=w["scenic"] + 0.2)

    def norm(key):
        vals = [key(o) for o in options if key(o) is not None]
        lo, hi = (min(vals), max(vals)) if vals else (0, 0)
        return lambda o: 0.5 if key(o) is None or hi == lo else (key(o) - lo) / (hi - lo)

    n_co2 = norm(lambda o: o["kg_pp"])
    n_price = norm(lambda o: o["price_pp"])
    n_time = norm(lambda o: o["door_to_door_min"] or o["duration_min"])

    def score(o):
        return (w["co2"] * (1 - n_co2(o)) + w["price"] * (1 - n_price(o)) + w["time"] * (1 - n_time(o))
                + w["comfort"] * comfort[o["mode"]] / 5 + w["scenic"] * scenic[o["mode"]] / 5 - (2 if o["over_budget"] else 0))

    options.sort(key=score, reverse=True)
    for i, o in enumerate(options):
        o["best_match"] = i == 0 and len(options) > 1


def carbon_headline(options):
    flight = next((o for o in options if o["mode"] == "flight"), None)
    low = min(options, key=lambda o: o["kg_pp"])
    if len(options) == 1 and flight:
        return "Flying is the only practical option for this route. Flying economy and direct keeps the footprint as low as it can be."
    if flight and low is not flight:
        pct = round((1 - low["kg_pp"] / flight["kg_pp"]) * 100)
        if pct >= 10:
            return f"Good news! The {low['title'].lower()} is estimated to produce about {pct}% less CO₂e per person than flying on this route."
        return "On this route the options are fairly close on emissions, so pick what suits you best."
    if not flight:
        return f"The {low['title'].lower()} is the lowest-emission option here. Flying doesn't make sense for a trip this short, so I've left it out."
    return "Here's how your options compare."


# ---------------------------------------------------------------------------
# The Rasa action
# ---------------------------------------------------------------------------
class ActionTripPlanner(Action):
    def name(self) -> Text:
        return "action_trip_planner"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: Dict[Text, Any]) -> List[Dict[Text, Any]]:
        trip = copy.deepcopy(tracker.get_slot("trip")) or new_trip()
        msg = tracker.latest_message or {}
        intent = (msg.get("intent") or {}).get("name") or ""
        ents, city_ents = {}, []
        for e in msg.get("entities", []):
            if e.get("entity") == "city":
                city_ents.append(e)
            else:
                ents[e.get("entity")] = e.get("value")
        text = msg.get("text") or ""
        out = Out(dispatcher, trip)
        try:
            Planner(trip, out, tracker).handle(intent, ents, city_ents, text)
        except Exception:
            log.exception("Trip planner failed")
            dispatcher.utter_message(text="Sorry, something went wrong on my side. Please try again, or type “talk to a human”.")
        return [SlotSet("trip", trip)]