from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import HTTPException

from web_app.main import capabilities, dispense, get_overrides, runtime, state_endpoint, stop_dispense
from web_app.models import DispenseRequest


def response_json(response):
    return json.loads(response.body)


def test_state_and_capabilities_are_semantic():
    runtime.config = {"available": True, "max_remote_volume_ml": 1500,
                      "override_api": True}
    state = response_json(asyncio.run(state_endpoint()))
    caps = capabilities()

    assert state["schema_version"] == 2
    assert caps["dispense"]["max_ml"] == 1500
    assert caps["dispense"]["temperatures"] == [25, 45, 55, 80, 90, 95]
    assert caps["dispense"]["flow_control"] is False
    assert caps["overrides"]["volume"] is True


def test_dispense_is_validated_and_forwards_semantically(monkeypatch):
    calls = []

    async def fake_esp(method, path, body=None, mutation=False):
        calls.append((method, path, body, mutation))
        return {"ok": True, "result": "OK"}

    monkeypatch.setattr(runtime, "esp", fake_esp)
    runtime.config = {"available": True, "max_remote_volume_ml": 900}
    response = asyncio.run(dispense(DispenseRequest(ml=350, temperature_c=90)))

    assert response_json(response)["result"] == "OK"
    assert calls[-1] == ("POST", "/api/dispense", {"ml": 350, "temperature_c": 90}, True)
    with pytest.raises(ValueError):
        DispenseRequest(ml=350, temperature_c=73)


def test_volume_above_reported_capability_is_rejected():
    runtime.config = {"available": True, "max_remote_volume_ml": 450}
    with pytest.raises(HTTPException) as error:
        asyncio.run(dispense(DispenseRequest(ml=600, temperature_c=25)))
    assert error.value.status_code == 422


def test_offline_firmware_capability_disables_override_mutation():
    runtime.config = {"available": False}
    runtime.esp_reachable = False
    response = response_json(asyncio.run(get_overrides()))
    assert response["available"] is False
    assert response["rules"] == []
    assert "unreachable" in response["reason"]


def test_mega_command_rejection_is_propagated(monkeypatch):
    async def rejected(*_args, **_kwargs):
        raise HTTPException(409, detail={"ok": False, "result": "TANK_EMPTY"})

    monkeypatch.setattr(runtime, "esp", rejected)
    with pytest.raises(HTTPException) as error:
        asyncio.run(stop_dispense())
    assert error.value.status_code == 409
    assert error.value.detail["result"] == "TANK_EMPTY"
