# src/grouper.py
import os
import pickle
import logging
from typing import List, Dict
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.cluster import DBSCAN
from .models import Client
from .config import Config

logger = logging.getLogger(__name__)


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
