# src/ai/trainer.py
import os
import logging
import time
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass
import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class TrainingConfig:
    input_size: int = 1
    hidden_size: int = 64
    num_layers: int = 2
    dropout: float = 0.2
    learning_rate: float = 0.001
    batch_size: int = 32
    epochs: int = 50
    window_size: int = 1440  # 24h in minutes
    validation_split: float = 0.2
    early_stopping_patience: int = 5


class ModelTrainer:
    def __init__(self, config: TrainingConfig, model_path: str):
        self.config = config
        self.model_path = model_path
        self._model = None
        self._scaler_mean = 0.0
        self._scaler_std = 1.0
    
    def train(self, data: List[float], force_retrain: bool = False) -> bool:
        """
        Train the autoencoder model with optical power data.
        
        Args:
            data: List of optical power values (dBm)
            force_retrain: Force retraining even if model exists
            
        Returns:
            True if training succeeded
        """
        try:
            import torch
            import torch.nn as nn
            from torch.utils.data import DataLoader, TensorDataset
            
            from .autoencoder import OpticalPowerAutoencoder
            
            if len(data) < self.config.window_size:
                logger.warning(f"Not enough data: {len(data)} < {self.config.window_size}")
                return False
            
            # Prepare sequences
            sequences = self._create_sequences(data)
            if len(sequences) < 10:
                logger.warning(f"Not enough sequences: {len(sequences)}")
                return False
            
            # Normalize data
            sequences_np = np.array(sequences, dtype=np.float32)
            self._scaler_mean = np.mean(sequences_np)
            self._scaler_std = np.std(sequences_np)
            if self._scaler_std == 0:
                self._scaler_std = 1.0
            sequences_np = (sequences_np - self._scaler_mean) / self._scaler_std
            
            # Split train/val
            split_idx = int(len(sequences_np) * (1 - self.config.validation_split))
            train_data = sequences_np[:split_idx]
            val_data = sequences_np[split_idx:]
            
            # Create tensors
            train_tensor = torch.FloatTensor(train_data).unsqueeze(-1)
            val_tensor = torch.FloatTensor(val_data).unsqueeze(-1)
            
            train_dataset = TensorDataset(train_tensor, train_tensor)
            val_dataset = TensorDataset(val_tensor, val_tensor)
            
            train_loader = DataLoader(
                train_dataset, 
                batch_size=self.config.batch_size,
                shuffle=True
            )
            val_loader = DataLoader(
                val_dataset,
                batch_size=self.config.batch_size,
                shuffle=False
            )
            
            # Create model
            model = OpticalPowerAutoencoder(
                input_size=self.config.input_size,
                hidden_size=self.config.hidden_size,
                num_layers=self.config.num_layers,
                dropout=self.config.dropout
            )
            
            optimizer = torch.optim.Adam(
                model.parameters(),
                lr=self.config.learning_rate
            )
            criterion = nn.MSELoss()
            
            # Training loop
            best_val_loss = float('inf')
            patience_counter = 0
            
            for epoch in range(self.config.epochs):
                # Train
                model.train()
                train_loss = 0.0
                for batch_x, batch_y in train_loader:
                    optimizer.zero_grad()
                    output = model(batch_x)
                    loss = criterion(output, batch_y)
                    loss.backward()
                    optimizer.step()
                    train_loss += loss.item()
                
                train_loss /= len(train_loader)
                
                # Validate
                model.eval()
                val_loss = 0.0
                with torch.no_grad():
                    for batch_x, batch_y in val_loader:
                        output = model(batch_x)
                        loss = criterion(output, batch_y)
                        val_loss += loss.item()
                
                val_loss /= len(val_loader)
                
                # Early stopping
                if val_loss < best_val_loss:
                    best_val_loss = val_loss
                    patience_counter = 0
                    # Save best model
                    self._save_model(model)
                else:
                    patience_counter += 1
                
                if patience_counter >= self.config.early_stopping_patience:
                    logger.info(f"Early stopping at epoch {epoch + 1}")
                    break
                
                if (epoch + 1) % 10 == 0:
                    logger.info(f"Epoch {epoch + 1}/{self.config.epochs} "
                              f"train_loss={train_loss:.6f} val_loss={val_loss:.6f}")
            
            # Calculate threshold from training data
            model.eval()
            with torch.no_grad():
                all_data = torch.FloatTensor(sequences_np).unsqueeze(-1)
                errors = model.get_reconstruction_error(all_data)
                threshold = float(errors.mean() + 2 * errors.std())
            
            # Save threshold
            self._save_threshold(threshold)
            
            logger.info(f"Training complete. Best val_loss={best_val_loss:.6f}, threshold={threshold:.6f}")
            return True
            
        except Exception as e:
            logger.error(f"Training failed: {e}")
            return False
    
    def _create_sequences(self, data: List[float]) -> List[List[float]]:
        """Create sliding window sequences from data."""
        sequences = []
        for i in range(0, len(data) - self.config.window_size + 1, 
                       self.config.window_size // 2):  # 50% overlap
            seq = data[i:i + self.config.window_size]
            sequences.append(seq)
        return sequences
    
    def _save_model(self, model):
        """Save model to file."""
        try:
            import torch
            os.makedirs(os.path.dirname(self.model_path), exist_ok=True)
            
            torch.save({
                'model_state_dict': model.state_dict(),
                'config': {
                    'input_size': self.config.input_size,
                    'hidden_size': self.config.hidden_size,
                    'num_layers': self.config.num_layers,
                    'dropout': self.config.dropout
                },
                'scaler': {
                    'mean': self._scaler_mean,
                    'std': self._scaler_std
                }
            }, self.model_path)
            
            logger.info(f"Model saved to {self.model_path}")
        except Exception as e:
            logger.error(f"Failed to save model: {e}")
    
    def _save_threshold(self, threshold: float):
        """Save threshold to file."""
        threshold_path = self.model_path.replace('.pt', '_threshold.txt')
        with open(threshold_path, 'w') as f:
            f.write(str(threshold))
    
    def load_model(self) -> Optional[object]:
        """Load trained model."""
        try:
            import torch
            from .autoencoder import OpticalPowerAutoencoder
            
            if not os.path.exists(self.model_path):
                logger.warning(f"Model not found: {self.model_path}")
                return None
            
            checkpoint = torch.load(self.model_path, map_location='cpu', weights_only=False)
            config = checkpoint['config']
            scaler = checkpoint['scaler']
            
            model = OpticalPowerAutoencoder(
                input_size=config['input_size'],
                hidden_size=config['hidden_size'],
                num_layers=config['num_layers'],
                dropout=config['dropout']
            )
            model.load_state_dict(checkpoint['model_state_dict'])
            model.eval()
            
            self._scaler_mean = scaler['mean']
            self._scaler_std = scaler['std']
            self._model = model
            
            logger.info(f"Model loaded from {self.model_path}")
            return model
            
        except Exception as e:
            logger.error(f"Failed to load model: {e}")
            return None
    
    def load_threshold(self) -> float:
        """Load threshold from file."""
        threshold_path = self.model_path.replace('.pt', '_threshold.txt')
        try:
            with open(threshold_path, 'r') as f:
                return float(f.read().strip())
        except:
            return 0.1  # Default threshold
    
    def normalize(self, data: List[float]) -> np.ndarray:
        """Normalize data using saved scaler."""
        return (np.array(data, dtype=np.float32) - self._scaler_mean) / self._scaler_std
    
    def denormalize(self, data: np.ndarray) -> np.ndarray:
        """Denormalize data using saved scaler."""
        return data * self._scaler_std + self._scaler_mean
