import shutil
import tempfile
import unittest
from pathlib import Path

from image_compression.manifest import ContentManifest
from image_compression.service import ImageCompressionService, ImagePipelineError


class CopyingImageProcessor:
    def __init__(self):
        self.sources = []

    def process(self, source, destination):
        self.sources.append(Path(source))
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return False


class NestedArchiveExtractor:
    def extract(self, archive_path, destination, depth, budget):
        destination.mkdir(parents=True)
        if depth == 1:
            first = destination / "a" / "pic.txt"
            second = destination / "b" / "pic.txt"
            nested = destination / "inner.zip"
            first.parent.mkdir()
            second.parent.mkdir()
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            nested.write_bytes(b"archive")
            return [
                (Path("a/pic.txt"), first),
                (Path("b/pic.txt"), second),
                (Path("inner.zip"), nested),
            ]
        leaf = destination / "c" / "pic.txt"
        leaf.parent.mkdir()
        leaf.write_bytes(b"third")
        return [(Path("c/pic.txt"), leaf)]


class ServiceLifecycleTests(unittest.TestCase):
    def make_service(self, root, extractor=None):
        source = root / "source"
        output = root / "output"
        pending = root / "pending"
        source.mkdir()
        return ImageCompressionService(
            source,
            output,
            root,
            pending_root=pending,
            archive_extractor=extractor or NestedArchiveExtractor(),
            image_processor=CopyingImageProcessor(),
        )

    def test_ingest_preserves_verified_pending_copy_and_removes_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self.make_service(root)
            source = service.source_directory / "group"
            source.mkdir()
            (source / "a.txt").write_bytes(b"data")

            pending = service.ensure_ingested(source, 1)

            self.assertFalse(source.exists())
            self.assertEqual(b"data", (pending / "a.txt").read_bytes())
            self.assertTrue((service.mission_directory(1) / "ingest.json").is_file())

    def test_ingest_recovers_after_pending_source_was_created(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self.make_service(root)
            source = service.source_directory / "group"
            source.mkdir()
            (source / "a.txt").write_bytes(b"data")
            quarantine = service.quarantine_path(3)
            source.rename(quarantine)
            mission_directory = service.mission_directory(3)
            mission_directory.mkdir(parents=True)
            shutil.copytree(quarantine, mission_directory / "source")

            pending = service.ensure_ingested(source, 3)

            self.assertEqual(b"data", (pending / "a.txt").read_bytes())
            self.assertFalse(quarantine.exists())
            self.assertTrue((mission_directory / "ingest.json").is_file())

    def test_recursive_archives_are_flattened_with_stable_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self.make_service(root)
            source = service.source_directory / "outer.zip"
            source.write_bytes(b"archive")
            service.ensure_ingested(source, 1)

            prepared = service.prepare_batch(1, "archive", "outer.zip", "outer")

            self.assertTrue(prepared.is_directory)
            self.assertEqual(
                {"pic.txt", "D1_pic.txt", "D2_pic.txt"},
                {path.name for path in prepared.result_path.iterdir()},
            )

    def test_prepare_batch_logs_each_file_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self.make_service(root)
            source = service.source_directory / "group"
            source.mkdir()
            (source / "first.jpg").write_bytes(b"first")
            (source / "second.png").write_bytes(b"second")
            service.ensure_ingested(source, 11)

            with self.assertLogs("image_compression.service", level="INFO") as logs:
                service.prepare_batch(11, "directory", "group", "group")

            output = "\n".join(logs.output)
            self.assertIn("progress=1/2", output)
            self.assertIn("progress=2/2", output)
            self.assertIn("percent=100%", output)
            self.assertEqual(2, output.count("批次文件处理完成"))
            self.assertEqual(2, output.count("action=copied"))

    def test_publish_then_cleanup_removes_only_pending_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self.make_service(root)
            source = service.source_directory / "note.txt"
            source.write_bytes(b"note")
            service.ensure_ingested(source, 7)
            prepared = service.prepare_batch(7, "file", "note.txt", "note.txt")
            plan = service.stage_for_publish(7, prepared)

            published = service.publish(plan)
            service.cleanup_completed(7)

            self.assertEqual(b"note", published.read_bytes())
            self.assertFalse(service.mission_directory(7).exists())

    def test_loose_file_keeps_its_extension_during_processing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self.make_service(root)
            source = service.source_directory / "photo.jpg"
            source.write_bytes(b"image")
            service.ensure_ingested(source, 10)

            service.prepare_batch(10, "file", "photo.jpg", "photo.jpg")

            self.assertEqual(".jpg", service.image_processor.sources[0].suffix)

    def test_publish_recovery_accepts_only_matching_final_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self.make_service(root)
            source = service.source_directory / "note.txt"
            source.write_bytes(b"note")
            service.ensure_ingested(source, 8)
            prepared = service.prepare_batch(8, "file", "note.txt", "note.txt")
            plan = service.stage_for_publish(8, prepared)
            service.publish(plan)
            recovered = service.recover_publish_plan(
                8, plan.destination, False, plan.manifest
            )

            self.assertEqual(plan.destination, service.publish(recovered))

    def test_cleanup_does_not_remove_a_new_source_with_the_same_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self.make_service(root)
            source = service.source_directory / "note.txt"
            source.write_bytes(b"old")
            service.ensure_ingested(source, 9)
            source.write_bytes(b"new")

            service.cleanup_completed(9)

            self.assertEqual(b"new", source.read_bytes())

    def test_existing_output_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self.make_service(root)
            source = service.source_directory / "note.txt"
            source.write_bytes(b"new")
            destination = service.output_directory / "note.txt"
            destination.write_bytes(b"old")
            service.ensure_ingested(source, 2)
            prepared = service.prepare_batch(2, "file", "note.txt", "note.txt")

            with self.assertRaises(FileExistsError):
                service.stage_for_publish(2, prepared)

            self.assertEqual(b"old", destination.read_bytes())
            self.assertTrue((service.mission_directory(2) / "source").exists())

    def test_recovery_output_is_confined_to_configured_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self.make_service(root)
            outside = root / "outside.txt"

            with self.assertRaisesRegex(
                ImagePipelineError, "outside its configured directory"
            ):
                service.recover_publish_plan(
                    4,
                    outside,
                    False,
                    ContentManifest("a" * 64, 1, 1),
                )


if __name__ == "__main__":
    unittest.main()
