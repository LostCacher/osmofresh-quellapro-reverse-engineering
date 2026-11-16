from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import secrets
import time
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .models import CONFIRMED_TEMPERATURES, DispenseRequest, OverrideRequest, PresetRequest, TankDelayRequest
from .state import build_state, read_json


ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
STATE_PATH = Path(os.getenv("QUELLA_STATE_PATH", str(ROOT / "data/live_state.json")))
ESP_URL = os.getenv("QUELLA_ESP_URL", "http://10.0.1.27").rstrip("/")
ESP_API_KEY = os.getenv("QUELLA_ESP_API_KEY", "")
VM_URL = os.getenv("QUELLA_VM_URL", "http://victoriametrics:8428").rstrip("/")
PUBLIC_ORIGIN = os.getenv("QUELLA_PUBLIC_ORIGIN", "https://osmofresh-quella.server.home").rstrip("/")
STALE_SECONDS = float(os.getenv("QUELLA_ONLINE_SECONDS", "5"))


class Runtime:
    def __init__(self) -> None:
        self.esp_status: dict[str, Any] = {}
        self.esp_health: dict[str, Any] = {}
        self.config: dict[str, Any] = {}
        self.overrides: list[dict[str, Any]] = []
        self.esp_last_ok: float | None = None
        self.esp_reachable = False
        self.esp_error: str | None = None
        self.command_lock = asyncio.Lock()
        # ESP8266WebServer serves one HTTP client at a time.  Serialize every
        # request (polling, page bootstrap and commands) so a slow Mega
        # transaction cannot be interrupted by another REST request.
        self.esp_lock = asyncio.Lock()
        self.client: httpx.AsyncClient | None = None

    async def esp_request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if self.client is None:
            raise httpx.ConnectError("client not initialized")
        headers = dict(kwargs.pop("headers", {}))
        # ESP8266WebServer closes connections after replies.  Explicitly avoid
        # reusing a socket which the device has already closed; otherwise
        # httpx reports RemoteProtocolError on the next request.
        headers["Connection"] = "close"
        async with self.esp_lock:
            return await self.client.request(
                method, ESP_URL + path, headers=headers, **kwargs
            )

    async def esp(self, method: str, path: str, body: dict | None = None, *, mutation: bool = False) -> dict:
        if self.client is None:
            raise HTTPException(503, "ESP client unavailable")
        headers = {"Accept": "application/json"}
        if mutation:
            if not ESP_API_KEY:
                raise HTTPException(503, "ESP API key is not configured on the server")
            headers["X-API-Key"] = ESP_API_KEY
        try:
            response = await self.esp_request(method, path, json=body, headers=headers)
        except httpx.HTTPError as exc:
            self.esp_reachable = False
            self.esp_error = f"{exc.__class__.__name__}: {exc}"
            raise HTTPException(503, f"ESP unavailable: {exc.__class__.__name__}") from exc
        try:
            payload = response.json()
        except ValueError:
            payload = {"error": "invalid ESP response"}
        if response.status_code >= 400:
            raise HTTPException(response.status_code, detail=payload)
        self.esp_reachable = True
        self.esp_error = None
        self.esp_last_ok = time.time()
        return payload

runtime = Runtime()


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Private ESP/VictoriaMetrics addresses must be contacted directly. Do not
    # inherit a host/container HTTP_PROXY that could turn a reachable LAN
    # endpoint into a false gateway-offline state.
    runtime.client = httpx.AsyncClient(
        timeout=httpx.Timeout(2.5, connect=1.0),
        limits=httpx.Limits(max_keepalive_connections=0),
        trust_env=False,
    )
    yield
    if runtime.client:
        await runtime.client.aclose()


app = FastAPI(title="Quella Pro", version="1.0.0", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def no_store(payload: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code, headers={"Cache-Control": "no-store"})


def current_state() -> dict:
    raw = read_json(STATE_PATH)
    receiver = raw.get("receiver", {}) if isinstance(raw, dict) else {}
    last_packet = receiver.get("last_packet_at")
    age = None if last_packet is None else max(0.0, time.time() - float(last_packet))
    # QT telemetry can only be produced when the Mega sends raw batches to the
    # ESP and the ESP forwards them to this host. A fresh JSON snapshot is
    # therefore the authoritative liveness source for both links. Live values
    # never depend on ESP REST polling.
    telemetry_online = age is not None and age <= STALE_SECONDS
    telemetry_health = {
        "_gateway_reachable": telemetry_online,
        "mega_link": telemetry_online,
        "mega_last_seen_ms": None if age is None else int(age * 1000),
        "source": "QT_UDP_JSON",
    }
    return build_state(raw, {}, telemetry_health,
                       telemetry_stale_seconds=STALE_SECONDS)


async def csrf_guard(request: Request, x_csrf_token: str | None = Header(None),
                     quella_csrf: str | None = Cookie(None)) -> None:
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != PUBLIC_ORIGIN:
        raise HTTPException(403, "Origin rejected")
    if not x_csrf_token or not quella_csrf or not secrets.compare_digest(x_csrf_token, quella_csrf):
        raise HTTPException(403, "CSRF validation failed")


def capabilities(config: dict | None = None) -> dict:
    config = config or runtime.config
    maximum = int(config.get("max_remote_volume_ml", 1500)) if config.get("available") else 1500
    return {
        "dispense": {"min_ml": 1, "max_ml": maximum,
                     "temperatures": list(CONFIRMED_TEMPERATURES), "flow_control": False},
        "overrides": {"volume": bool(config.get("override_api", False)),
                      "temperature": False, "flow": False},
        "limits_source": "MEGA_CONFIG" if config.get("available") else "AUDITED_PROTOCOL_FALLBACK",
    }


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json",
                        headers={"Cache-Control": "no-cache"})


@app.get("/sw.js")
async def service_worker():
    return FileResponse(STATIC / "sw.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})


@app.get("/state")
@app.get("/api/state")
async def state_endpoint():
    return no_store(current_state())


@app.get("/api/session")
async def session():
    token = secrets.token_urlsafe(32)
    response = no_store({"csrf_token": token})
    response.set_cookie("quella_csrf", token, secure=PUBLIC_ORIGIN.startswith("https://"),
                        httponly=False, samesite="strict", path="/")
    return response


@app.get("/api/health")
async def health():
    state = current_state()
    ok = state["connectivity"]["telemetry"]["online"]
    return no_store({"ok": ok, "connectivity": state["connectivity"],
                     "esp_last_ok": runtime.esp_last_ok}, 200 if ok else 503)


@app.get("/api/config")
async def get_config():
    try:
        runtime.config = await runtime.esp("GET", "/api/config")
    except HTTPException:
        if not runtime.config:
            runtime.config = {"available": False, "presets": []}
    return no_store(runtime.config)


@app.get("/api/capabilities")
async def get_capabilities():
    if not runtime.config or not runtime.config.get("available"):
        try:
            runtime.config = await runtime.esp("GET", "/api/config")
        except HTTPException:
            pass
    return no_store(capabilities())


@app.post("/api/dispense", dependencies=[Depends(csrf_guard)])
async def dispense(command: DispenseRequest):
    caps = capabilities()
    if not caps["dispense"]["min_ml"] <= command.ml <= caps["dispense"]["max_ml"]:
        raise HTTPException(422, "volume outside Mega capability")
    async with runtime.command_lock:
        return no_store(await runtime.esp("POST", "/api/dispense", command.model_dump(), mutation=True))


@app.post("/api/dispense/stop", dependencies=[Depends(csrf_guard)])
async def stop_dispense():
    async with runtime.command_lock:
        return no_store(await runtime.esp("POST", "/api/dispense/stop", {}, mutation=True))


@app.post("/api/tank-delay", dependencies=[Depends(csrf_guard)])
async def tank_delay(command: TankDelayRequest):
    async with runtime.command_lock:
        result = await runtime.esp("POST", "/api/tank-delay", command.model_dump(), mutation=True)
        runtime.config = {}
        return no_store(result)


@app.post("/api/preset", dependencies=[Depends(csrf_guard)])
async def preset(command: PresetRequest):
    caps = capabilities()
    if command.ml > caps["dispense"]["max_ml"]:
        raise HTTPException(422, "volume outside Mega capability")
    async with runtime.command_lock:
        result = await runtime.esp("POST", "/api/preset", command.model_dump(), mutation=True)
        runtime.config = {}
        return no_store(result)


@app.get("/api/overrides")
async def get_overrides():
    if not runtime.config or not runtime.config.get("available"):
        try:
            runtime.config = await runtime.esp("GET", "/api/config")
        except HTTPException:
            pass
    if not capabilities()["overrides"]["volume"]:
        connectivity = current_state()["connectivity"]
        if runtime.esp_error:
            reason = f"ESP REST API error: {runtime.esp_error}"
        elif not connectivity["gateway"]["online"]:
            reason = f"ESP REST API unreachable at {ESP_URL}"
        elif not connectivity["mega"]["online"]:
            reason = "ESP is online, but the Mega QG link is offline"
        elif runtime.config.get("available") and int(runtime.config.get("version", 0)) < 2:
            reason = "Mega firmware config schema is older than v2; flash the updated Mega firmware"
        else:
            reason = "Installed Mega/ESP firmware does not advertise semantic override API"
        return no_store({"available": False, "reason": reason, "rules": []})
    result = await runtime.esp("GET", "/api/overrides")
    runtime.overrides = result.get("rules", [])
    return no_store(result)


@app.post("/api/overrides", dependencies=[Depends(csrf_guard)])
async def set_override(rule: OverrideRequest):
    if not capabilities()["overrides"]["volume"]:
        raise HTTPException(501, "Semantic override API is not available in installed firmware")
    async with runtime.command_lock:
        return no_store(await runtime.esp("POST", "/api/overrides", rule.model_dump(), mutation=True))


@app.delete("/api/overrides/{rule_id}", dependencies=[Depends(csrf_guard)])
async def delete_override(rule_id: int):
    if not 0 <= rule_id <= 7:
        raise HTTPException(422, "invalid override id")
    async with runtime.command_lock:
        return no_store(await runtime.esp("POST", "/api/overrides/delete", {"id": rule_id}, mutation=True))


HISTORY_FIELDS = {
    "inlet_temperature_c": 'osmofresh_quella_field_value{field="inlet_temperature_c"}',
    "outlet_temperature_c": 'osmofresh_quella_field_value{field="outlet_temperature_c"}',
    "flow_ml_min": 'osmofresh_quella_field_value{field="derived_flow_rate_ml_min"}',
    "current_ml": 'osmofresh_quella_field_value{field="current_dispensed_volume_ml"}',
    "tds": 'osmofresh_quella_field_value{field="tds_display"}',
    "pump": 'osmofresh_quella_field_value{field="feed_pump_active"}',
    "fill": 'osmofresh_quella_field_value{field="booster_fill_active"}',
    "operating_mode": "osmofresh_quella_operating_mode_code",
}


@app.get("/api/history")
async def history(field: str, range_seconds: int = 3600):
    if field not in HISTORY_FIELDS or range_seconds not in (900, 3600, 21600, 86400):
        raise HTTPException(422, "unsupported history query")
    if runtime.client is None:
        raise HTTPException(503, "history client unavailable")
    end = time.time()
    try:
        response = await runtime.client.get(VM_URL + "/api/v1/query_range", params={
            "query": HISTORY_FIELDS[field], "start": end - range_seconds,
            "end": end, "step": max(1, range_seconds // 600),
        })
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(503, "history unavailable") from exc
    return no_store(data)


@app.websocket("/ws")
async def websocket_state(socket: WebSocket):
    origin = socket.headers.get("origin")
    if origin and origin.rstrip("/") != PUBLIC_ORIGIN:
        await socket.close(code=1008)
        return
    await socket.accept()
    try:
        while True:
            state = current_state()
            await socket.send_json({"type": "state", "data": state})
            await asyncio.sleep(0.2 if state["machine"]["mode"] == "DISPENSING" else 0.75)
    except WebSocketDisconnect:
        return
