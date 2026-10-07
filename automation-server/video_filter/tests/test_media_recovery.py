"""Decode regressions use synthetic buffers, never configured media or CUDA."""
import json
import io
import subprocess
import tempfile
import unittest
import shutil
import wave
from pathlib import Path
from unittest.mock import patch, MagicMock

import numpy as np

from .support import bundle_arguments, signature
from .test_performance import FakeDecoder, factory
from video_filter.media import MediaDecoder, run_media_tool
from video_filter.extraction import extract
from video_filter.feature_store import FeatureStore
from video_filter.features.contract import DIMENSIONS


class ContinuousAudioTests(unittest.TestCase):
    def test_synthetic_pcm_matches_bounded_decoder_when_ffmpeg_is_available(self):
        binary = shutil.which("ffmpeg")
        if binary is None:
            self.skipTest("Local FFmpeg unavailable; mock decoder regressions still run.")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.wav"
            samples = (np.sin(np.arange(12 * 16000) * .13) * 10000).astype("<i2")
            with wave.open(str(path), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(16000)
                output.writeframes(samples.tobytes())
            decoder = MediaDecoder(Path(binary).parent)
            decoder.ffmpeg = Path(binary)
            streamed = list(decoder.audio_windows(path, [[0, 10], [10, 12]], 0))
            for index, (start, end) in enumerate(((0, 10), (10, 12))):
                np.testing.assert_array_equal(decoder.audio(path, start, end - start, 0), streamed[index][1])

    def process(self, data):
        process = MagicMock()
        process.__enter__.return_value = process
        process.stdout = io.BytesIO(data)
        samples = max(1, len(data) // 4) if data else 0
        metadata = []
        for offset in range(0, samples, 160000):
            metadata.append(f"[ashowinfo] pts:{offset} pts_time:{offset / 16000} nb_samples:{min(160000, samples - offset)}\n")
        process.stderr = io.BytesIO("".join(metadata).encode())
        process.returncode = 0
        process.poll.return_value = 0
        return process

    def test_one_process_preserves_window_boundaries_and_short_tail(self):
        signal = np.arange(12 * 16000, dtype=np.float32)
        process = self.process(signal.tobytes())
        with patch("video_filter.media.subprocess.Popen", return_value=process) as launch:
            windows = list(MediaDecoder("fixture-tools").audio_windows("fixture.mp4", [[0, 10], [10, 12]], 1))
        self.assertEqual(1, launch.call_count)
        self.assertEqual([0, 1], [index for index, audio in windows])
        np.testing.assert_array_equal(signal[:160000], windows[0][1])
        np.testing.assert_array_equal(signal[160000:], windows[1][1])
        self.assertIn("pipe:1", launch.call_args.args[0])
        process.kill.assert_not_called()

    def test_empty_tail_and_failed_exit_are_distinct(self):
        for returncode in (0, 1):
            process = self.process(np.ones(160000, dtype=np.float32).tobytes())
            process.returncode = returncode
            with patch("video_filter.media.subprocess.Popen", return_value=process):
                iterator = MediaDecoder("fixture-tools").audio_windows("fixture.mp4", [[0, 10], [10, 12]], 1)
                self.assertEqual(160000, len(next(iterator)[1]))
                self.assertEqual(0, len(next(iterator)[1]))
                with self.assertRaises(StopIteration if returncode == 0 else subprocess.CalledProcessError):
                    next(iterator)

    def test_consumer_failure_closes_decoder_and_truncated_sample_fails(self):
        process = self.process(np.ones(160000, dtype=np.float32).tobytes())
        process.poll.return_value = None
        with patch("video_filter.media.subprocess.Popen", return_value=process):
            iterator = MediaDecoder("fixture-tools").audio_windows("fixture.mp4", [[0, 10], [10, 12]], 1)
            next(iterator)
            iterator.close()
        process.kill.assert_called_once()
        process = self.process(b"bad")
        with patch("video_filter.media.subprocess.Popen", return_value=process):
            with self.assertRaisesRegex(ValueError, "Incomplete decoded audio"):
                list(MediaDecoder("fixture-tools").audio_windows("fixture.mp4", [[0, 10]], 1))

    def test_read_timeout_terminates_decoder(self):
        process = self.process(b"four")
        process.poll.return_value = None
        process.stdout.read = MagicMock(side_effect=subprocess.TimeoutExpired("fixture", 1))
        with patch("video_filter.media.subprocess.Popen", return_value=process):
            with self.assertRaises(subprocess.TimeoutExpired):
                list(MediaDecoder("fixture-tools").audio_windows("fixture.mp4", [[0, 10]], 1))
        process.kill.assert_called_once()

    def test_delayed_audio_and_timestamp_gaps_do_not_shift_later_windows(self):
        signal = np.concatenate([np.ones(16000, dtype=np.float32), np.full(16000, 2, dtype=np.float32)])
        process = self.process(signal.tobytes())
        process.stderr = io.BytesIO(b"[ashowinfo] pts:80000 pts_time:5 nb_samples:16000\n[ashowinfo] pts:400000 pts_time:25 nb_samples:16000\n")
        with patch("video_filter.media.subprocess.Popen", return_value=process):
            windows = list(MediaDecoder("fixture-tools").audio_windows("fixture.mp4", [[0, 10], [10, 20], [20, 30]], 1))
        self.assertEqual([16000, 0, 16000], [len(audio) for index, audio in windows])
        np.testing.assert_array_equal(np.ones(16000), windows[0][1])
        np.testing.assert_array_equal(np.full(16000, 2), windows[2][1])

    def test_audio_packet_crossing_window_boundary_is_split_once(self):
        signal = np.arange(32000, dtype=np.float32)
        process = self.process(signal.tobytes())
        process.stderr = io.BytesIO(b"[ashowinfo] pts:144000 pts_time:9 nb_samples:32000\n")
        with patch("video_filter.media.subprocess.Popen", return_value=process):
            windows = list(MediaDecoder("fixture-tools").audio_windows("fixture.mp4", [[0, 10], [10, 12]], 1))
        np.testing.assert_array_equal(signal[:16000], windows[0][1])
        np.testing.assert_array_equal(signal[16000:], windows[1][1])

    def test_rounded_seconds_on_long_audio_do_not_create_false_overlaps(self):
        signal = np.arange(742, dtype=np.float32)
        process = self.process(signal.tobytes())
        # FFmpeg's six-significant-digit pts_time loses three samples here.
        process.stderr = io.BytesIO(b"[ashowinfo] pts:1600000 pts_time:100 nb_samples:371\n"
                                    b"[ashowinfo] pts:1600371 pts_time:100.023 nb_samples:371\n")
        requested = [[start, start + 10] for start in range(0, 100, 10)] + [[100, 101]]
        with patch("video_filter.media.subprocess.Popen", return_value=process) as launch:
            windows = list(MediaDecoder("fixture-tools").audio_windows("fixture.mp4", requested, 1))
        self.assertTrue(all(not len(audio) for _, audio in windows[:-1]))
        np.testing.assert_array_equal(signal, windows[-1][1])
        self.assertIn("asettb=1/16000", launch.call_args.args[0][launch.call_args.args[0].index("-af") + 1])

    def test_exact_pts_preserves_samples_at_window_boundary_despite_rounded_seconds(self):
        signal = np.arange(640, dtype=np.float32)
        process = self.process(signal.tobytes())
        # Rounded seconds appear to end at 100, but three samples belong after it.
        process.stderr = io.BytesIO(b"[ashowinfo] pts:1599683 pts_time:99.98 nb_samples:320\n"
                                    b"[ashowinfo] pts:1600003 pts_time:100 nb_samples:320\n")
        requested = [[start, start + 10] for start in range(0, 110, 10)]
        with patch("video_filter.media.subprocess.Popen", return_value=process):
            windows = list(MediaDecoder("fixture-tools").audio_windows("fixture.mp4", requested, 1))
        np.testing.assert_array_equal(signal[:317], windows[9][1])
        np.testing.assert_array_equal(signal[317:], windows[10][1])

    def test_real_overlap_and_invalid_pts_still_fail_and_terminate_decoder(self):
        for metadata, message in ((b"[ashowinfo] pts:0 pts_time:0 nb_samples:16000\n"
                                  b"[ashowinfo] pts:8000 pts_time:0.5 nb_samples:16000\n", "timestamps overlap"),
                                 (b"[ashowinfo] pts:NOPTS pts_time:NOPTS nb_samples:16000\n", "frame metadata")):
            with self.subTest(metadata=metadata):
                process = self.process(np.ones(32000, dtype=np.float32).tobytes())
                process.stderr = io.BytesIO(metadata)
                process.poll.return_value = None
                with patch("video_filter.media.subprocess.Popen", return_value=process):
                    with self.assertRaisesRegex(ValueError, message):
                        list(MediaDecoder("fixture-tools").audio_windows("fixture.mp4", [[0, 10]], 1))
                process.kill.assert_called_once()


class ColorRecoveryTests(unittest.TestCase):
    def probe(self, **colors):
        return subprocess.CompletedProcess([], 0, json.dumps({"streams": [{
            "index": 0, "codec_type": "video", "codec_name": "h264", "duration": "2", **colors}],
            "format": {"duration": "2"}}).encode(), b"")

    def pixels(self):
        return subprocess.CompletedProcess([], 0, np.zeros((1, 224, 224, 3), dtype=np.uint8).tobytes(), b"")

    def test_reserved_tags_are_corrected_before_scale_without_changing_nvdec(self):
        decoder = MediaDecoder("fixture-tools")
        with patch("video_filter.media.run_media_tool", side_effect=[
                self.probe(color_primaries="reserved", color_transfer="reserved"), self.pixels()]) as run:
            frames = decoder.frames("fixture.mp4", 0, 1, 0, count=1)
        args = run.call_args.args[0]
        self.assertEqual("h264_cuvid", args[args.index("-c:0") + 1])
        self.assertEqual("setparams=color_primaries=bt709:color_trc=bt709,scale=256:256:force_original_aspect_ratio=increase,crop=224:224",
            args[args.index("-vf") + 1])
        self.assertEqual((1, 224, 224, 3), frames.shape)

    def test_valid_hdr_tags_are_preserved_and_unknown_tags_are_not_overridden(self):
        for colors in ({"color_primaries": "bt2020", "color_transfer": "smpte2084"}, {}):
            decoder = MediaDecoder("fixture-tools")
            with patch("video_filter.media.run_media_tool", side_effect=[self.probe(**colors), self.pixels()]) as run:
                decoder.frames("fixture.mp4", 0, 1, 0, count=1)
            self.assertNotIn("setparams", run.call_args.args[0][run.call_args.args[0].index("-vf") + 1])
        self.assertEqual("setparams=color_trc=bt709,", MediaDecoder._color_correction(("bt2020", "reserved")))

    def test_frame_only_reserved_tags_retry_once_and_reuse_correction(self):
        decoder = MediaDecoder("fixture-tools", device="cuda:2")
        error = subprocess.CalledProcessError(129, ["ffmpeg"], stderr=b"Unsupported input: fmt:nv12 prim:reserved trc:reserved -> fmt:rgb24")
        with patch("video_filter.media.run_media_tool", side_effect=[
                self.probe(color_primaries="bt2020", color_transfer="smpte2084"), error, self.pixels(), self.pixels()]) as run:
            decoder.frames("fixture.mp4", 0, 1, 0, count=1)
            decoder.frames("fixture.mp4", 1, 1, 0, count=1)
        self.assertEqual(4, run.call_count)
        for call in run.call_args_list[2:]:
            args = call.args[0]
            self.assertEqual("h264_cuvid", args[args.index("-c:0") + 1])
            self.assertEqual("2", args[args.index("-hwaccel_device:0") + 1])
            self.assertTrue(args[args.index("-vf") + 1].startswith("setparams=color_primaries=bt2020:color_trc=smpte2084,"))

    def test_recovery_failure_is_not_retried_indefinitely(self):
        error = subprocess.CalledProcessError(129, ["ffmpeg"], stderr=b"Unsupported input: prim:reserved trc:reserved")
        decoder = MediaDecoder("fixture-tools")
        with patch("video_filter.media.run_media_tool", side_effect=[self.probe(), error, error]) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                decoder.frames("fixture.mp4", 0, 1, 0, count=1)
        self.assertEqual(3, run.call_count)

    def test_recoverable_error_logs_warning_but_driver_error_stays_error(self):
        for message, level in ((b"Unsupported input: prim:reserved", "WARNING"), (b"CUDA out of memory", "ERROR")):
            error = subprocess.CalledProcessError(1, ["fixture"], stderr=message)
            with patch("video_filter.media.subprocess.run", side_effect=error), self.assertLogs("video_filter.media", level="WARNING") as logs:
                with self.assertRaises(subprocess.CalledProcessError):
                    run_media_tool(["fixture"], 1, recover_reserved_color=True)
            self.assertEqual(level, logs.records[0].levelname)


class EmptyVideoRecoveryTests(unittest.TestCase):
    def probe(self):
        return subprocess.CompletedProcess([], 0, json.dumps({"streams": [{"index": 0,
            "codec_type": "video", "codec_name": "h264", "duration": "10.01"}]}).encode(), b"")

    def result(self, pixels=b""):
        return subprocess.CompletedProcess([], 0, pixels, b"")

    def test_empty_fps_clip_recovers_actual_nvdec_frames_and_pads_short_tail(self):
        pixels = np.full((1, 224, 224, 3), 42, dtype=np.uint8).tobytes()
        decoder = MediaDecoder("fixture-tools")
        with patch("video_filter.media.run_media_tool", side_effect=[self.probe(), self.result(), self.result(pixels)]) as run:
            frames = decoder.frames("fixture.mp4", 10., .01, 0, count=16)
        self.assertEqual((16, 224, 224, 3), frames.shape)
        self.assertTrue((frames == 42).all())
        first, retry = [call.args[0] for call in run.call_args_list[1:]]
        self.assertIn("fps=", first[first.index("-vf") + 1])
        self.assertNotIn("fps=", retry[retry.index("-vf") + 1])
        for args in (first, retry):
            self.assertEqual("h264_cuvid", args[args.index("-c:0") + 1])
            self.assertEqual("10.0", args[args.index("-ss") + 1])

    def test_empty_tail_looks_back_only_one_second_for_one_real_frame(self):
        pixels = np.full((1, 224, 224, 3), 24, dtype=np.uint8).tobytes()
        with patch("video_filter.media.run_media_tool", side_effect=[self.probe(), self.result(), self.result(), self.result(pixels)]) as run:
            frames = MediaDecoder("fixture-tools").frames("fixture.mp4", 10., .01, 0, count=16)
        args = run.call_args.args[0]
        self.assertEqual("9.0", args[args.index("-ss") + 1])
        self.assertEqual("1", args[args.index("-frames:v") + 1])
        self.assertLessEqual(float(args[args.index("-t") + 1]), 10.)
        self.assertTrue((frames == 24).all())

    def test_empty_video_remains_fatal_after_bounded_retries(self):
        for start, count, calls in ((10., 16, 4), (0., 16, 3), (0., 1, 2)):
            with self.subTest(start=start, count=count), patch("video_filter.media.run_media_tool",
                    side_effect=[self.probe()] + [self.result()] * (calls - 1)) as run:
                with self.assertRaisesRegex(ValueError, "bounded NVDEC recovery"):
                    MediaDecoder("fixture-tools").frames("fixture.mp4", start, .01, 0, count=count)
                self.assertEqual(calls, run.call_count)


class MissingAudioTests(unittest.TestCase):
    def test_successful_empty_audio_is_empty_but_video_stays_fatal(self):
        decoder = MediaDecoder("fixture-tools")
        with patch("video_filter.media.run_media_tool", return_value=subprocess.CompletedProcess([], 0, b"", b"")) as run:
            audio = decoder.audio("fixture.mp4", 10, 2, 1)
            self.assertEqual((0,), audio.shape)
            self.assertEqual(np.float32, audio.dtype)
            self.assertIn("-xerror", run.call_args.args[0])
            with self.assertRaisesRegex(ValueError, "no media samples"):
                decoder._decode("fixture.mp4", 10, 2, [])

    def test_audio_decode_error_is_never_treated_as_absence(self):
        decoder = MediaDecoder("fixture-tools")
        error = subprocess.CalledProcessError(1, ["fixture"], stderr=b"corrupt audio")
        with patch("video_filter.media.run_media_tool", side_effect=error):
            with self.assertRaises(subprocess.CalledProcessError):
                decoder.audio("fixture.mp4", 10, 2, 1)

    def test_missing_tail_is_masked_cached_and_serialized_with_presence_evidence(self):
        class ShortAudioDecoder(FakeDecoder):
            def audio(self, path, start, duration, stream):
                if start >= 10:
                    self.audio_calls += 1
                    return np.empty(0, dtype=np.float32)
                return super().audio(path, start, duration, stream)
        decoder = ShortAudioDecoder()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.mp4"
            path.write_bytes(b"synthetic, not a video")
            result = extract(path, decoder, signature(), {name: None for name in DIMENSIONS}, device="cpu",
                adapter_factories={name: factory(name) for name in DIMENSIONS}, audio_cache_mib=1)
        self.assertEqual(2, decoder.audio_calls)
        self.assertEqual([1], result["audio_missing_windows"])
        self.assertEqual("present", result["audio_status"])
        for name in ("beats", "egemaps"):
            self.assertTrue(result["validity"][name][0].all())
            self.assertFalse(result["validity"][name][1].any())
            self.assertFalse(result["vectors"][name][1].any())
        arguments = bundle_arguments()
        for key in ("vectors", "validity", "audio_missing_windows"):
            arguments[key] = result[key]
        prepared = FeatureStore().prepare(**arguments)
        arrays = FeatureStore()._decode(prepared)
        self.assertEqual([1], prepared.manifest["audio_missing_windows"])
        np.testing.assert_array_equal(arrays["beats_mean"], result["vectors"]["beats"][0])
        self.assertTrue(arrays["dino_valid"].all())
