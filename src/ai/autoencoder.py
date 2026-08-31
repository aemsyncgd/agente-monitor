# src/ai/autoencoder.py
import torch
import torch.nn as nn
from typing import Tuple


class OpticalPowerAutoencoder(nn.Module):
    """
    Autoencoder LSTM para detección de anomalías en series temporales
    de potencia óptica de ONUs.
    
    Input: Ventana de 24h de datos (1440 puntos por ONU)
    Output: Reconstrucción del input
    Anomalía: Error de reconstrucción > umbral dinámico
    """
    
    def __init__(self, input_size: int = 1, hidden_size: int = 64,
                 num_layers: int = 2, dropout: float = 0.2):
        super().__init__()
        
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.num_layers = num_layers
        
        # Encoder
        self.encoder = nn.LSTM(
            input_size=input_size,
            hidden_size=hidden_size,
            num_layers=num_layers,
            batch_first=True,
            dropout=dropout if num_layers > 1 else 0
        )
        
        # Decoder
        self.decoder = nn.LSTM(
            input_size=hidden_size,
            hidden_size=hidden_size,
            num_layers=num_layers,
            batch_first=True,
            dropout=dropout if num_layers > 1 else 0
        )
        
        # Output projection
        self.fc_out = nn.Linear(hidden_size, input_size)
        
        # Activation
        self.relu = nn.ReLU()
    
    def encode(self, x: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Encode input sequence to latent representation.
        
        Args:
            x: Input tensor of shape (batch, seq_len, input_size)
            
        Returns:
            latent: Latent representation
            hidden: LSTM hidden state
        """
        latent, (h_n, c_n) = self.encoder(x)
        return latent, (h_n, c_n)
    
    def decode(self, latent: torch.Tensor, 
               hidden: Tuple[torch.Tensor, torch.Tensor],
               seq_len: int) -> torch.Tensor:
        """
        Decode latent representation back to input space.
        
        Args:
            latent: Latent tensor from encoder
            hidden: LSTM hidden state
            seq_len: Length of output sequence
            
        Returns:
            Reconstructed tensor of shape (batch, seq_len, input_size)
        """
        # Repeat latent for each timestep
        decoder_input = latent.repeat(1, 1, 1)
        
        # Decode
        decoder_output, _ = self.decoder(decoder_input, hidden)
        
        # Project to input space
        reconstructed = self.fc_out(decoder_output)
        
        return reconstructed
    
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        Forward pass: encode then decode.
        
        Args:
            x: Input tensor of shape (batch, seq_len, input_size)
            
        Returns:
            Reconstructed tensor of same shape as input
        """
        batch_size, seq_len, _ = x.shape
        
        # Encode
        latent, hidden = self.encode(x)
        
        # Decode
        reconstructed = self.decode(latent, hidden, seq_len)
        
        return reconstructed
    
    def get_reconstruction_error(self, x: torch.Tensor) -> torch.Tensor:
        """
        Calculate per-sample reconstruction error (MSE).
        
        Args:
            x: Input tensor of shape (batch, seq_len, input_size)
            
        Returns:
            Error tensor of shape (batch,)
        """
        with torch.no_grad():
            reconstructed = self.forward(x)
            # MSE per sample
            error = torch.mean((x - reconstructed) ** 2, dim=(1, 2))
        
        return error
    
    def predict(self, x: torch.Tensor, threshold: float = 0.1) -> dict:
        """
        Predict if input is anomalous.
        
        Args:
            x: Input tensor of shape (batch, seq_len, input_size)
            threshold: Anomaly threshold
            
        Returns:
            Dictionary with predictions and scores
        """
        error = self.get_reconstruction_error(x)
        
        is_anomaly = error > threshold
        
        return {
            "is_anomaly": is_anomaly,
            "reconstruction_error": error,
            "threshold": threshold,
            "confidence": torch.min(error / threshold, torch.ones_like(error))
        }
    
    @staticmethod
    def load_model(path: str, input_size: int = 1, hidden_size: int = 64,
                   num_layers: int = 2) -> "OpticalPowerAutoencoder":
        """Load model from file."""
        model = OpticalPowerAutoencoder(
            input_size=input_size,
            hidden_size=hidden_size,
            num_layers=num_layers
        )
        model.load_state_dict(torch.load(path, map_location='cpu', weights_only=True))
        model.eval()
        return model
    
    def save_model(self, path: str):
        """Save model to file."""
        torch.save(self.state_dict(), path)
