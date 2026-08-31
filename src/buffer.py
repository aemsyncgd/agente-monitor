# src/buffer.py
import time
import logging
from typing import Dict, List, Tuple
from .models import Event

logger = logging.getLogger(__name__)

class EventBuffer:
    def __init__(self, window_seconds: int = 300):
        self.window_seconds = window_seconds
        self._buffer: Dict[Tuple[str, str], List[Event]] = {}
    
    def _key(self, event: Event) -> Tuple[str, str]:
        return (event.olt_name, event.puerto_pon)
    
    def add_event(self, event: Event) -> None:
        key = self._key(event)
        
        if key not in self._buffer:
            self._buffer[key] = []
        
        for existing in self._buffer[key]:
            if existing.item_id == event.item_id:
                if abs(existing.timestamp - event.timestamp) < 5:
                    return
        
        self._buffer[key].append(event)
        logger.debug(f"Event added: {event.olt_name}:{event.puerto_pon} tipo={event.tipo}")
    
    def get_events(self, olt_name: str, puerto_pon: str) -> List[Event]:
        key = (olt_name, puerto_pon)
        return self._buffer.get(key, [])
    
    def group_by_pon(self) -> Dict[Tuple[str, str], List[Event]]:
        return dict(self._buffer)
    
    def cleanup(self) -> None:
        now = time.time()
        to_remove = []
        
        for key, events in self._buffer.items():
            filtered = [e for e in events if (now - e.timestamp) < self.window_seconds]
            if not filtered:
                to_remove.append(key)
            else:
                self._buffer[key] = filtered
        
        for key in to_remove:
            del self._buffer[key]
        
        logger.debug(f"Buffer cleanup: {len(to_remove)} keys removed")
    
    def get_event_count(self, olt_name: str, puerto_pon: str) -> int:
        return len(self.get_events(olt_name, puerto_pon))
    
    def clear(self) -> None:
        self._buffer.clear()
