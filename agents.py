"""Concurrent GPT-5.6 persona orchestration for a NomadNest simulation."""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from openai import AsyncOpenAI

MODEL_NAME = os.getenv("OPENAI_MODEL", "gpt-5.6")
COMMON_SCHEMA = """Return only JSON: {"assessment": string, "score": integer 0-100, "events": [{"hour": integer 0-23, "minute": integer 0-59, "title": string, "detail": string, "severity": "low"|"medium"|"high", "source": string}]}. Provide four to six exact-time incidents."""
COMMUTER_PROMPT = f"""You are Persona A: The 9-to-5 Commuter. The input contains a raw, live OpenStreetMap facility array filtered to one kilometre around a geocoded target. Ground every infrastructure claim in that array; name concrete nearby facilities. Evaluate transit, schools/drop-off friction, groceries, emergency services, commute traffic, and lunch-time movement. {COMMON_SCHEMA} Every event source must be "Commuter"."""
SLEEPER_PROMPT = f"""You are Persona B: The Light Sleeper. The input contains a raw, live OpenStreetMap facility array filtered to one kilometre around a geocoded target. Ground every infrastructure claim in that array; name concrete nearby facilities. Evaluate nightlife, eateries, roads, emergency-service activity and quiet hours, with a specific 2:00 AM risk assessment. {COMMON_SCHEMA} Every event source must be "Light Sleeper"."""


def _event(hour: int, minute: int, title: str, detail: str, severity: str, source: str) -> dict[str, Any]:
    return {"hour": hour, "minute": minute, "title": title, "detail": detail, "severity": severity, "source": source}


def _local_persona(snapshot: dict[str, Any], source: str) -> dict[str, Any]:
    pois = snapshot["pois"]
    first = lambda kind, fallback: next((p["name"] for p in pois if p["category"] == kind), fallback)
    counts = snapshot["poi_counts"]
    if source == "Commuter":
        transit, school, grocery = first("transit", "local transit"), first("school", "nearby schools"), first("grocery", "local grocers")
        score = max(25, min(94, 88 - snapshot["traffic_index"] // 2 - counts["school"] * 2))
        return {"assessment": f"Commuter fit is {score}/100. {transit}, {school}, and {grocery} shape the highest daytime movement periods.", "score": score, "events": [_event(7,30,"Traffic congestion pulse",f"School and transit movement around {school} creates a {snapshot['traffic_index']}/100 road-pressure estimate.","high" if snapshot["traffic_index"] > 65 else "medium",source),_event(8,10,"Transit departure window",f"{transit} is a primary morning departure anchor within the 1 km layer.","low",source),_event(12,20,"Lunch movement wave",f"Footfall rises around {grocery} and nearby food services.","medium",source),_event(17,45,"Homebound friction", "Evening return traffic combines with curbside pickup activity.","medium",source)]}
    night, road, emergency = first("nightlife", "late venues"), first("road", "nearby arterial roads"), first("emergency", "emergency services")
    score = max(20, min(94, 92 - counts["nightlife"] * 5 - max(0, snapshot["night_decibels"] - 45)))
    return {"assessment": f"Light-sleeper fit is {score}/100. {night}, {road}, and {emergency} are the principal after-dark activity signals.", "score": score, "events": [_event(20,30,"Evening venue activation",f"Activity around {night} begins to increase local pickup and pedestrian noise.","medium",source),_event(23,45,"Nightlife audio spike",f"Late departures near {night} can spill toward {road}.","high" if counts["nightlife"] > 3 else "medium",source),_event(2,0,"2:00 AM ambient check",f"Modeled overnight ambience is {snapshot['night_decibels']} dB; emergency access remains associated with {emergency}.","high" if snapshot["night_decibels"] >= 60 else "medium",source),_event(5,15,"Quiet recovery window","Venue activity contracts before early deliveries and commuting resume.","low",source)]}


def _parse(text: str, expected_source: str) -> dict[str, Any]:
    value = json.loads(text.strip().removeprefix("```json").removesuffix("```").strip())
    if not isinstance(value.get("score"), int) or not isinstance(value.get("events"), list):
        raise ValueError("Invalid persona response schema")
    for event in value["events"]:
        event["hour"] = max(0, min(23, int(event["hour"])))
        event["minute"] = max(0, min(59, int(event.get("minute", 0))))
        event["source"] = expected_source
        event["severity"] = event.get("severity") if event.get("severity") in {"low", "medium", "high"} else "medium"
    value["score"] = max(0, min(100, value["score"]))
    return value


async def _ask(client: AsyncOpenAI, instructions: str, snapshot: dict[str, Any], source: str) -> dict[str, Any]:
    response = await client.responses.create(model=MODEL_NAME, instructions=instructions, input=json.dumps(snapshot))
    return _parse(response.output_text, source)


async def run_personas(snapshot: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], str]:
    if not os.getenv("OPENAI_API_KEY"):
        return _local_persona(snapshot, "Commuter"), _local_persona(snapshot, "Light Sleeper"), "local-fallback"
    try:
        client = AsyncOpenAI()
        commuter, sleeper = await asyncio.gather(_ask(client, COMMUTER_PROMPT, snapshot, "Commuter"), _ask(client, SLEEPER_PROMPT, snapshot, "Light Sleeper"))
        return commuter, sleeper, "openai"
    except Exception:
        return _local_persona(snapshot, "Commuter"), _local_persona(snapshot, "Light Sleeper"), "local-fallback"
