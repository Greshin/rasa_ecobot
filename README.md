# Eco-Travel Assistant 🌿

A Rasa chatbot that helps travellers plan lower-carbon trips. It compares train, coach, car and
flight emissions (Climatiq), finds flights (Duffel) and hotels (LiteAPI), shows the weather
(Open-Meteo) and hands over to a human advisor. The interface is built with Streamlit.

## Project structure
- `data/` – NLU examples, rules and stories
- `domain.yml`, `config.yml` – Rasa setup
- `actions/actions.py` – dialogue logic and weighted ranking
- `actions/travel_apis.py` – API clients with fallbacks
- `streamlit_app.py` – chat interface

