"""The speech detector of the audio route (vad.py): where its model is found, how the model is fed (a stand-in session
here: frames, context, state, rate), and, in the image, the pinned model itself on the pinned onnxruntime."""
import math
import os
import sys
import types
import unittest
from unittest import mock

import numpy as np

from helpers import IMAGE_BUILD
from subsync import vad  # noqa: E402


class FakeSession:
    """Stands in for onnxruntime's session of the Silero model: each run's inputs kept, its probability the next of
    `probs`, the state it returns the one it was given plus one (so the next run must be given that one)."""

    def __init__(self, probs):
        self.probs, self.feeds = list(probs), []

    def run(self, outputs, feeds):
        self.feeds.append({k: np.array(v, copy=True) for k, v in feeds.items()})
        return np.array([[self.probs[len(self.feeds) - 1]]], dtype=np.float32), feeds["state"] + 1


def fake_onnxruntime(session):
    """A stand-in onnxruntime module whose sessions are `session`; what each was made with goes in its `made`."""
    made, telemetry = [], []

    def inference_session(path, providers, sess_options):
        made.append((path, providers, sess_options.inter_op_num_threads, sess_options.intra_op_num_threads))
        return session
    return types.SimpleNamespace(SessionOptions=types.SimpleNamespace, InferenceSession=inference_session,
                                 disable_telemetry_events=lambda: telemetry.append(True), made=made,
                                 telemetry=telemetry)


class ModelPathTest(unittest.TestCase):
    def test_the_model_is_the_installed_packages_and_another_is_set_in_settings_alone(self):
        # SUBSYNC_VAD_MODEL was read here from the process's environment, outside Settings (which passes it
        # to SileroVad now, test_public_audio)
        package = types.SimpleNamespace(submodule_search_locations=["/site/silero_vad"])
        with mock.patch.dict(os.environ, {"SUBSYNC_VAD_MODEL": "/elsewhere/vad.onnx"}), \
                mock.patch("importlib.util.find_spec", return_value=package):
            self.assertEqual(vad.model_path(), "/site/silero_vad/data/silero_vad.onnx")
        with mock.patch("importlib.util.find_spec", return_value=None), self.assertRaises(RuntimeError):
            vad.model_path()


class SileroVadFeedTest(unittest.TestCase):
    """SileroVad never ran in a test; here it runs on a stand-in session."""
    INSTALLED = "/site/silero_vad/data/silero_vad.onnx"

    def detector(self, probs, path=None):
        session = FakeSession(probs)
        module = fake_onnxruntime(session)
        with mock.patch.dict(sys.modules, {"onnxruntime": module}), \
                mock.patch.object(vad, "model_path", lambda: self.INSTALLED):
            detector = vad.SileroVad(path)
        self.assertEqual(module.made, [(path or self.INSTALLED, ["CPUExecutionProvider"], 1, 1)])   # one thread
        self.assertEqual(module.telemetry, [True])                                                 # no usage events
        self.assertIs(detector.session, session)
        return detector, session

    def test_each_frame_is_fed_with_the_context_and_state_before_it(self):
        audio = np.random.default_rng(1).uniform(-1, 1, 2 * vad.FRAME + 100).astype(np.float32)
        detector, session = self.detector([0.9, 0.2, 0.7], "/models/vad.onnx")
        self.assertEqual(detector.probs(audio), [float(np.float32(p)) for p in (0.9, 0.2, 0.7)])
        self.assertEqual(len(session.feeds), math.ceil(len(audio) / vad.FRAME))
        context = np.zeros(vad.CONTEXT, dtype=np.float32)
        for k, feed in enumerate(session.feeds):
            self.assertEqual(set(feed), {"input", "state", "sr"})
            self.assertEqual((feed["sr"].dtype, feed["sr"].shape, int(feed["sr"])), (np.dtype(np.int64), (), vad.SR))
            self.assertEqual((feed["state"].dtype, feed["state"].shape), (np.dtype(np.float32), (2, 1, 128)))
            self.assertTrue((feed["state"] == k).all())                   # the state the run before gave back
            frame = np.pad(audio[k * vad.FRAME:(k + 1) * vad.FRAME], (0, max(0, (k + 1) * vad.FRAME - len(audio))))
            self.assertEqual((feed["input"].dtype, feed["input"].shape),
                             (np.dtype(np.float32), (1, vad.CONTEXT + vad.FRAME)))
            np.testing.assert_array_equal(feed["input"][0], np.concatenate([context, frame]))   # the last, padded
            context = frame[-vad.CONTEXT:]

    def test_speech_is_read_from_the_probabilities_with_the_modules_values(self):
        probs = [0.9] * 8 + [0.1] * 12
        detector, _ = self.detector(probs)                 # no path: the installed package's model
        audio = np.zeros(len(probs) * vad.FRAME, dtype=np.float32)
        self.assertEqual(detector.speech(audio), [(0, 8 * vad.FRAME)])
        self.assertEqual(vad.timestamps([float(np.float32(p)) for p in probs], len(audio)), [(0, 8 * vad.FRAME)])


class ThresholdsTest(unittest.TestCase):
    def test_the_thresholds_are_named_once(self):
        # Speech and timestamps each wrote 0.5, 100 and 60, and the exit threshold's 0.15 and 0.01 inline
        self.assertEqual((vad.THRESHOLD, vad.EXIT_GAP, vad.EXIT_FLOOR, vad.MIN_SPEECH_MS, vad.MIN_SILENCE_MS),
                         (0.5, 0.15, 0.01, 100, 60))
        self.assertEqual(vad.timestamps.__defaults__, (vad.THRESHOLD, vad.MIN_SPEECH_MS, vad.MIN_SILENCE_MS))
        # the exit threshold: 0.34 after speech is a pause, 0.36 is not (THRESHOLD - EXIT_GAP = 0.35)
        n = 40 * vad.FRAME
        self.assertEqual(vad.timestamps([0.9] * 10 + [0.36] * 30, n), [(0, n)])
        self.assertEqual(vad.timestamps([0.9] * 10 + [0.34] * 30, n), [(0, 10 * vad.FRAME)])


def _silero_here():
    """Whether onnxruntime and the Silero model are here (requirements.txt installs both in the image)."""
    try:
        import onnxruntime  # noqa: F401
        return os.path.exists(vad.model_path())
    except (ImportError, RuntimeError):
        return False


@unittest.skipUnless(_silero_here() or IMAGE_BUILD, "onnxruntime and the Silero VAD model are installed in the image")
class SileroModelTest(unittest.TestCase):
    """In the image: the pinned model runs on the pinned onnxruntime as probs() feeds it (its inputs by name, the state
    it gives back, the context). The image build always runs it: an update of either that changes what the model
    takes fails the build, not a person's first audio sync."""

    def setUp(self):
        self.assertTrue(_silero_here(), "no onnxruntime or no Silero VAD model (the silero_vad package)")

    def test_the_model_gives_one_speech_probability_per_frame(self):
        detector = vad.SileroVad()
        self.assertEqual({i.name for i in detector.session.get_inputs()}, {"input", "state", "sr"})
        for n in (vad.SR, vad.SR + 1):
            probs = detector.probs(np.zeros(n, dtype=np.float32))
            self.assertEqual(len(probs), math.ceil(n / vad.FRAME))
            self.assertTrue(all(0.0 <= p <= 1.0 for p in probs), probs)
        self.assertEqual(detector.speech(np.zeros(3 * vad.SR, dtype=np.float32)), [])   # silence


if __name__ == "__main__":
    unittest.main()
