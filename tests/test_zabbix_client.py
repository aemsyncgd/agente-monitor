# tests/test_zabbix_client.py
import responses
from src.zabbix_client import ZabbixClient

ZABBIX_URL = "http://test-zabbix/api_jsonrpc.php"

@responses.activate
def test_login_success():
    responses.post(ZABBIX_URL, json={
        "jsonrpc": "2.0",
        "result": "test_auth_token_123",
        "id": 1
    })
    
    client = ZabbixClient(ZABBIX_URL, "user", "pass")
    token = client.login()
    
    assert token == "test_auth_token_123"
    assert client.auth == "test_auth_token_123"

@responses.activate
def test_login_failure():
    responses.post(ZABBIX_URL, json={
        "jsonrpc": "2.0",
        "error": {"code": -32602, "message": "Invalid params", "data": "Login failed"},
        "id": 1
    })
    
    client = ZabbixClient(ZABBIX_URL, "user", "wrong_pass")
    token = client.login()
    
    assert token is None

@responses.activate
def test_get_problems():
    responses.post(ZABBIX_URL, json={
        "jsonrpc": "2.0",
        "result": "test_token",
        "id": 1
    })
    responses.post(ZABBIX_URL, json={
        "jsonrpc": "2.0",
        "result": [
            {"eventid": "1001", "name": "LOS detected", "severity": "3", "clock": "1689000000"}
        ],
        "id": 2
    })
    
    client = ZabbixClient(ZABBIX_URL, "user", "pass")
    client.login()
    problems = client.get_problems(time_from=1688999000)
    
    assert len(problems) == 1
    assert problems[0]["name"] == "LOS detected"
