import tempfile
import unittest
import zipfile
from pathlib import Path

from env import EnvConfig
from image_compression.archive import (
    ArchiveBudget,
    ArchiveExtractor,
    ArchiveLimitError,
    UnsafeArchiveEntryError,
)
from image_compression.policy import MAX_ARCHIVE_MEMBERS


class ArchiveParsingTests(unittest.TestCase):
    def test_technical_listing_records_are_parsed(self):
        output = "Path = a.txt\nSize = 3\nFolder = -\n\nPath = dir\nFolder = +\n"
        records = ArchiveExtractor._parse_records(output)
        self.assertEqual("a.txt", records[0]["Path"])
        self.assertEqual("+", records[1]["Folder"])

    def test_parent_member_path_is_rejected(self):
        with self.assertRaises(UnsafeArchiveEntryError):
            ArchiveExtractor._validate_member_name("../outside.txt")

    def test_member_budget_is_cumulative(self):
        budget = ArchiveBudget(members=MAX_ARCHIVE_MEMBERS)
        with self.assertRaises(ArchiveLimitError):
            budget.add(1, 1, 1)

    def test_configured_seven_zip_extracts_a_temporary_zip(self):
        try:
            bin_directory = EnvConfig.image_compression_7zip_bin_directory()
        except RuntimeError:
            self.skipTest("7-Zip is not configured")
        if not (bin_directory / "7z.exe").is_file():
            self.skipTest("Configured 7-Zip executable is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "source"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("nested/example.txt", b"example")
            extractor = ArchiveExtractor(bin_directory)

            extracted = extractor.extract(
                archive, root / "expanded", 1, ArchiveBudget()
            )

            self.assertEqual([Path("nested/example.txt")], [item[0] for item in extracted])
            self.assertEqual(b"example", extracted[0][1].read_bytes())


if __name__ == "__main__":
    unittest.main()
