# src/notifier.py
import time
import logging
from typing import List, Dict
from .models import Alert, Client, FaultVerification
from .config import Config

logger = logging.getLogger(__name__)

class TelegramNotifier:
    def __init__(self, config: Config):
        self.config = config
        self._last_sent: Dict[str, float] = {}
        self._bot = None
    
    def _get_bot(self):
        if self._bot is None:
            try:
                import telegram
                self._bot = telegram.Bot(token=self.config.telegram.bot_token)
            except ImportError:
                logger.error("python-telegram-bot not installed")
                return None
        return self._bot
    
    def format_message(self, zona: str, clientes: List[Client], 
                       verification: FaultVerification) -> str:
        lines = [
            f"🚨 Posible fallo de caja NAP en {zona}",
            "",
            f"Clientes afectados: {len(clientes)}"
        ]
        
        for c in clientes[:10]:
            lines.append(f"- {c.nombre} | {c.direccion} | {c.serial_onu} | {c.nodo}")
        
        if len(clientes) > 10:
            lines.append(f"... y {len(clientes) - 10} más")
        
        lines.append("")
        
        if verification.ping_result:
            status = "✅" if verification.ping_result.success else "❌"
            lines.append(f"{status} Ping: {'OK' if verification.ping_result.success else 'Falló'}")
        
        if verification.snmp_result and verification.snmp_result.success:
            lines.append(f"✅ SNMP: OK")
        
        if verification.ssh_result and verification.ssh_result.success:
            lines.append(f"✅ SSH: OK")
        
        lines.append(f"\nHora: {time.strftime('%H:%M')}")
        
        return "\n".join(lines)
    
    def send_alert(self, zona: str, clientes: List[Client], 
                   verification: FaultVerification) -> bool:
        cooldown_seconds = self.config.telegram.cooldown_minutes * 60
        
        if zona in self._last_sent:
            elapsed = time.time() - self._last_sent[zona]
            if elapsed < cooldown_seconds:
                logger.debug(f"Cooldown active for {zona}: {int(cooldown_seconds - elapsed)}s remaining")
                return False
        
        message = self.format_message(zona, clientes, verification)
        
        try:
            import requests
            
            url = f"https://api.telegram.org/bot{self.config.telegram.bot_token}/sendMessage"
            payload = {
                "chat_id": self.config.telegram.chat_id,
                "text": message,
                "disable_web_page_preview": True
            }
            
            resp = requests.post(url, json=payload, timeout=30)
            resp.raise_for_status()
            
            self._last_sent[zona] = time.time()
            logger.info(f"Alert sent for zone: {zona}")
            return True
        
        except Exception as e:
            logger.error(f"Failed to send Telegram alert: {e}")
            return False
    
    def send_daily_summary(self, stats: Dict) -> bool:
        message = (
            f"📊 Resumen Diario - {time.strftime('%d/%m/%Y')}\n\n"
            f"Total clientes: {stats.get('total', 0)}\n"
            f"Zonas activas: {stats.get('zones', 0)}\n"
            f"Alertas hoy: {stats.get('alerts', 0)}\n"
            f"\nHora: {time.strftime('%H:%M')}"
        )
        
        try:
            import requests
            
            url = f"https://api.telegram.org/bot{self.config.telegram.bot_token}/sendMessage"
            payload = {
                "chat_id": self.config.telegram.chat_id,
                "text": message,
                "disable_web_page_preview": True
            }
            
            resp = requests.post(url, json=payload, timeout=30)
            resp.raise_for_status()
            
            return True
        
        except Exception as e:
            logger.error(f"Failed to send daily summary: {e}")
            return False
