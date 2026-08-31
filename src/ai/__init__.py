# src/ai/__init__.py
# Lazy imports to avoid torch dependency on import time
# Import directly when needed: from src.ai.autoencoder import OpticalPowerAutoencoder

def __getattr__(name):
    if name == "OpticalPowerAutoencoder":
        from .autoencoder import OpticalPowerAutoencoder
        return OpticalPowerAutoencoder
    elif name == "ModelTrainer":
        from .trainer import ModelTrainer
        return ModelTrainer
    elif name == "AnomalyDetector":
        from .detector import AnomalyDetector
        return AnomalyDetector
    elif name == "AnomalyResult":
        from .detector import AnomalyResult
        return AnomalyResult
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "OpticalPowerAutoencoder",
    "ModelTrainer",
    "AnomalyDetector",
    "AnomalyResult",
]
