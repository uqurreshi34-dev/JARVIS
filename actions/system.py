from datetime import datetime
import os

import psutil
import requests
from dotenv import load_dotenv


load_dotenv()


def _env_float(name, default):
    try:
        return float(os.getenv(name) or default)
    except ValueError:
        return default


# Set WEATHER_LATITUDE, WEATHER_LONGITUDE and WEATHER_LOCATION in .env to
# report somewhere other than London.
LATITUDE = _env_float("WEATHER_LATITUDE", 51.5074)
LONGITUDE = _env_float("WEATHER_LONGITUDE", -0.1278)
LOCATION_NAME = os.getenv("WEATHER_LOCATION") or "London"

_WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT = 8

_ORDINALS = {1: "st", 2: "nd", 3: "rd", 21: "st", 22: "nd", 23: "rd", 31: "st"}

# Open-Meteo WMO weather codes, grouped into spoken descriptions.
_CONDITIONS = {
    0: "clear",
    1: "mostly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "foggy",
    48: "foggy",
    51: "drizzling lightly",
    53: "drizzling",
    55: "drizzling heavily",
    56: "freezing drizzle",
    57: "freezing drizzle",
    61: "raining lightly",
    63: "raining",
    65: "raining heavily",
    66: "freezing rain",
    67: "freezing rain",
    71: "snowing lightly",
    73: "snowing",
    75: "snowing heavily",
    77: "hailing",
    80: "with light showers",
    81: "with showers",
    82: "with heavy showers",
    85: "with snow showers",
    86: "with heavy snow showers",
    95: "thundery",
    96: "thundery with hail",
    99: "thundery with hail",
}


def _ordinal(day):
    return f"{day}{_ORDINALS.get(day, 'th')}"


_UNITS = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen",
)

_TENS = (
    "", "", "twenty", "thirty", "forty", "fifty",
)


def _in_words(number):
    """Numbers up to 59 as words, for speech that reads naturally."""
    if number < 20:
        return _UNITS[number]

    tens, units = divmod(number, 10)

    if units:
        return f"{_TENS[tens]} {_UNITS[units]}"

    return _TENS[tens]


def _spoken_clock(now):
    """A clock time the speech engine reads correctly.

    "4:43 PM" is mangled by the voice into something like "for four three",
    so the time is spelled out instead.
    """
    hour = now.hour % 12 or 12
    minute = now.minute
    meridiem = "AM" if now.hour < 12 else "PM"

    if minute == 0:
        if now.hour == 0:
            return "midnight"

        if now.hour == 12:
            return "midday"

        return f"{_in_words(hour)} {meridiem}"

    if minute < 10:
        return f"{_in_words(hour)} oh {_in_words(minute)} {meridiem}"

    return f"{_in_words(hour)} {_in_words(minute)} {meridiem}"


def describe_time(now=None):
    """A spoken-friendly description of the current time and date."""
    now = now or datetime.now()

    weekday = now.strftime("%A")
    month = now.strftime("%B")

    return (
        f"It's {_spoken_clock(now)} on {weekday}, "
        f"the {_ordinal(now.day)} of {month}."
    )


def _format_weather(payload, location=LOCATION_NAME):
    """Turn an Open-Meteo response into a spoken sentence."""
    current = payload.get("current") or {}
    daily = payload.get("daily") or {}

    temperature = current.get("temperature_2m")
    code = current.get("weather_code")

    if temperature is None:
        return None

    condition = _CONDITIONS.get(code, "unsettled")

    sentence = (
        f"It's currently {round(temperature)} degrees and {condition} "
        f"in {location}."
    )

    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    rain = daily.get("precipitation_probability_max") or []

    if highs and lows:
        sentence += (
            f" Today's high is {round(highs[0])}, "
            f"with a low of {round(lows[0])}."
        )

    if rain and rain[0] is not None:
        chance = round(rain[0])

        if chance >= 50:
            article = "an" if chance in (
                8, 11, 18) or 80 <= chance <= 89 else "a"
            sentence += f" There's {article} {chance} percent chance of rain, sir."
        elif chance >= 20:
            sentence += f" Only a {chance} percent chance of rain."
        else:
            sentence += " Rain is unlikely."

    return sentence


_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"


def _lookup_place(place):
    """Turn a place name into (latitude, longitude, proper name), or None.

    Open-Meteo's geocoding is free and needs no key. The result is stored,
    so a place is only ever looked up once.
    """
    try:
        response = requests.get(
            _GEOCODE_URL,
            params={"name": place, "count": 1, "language": "en"},
            timeout=_TIMEOUT,
        )
        response.raise_for_status()

        results = (response.json() or {}).get("results") or []

    except (requests.RequestException, ValueError) as error:
        print(f"[JARVIS] could not find {place}: {error}")
        return None

    if not results:
        print(f"[JARVIS] no such place: {place}")
        return None

    first = results[0]

    try:
        return (
            float(first["latitude"]),
            float(first["longitude"]),
            first.get("name") or place,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _where():
    """Where to report the weather for: (latitude, longitude, name).

    A remembered location wins over the .env setting, so saying "I'm based
    in Birmingham" is enough and nothing has to be edited by hand.
    """
    try:
        from actions import memory

        place = memory.location()

        if not place:
            print(
                "[JARVIS] no remembered location; using .env "
                f"({LOCATION_NAME})"
            )

            return LATITUDE, LONGITUDE, LOCATION_NAME

        latitude, longitude = memory.coordinates()

        if latitude is not None and longitude is not None:
            print(f"[JARVIS] weather for {place} (remembered)")

            return latitude, longitude, place

        found = _lookup_place(place)

        if not found:
            return LATITUDE, LONGITUDE, LOCATION_NAME

        latitude, longitude, proper = found

        print(f"[JARVIS] found {proper} at {latitude:.3f}, {longitude:.3f}")

        # Remembered so the lookup happens once, not on every forecast.
        memory.set_coordinates(latitude, longitude)

        return latitude, longitude, proper

    except Exception as error:
        print(f"[JARVIS] could not read your location: {error}")
        return LATITUDE, LONGITUDE, LOCATION_NAME


def describe_weather():
    """Fetch and describe the current weather. Returns None on failure."""
    latitude, longitude, place = _where()

    try:
        response = requests.get(
            _WEATHER_URL,
            params={
                "latitude": latitude,
                "longitude": longitude,
                "current": "temperature_2m,weather_code",
                "daily": (
                    "temperature_2m_max,temperature_2m_min,"
                    "precipitation_probability_max"
                ),
                "timezone": "auto",
                "forecast_days": 1,
            },
            timeout=_TIMEOUT,
        )
        response.raise_for_status()

    except requests.RequestException as error:
        print(f"[JARVIS] weather lookup failed: {error}")
        return None

    try:
        return _format_weather(response.json(), location=place)
    except (ValueError, KeyError, TypeError) as error:
        print(f"[JARVIS] weather parse failed: {error}")
        return None


def _plural(value, noun):
    return f"{value} {noun}" if value == 1 else f"{value} {noun}s"


def _format_duration(seconds):
    hours, remainder = divmod(int(seconds), 3600)
    minutes = remainder // 60

    if hours and minutes:
        return f"{_plural(hours, 'hour')} and {_plural(minutes, 'minute')}"

    if hours:
        return _plural(hours, "hour")

    return _plural(minutes, "minute")


def describe_system():
    """Report CPU, memory, disk, and battery in a spoken sentence."""
    cpu = psutil.cpu_percent(interval=0.5)
    memory = psutil.virtual_memory()

    parts = [
        f"CPU is at {round(cpu)} percent",
        f"memory at {round(memory.percent)} percent",
    ]

    try:
        disk = psutil.disk_usage("C:\\")
        free_gb = disk.free / (1024 ** 3)
        parts.append(f"with {free_gb:.0f} gigabytes free on the C drive")
    except OSError:
        pass

    sentence = "Systems nominal. " + ", ".join(parts) + "."

    try:
        battery = psutil.sensors_battery()
    except Exception:
        battery = None

    if battery is not None:
        charge = round(battery.percent)

        if battery.power_plugged:
            sentence += f" Battery at {charge} percent and charging."
        else:
            sentence += f" Battery at {charge} percent"

            remaining = battery.secsleft

            if remaining and remaining > 0 and remaining < 86400:
                sentence += f", about {_format_duration(remaining)} remaining."
            else:
                sentence += "."

    return sentence
