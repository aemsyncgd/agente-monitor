# src/grouper.py
import os
import csv
import pickle
import logging
from typing import List, Dict, Optional
import pandas as pd
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.cluster import DBSCAN
from .models import Client
from .config import Config

logger = logging.getLogger(__name__)

EXPECTED_CSV_COLS = 15
# Last 9 columns (Router..Fecha Instalacion) never contain commas — used as anchor
_CSV_TAIL_COUNT = 9
# First 6 columns (Documento..Direccion Servicio) — left anchor
_CSV_HEAD_COUNT = 6


def _parse_csv_robust(path: str) -> pd.DataFrame:
    """Parse grid_servicios.csv handling unquoted commas in Nombre Cliente
    and Direccion Servicio fields.

    Strategy: anchor from the RIGHT (last 9 cols are structurally stable)
    and LEFT (first 5 cols are clean), merge any extra middle fields into
    Direccion Servicio (col index 5).
    """
    rows: list[list[str]] = []
    header: list[str] = []

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        reader = csv.reader(fh)
        header = [h.strip() for h in next(reader)]

        for line_num, raw in enumerate(reader, start=2):
            n = len(raw)
            if n == EXPECTED_CSV_COLS:
                rows.append([f.strip() for f in raw])
            elif n > EXPECTED_CSV_COLS:
                tail = raw[-_CSV_TAIL_COUNT:]
                head = raw[:_CSV_HEAD_COUNT]
                middle = raw[_CSV_HEAD_COUNT:-_CSV_TAIL_COUNT]
                if middle:
                    head[5] = head[5] + ", " + ", ".join(middle)
                recovered = head + tail
                if len(recovered) == EXPECTED_CSV_COLS:
                    rows.append([f.strip() for f in recovered])
                else:
                    logger.warning(
                        f"CSV line {line_num}: could not recover "
                        f"({n} fields, recovered {len(recovered)})"
                    )
            else:
                padded = [f.strip() for f in raw] + [""] * (EXPECTED_CSV_COLS - n)
                rows.append(padded[:EXPECTED_CSV_COLS])

    return pd.DataFrame(rows, columns=header)


class ZoneGrouper:
    def __init__(self, config: Config):
        self.config = config
        self.vectorizer = TfidfVectorizer(
            analyzer='char_wb',
            ngram_range=(2, 4),
            max_features=1000
        )
        self.clustering = DBSCAN(
            eps=config.ml.eps,
            min_samples=config.ml.min_samples,
            metric='cosine'
        )
        self.model_fitted = False
        self._label_map: Dict[int, str] = {}

    def load_csv(self, path: str) -> List[Client]:
        df = _parse_csv_robust(path)
        df.columns = df.columns.str.strip()

        clients = []
        for _, row in df.iterrows():
            client = Client(
                nombre=str(row.get('Nombre Cliente', '')),
                serial_onu=str(row.get('Serial Onu', '')),
                olt="",
                nodo=str(row.get('Router', '')),
                puerto_pon=str(row.get('Puerto PON', row.get('Puerto PON ', ''))),
                direccion=str(row.get('Direccion Servicio', '')),
                documento=str(row.get('Documento', '')),
                ip_servicio=str(row.get('Ip Servicio', '')),
                estado=str(row.get('Estado', ''))
            )
            clients.append(client)

        logger.info(f"Loaded {len(clients)} clients from CSV")
        return clients
    
    def fit(self, addresses: List[str]) -> None:
        if len(addresses) < self.config.ml.min_samples:
            logger.warning(f"Not enough addresses ({len(addresses)}) for clustering")
            self.model_fitted = False
            return
        
        X = self.vectorizer.fit_transform(addresses)
        self.clustering.fit(X)
        
        for label in set(self.clustering.labels_):
            if label == -1:
                continue
            mask = self.clustering.labels_ == label
            cluster_addresses = [a for a, m in zip(addresses, mask) if m]
            if cluster_addresses:
                self._label_map[label] = self._extract_zone_from_cluster(cluster_addresses)
        
        self.model_fitted = True
        n_clusters = len(set(self.clustering.labels_)) - (1 if -1 in self.clustering.labels_ else 0)
        logger.info(f"Zone model fitted: {n_clusters} zones identified")
    
    def _extract_zone_from_cluster(self, addresses: List[str]) -> str:
        all_words = []
        for addr in addresses:
            words = addr.split()
            all_words.extend(words[:3])
        
        word_freq = {}
        for word in all_words:
            word_freq[word] = word_freq.get(word, 0) + 1
        
        sorted_words = sorted(word_freq.items(), key=lambda x: x[1], reverse=True)
        zone_name = " ".join([w for w, _ in sorted_words[:3]])
        
        return zone_name
    
    def predict_zone(self, address: str) -> str:
        if not self.model_fitted:
            return self._extract_zone_simple(address)
        
        try:
            X = self.vectorizer.transform([address])
            cluster = self.clustering.fit_predict(X)[0]
            
            if cluster == -1:
                return self._extract_zone_simple(address)
            
            return self._label_map.get(cluster, f"Zona_{cluster}")
        
        except Exception as e:
            logger.warning(f"Zone prediction failed: {e}")
            return self._extract_zone_simple(address)
    
    def _extract_zone_simple(self, address: str) -> str:
        words = address.split()[:3]
        return " ".join(words)
    
    def get_clients_by_zone(self, clients: List[Client]) -> Dict[str, List[Client]]:
        zones: Dict[str, List[Client]] = {}
        for client in clients:
            zone = client.zona or self.predict_zone(client.direccion)
            if zone not in zones:
                zones[zone] = []
            zones[zone].append(client)
        return zones
    
    def save_model(self, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            pickle.dump({
                'vectorizer': self.vectorizer,
                'clustering': self.clustering,
                'label_map': self._label_map
            }, f)
        logger.info(f"Model saved to {path}")
    
    def load_model(self, path: str) -> bool:
        if not os.path.exists(path):
            return False
        
        try:
            with open(path, 'rb') as f:
                data = pickle.load(f)
                self.vectorizer = data['vectorizer']
                self.clustering = data['clustering']
                self._label_map = data['label_map']
                self.model_fitted = True
            logger.info(f"Model loaded from {path}")
            return True
        except Exception as e:
            logger.error(f"Failed to load model: {e}")
            return False
