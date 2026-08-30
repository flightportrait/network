"""The only module that talks to the upstream readsb.

Everything the API serves originates here: aircraft.json (snapshot poller),
clients.json / receivers.json (station poller), and the re-api circle and
filter_uuid queries. Injectable via the app factory so tests never touch
the network.
"""
import httpx


class ReadsbClient:
    def __init__(self, base_url: str, timeout_s: float = 2.0):
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_s,
            headers={"User-Agent": "fp-network-api"},
        )

    async def _get_json(self, path: str) -> dict:
        response = await self._client.get(path)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("upstream returned non-object JSON")
        return payload

    async def aircraft(self) -> dict:
        return await self._get_json("/data/aircraft.json")

    async def point_source(self, url_template: str, lat: float, lon: float,
                           radius_nm: int) -> dict:
        """Remote-source mode: one v2 point query against a public
        aggregator, normalized to the aircraft.json shape the snapshot
        builder eats. Their `now` may be milliseconds."""
        url = url_template.format(lat=lat, lon=lon, radius=radius_nm)
        response = await self._client.get(url)
        response.raise_for_status()
        payload = response.json()
        now = payload.get("now") or 0
        if now > 1e12:
            now = now / 1000.0
        return {"now": now,
                "aircraft": payload.get("ac") or payload.get("aircraft")
                or []}

    async def clients(self) -> dict:
        return await self._get_json("/data/clients.json")

    async def receivers(self) -> dict:
        return await self._get_json("/data/receivers.json")

    async def circle(self, lat: float, lon: float, radius_nm: float) -> dict:
        return await self._get_json(
            "/re-api/?circle=%.6f,%.6f,%.1f" % (lat, lon, radius_nm))

    async def count_for_station(self, half_id_hex16: str) -> int:
        """Aircraft currently seen by one station.

        readsb's filter_uuid takes the first 16 hex chars of the station
        UUID (dashes stripped) and needs the aggregator started with
        --net-receiver-id.
        """
        payload = await self._get_json(
            "/re-api/?all_with_pos&filter_uuid=%s" % half_id_hex16)
        return len(payload.get("aircraft") or [])

    async def aclose(self) -> None:
        await self._client.aclose()
