from datetime import datetime

import psutil
import requests


# Change these to your location. Defaults to London.
LATITUDE = 51.5074
LONGITUDE = -0.1278
LOCATION_NAME = "London"

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


def describe_time(now=None):
    """A spoken-friendly description of the current time and date."""
    now = now or datetime.now()

    clock = now.strftime("%I:%M %p").lstrip("0")
    weekday = now.strftime("%A")
    month = now.strftime("%B")

    return (
        f"It's {clock} on {weekday}, "
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


def describe_weather():
    """Fetch and describe the current weather. Returns None on failure."""
    try:
        response = requests.get(
            _WEATHER_URL,
            params={
                "latitude": LATITUDE,
                "longitude": LONGITUDE,
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
        return _format_weather(response.json())
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
