"""
Twilio Media Streams — Bidirectional real-time audio via WebSocket.

Instead of the webhook model (speak → wait → listen → repeat),
Media Streams give us RAW AUDIO in real-time. This enables:
- Interrupt detection (caller speaks while bot is talking → stop bot)
- Sub-200ms latency (no HTTP round-trips)
- Streaming STT → LLM → TTS in one continuous pipeline

Architecture:
  Phone ←→ Twilio ←→ WebSocket ←→ Flask/Quart
                                      │
                         ┌─────────────┼──────────────┐
                         ▼             ▼              ▼
                    Whisper STT   Gemini Stream   ElevenLabs TTS
                    (real-time)   (streaming)     (streaming)
"""
import os
import json
import base64
import asyncio
import logging
import time
import websockets
from datetime import datetime

logger = logging.getLogger("callbot")


class MediaStreamHandler:
    """
    Handles a single Twilio Media Stream WebSocket connection.
    
    Twilio sends:
    - "connected" → stream started
    - "start" → metadata (call SID, stream SID)
    - "media" → audio chunks (mulaw 8kHz base64)
    - "stop" → stream ended
    
    We send back:
    - "media" → audio chunks to play to the caller
    - "clear" → interrupt current playback
    """
    
    def __init__(self, websocket):
        self.ws = websocket
        self.call_sid = None
        self.stream_sid = None
        self.audio_buffer = bytearray()
        self.is_speaking = False        # Bot is currently playing audio
        self.interrupted = False        # Caller interrupted the bot
        self.silence_start = None       # Track silence for end-of-speech detection
        self.metrics = {
            "start_time": time.time(),
            "audio_chunks_received": 0,
            "audio_chunks_sent": 0,
            "interruptions": 0,
        }
    
    async def handle(self):
        """Main WebSocket handler loop."""
        try:
            async for message in self.ws:
                data = json.loads(message)
                event = data.get("event")
                
                if event == "connected":
                    logger.info("media_stream_connected")
                
                elif event == "start":
                    self.call_sid = data["start"]["callSid"]
                    self.stream_sid = data["start"]["streamSid"]
                    logger.info("media_stream_started", extra={
                        "json_fields": {"call_sid": self.call_sid}
                    })
                
                elif event == "media":
                    await self._handle_audio(data["media"])
                
                elif event == "stop":
                    logger.info("media_stream_stopped", extra={
                        "json_fields": {
                            "call_sid": self.call_sid,
                            "duration_s": int(time.time() - self.metrics["start_time"]),
                            "chunks_in": self.metrics["audio_chunks_received"],
                            "chunks_out": self.metrics["audio_chunks_sent"],
                            "interruptions": self.metrics["interruptions"],
                        }
                    })
                    break
        
        except websockets.exceptions.ConnectionClosed:
            logger.info("media_stream_disconnected")
    
    async def _handle_audio(self, media_data: dict):
        """
        Process incoming audio chunk from caller.
        
        Audio format: mulaw 8000Hz mono, base64 encoded
        Each chunk ≈ 20ms of audio
        """
        self.metrics["audio_chunks_received"] += 1
        
        audio_bytes = base64.b64decode(media_data["payload"])
        self.audio_buffer.extend(audio_bytes)
        
        # Voice Activity Detection (simple energy-based)
        energy = sum(abs(b - 128) for b in audio_bytes) / len(audio_bytes)
        
        if energy > 10:  # Caller is speaking
            self.silence_start = None
            
            # Interrupt detection: if bot is speaking and caller talks → stop bot
            if self.is_speaking:
                await self._interrupt()
        else:
            # Silence detected
            if self.silence_start is None:
                self.silence_start = time.time()
            elif time.time() - self.silence_start > 0.8:
                # 800ms of silence → process accumulated audio
                if len(self.audio_buffer) > 3200:  # At least 200ms of audio
                    await self._process_speech()
                    self.audio_buffer = bytearray()
                    self.silence_start = None
    
    async def _interrupt(self):
        """
        Caller spoke while bot was playing → clear bot audio.
        This is the key UX improvement: natural conversation flow.
        """
        self.interrupted = True
        self.is_speaking = False
        self.metrics["interruptions"] += 1
        
        # Send clear event to Twilio → stops current playback
        await self.ws.send(json.dumps({
            "event": "clear",
            "streamSid": self.stream_sid,
        }))
        
        logger.info("caller_interrupted", extra={
            "json_fields": {"call_sid": self.call_sid}
        })
    
    async def _process_speech(self):
        """
        Send accumulated audio to STT, then LLM, then TTS.
        In a real implementation, this would use a streaming STT service.
        """
        # For now, this is the integration point.
        # In production: 
        # 1. Send audio_buffer to Google Cloud Speech-to-Text (streaming)
        # 2. Get transcript → send to Gemini
        # 3. Stream Gemini response → TTS → send audio back
        pass
    
    async def send_audio(self, audio_bytes: bytes):
        """
        Send audio back to the caller via the Media Stream.
        Audio must be mulaw 8kHz mono.
        """
        self.is_speaking = True
        
        # Split into 20ms chunks (160 bytes at 8kHz mulaw)
        chunk_size = 160
        for i in range(0, len(audio_bytes), chunk_size):
            if self.interrupted:
                self.interrupted = False
                break
            
            chunk = audio_bytes[i:i + chunk_size]
            payload = base64.b64encode(chunk).decode()
            
            await self.ws.send(json.dumps({
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {
                    "payload": payload,
                }
            }))
            
            self.metrics["audio_chunks_sent"] += 1
            
            # Pace the audio output (20ms per chunk)
            await asyncio.sleep(0.02)
        
        self.is_speaking = False


class TwilioStreamTwiML:
    """
    Generate TwiML that connects to our Media Stream WebSocket.
    
    Usage in Flask:
        @app.route("/voice-stream", methods=["POST"])
        def voice_stream():
            return TwilioStreamTwiML.connect("wss://your-server.com/media-stream")
    """
    
    @staticmethod
    def connect(websocket_url: str) -> str:
        """Generate TwiML to start a Media Stream."""
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say language="fr-FR">Bonjour, bienvenue. Je suis votre assistante.</Say>
    <Connect>
        <Stream url="{websocket_url}">
            <Parameter name="caller_id" value="{{{{Caller}}}}" />
        </Stream>
    </Connect>
</Response>"""


# --- Audio conversion utilities ---

def pcm_to_mulaw(pcm_bytes: bytes) -> bytes:
    """Convert PCM 16-bit audio to mulaw 8-bit (Twilio format)."""
    import audioop
    return audioop.lin2ulaw(pcm_bytes, 2)


def mulaw_to_pcm(mulaw_bytes: bytes) -> bytes:
    """Convert mulaw 8-bit audio to PCM 16-bit."""
    import audioop
    return audioop.ulaw2lin(mulaw_bytes, 2)


def mp3_to_mulaw(mp3_path: str) -> bytes:
    """
    Convert MP3 (ElevenLabs output) to mulaw 8kHz (Twilio input).
    Requires ffmpeg.
    """
    import subprocess
    
    result = subprocess.run(
        ["ffmpeg", "-i", mp3_path, "-ar", "8000", "-ac", "1", 
         "-f", "mulaw", "-acodec", "pcm_mulaw", "-"],
        capture_output=True
    )
    
    if result.returncode == 0:
        return result.stdout
    return b""
