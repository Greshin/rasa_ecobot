import json
import os
import uuid
from datetime import date, timedelta

import requests
import streamlit as st
from PIL import Image, ImageDraw

RASA_URL = os.getenv("RASA_URL", "http://localhost:5005")
WEBHOOK = f"{RASA_URL}/webhooks/rest/webhook"

st.set_page_config(page_title="Eco Travel Assistant", page_icon="🌿", layout="centered")
S = st.session_state


# =============================================================================
# Talking to Rasa
# =============================================================================
def payload(cmd, value=""):
    """Button/widget answers go to Rasa as /inform{"cmd": ..., "value": ...}."""
    return "/inform" + json.dumps({"cmd": cmd, "value": str(value)})


def send(message, shown=None):
    """Send a message to Rasa and store the reply. `shown` is what appears in the user's bubble."""
    if shown:
        S.history.append({"role": "user", "text": shown, "parse": parse_debug(message)})
    try:
        r = requests.post(WEBHOOK, json={"sender": S.sender, "message": message}, timeout=120)
        r.raise_for_status()
        S.history.append({"role": "bot", "items": r.json()})
        S.error = None
    except requests.RequestException as e:
        S.error = f"I couldn't reach the Rasa server at {RASA_URL} ({e.__class__.__name__})."


def parse_debug(message):
    """When the sidebar toggle is on, ask Rasa's NLU what it understood (needs --enable-api)."""
    if not S.get("show_intent") or message.startswith("/"):
        return None
    try:
        j = requests.post(f"{RASA_URL}/model/parse", json={"text": message}, timeout=10).json()
        ents = ", ".join(f"{e['entity']}={e['value']}" for e in j.get("entities", []))
        return f"🧠 intent: {j['intent']['name']} ({j['intent']['confidence']:.2f})" + (f" · {ents}" if ents else "")
    except Exception:
        return None


def rasa_status():
    try:
        return requests.get(RASA_URL, timeout=2).ok
    except requests.RequestException:
        return False


# =============================================================================
# Small helpers
# =============================================================================
def money(amount, currency):
    if amount is None:
        return None
    symbol = {"GBP": "£", "EUR": "€", "USD": "\\$"}.get(currency, f"{currency} ")
    return f"{symbol}{amount:,.0f}"


def kg(n):
    if n is None:
        return "—"
    if n >= 1000:
        return f"{n / 1000:.2f} t"
    return f"{n:.1f} kg" if n < 10 else f"{n:.0f} kg"


def dur(minutes):
    if not minutes:
        return "—"
    h, m = divmod(round(minutes), 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def md(text):
    return str(text).replace("$", "\\$")


def fmt_date(d):
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"


@st.cache_resource
def bot_avatar(size=96):
    """A small green robot with a leaf, drawn with Pillow (no image files needed)."""
    k = 8
    w = 64 * k
    img = Image.new("RGBA", (w, w), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def p(*v):
        return [x * k for x in v]

    d.ellipse(p(0, 0, 64, 64), fill="#245A36")
    d.line(p(32, 19, 32, 11), fill="#CFE6C8", width=3 * k)
    d.polygon([tuple(p(x, y)) for x, y in [(32, 11), (35, 5.5), (40, 3), (46, 3.2), (44, 8.5), (39, 12), (34, 12.2)]], fill="#8FD19E")
    d.rounded_rectangle(p(14, 18, 50, 46), radius=11 * k, fill="#F7F6F1")
    d.rounded_rectangle(p(19, 24, 45, 37), radius=int(6.5 * k), fill="#1B3B26")
    d.ellipse(p(24.3, 27.8, 29.7, 33.2), fill="#9BF0B4")
    d.ellipse(p(34.3, 27.8, 39.7, 33.2), fill="#9BF0B4")
    d.arc(p(28, 37.5, 36, 43.5), start=20, end=160, fill="#245A36", width=2 * k)
    d.rounded_rectangle(p(10, 27, 14.5, 36), radius=2 * k, fill="#8FD19E")
    d.rounded_rectangle(p(49.5, 27, 54, 36), radius=2 * k, fill="#8FD19E")
    d.rounded_rectangle(p(14, 50, 50, 72), radius=6 * k, fill="#F7F6F1")
    d.ellipse(p(28.8, 53.8, 35.2, 60.2), fill="#8FD19E")
    mask = Image.new("L", (w, w), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, w - 1, w - 1), fill=255)
    img.putalpha(mask)
    return img.resize((size, size), getattr(Image, "Resampling", Image).LANCZOS)


# =============================================================================
# Widgets for each custom payload type
# =============================================================================
def button_row(buttons, key, per_row=3, primary_first=False):
    """Rasa buttons → Streamlit buttons. Clicking sends the payload."""
    for i in range(0, len(buttons), per_row):
        cols = st.columns(per_row)
        for j, (col, b) in enumerate(zip(cols, buttons[i:i + per_row])):
            col.button(b["title"], key=f"{key}_b{i + j}", on_click=send, args=(b["payload"], b["title"]),
                       type="primary" if primary_first and i + j == 0 else "secondary", use_container_width=True)


def w_name_input(c, key):
    def go():
        v = S.get(f"{key}_v", "").strip()
        if v:
            send(payload("name", v), v)

    with st.form(f"{key}_form"):
        st.text_input("Your name", placeholder="Enter your name...", key=f"{key}_v")
        st.form_submit_button("Let's go!", type="primary", on_click=go)


def w_city_input(c, key):
    field, multi = c.get("field", "destination"), c.get("multi", False)
    lst = f"{key}_list"
    S.setdefault(lst, [])

    def add(city):
        if city not in S[lst]:
            S[lst] = S[lst] + [city]

    if c.get("suggestions"):
        cols = st.columns(3)
        for i, city in enumerate(c["suggestions"]):
            cols[i % 3].button(("✓ " if city in S[lst] else "") + city, key=f"{key}_s{i}", on_click=add, args=(city,), use_container_width=True)
    if multi and S[lst]:
        st.caption("Your stops, in order: " + " → ".join(S[lst]))
    st.text_input("City", key=f"{key}_v", placeholder="Type a city…" if field == "destination" else "e.g. London",
                  label_visibility="collapsed")

    def go():
        typed = [x.strip() for x in S.get(f"{key}_v", "").split(",") if x.strip()]
        names = (S[lst] + typed) if multi else (typed[:1] or S[lst][:1])
        if names:
            send(payload(field, "|".join(names)), " → ".join(names))

    st.button("Continue" if field == "destination" else "Next", key=f"{key}_go", type="primary", on_click=go)
    if multi:
        st.caption("Pick more than one (or separate with commas) to plan a multi-stop trip.")


def w_date_range(c, key):
    today = date.today()
    val = st.date_input("Departure and return dates", value=(today + timedelta(days=7), today + timedelta(days=10)),
                        min_value=today, key=f"{key}_v", format="DD/MM/YYYY")
    ok = isinstance(val, (list, tuple)) and len(val) == 2
    st.caption(f"{fmt_date(val[0])} → {fmt_date(val[1])} · {(val[1] - val[0]).days} nights" if ok else "Now pick your return date.")
    c1, c2 = st.columns(2)
    if ok:
        c1.button("Confirm dates", key=f"{key}_ok", type="primary", use_container_width=True, on_click=send,
                  args=(payload("dates", f"{val[0].isoformat()}|{val[1].isoformat()}"), f"{fmt_date(val[0])} → {fmt_date(val[1])}"))
    c2.button("I'm not sure yet", key=f"{key}_flex", use_container_width=True, on_click=send, args=(payload("flexible", "yes"), "I'm not sure yet"))


def w_route_editor(c, key):
    st.markdown(f"🏁 **From:** {c['origin']}")
    st.text_input("Stops along the way (optional, separate with commas)", value=", ".join(c.get("stops", [])), key=f"{key}_stops")
    st.text_input("📍 Destination", value=c["destination"], key=f"{key}_dest")

    def go():
        dest = S[f"{key}_dest"].strip()
        stops = [x.strip() for x in S[f"{key}_stops"].split(",") if x.strip()]
        if dest:
            send(payload("route", f"{dest}|{';'.join(stops)}"), " → ".join([c["origin"], *stops, dest]))

    st.button("Confirm route", key=f"{key}_go", type="primary", on_click=go)


def w_number_picker(c, key):
    cols = st.columns(6)
    for i, n in enumerate([1, 2, 3, 4, 5]):
        cols[i].button(str(n), key=f"{key}_{n}", use_container_width=True, on_click=send, args=(payload("travellers", n), str(n)))
    if cols[5].button("6+", key=f"{key}_more", use_container_width=True):
        S[f"{key}_custom"] = True
    if S.get(f"{key}_custom"):
        n = st.number_input("How many travellers?", min_value=6, max_value=60, value=6, key=f"{key}_n")
        st.button("Confirm", key=f"{key}_ok", type="primary", on_click=send, args=(payload("travellers", int(n)), str(int(n))))


def w_multi_select(c, key):
    sel_key = f"{key}_sel"
    S.setdefault(sel_key, [])
    sel = S[sel_key]

    def toggle(oid):
        cur = S[sel_key]
        if oid == "balance":
            S[sel_key] = [] if "balance" in cur else ["balance"]
        else:
            S[sel_key] = [x for x in cur if x != oid] if oid in cur else [x for x in cur if x != "balance"] + [oid]

    opts = c["options"]
    for i in range(0, len(opts), 3):
        cols = st.columns(3)
        for col, o in zip(cols, opts[i:i + 3]):
            col.button(("✓ " if o["id"] in sel else "") + o["label"], key=f"{key}_{o['id']}", on_click=toggle, args=(o["id"],),
                       type="primary" if o["id"] in sel else "secondary", use_container_width=True)
    labels = ", ".join(o["label"] for o in opts if o["id"] in sel)
    st.button(f"Done ({len(sel)})" if sel else "Done", key=f"{key}_done", disabled=not sel,
              on_click=send, args=(payload(c.get("cmd", "preferences"), ",".join(sel)), labels))


def w_trip_summary(c, key, active):
    t = c["trip"]
    with st.container(border=True):
        st.markdown("**Here's your adventure so far! 🌍**")
        nights = "—" if t["nights"] is None else f"{'~' if t['flexible'] else ''}{t['nights']}"
        rows = [("Traveller", t["name"]), ("From", t["origin"]), ("To", " → ".join([*t["stops"], t["destination"]])),
                ("Dates", t["dates"]), ("Nights", nights), ("Travellers", f"{t['travellers']} · {t['companions'] or ''}"),
                ("Preferences", ", ".join(t["preferences"]))]
        if t.get("budget"):
            rows.append(("Budget", f"{t['budget']} per person"))
        st.markdown("  \n".join(f"**{k}:** {md(v)}" for k, v in rows))
        if active:
            button_row(c["buttons"], key, per_row=2, primary_first=True)


def carbon_chart(options, selected_id=None):
    top = max(o["kg_pp"] for o in options) or 1
    for o in sorted(options, key=lambda x: x["kg_pp"]):
        mark = "  ← your pick" if o["id"] == selected_id else ""
        st.progress(max(0.02, o["kg_pp"] / top), text=f"{o['emoji']} {o['title']}: {kg(o['kg_pp'])}{mark}")
    st.caption("Estimated kg CO₂e per person, one way.")


def transport_card(o, key, active, chosen):
    with st.container(border=True):
        if o.get("logo"):
            st.image(o["logo"], width=28)
        st.markdown(f"**{o['emoji']} {o['title']}**")
        st.caption(o["operator"])
        tags = []
        if o.get("best_match"):
            tags.append(":green[**★ Best match**]")
        if o.get("lowest"):
            tags.append(":green[**Lowest CO₂e**]")
        if o.get("over_budget"):
            tags.append(":orange[**Over your budget**]")
        if tags:
            st.markdown(" · ".join(tags))
        est = " (est.)" if o["duration_estimated"] else ""
        if o.get("depart"):
            stops = "Direct" if not o.get("transfers") else f"{o['transfers']} stop{'s' if o['transfers'] > 1 else ''}"
            st.markdown(f"**{o['depart']}** → **{o['arrive']}**  \n{dur(o['duration_min'])} · {stops}")
        else:
            st.markdown(f"⏱️ {dur(o['duration_min'])}{est} · {o['distance_km']:,} km")
        if o.get("door_to_door_min"):
            st.caption(f"≈ {dur(o['door_to_door_min'])} door to door")
        if o.get("price_total") is not None:
            st.markdown(f"💷 **{money(o['price_pp'], o['currency'])}** per person · {money(o['price_total'], o['currency'])} group")
        else:
            st.markdown(f"💷 _{o['price_note']}_")
        st.caption(o["price_note"] if o.get("price_total") is not None else "")
        st.markdown(f"🌍 **{kg(o['kg_pp'])} CO₂e** per person · {kg(o['kg_total'])} group")
        if o.get("badge"):
            st.markdown(f":green[**{o['badge']}**]")
        for n in o.get("notes", [])[:2]:
            st.caption(n)
        if active:
            st.button("Select", key=f"{key}_{o['id']}", type="primary", use_container_width=True,
                      on_click=send, args=(payload("transport", o["id"]), f"I'll take the {o['title'].lower()} {o['emoji']}"))
        elif chosen:
            st.markdown(":green[**✓ Selected**]")


def w_transport_options(c, key, active, chosen=None):
    options = c["options"]
    for i in range(0, len(options), 3):
        cols = st.columns(3)
        for col, o in zip(cols, options[i:i + 3]):
            with col:
                transport_card(o, key, active, chosen == o["id"])
    with st.container(border=True):
        st.markdown(c["headline"])
        carbon_chart(options, chosen)
        with st.expander("How are these estimated?"):
            st.markdown("**Emissions = distance × emission factor × travellers.** Cars are counted per vehicle and shared "
                        "by up to 5 people. Flights include radiative forcing.")
            for n in c.get("notices", []):
                st.markdown(f"- {n}")
        st.link_button("Compare live times & prices on Rome2Rio ↗", c["route_link"])


def w_weather(c, key):
    days = c["days"]
    with st.container(border=True):
        title = "Weather forecast" if c["kind"] == "forecast" else "Weather on these dates last year"
        st.markdown(f"**🌤️ {title} · {c['city']}** ({c['summary']})")
        cols = st.columns(len(days))
        for col, d in zip(cols, days):
            day = date.fromisoformat(d["date"])
            rain = (f"💧 {d['rain']:.0f}%" if c["kind"] == "forecast" else f"💧 {d['rain']:.1f} mm") if d["rain"] is not None else ""
            col.markdown(f"**{day.strftime('%a')}**  \n{d['emoji']}  \n{d['t_max']:.0f}° / {d['t_min']:.0f}°  \n{rain}")
        st.caption(c["tip"])
        if c["kind"] == "forecast":
            st.caption("Forecast from Open-Meteo. It can change, so check again before you travel.")
        else:
            st.caption("Your dates are more than 2 weeks away, so this is last year's weather (Open-Meteo archive), not a forecast.")


def w_selected_transport(c, key):
    o = c["option"]
    with st.container(border=True):
        st.caption("YOUR SELECTED TRANSPORT")
        lines = [f"**Mode:** {o['emoji']} {o['title']}",
                 f"**Estimated emissions:** {kg(o['kg_total'])} CO₂e" + (f" ({kg(o['kg_pp'])} per person)" if c["travellers"] > 1 else "")]
        if c.get("saving_vs_flight_kg"):
            lines.append(f"**Estimated saving vs flight:** :green[**{kg(c['saving_vs_flight_kg'])} CO₂e**]")
        st.markdown("  \n".join(lines))
        st.caption("One-way estimate for your group.")


def w_attraction_picker(c, key):
    marks = c.get("landmarks", [])
    opts = [(m, f"📍 {m}") for m in marks[:8]] + [("", "🏙️ Anywhere central")]
    for i in range(0, len(opts), 3):
        cols = st.columns(3)
        for col, (value, label) in zip(cols, opts[i:i + 3]):
            col.button(label, key=f"{key}_{i}_{value}", use_container_width=True, on_click=send, args=(payload("attraction", value), label))
    if marks:
        st.caption("Landmarks near the centre, from Wikipedia.")

    def go():
        v = S.get(f"{key}_v", "").strip()
        if v:
            send(payload("attraction", v), v)

    with st.form(f"{key}_form"):
        st.text_input("Or type a landmark", key=f"{key}_v", placeholder="e.g. the old town")
        st.form_submit_button("Search", on_click=go)


def hotel_card(h, c, key, active, chosen):
    with st.container(border=True):
        if h.get("photo"):
            st.image(h["photo"])
        stars = " · " + "★" * int(h["stars"]) if h.get("stars") else ""
        rating = f" · ⭐ {h['rating']}/10" if h.get("rating") else ""
        reviews = f" ({h['reviews']:,} reviews)" if h.get("reviews") else ""
        st.markdown(f"**{h['name']}**  \n{h.get('type', 'Hotel')}{stars}{rating}{reviews}")
        if h.get("address"):
            st.caption(h["address"])
        st.markdown(f"📍 {h['distance_km']} km from {c['attraction']}")
        if h.get("wheelchair"):
            st.caption("♿ Wheelchair accessible")
        if h.get("price_total") is not None:
            per_night = h["price_total"] / max(1, c["nights"])
            st.markdown(f"**{money(per_night, h['currency'])}** / night · {money(h['price_total'], h['currency'])} total")
            details = [x for x in [h.get("room_name"), h.get("board")] if x]
            if h.get("refundable") is True:
                details.append("free cancellation")
            elif h.get("refundable") is False:
                details.append("non-refundable")
            if details:
                st.caption(" · ".join(details))
            label = "sandbox example rate" if h.get("source") == "LiteAPI sandbox" else "live rate"
            st.caption(f"{c['nights']} night{'s' if c['nights'] != 1 else ''} · {c['rooms']} room{'s' if c['rooms'] != 1 else ''} · {label}")
        elif h.get("source") == "OpenStreetMap":
            st.caption("💷 Price: check on the booking site")
        else:
            st.caption("No rate for these dates")
        if h.get("description"):
            with st.expander("About this place"):
                st.write(h["description"])
        if active:
            st.button("Select", key=f"{key}_{h['id']}", type="primary", use_container_width=True,
                      on_click=send, args=(payload("hotel", h["id"]), f"{h['name']}, please!"))
            if h.get("booking_url"):
                st.link_button("Book / website ↗", h["booking_url"], use_container_width=True)
            st.link_button("View on map ↗", f"https://www.google.com/maps/search/?api=1&query={h['lat']},{h['lon']}", use_container_width=True)
        elif chosen:
            st.markdown(":green[**✓ Selected**]")


def w_hotel_options(c, key, active, chosen=None):
    hotels = list(c["hotels"])
    if c.get("stay_kg"):
        st.caption(f"🌍 Estimated emissions for this stay: {kg(c['stay_kg'])} CO₂e (Climatiq, country average for hotel nights).")
    st.caption("Sustainability certifications aren't included in this hotel data, so no eco badges are shown.")
    if active:
        sort = st.radio("Sort by", ["Price", "Distance", "Rating", "Comfort"], horizontal=True, key=f"{key}_sort")
        keyf = {"Price": lambda h: h["price_total"] if h["price_total"] is not None else 1e9, "Distance": lambda h: h["distance_km"],
                "Rating": lambda h: -(h["rating"] or 0), "Comfort": lambda h: -(h["stars"] or 0)}[sort]
        hotels.sort(key=keyf)
    else:
        hotels = [h for h in hotels if str(h["id"]) == str(chosen)] or hotels[:3]
    for i in range(0, len(hotels), 3):
        cols = st.columns(3)
        for col, h in zip(cols, hotels[i:i + 3]):
            with col:
                hotel_card(h, c, key, active, str(h["id"]) == str(chosen))


def w_final_summary(c, key, active):
    t, o, h = c["trip"], c["transport"], c.get("hotel")
    with st.container(border=True):
        st.markdown("### Your planet-friendly adventure is taking shape! 🌍")
        st.markdown("**Your trip**")
        stay = f"{h['name']} (selected, not booked)" if h else "Not chosen"
        price = f" · {money(o['price_total'], o['currency'])}" if o.get("price_total") is not None else ""
        st.markdown(md(f"📍 {t['route']}  \n📅 {t['dates']}  \n👥 {t['travellers']} traveller{'s' if t['travellers'] != 1 else ''}  \n"
                       f"{o['emoji']} {o['title']}{price}  \n🏨 {stay}"))
        if c.get("weather"):
            w = c["weather"]
            label = "forecast" if w["kind"] == "forecast" else "last year on these dates"
            st.caption(f"🌤️ Weather ({label}): {w['summary']}. {w['tip']}")
        st.markdown("**Your environmental impact**")
        c1, c2, c3 = st.columns(3)
        c1.metric("Per person, one way", kg(o["kg_pp"]))
        c2.metric("Transport, round trip", kg(c["transport_round_trip_kg"]))
        c3.metric("Saved vs flying", kg(c["saving_vs_flight_kg"]) if c.get("saving_vs_flight_kg") and c["saving_vs_flight_kg"] > 0 else "—")
        stay_line = f"Stay: {kg(c['stay_kg'])} CO₂e (Climatiq). " if c.get("stay_kg") else "Stay: not included (no reliable data). "
        st.caption(f"{stay_line}Estimated trip footprint: **{kg(c['total_kg'])} CO₂e** for your group.")
        carbon_chart(c["options"], o["id"])
        st.success("Every thoughtful travel choice makes a difference. You've taken a step towards a more conscious adventure! 🌱")
        if active:
            button_row(c["buttons"], key, per_row=3, primary_first=True)


def trip_text(c):
    t = c["trip"]
    return "\n".join([f"Trip for {t['name']}", f"Route: {t['route']}", f"Dates: {t['dates']}", f"Travellers: {t['travellers']}",
                      f"Preferences: {', '.join(t['preferences'])}", f"Transport: {c.get('transport') or 'not chosen'}",
                      f"Stay: {c.get('hotel') or 'not chosen'}"])


def w_handoff(c, key, active):
    with st.container(border=True):
        st.markdown(f"### 🧑🏽‍💼 {c['advisor']}")
        st.caption(c["role"])
        if active:
            button_row(c["buttons"], key, per_row=1, primary_first=True)


def w_handoff_status(c, key):
    status = c["status"]
    if status == "sent":
        st.success("Your trip details were sent to our advisors. Alex will get back to you.")
    elif status == "failed":
        st.warning("I couldn't reach the advisor service just now, so Alex hasn't been contacted.")
    else:
        st.warning("Your request is noted. Live advisor chat isn't connected yet, so Alex hasn't been contacted.")
    if c.get("email"):
        st.markdown(f"You can reach the team at **{c['email']}**.")
    if status != "sent":
        st.caption("Copy your trip details to share with an advisor:")
        st.code(trip_text(c), language=None)


def w_success(c, key, active):
    t, o, h = c["trip"], c.get("transport"), c.get("hotel")
    with st.container(border=True):
        c1, c2 = st.columns([1, 3])
        c1.image(bot_avatar(160), width=110)
        c2.markdown(f"### 👋 Bon voyage, {t['name']}!")
        lines = [f"📍 {t['route']}", f"📅 {t['dates']}"]
        if o:
            lines.append(f"{o['emoji']} {o['title']} · ~{kg(o['kg_total'] * 2)} CO₂e round trip")
        lines.append(f"🏨 {h['name']} (not booked yet)" if h else "🏨 Stay to be decided")
        c2.markdown(md("  \n".join(lines)))
        if active:
            button_row(c["buttons"], key, per_row=1, primary_first=True)


def w_ideas(c, key, active):
    ideas = c["ideas"]
    for i in range(0, len(ideas), 2):
        cols = st.columns(2)
        for col, idea in zip(cols, ideas[i:i + 2]):
            with col, st.container(border=True):
                st.markdown(f"### {idea['city']}")
                st.markdown(idea["why"])
                st.markdown(f":green[**{idea['tag']}**]")
                if active:
                    st.button("Plan this trip", key=f"{key}_{idea['city']}", type="primary", use_container_width=True,
                              on_click=send, args=(payload("plan_idea", idea["city"]), f"Let's plan {idea['city']}!"))
    if active:
        c1, c2 = st.columns(2)
        c1.button("🔄 Show different ideas", key=f"{key}_more", use_container_width=True,
                  on_click=send, args=(payload("more_ideas", c.get("vibe", "")), "Show me different ideas"))
        c2.button("✏️ I have somewhere in mind", key=f"{key}_own", use_container_width=True,
                  on_click=send, args=(payload("trip_intent", "planning"), "I have somewhere in mind"))


INPUTS = {"name_input": w_name_input, "city_input": w_city_input, "date_range": w_date_range, "route_editor": w_route_editor,
          "number_picker": w_number_picker, "multi_select": w_multi_select, "attraction_picker": w_attraction_picker}


def render_custom(c, key, active, index):
    kind = c.get("type")
    if kind in INPUTS:
        if active:
            INPUTS[kind](c, key)
        return
    if kind == "trip_summary":
        return w_trip_summary(c, key, active)
    if kind == "transport_options":
        return w_transport_options(c, key, active, S.picks.get(f"transport{index}"))
    if kind == "selected_transport":
        return w_selected_transport(c, key)
    if kind == "weather":
        return w_weather(c, key)
    if kind == "hotel_options":
        return w_hotel_options(c, key, active, S.picks.get(f"hotel{index}"))
    if kind == "final_summary":
        return w_final_summary(c, key, active)
    if kind == "handoff":
        return w_handoff(c, key, active)
    if kind == "handoff_status":
        return w_handoff_status(c, key)
    if kind == "success":
        return w_success(c, key, active)
    if kind == "ideas":
        return w_ideas(c, key, active)
    st.json(c)  # unknown type: show the raw data rather than hiding it


# =============================================================================
# Page
# =============================================================================
if "sender" not in S:
    S.sender = f"web-{uuid.uuid4().hex[:10]}"
    S.history = []
    S.picks = {}
    S.error = None
    send("/greet")

AVATAR = bot_avatar(96)

with st.sidebar:
    st.image(bot_avatar(160), width=72)
    st.markdown("**Eco Travel Assistant**")
    st.caption("Your planet-friendly travel companion")
    st.button("🙋 Talk to a human", use_container_width=True, on_click=send, args=("/request_human", "I'd like to speak to a real person."))
    st.button("🔄 New trip", use_container_width=True, on_click=send, args=("/start_over", "New trip"))
    st.divider()
    online = rasa_status()
    st.markdown(f"**Rasa server:** {'🟢 connected' if online else '🔴 not reachable'}")
    st.caption(RASA_URL)
    st.toggle("Show detected intent", key="show_intent", help="Shows what the NLU model understood from each typed message.")
    st.toggle("Show raw Rasa replies", key="show_raw")

h1, h2 = st.columns([1, 6])
h1.image(AVATAR, width=64)
h2.markdown("## Eco Travel Assistant")
h2.caption("Your planet-friendly travel companion")

if S.error:
    st.error(S.error + "\n\nStart it in two terminals:\n\n`rasa run actions`\n\n`rasa run --enable-api --cors \"*\"`")
    if st.button("Try again"):
        S.error = None
        if not S.history:
            send("/greet")
        st.rerun()

# Remember which option was picked in reply to each card, so old cards show "✓ Selected".
for i, entry in enumerate(S.history):
    if entry["role"] != "bot":
        continue
    nxt = S.history[i + 1] if i + 1 < len(S.history) else None
    for item in entry["items"]:
        c = item.get("custom") or {}
        if nxt and c.get("type") in ("transport_options", "hotel_options") and nxt["role"] == "user":
            for o in c.get("options", c.get("hotels", [])):
                title = f"I'll take the {o.get('title', '').lower()}" if c["type"] == "transport_options" else f"{o.get('name')}, please!"
                if nxt["text"].startswith(title):
                    S.picks[f"{'transport' if c['type'] == 'transport_options' else 'hotel'}{i}"] = o["id"]

last_bot = max((i for i, e in enumerate(S.history) if e["role"] == "bot"), default=-1)
for i, entry in enumerate(S.history):
    if entry["role"] == "user":
        with st.chat_message("user", avatar="🧑"):
            st.markdown(md(entry["text"]))
            if entry.get("parse"):
                st.caption(entry["parse"])
        continue
    active = i == last_bot and i == len(S.history) - 1
    for j, item in enumerate(entry["items"]):
        key = f"m{i}_{j}"
        custom = item.get("custom") or {}
        if not (item.get("text") or (item.get("buttons") and active) or (custom and (active or custom.get("type") not in INPUTS))):
            continue  # nothing visible (e.g. an old input widget)
        with st.chat_message("assistant", avatar=AVATAR):
            if item.get("text"):
                st.markdown(md(item["text"]))
            if item.get("buttons") and active and not item.get("custom"):
                button_row(item["buttons"], key)
            if item.get("custom"):
                render_custom(item["custom"], key, active, i)
            if S.get("show_raw"):
                with st.expander("Raw reply"):
                    st.json(item)

prompt = st.chat_input("Type a message… e.g. “Paris next weekend with my partner” or “talk to a human”")
if prompt:
    send(prompt, prompt)
    st.rerun()
