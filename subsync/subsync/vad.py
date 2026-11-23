"""Silero VAD (MIT, snakers4/silero-vad 6.2.3) on onnxruntime, no torch.

Its frames and context, and the speech thresholds and shortest speech and silence it is read with, are the values
below; no padding, no maximum length.
"""
import os

import numpy as np

SR = 16000
FRAME = 512
CONTEXT = 64
THRESHOLD = 0.5        # a frame this likely to be speech, or more, starts speech or keeps it going
EXIT_GAP = 0.15        # a pause in speech is frames below THRESHOLD - EXIT_GAP (0.35) ...
EXIT_FLOOR = 0.01      # ... and never below this
MIN_SPEECH_MS = 100    # speech no longer than this is left out
MIN_SILENCE_MS = 60    # a shorter pause doesn't end speech


def model_path():
    """The model installed with the silero_vad package (SUBSYNC_VAD_MODEL names another: Settings.vad_model)."""
    import importlib.util
    spec = importlib.util.find_spec("silero_vad")
    if not spec or not spec.submodule_search_locations:
        raise RuntimeError("silero_vad package not installed and SUBSYNC_VAD_MODEL not set")
    return os.path.join(list(spec.submodule_search_locations)[0], "data", "silero_vad.onnx")


class SileroVad:
    def __init__(self, path=None):
        import onnxruntime
        onnxruntime.disable_telemetry_events()   # no usage events from the runtime
        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        self.session = onnxruntime.InferenceSession(path or model_path(), providers=["CPUExecutionProvider"],
                                                    sess_options=opts)

    def probs(self, audio):
        """Speech probability per 512-sample frame of float32 mono 16 kHz audio."""
        state = np.zeros((2, 1, 128), dtype=np.float32)
        context = np.zeros((1, CONTEXT), dtype=np.float32)
        sr = np.array(SR, dtype=np.int64)
        out = []
        for i in range(0, len(audio), FRAME):
            chunk = audio[i:i + FRAME]
            if len(chunk) < FRAME:
                chunk = np.pad(chunk, (0, FRAME - len(chunk)))
            x = np.concatenate([context, chunk.reshape(1, FRAME).astype(np.float32)], axis=1)
            prob, state = self.session.run(None, {"input": x, "state": state, "sr": sr})
            context = x[:, -CONTEXT:]
            out.append(float(prob[0][0]))
        return out

    def speech(self, audio):
        """Speech segments [(start sample, end sample)], as silero's get_speech_timestamps with no padding and
        no maximum length."""
        return timestamps(self.probs(audio), len(audio))


def timestamps(probs, n_samples, threshold=THRESHOLD, min_speech_ms=MIN_SPEECH_MS, min_silence_ms=MIN_SILENCE_MS):
    """The speech segments [(start sample, end sample)] of per-frame speech probabilities."""
    neg = max(threshold - EXIT_GAP, EXIT_FLOOR)
    min_speech = SR * min_speech_ms / 1000
    min_silence = SR * min_silence_ms / 1000
    triggered, temp_end, start = False, 0, 0
    out = []
    for i, p in enumerate(probs):
        cur = FRAME * i
        if p >= threshold and temp_end:
            temp_end = 0
        if p >= threshold and not triggered:
            triggered, start = True, cur
            continue
        if p < neg and triggered:
            if not temp_end:
                temp_end = cur
            if cur - temp_end < min_silence:
                continue
            if temp_end - start > min_speech:
                out.append((start, temp_end))
            triggered, temp_end = False, 0
    if triggered and n_samples - start > min_speech:
        out.append((start, n_samples))
    return out
