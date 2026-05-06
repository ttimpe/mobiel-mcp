import os
import re
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "Westfalenfahrplan EFA",
    host=os.environ.get("FASTMCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("FASTMCP_PORT", "8000")),
)

BASE_URL = "https://westfalenfahrplan.de/nwl-efa"
COMMON_PARAMS = {
    "outputFormat": "rapidJSON",
    "serverInfo": "1",
    "language": "de",
    "version": "11.0.6.72",
}
MOT_PARAMS = {f"inclMOT_{i}": "true" for i in [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19]}
BERLIN = ZoneInfo("Europe/Berlin")


async def efa_get(endpoint: str, params: dict) -> dict:
    merged = {**COMMON_PARAMS, **params}
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{BASE_URL}/{endpoint}", params=merged)
        r.raise_for_status()
        return r.json()


async def efa_get_multi(endpoint: str, base_params: dict, multi_params: list[tuple]) -> dict:
    merged = list({**COMMON_PARAMS, **base_params}.items()) + multi_params
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{BASE_URL}/{endpoint}", params=merged)
        r.raise_for_status()
        return r.json()


def _to_local(ts: str) -> Optional[datetime]:
    """Parse a UTC ISO timestamp (with or without Z) into a Berlin-local datetime."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(BERLIN)
    except Exception:
        return None


def _fmt_time(ts: str) -> str:
    dt = _to_local(ts)
    return dt.strftime("%H:%M") if dt else "?"


def _delay_str(planned: str, estimated: str) -> str:
    p = _to_local(planned)
    e = _to_local(estimated)
    if p and e:
        diff = int((e - p).total_seconds() / 60)
        if diff > 0:
            return f" [+{diff}min]"
        if diff < 0:
            return f" [{diff}min]"
    return ""


@mcp.tool()
async def search_stops(query: str) -> str:
    """
    Search for public transport stops and stations by name.
    Returns matching stops with their IDs — use these IDs in other tools.

    Args:
        query: Stop name or partial name, e.g. "Bielefeld Hauptbahnhof" or "Dortmund Hbf"
    """
    data = await efa_get("XML_STOPFINDER_REQUEST", {
        "coordOutputFormat": "WGS84[dd.ddddd]",
        "doNotSearchForStops_sf": "1",
        "locationInfoActive": "1",
        "locationServerActive": "1",
        "name_sf": query,
        "nwlStopFinderMacro": "1",
        "sl3plusStopFinderMacro": "trip",
        "type_sf": "any",
    })

    locations = data.get("locations", [])
    if not locations:
        return f"No stops found for: {query}"

    lines = [f"Found {len(locations)} result(s) for '{query}':\n"]
    for loc in locations[:20]:
        name = loc.get("name", "?")
        stop_id = loc.get("id", "")
        loc_type = loc.get("type", "")
        parent = loc.get("parent") or {}
        place = parent.get("name", "")
        coord = loc.get("coord") or []  # [lat, lon]
        lat = coord[0] if len(coord) > 0 else ""
        lon = coord[1] if len(coord) > 1 else ""

        line = f"• {name}"
        if place and place != name:
            line += f" — {place}"
        line += f"\n  ID: {stop_id}  Type: {loc_type}"
        if lat and lon:
            line += f"  Coords: {lat}, {lon}"
        lines.append(line)

    return "\n\n".join(lines)


@mcp.tool()
async def get_departures(
    stop_id: str,
    date: Optional[str] = None,
    time: Optional[str] = None,
    limit: int = 20,
) -> str:
    """
    Get upcoming departures from a stop.

    Args:
        stop_id: Stop ID from search_stops, e.g. "de:05711:5500"
        date: Date in DD.MM.YYYY format (omit for today)
        time: Time in HH:MM format (omit for now)
        limit: How many departures to return (max 30, default 20)
    """
    params = {
        "coordOutputFormat": "WGS84[dd.ddddd]",
        "deleteAssignedStops_dm": "1",
        "depSequence": str(min(limit, 30)),
        "depType": "stopEvents",
        "doNotSearchForStops": "1",
        "genMaps": "0",
        "imparedOptionsActive": "1",
        **MOT_PARAMS,
        "includeCompleteStopSeq": "1",
        "includedMeans": "checkbox",
        "itOptionsActive": "1",
        "itdDateTimeDepArr": "dep",
        "locationServerActive": "1",
        "maxTimeLoop": "1",
        "mode": "direct",
        "name_dm": stop_id,
        "nwlDMMacro": "1",
        "ptOptionsActive": "1",
        "sl3plusDMMacro": "1",
        "type_dm": "any",
        "useAllStops": "1",
        "useProxFootSearchDestination": "true",
        "useProxFootSearchOrigin": "true",
        "useRealtime": "1",
    }
    if date:
        params["itdDateDayMonthYear"] = date
    if time:
        params["itdTime"] = time

    data = await efa_get("XML_DM_REQUEST", params)
    events = data.get("stopEvents", [])

    if not events:
        return f"No departures found for stop {stop_id}."

    # Derive stop name from the first event's parent stop
    stop_name = stop_id
    first_loc = (events[0].get("location") or {})
    parent = first_loc.get("parent") or {}
    if parent.get("name"):
        stop_name = parent["name"]

    when = f" from {date or 'today'} {time or 'now'}" if (date or time) else ""
    lines = [f"Departures from {stop_name}{when}:\n"]
    for ev in events:
        trans = ev.get("transportation") or {}
        line_name = trans.get("disassembledName") or trans.get("name", "?")
        direction = (trans.get("destination") or {}).get("name", "?")
        planned = ev.get("departureTimePlanned", "")
        estimated = ev.get("departureTimeEstimated", "")
        t = _fmt_time(planned)
        delay = _delay_str(planned, estimated) if estimated else ""
        cancelled = " [CANCELLED]" if ev.get("isCancelled") else ""
        plat_name = (ev.get("location") or {}).get("properties", {}).get("platformName", "")
        plat = f"  {plat_name}" if plat_name else ""
        lines.append(f"  {t}{delay}{cancelled}  {line_name} → {direction}{plat}")

    return "\n".join(lines)


@mcp.tool()
async def get_arrivals(
    stop_id: str,
    date: Optional[str] = None,
    time: Optional[str] = None,
    limit: int = 20,
) -> str:
    """
    Get upcoming arrivals at a stop.

    Args:
        stop_id: Stop ID from search_stops, e.g. "de:05711:5500"
        date: Date in DD.MM.YYYY format (omit for today)
        time: Time in HH:MM format (omit for now)
        limit: How many arrivals to return (max 30, default 20)
    """
    params = {
        "coordOutputFormat": "WGS84[dd.ddddd]",
        "deleteAssignedStops_dm": "1",
        "depSequence": str(min(limit, 30)),
        "depType": "stopEvents",
        "doNotSearchForStops": "1",
        "genMaps": "0",
        "imparedOptionsActive": "1",
        **MOT_PARAMS,
        "includeCompleteStopSeq": "1",
        "includedMeans": "checkbox",
        "itOptionsActive": "1",
        "itdDateTimeDepArr": "arr",
        "locationServerActive": "1",
        "maxTimeLoop": "1",
        "mode": "direct",
        "name_dm": stop_id,
        "nwlDMMacro": "1",
        "ptOptionsActive": "1",
        "sl3plusDMMacro": "1",
        "type_dm": "any",
        "useAllStops": "1",
        "useProxFootSearchDestination": "true",
        "useProxFootSearchOrigin": "true",
        "useRealtime": "1",
    }
    if date:
        params["itdDateDayMonthYear"] = date
    if time:
        params["itdTime"] = time

    data = await efa_get("XML_DM_REQUEST", params)
    events = data.get("stopEvents", [])

    if not events:
        return f"No arrivals found for stop {stop_id}."

    stop_name = stop_id
    first_loc = (events[0].get("location") or {})
    parent = first_loc.get("parent") or {}
    if parent.get("name"):
        stop_name = parent["name"]

    when = f" up to {date or 'today'} {time or 'now'}" if (date or time) else ""
    lines = [f"Arrivals at {stop_name}{when}:\n"]
    for ev in events:
        trans = ev.get("transportation") or {}
        line_name = trans.get("disassembledName") or trans.get("name", "?")
        origin = (trans.get("origin") or {}).get("name", "?")
        planned = ev.get("arrivalTimePlanned", "")
        estimated = ev.get("arrivalTimeEstimated", "")
        t = _fmt_time(planned)
        delay = _delay_str(planned, estimated) if estimated else ""
        cancelled = " [CANCELLED]" if ev.get("isCancelled") else ""
        plat_name = (ev.get("location") or {}).get("properties", {}).get("platformName", "")
        plat = f"  {plat_name}" if plat_name else ""
        lines.append(f"  {t}{delay}{cancelled}  {line_name} from {origin}{plat}")

    return "\n".join(lines)


@mcp.tool()
async def search_routes(
    origin: str,
    destination: str,
    date: Optional[str] = None,
    time: Optional[str] = None,
    dep_arr: str = "dep",
) -> str:
    """
    Search for routes/connections between two stops.
    Accepts stop IDs (from search_stops) or plain stop names.

    Args:
        origin: Origin stop ID or name, e.g. "de:05711:5500" or "Bielefeld Hbf"
        destination: Destination stop ID or name
        date: Date in DD.MM.YYYY format (omit for today)
        time: Time in HH:MM format (omit for now)
        dep_arr: "dep" to search from departure time, "arr" for arrival time
    """
    params = {
        "accessProfile": "0",
        "allInterchangesAsLegs": "1",
        "coordOutputDistance": "1",
        "coordOutputFormat": "WGS84[dd.ddddd]",
        "genC": "1",
        "genMaps": "0",
        "imparedOptionsActive": "1",
        **MOT_PARAMS,
        "includedMeans": "checkbox",
        "itOptionsActive": "1",
        "itdTripDateTimeDepArr": dep_arr,
        "lineRestriction": "400",
        "locationServerActive": "1",
        "name_destination": destination,
        "name_origin": origin,
        "nwlTripMacro": "1",
        "ptOptionsActive": "1",
        "routeType": "LEASTTIME",
        "sl3plusTripMacro": "1",
        "trITMOTvalue100": "10",
        "type_destination": "any",
        "type_notVia": "any",
        "type_origin": "any",
        "type_via": "any",
        "useProxFootSearchDestination": "true",
        "useProxFootSearchOrigin": "true",
        "useRealtime": "1",
        "useUT": "1",
    }
    if date:
        params["itdDateDayMonthYear"] = date
    if time:
        params["itdTime"] = time

    data = await efa_get("XML_TRIP_REQUEST2", params)
    journeys = data.get("journeys", [])

    if not journeys:
        return f"No routes found from '{origin}' to '{destination}'."

    sections = [f"Routes from '{origin}' to '{destination}':\n"]
    for i, journey in enumerate(journeys[:5], 1):
        legs = journey.get("legs", [])
        if not legs:
            continue

        first, last = legs[0], legs[-1]
        dep_planned = (first.get("origin") or {}).get("departureTimePlanned", "")
        arr_planned = (last.get("destination") or {}).get("arrivalTimePlanned", "")
        dep_t = _fmt_time(dep_planned)
        arr_t = _fmt_time(arr_planned)

        dep_dt = _to_local(dep_planned)
        arr_dt = _to_local(arr_planned)
        if dep_dt and arr_dt:
            duration = int((arr_dt - dep_dt).total_seconds() / 60)
        else:
            duration = journey.get("duration", 0)
        dur_str = f"{duration // 60}h {duration % 60}m" if duration >= 60 else f"{duration}min"
        pt_legs = [l for l in legs if l.get("transportation")]
        transfers = max(0, len(pt_legs) - 1)

        block = [f"Option {i}: {dep_t} → {arr_t}  ({dur_str}, {transfers} transfer(s))"]
        for leg in legs:
            trans = leg.get("transportation") or {}
            orig = leg.get("origin") or {}
            dest = leg.get("destination") or {}
            if trans:
                line_name = trans.get("disassembledName") or trans.get("name", "?")
                direction = (trans.get("destination") or {}).get("name", "?")
                leg_dep = _fmt_time(orig.get("departureTimePlanned", ""))
                leg_arr = _fmt_time(dest.get("arrivalTimePlanned", ""))
                from_stop = orig.get("name", "?")
                to_stop = dest.get("name", "?")
                block.append(f"  {leg_dep}  {from_stop}  → [{line_name} dir. {direction}]")
                block.append(f"  {leg_arr}  {to_stop}  (arrive)")
            else:
                dist = leg.get("distance", 0)
                walk_dur = leg.get("duration", 0)
                block.append(
                    f"  Walk {dist}m (~{walk_dur}min): "
                    f"{orig.get('name','?')} → {dest.get('name','?')}"
                )

        sections.append("\n".join(block))

    return "\n\n".join(sections)


@mcp.tool()
async def get_disruptions(
    stop_id: Optional[str] = None,
) -> str:
    """
    Get disruptions, service alerts and travel information.

    Args:
        stop_id: Optional stop ID to filter alerts for a specific stop.
                 Omit to retrieve all current network-wide disruptions.
    """
    if stop_id:
        data = await efa_get("XML_ADDINFO_REQUEST", {
            "filterShowLineList": "0",
            "filterShowPlaceList": "0",
            "filterShowStopList": "0",
            "filterStopId": stop_id,
            "sl3plusAddInfoMacro": "1",
        })
    else:
        base = {
            "filterShowLineList": "0",
            "filterShowPlaceList": "0",
            "filterShowStopList": "0",
            "sl3plusAddInfoMacro": "1",
            "sl3plusAddInfoReducedMacro": "1",
        }
        multi = [
            ("AIXMLReduction", "removeValidity"),
            ("AIXMLReduction", "removeCreationTime"),
            ("AIXMLReduction", "removePublication"),
            ("filterInfoType", "stopInfo"),
            ("filterInfoType", "stopBlocking"),
            ("filterInfoType", "lineInfo"),
            ("filterInfoType", "lineBlocking"),
            ("filterInfoType", "routeInfo"),
            ("filterInfoType", "routeBlocking"),
            ("filterInfoType", "areaInfo"),
            ("filterInfoType", "infrastructure"),
        ]
        data = await efa_get_multi("XML_ADDINFO_REQUEST", base, multi)

    infos_raw = data.get("infos", {})
    # API returns {"current": [...]} or a list
    if isinstance(infos_raw, dict):
        infos = infos_raw.get("current", [])
    else:
        infos = infos_raw

    if not infos:
        scope = f"stop {stop_id}" if stop_id else "the network"
        return f"No disruptions or alerts found for {scope}."

    scope = f"stop {stop_id}" if stop_id else "the network"
    def _strip_html(html: str) -> str:
        return re.sub(r"<[^>]+>", "", html).replace("&auml;", "ä").replace("&uuml;", "ü").replace(
            "&ouml;", "ö").replace("&Auml;", "Ä").replace("&Uuml;", "Ü").replace("&Ouml;", "Ö").replace(
            "&szlig;", "ß").replace("&nbsp;", " ").replace("&amp;", "&").strip()

    lines = [f"{len(infos)} disruption(s)/alert(s) for {scope}:\n"]
    for info in infos[:25]:
        info_type = info.get("type", "")
        links = info.get("infoLinks") or []
        link = links[0] if links else {}
        title = link.get("subtitle") or link.get("title") or link.get("urlText") or info.get("id", "Alert")
        timestamps = info.get("timestamps") or {}
        avail = timestamps.get("availability") or {}
        valid_from = (avail.get("from") or "")[:10]
        valid_to = (avail.get("to") or "")[:10]
        raw_content = link.get("content", "")
        content = _strip_html(raw_content)[:300] if raw_content else ""
        affected = info.get("affected") or {}
        aff_lines = [l.get("name", "") for l in (affected.get("lines") or []) if l.get("name")]

        entry = f"• [{info_type}] {title}"
        if valid_from or valid_to:
            entry += f"\n  Valid: {valid_from} – {valid_to}"
        if aff_lines:
            entry += f"\n  Lines: {', '.join(aff_lines[:10])}"
        if content:
            entry += f"\n  {content}"
        lines.append(entry)

    return "\n\n".join(lines)


if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    mcp.run(transport=transport)
