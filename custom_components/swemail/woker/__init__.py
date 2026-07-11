import asyncio
import html
import logging
import re
from datetime import datetime
from typing import Any, Dict, Optional

import aiohttp

from ..const import CONF_PROVIDER_CITYMAIL, CONF_PROVIDER_POSTNORD

_LOGGER = logging.getLogger(__name__)


class DeliveryDetails:
    def __init__(self, last_update, postal_city, next_delivery):
        self._last_update = last_update
        self._postal_city = postal_city
        self._next_delivery = next_delivery

    def __repr__(self):
        return f"{self.__class__.__name__}(last_update: {self._last_update}, postal_city '{self._postal_city}', next_delivery: {self._next_delivery})"

    @property
    def last_update(self):
        return self._last_update

    @property
    def postal_city(self):
        return self._postal_city

    @property
    def next_delivery(self):
        return self._next_delivery


class HttpWorker:
    _URL = {
        CONF_PROVIDER_CITYMAIL: "https://postnummersok.citymail.se/?search={}",
        CONF_PROVIDER_POSTNORD: "https://portal.postnord.com/api/sendoutarrival/closest?postalCode={}",
    }

    _dateTable = {
        "januari": "01",
        "februari": "02",
        "mars": "03",
        "april": "04",
        "maj": "05",
        "juni": "06",
        "juli": "07",
        "augusti": "08",
        "september": "09",
        "oktober": "10",
        "november": "11",
        "december": "12",
    }

    def __init__(self):
        self._data = {CONF_PROVIDER_CITYMAIL: {}, CONF_PROVIDER_POSTNORD: {}}

    @property
    def data(self):
        return self._data

    async def _fetch_data_async(self, url: str, datatype: str = "text") -> Any:
        """Fetch data from URL asynchronously."""
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        ) as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
                if datatype == "json":
                    return await resp.json()
                else:
                    return await resp.text()

    def _handle_pn_data(self, data: Dict[str, Any], postalcode: int) -> None:
        """Handle PostNord data response."""
        try:
            delivery_text = str(data.get("delivery", "")).strip()
            arr = delivery_text.split()

            # Expected format: "26 juni 2026" â€” PostNord may or may not append a
            # trailing comma after the month, so strip it before the lookup.
            if len(arr) >= 3 and arr[0].isdigit():
                month = self._dateTable.get(arr[1].lower().rstrip(","))
                if month:
                    formatted_date = f"{arr[2]}-{month}-{arr[0].zfill(2)}"
                else:
                    _LOGGER.warning(
                        "PostNord: unknown month in delivery string '%s'",
                        delivery_text,
                    )
                    formatted_date = delivery_text
            elif delivery_text:
                _LOGGER.warning(
                    "PostNord: unexpected delivery format '%s'", delivery_text
                )
                formatted_date = delivery_text
            else:
                formatted_date = "No delivery scheduled"

            payload = {
                "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "postal_city": str(data.get("city", "")).capitalize(),
                "next_delivery": formatted_date,
            }

            # The /closest endpoint may answer for a different code than requested.
            returned_pc = str(data.get("postalCode", "")).strip()
            if returned_pc and returned_pc != str(postalcode):
                _LOGGER.warning(
                    "PostNord returned data for %s but %s was requested "
                    "(closest match?)",
                    returned_pc,
                    postalcode,
                )

            # Store under the requested code (int + str) so the caller always
            # finds it, plus the code PostNord actually returned.
            self._data[CONF_PROVIDER_POSTNORD][int(postalcode)] = payload
            self._data[CONF_PROVIDER_POSTNORD][str(postalcode)] = payload
            if returned_pc:
                self._data[CONF_PROVIDER_POSTNORD][returned_pc] = payload
                if returned_pc.isdigit():
                    self._data[CONF_PROVIDER_POSTNORD][int(returned_pc)] = payload

        except Exception as error:
            _LOGGER.error("Process data failed (PN): %s", error)
            self._data[CONF_PROVIDER_POSTNORD][postalcode] = {
                "last_update": "",
                "postal_city": "",
                "next_delivery": "",
            }

    def _handle_cm_data(self, data: str, postalcode: int) -> None:
        """Handle CityMail data response."""
        try:
            match = re.search(r"<h2>([0-9]{5}) (.*)<\/h2>[\w\W]*>(.*)<\/span>", data)
            if match:
                self._data[CONF_PROVIDER_CITYMAIL][int(match.group(1))] = {
                    "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "postal_city": html.unescape(match.group(2)).capitalize(),
                    "next_delivery": match.group(3),
                }
        except Exception as error:
            _LOGGER.error("Process data failed (CM): %s", error)
            self._data[CONF_PROVIDER_CITYMAIL][postalcode] = {
                "last_update": "",
                "postal_city": "",
                "next_delivery": "",
            }

    async def fetch_postal_city(self, postalcode: int) -> str:
        """Fetch postal city name from PostNord API."""
        data = await self._fetch_data_async(
            self._URL[CONF_PROVIDER_POSTNORD].format(postalcode), "json"
        )
        return data["city"].capitalize()

    async def fetch_async(self, postalcode: int, provider: str) -> None:
        """Fetch delivery data for a specific provider asynchronously."""
        try:
            if provider == CONF_PROVIDER_POSTNORD:
                data = await self._fetch_data_async(
                    self._URL[provider].format(postalcode), "json"
                )
                self._handle_pn_data(data, postalcode)
            elif provider == CONF_PROVIDER_CITYMAIL:
                data = await self._fetch_data_async(
                    self._URL[provider].format(postalcode), "text"
                )
                self._handle_cm_data(data, postalcode)
        except Exception as error:
            _LOGGER.error("Fetch failed for %s (%s): %s", provider, postalcode, error)
