# src/ai/detector.py
import logging
import time
from typing import List, Dict, Optional, Any
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class AnomalyType(Enum):
    NONE = "none"
    GRADUAL_DEGRADATION = "gradual_degradation"
    SUDDEN_DROP = "sudden_drop"
    TOTAL_FAILURE = "total_failure"
    RECOVERY = "recovery"


@dataclass
class AnomalyResult:
    olt_name: str
    pon_port: str
    onu_index: str
    onu_serial: str
    is_anomaly: bool
    anomaly_type: AnomalyType
    confidence: float
    current_power: float
    avg_power: float
    trend: float  # dB per hour
    predicted_time_to_failure: Optional[float] = None  # hours
    message: str = ""
    timestamp: float = field(default_factory=time.time)


class AnomalyDetector:
    def __init__(self, model, trainer, influx_client):
        self.model = model
        self.trainer = trainer
        self.influx = influx_client
        self._anomaly_cache: Dict[str, AnomalyResult] = {}
    
    def detect(self, olt_name: str, pon_port: str, 
               onu_index: str, onu_serial: str,
               power_history: List[float],
               current_power: float) -> AnomalyResult:
        """
        Detect anomalies in optical power data.
        
        Args:
            olt_name: OLT hostname
            pon_port: PON port (e.g., "GPON0/1")
            onu_index: ONU index (e.g., "1_32")
            onu_serial: ONU serial number
            power_history: Historical power values (dBm)
            current_power: Current power value (dBm)
            
        Returns:
            AnomalyResult with detection results
        """
        # Default result (no anomaly)
        result = AnomalyResult(
            olt_name=olt_name,
            pon_port=pon_port,
            onu_index=onu_index,
            onu_serial=onu_serial,
            is_anomaly=False,
            anomaly_type=AnomalyType.NONE,
            confidence=0.0,
            current_power=current_power,
            avg_power=sum(power_history) / len(power_history) if power_history else current_power,
            trend=0.0
        )
        
        if not power_history or len(power_history) < 10:
            return result
        
        # Calculate statistics
        avg_power = sum(power_history) / len(power_history)
        min_power = min(power_history)
        max_power = max(power_history)
        
        # Calculate trend (dB per hour, assuming 5-min intervals)
        if len(power_history) >= 2:
            recent = power_history[-12:]  # Last hour
            older = power_history[-24:-12] if len(power_history) >= 24 else power_history[:len(power_history)//2]
            
            recent_avg = sum(recent) / len(recent)
            older_avg = sum(older) / len(older)
            
            trend = (recent_avg - older_avg) / (len(recent) * 5 / 60)  # dB per hour
        else:
            trend = 0.0
        
        result.avg_power = avg_power
        result.trend = trend
        
        # 1. Check for total failure (power below -30 dBm)
        if current_power < -30:
            result.is_anomaly = True
            result.anomaly_type = AnomalyType.TOTAL_FAILURE
            result.confidence = 1.0
            result.message = f"ONU {onu_serial} falló completamente (power={current_power:.1f} dBm)"
            return result
        
        # 2. Check for sudden drop (more than 5 dB in last reading)
        if len(power_history) >= 2:
            last_drop = power_history[-2] - current_power
            if last_drop > 5:
                result.is_anomaly = True
                result.anomaly_type = AnomalyType.SUDDEN_DROP
                result.confidence = min(1.0, last_drop / 10)
                result.message = f"ONU {onu_serial}: Caída súbita de {last_drop:.1f} dBm"
                return result
        
        # 3. Check for gradual degradation
        if trend < -0.5:  # Losing more than 0.5 dB per hour
            result.is_anomaly = True
            result.anomaly_type = AnomalyType.GRADUAL_DEGRADATION
            
            # Calculate confidence based on trend severity
            result.confidence = min(1.0, abs(trend) / 2.0)
            
            # Estimate time to failure (assuming failure at -30 dBm)
            if current_power > -30 and trend < 0:
                hours_to_failure = (current_power - (-30)) / abs(trend)
                result.predicted_time_to_failure = hours_to_failure
                result.message = (f"ONU {onu_serial}: Degradación gradual "
                                f"({trend:.2f} dB/h). "
                                f"Falla estimada en ~{hours_to_failure:.0f}h")
            else:
                result.message = f"ONU {onu_serial}: Degradación gradual ({trend:.2f} dB/h)"
            
            return result
        
        # 4. Check for recovery
        if trend > 0.5 and min_power < -25:
            result.is_anomaly = True
            result.anomaly_type = AnomalyType.RECOVERY
            result.confidence = min(1.0, trend / 2.0)
            result.message = f"ONU {onu_serial}: Recuperación detectada (+{trend:.2f} dB/h)"
            return result
        
        # 5. Check against threshold using model
        if self.model and len(power_history) >= 1440:  # Need 24h of data
            try:
                import torch
                
                # Prepare data
                recent_data = power_history[-1440:]  # Last 24h
                normalized = self.trainer.normalize(recent_data)
                tensor = torch.FloatTensor(normalized).unsqueeze(0).unsqueeze(-1)
                
                # Get model prediction
                prediction = self.model.predict(tensor, threshold=self.trainer.load_threshold())
                
                if prediction["is_anomaly"][0]:
                    result.is_anomaly = True
                    result.anomaly_type = AnomalyType.GRADUAL_DEGRADATION
                    result.confidence = float(prediction["confidence"][0])
                    result.message = (f"ONU {onu_serial}: Anomalía IA detectada "
                                    f"(error={prediction['reconstruction_error'][0]:.4f})")
                    return result
                    
            except Exception as e:
                logger.debug(f"Model prediction failed: {e}")
        
        return result
    
    def detect_batch(self, measurements: List[Dict[str, Any]]) -> List[AnomalyResult]:
        """
        Detect anomalies for multiple ONUs.
        
        Args:
            measurements: List of dicts with olt_name, pon_port, onu_index,
                         onu_serial, power_history, current_power
                         
        Returns:
            List of AnomalyResult
        """
        results = []
        for m in measurements:
            result = self.detect(
                olt_name=m["olt_name"],
                pon_port=m["pon_port"],
                onu_index=m["onu_index"],
                onu_serial=m["onu_serial"],
                power_history=m.get("power_history", []),
                current_power=m["current_power"]
            )
            if result.is_anomaly:
                results.append(result)
        
        return results
    
    def get_trend_analysis(self, power_history: List[float]) -> Dict[str, Any]:
        """
        Analyze trend in power history.
        
        Returns:
            Dict with trend analysis
        """
        if not power_history or len(power_history) < 2:
            return {"trend": 0, "stable": True}
        
        # Linear regression
        n = len(power_history)
        x = list(range(n))
        x_mean = sum(x) / n
        y_mean = sum(power_history) / n
        
        numerator = sum((xi - x_mean) * (yi - y_mean) 
                       for xi, yi in zip(x, power_history))
        denominator = sum((xi - x_mean) ** 2 for xi in x)
        
        if denominator == 0:
            slope = 0
        else:
            slope = numerator / denominator
        
        # Convert to dB per hour (assuming 5-min intervals)
        db_per_hour = slope * 12  # 12 readings per hour
        
        return {
            "slope": slope,
            "db_per_hour": db_per_hour,
            "stable": abs(db_per_hour) < 0.1,
            "degrading": db_per_hour < -0.1,
            "improving": db_per_hour > 0.1
        }
