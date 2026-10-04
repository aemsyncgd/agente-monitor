import pytest
from fastapi.testclient import TestClient
from src.api.main import app

client = TestClient(app)

def test_backbone_route():
    response = client.get("/backbone")
    assert response.status_code == 200
    assert "VIDANET" in response.text
    assert "/api/v1/metrics/mikrotik/interfaces" in response.text

def test_olts_route():
    response = client.get("/olts")
    assert response.status_code == 200
    assert "VIDANET" in response.text
    assert "dyingGasp" in response.text
    assert "LOS" in response.text
