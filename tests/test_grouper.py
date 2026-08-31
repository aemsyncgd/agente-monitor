# tests/test_grouper.py
import os
from src.grouper import ZoneGrouper
from src.config import Config, CsvConfig, MlConfig

CSV_PATH = "/home/server/agente-monitor/grid_servicios.csv"

def create_test_grouper():
    config = Config()
    config.csv = CsvConfig(path=CSV_PATH)
    config.ml = MlConfig(model_path="/tmp/test_model.pkl", min_samples=2, eps=0.3)
    return ZoneGrouper(config)

def test_load_csv():
    grouper = create_test_grouper()
    clients = grouper.load_csv(CSV_PATH)
    
    assert len(clients) > 100
    assert clients[0].nombre != ""
    assert clients[0].serial_onu != ""
    assert clients[0].nodo != ""  # Nodo del CSV (columna Router)

def test_fit_model():
    grouper = create_test_grouper()
    clients = grouper.load_csv(CSV_PATH)
    addresses = [c.direccion for c in clients[:100]]
    
    grouper.fit(addresses)
    assert grouper.model_fitted is True

def test_predict_zone():
    grouper = create_test_grouper()
    clients = grouper.load_csv(CSV_PATH)
    addresses = [c.direccion for c in clients[:100]]
    
    grouper.fit(addresses)
    
    zone = grouper.predict_zone("Sector Los Naranjillos, calle Plaza, casa nro 2")
    assert zone is not None

def test_get_clients_by_zone():
    grouper = create_test_grouper()
    clients = grouper.load_csv(CSV_PATH)
    addresses = [c.direccion for c in clients[:100]]
    
    grouper.fit(addresses)
    
    for client in clients[:100]:
        client.zona = grouper.predict_zone(client.direccion)
    
    zones = grouper.get_clients_by_zone(clients[:100])
    assert len(zones) > 0
