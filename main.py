"""NomadNest AI FastAPI service using Nominatim and live OpenStreetMap facilities."""
from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from agents import MODEL_NAME, run_personas
from database import initialize_database, persist_simulation

logger = logging.getLogger("nomadnest")
BASE_DIR = Path(__file__).resolve().parent
NOMINATIM_URL = os.getenv("NOMINATIM_URL", "https://nominatim.openstreetmap.org/search")
NOMINATIM_EMAIL = os.getenv("NOMINATIM_EMAIL", "")
OVERPASS_URL = os.getenv("OVERPASS_URL", "https://overpass-api.de/api/interpreter")
SEARCH_RADIUS_METERS = 2_000
MAX_FACILITIES = 250
FALLBACK_LOCATIONS: dict[str, tuple[str, float, float]] = {
    "iskandar puteri, johor": ("Iskandar Puteri, Johor, Malaysia", 1.4140, 103.6489),
    "iskandar puteri johor": ("Iskandar Puteri, Johor, Malaysia", 1.4140, 103.6489),
    "ayer tawar, malaysia": ("Ayer Tawar, Perak, Malaysia", 4.3380, 100.7410),
    "ayer tawar johor": ("Ayer Tawar, Johor, Malaysia", 1.6650, 103.6090),
}
_last_geocode = 0.0
_geocode_lock = asyncio.Lock()
_geocode_cache: dict[str, tuple[str, float, float]] = {}


class CoordinateInput(BaseModel):
    latitude: Annotated[float, Field(ge=-90, le=90)]
    longitude: Annotated[float, Field(ge=-180, le=180)]


class SimulationRequest(BaseModel):
    target_location: Annotated[str, Field(min_length=3, max_length=300)]
    run_type: Literal["balanced", "commuter", "nightlife", "family"] = "balanced"
    coordinates: CoordinateInput | None = None
    resolved_name: Annotated[str | None, Field(default=None, max_length=500)] = None

    @field_validator("target_location")
    @classmethod
    def clean_location(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 3:
            raise ValueError("Target Location must contain at least 3 non-space characters.")
        return value

    @field_validator("resolved_name")
    @classmethod
    def clean_resolved_name(cls, value: str | None) -> str | None:
        return value.strip() if value else None


class TimelineEvent(BaseModel):
    hour: Annotated[int, Field(ge=0, le=23)]
    minute: Annotated[int, Field(ge=0, le=59)]
    title: str
    detail: str
    severity: Literal["low", "medium", "high"]
    source: Literal["Commuter", "Light Sleeper"]


class SimulationResponse(BaseModel):
    run_id: int
    display_name: str
    latitude: float
    longitude: float
    run_type: str
    source_mode: str
    score: int
    summary: str
    proximity: dict[str, Any]
    timeline: list[TimelineEvent]


async def geocode_location(query: str) -> tuple[str, float, float]:
    """Respect Nominatim's public-service one-request-per-second limit."""
    global _last_geocode
    normalized = " ".join(query.casefold().split())
    cached = _geocode_cache.get(normalized)
    if cached:
        return cached
    async with _geocode_lock:
        cached = _geocode_cache.get(normalized)
        if cached:
            return cached
        delay = 1.0 - (time.monotonic() - _last_geocode)
        if delay > 0:
            await asyncio.sleep(delay)
        params = {"q": query, "format": "jsonv2", "limit": 1, "addressdetails": 1, "countrycodes": "my"}
        if NOMINATIM_EMAIL:
            params["email"] = NOMINATIM_EMAIL
        headers = {"User-Agent": "NomadNestAI/1.0 (local neighborhood simulator)"}
        try:
            async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
                response = await client.get(NOMINATIM_URL, params=params)
                response.raise_for_status()
            _last_geocode = time.monotonic()
            matches = response.json()
            if not matches:
                raise HTTPException(status_code=404, detail="Nominatim did not find that Target Location.")
            first = matches[0]
            resolved = first["display_name"], float(first["lat"]), float(first["lon"])
            _geocode_cache[normalized] = resolved
            return resolved
        except HTTPException:
            raise
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            logger.warning("Nominatim geocoding failed: %s", exc)
            fallback = FALLBACK_LOCATIONS.get(normalized)
            if fallback:
                logger.info("Using cached coordinate fallback for %s", query)
                _geocode_cache[normalized] = fallback
                return fallback
            raise HTTPException(status_code=502, detail="Nominatim geocoding is temporarily unavailable.") from exc


def _distance_meters(latitude_a: float, longitude_a: float, latitude_b: float, longitude_b: float) -> int:
    earth_radius = 6_371_000
    latitude_delta = math.radians(latitude_b - latitude_a)
    longitude_delta = math.radians(longitude_b - longitude_a)
    first, second = math.radians(latitude_a), math.radians(latitude_b)
    chord = math.sin(latitude_delta / 2) ** 2 + math.cos(first) * math.cos(second) * math.sin(longitude_delta / 2) ** 2
    return round(2 * earth_radius * math.asin(math.sqrt(chord)))


def _category(tags: dict[str, str]) -> str | None:
    amenity = tags.get("amenity")
    if tags.get("railway") in {"station", "halt", "tram_stop"} or tags.get("public_transport") in {"station", "platform"} or amenity == "bus_station":
        return "transit"
    if amenity in {"school", "kindergarten", "college", "university"}:
        return "school"
    if amenity in {"hospital", "clinic", "doctors", "pharmacy"} or tags.get("healthcare") in {"clinic", "hospital", "doctor", "pharmacy"}:
        return "emergency"
    if tags.get("shop") in {"supermarket", "convenience", "grocery"}:
        return "grocery"
    if amenity in {"marketplace"} or tags.get("shop") == "mall":
        return "grocery"
    if amenity in {"bar", "pub", "nightclub"}:
        return "nightlife"
    if amenity in {"restaurant", "cafe", "fast_food", "food_court"}:
        return "eateries"
    if tags.get("highway") in {"trunk", "primary", "secondary"}:
        return "road"
    if amenity in {"bank", "library", "place_of_worship"} or tags.get("leisure") in {"park", "sports_centre", "fitness_centre"}:
        return "community"
    return None


def _overpass_query(latitude: float, longitude: float) -> str:
    return f'''[out:json][timeout:40];(
nwr(around:{SEARCH_RADIUS_METERS},{latitude},{longitude})[amenity~"bus_station|school|kindergarten|college|university|hospital|clinic|doctors|pharmacy|restaurant|cafe|fast_food|food_court|bar|pub|nightclub|marketplace|bank|library|place_of_worship"];
nwr(around:{SEARCH_RADIUS_METERS},{latitude},{longitude})[railway~"station|halt|tram_stop"];
nwr(around:{SEARCH_RADIUS_METERS},{latitude},{longitude})[public_transport~"station|platform"];
nwr(around:{SEARCH_RADIUS_METERS},{latitude},{longitude})[shop~"supermarket|convenience|grocery|mall"];
nwr(around:{SEARCH_RADIUS_METERS},{latitude},{longitude})[leisure~"park|sports_centre|fitness_centre"];
way(around:{SEARCH_RADIUS_METERS},{latitude},{longitude})[highway~"trunk|primary|secondary"];
);out center tags;'''


async def live_poi_infrastructure(latitude: float, longitude: float) -> tuple[list[dict[str, Any]], str]:
    """Filter named OpenStreetMap facilities in a 1 km radius through Overpass."""
    try:
        async with httpx.AsyncClient(timeout=35.0, headers={"User-Agent": "NomadNestAI/1.0 (neighborhood simulator)"}) as client:
            response = await client.post(OVERPASS_URL, data={"data": _overpass_query(latitude, longitude)})
            response.raise_for_status()
        pois: list[dict[str, Any]] = []
        for record in response.json().get("elements", []):
            tags = record.get("tags", {})
            category = _category(tags)
            point = record if "lat" in record else record.get("center", {})
            if not category or "lat" not in point or "lon" not in point:
                continue
            name = tags.get("name") or tags.get("name:en")
            if not name:
                continue
            pois.append({"id": f"osm-{record['type']}-{record['id']}", "name": name, "category": category,
                         "distance_m": _distance_meters(latitude, longitude, point["lat"], point["lon"]),
                         "latitude": point["lat"], "longitude": point["lon"]})
        unique = {poi["id"]: poi for poi in pois}
        return sorted(unique.values(), key=lambda poi: poi["distance_m"])[:MAX_FACILITIES], "Live OpenStreetMap facility dataset via Overpass"
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        logger.warning("Overpass facility extraction failed: %s", exc)
        return [], "OpenStreetMap facility dataset temporarily unavailable"


def build_proximity(pois: list[dict[str, Any]], source: str) -> dict[str, Any]:
    categories = ("transit", "school", "emergency", "grocery", "eateries", "nightlife", "road")
    nearest = {kind: next((poi for poi in pois if poi["category"] == kind), None) for kind in categories}
    counts = {kind: sum(poi["category"] == kind for poi in pois) for kind in categories}
    traffic = min(95, 35 + counts["school"] * 10 + counts["transit"] * 8 + counts["road"] * 6)
    noise = min(88, 40 + counts["nightlife"] * 9 + counts["road"] * 5 + counts["eateries"] * 2)
    return {"radius_meters": SEARCH_RADIUS_METERS, "source": source, "pois": pois, "poi_counts": counts, "nearest": nearest,
            "traffic_index": traffic, "night_decibels": noise,
            "transit_walk_minutes": max(2, round((nearest["transit"] or {"distance_m": 900})["distance_m"] / 80))}


async def run_simulation(request: SimulationRequest) -> SimulationResponse:
    if request.coordinates:
        latitude, longitude = request.coordinates.latitude, request.coordinates.longitude
        display_name = request.resolved_name or request.target_location
    else:
        display_name, latitude, longitude = await geocode_location(request.target_location)
    pois, source = await live_poi_infrastructure(latitude, longitude)
    proximity = build_proximity(pois, source)
    agent_payload = {"target_location": display_name, "coordinates": {"latitude": latitude, "longitude": longitude}, "run_type": request.run_type, **proximity}
    commuter, sleeper, source_mode = await run_personas(agent_payload)
    timeline = sorted(commuter["events"] + sleeper["events"], key=lambda event: (event["hour"], event["minute"], event["source"]))
    score = round((commuter["score"] + sleeper["score"]) / 2)
    summary = f"{commuter['assessment']} {sleeper['assessment']}"
    run_id = await asyncio.to_thread(persist_simulation, query=request.target_location, display_name=display_name, latitude=latitude, longitude=longitude, run_type=request.run_type, model_name=MODEL_NAME, agent_source=source_mode, poi_snapshot=proximity, timeline=timeline, summary=summary, score=score)
    return SimulationResponse(run_id=run_id, display_name=display_name, latitude=latitude, longitude=longitude, run_type=request.run_type, source_mode=source_mode, score=score, summary=summary, proximity=proximity, timeline=timeline)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await asyncio.to_thread(initialize_database)
    yield


app = FastAPI(title="NomadNest AI", version="3.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=[], allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:[0-9]+)?$", allow_credentials=False, allow_methods=["GET", "POST"], allow_headers=["Content-Type"])


@app.get("/", include_in_schema=False)
async def home() -> FileResponse:
    return FileResponse(BASE_DIR / "templates" / "index.html")


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "healthy", "mapping": "Leaflet + OpenStreetMap", "model": MODEL_NAME}


@app.post("/api/simulate", response_model=SimulationResponse)
async def simulate(request: SimulationRequest) -> SimulationResponse:
    try:
        return await run_simulation(request)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Simulation failed")
        raise HTTPException(status_code=500, detail="Unable to simulate this neighborhood right now.") from exc
