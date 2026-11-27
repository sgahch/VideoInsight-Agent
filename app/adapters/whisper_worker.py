"""One offline CPU inference process; exiting releases model memory."""
import json
import sys
import wave
import numpy as np
from faster_whisper import WhisperModel

if __name__ == "__main__":
    model = WhisperModel(sys.argv[1], device="cpu", compute_type="int8", cpu_threads=1,
                         num_workers=1, local_files_only=True)
    with wave.open(sys.argv[2], "rb") as audio:
        if audio.getnchannels() != 1 or audio.getsampwidth() != 2 or audio.getframerate() != 16000:
            raise ValueError("Expected mono 16k PCM")
        pcm = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
    segments, info = model.transcribe(pcm, beam_size=3, vad_filter=False,
                                     condition_on_previous_text=False)
    print(json.dumps({"segments": [{"start": s.start, "end": s.end, "text": s.text}
                                   for s in segments], "duration": info.duration}, ensure_ascii=True))
