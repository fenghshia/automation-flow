import unittest

from image_compression.naming import (
    NameCandidate,
    allocate_names,
    collision_key,
    output_name,
    safe_basename,
)


class NamingTests(unittest.TestCase):
    def test_archive_compound_suffix_is_removed(self):
        self.assertEqual("album", output_name("album.tar.gz", "archive"))

    def test_windows_reserved_and_invalid_names_are_sanitized(self):
        self.assertEqual("_CON.jpg", safe_basename("dir/CON.jpg"))
        self.assertEqual("a_b_.jpg", safe_basename('a:b?.jpg'))

    def test_collision_keys_are_case_insensitive(self):
        self.assertEqual(collision_key("Pic.JPG"), collision_key("pic.jpg"))

    def test_duplicate_names_get_d_prefixes(self):
        names = allocate_names(
            [
                NameCandidate("a", "a/pic.jpg", "pic.jpg"),
                NameCandidate("b", "b/pic.jpg", "pic.jpg"),
                NameCandidate("c", "c/pic.jpg", "pic.jpg"),
            ]
        )
        self.assertEqual(
            {"a": "pic.jpg", "b": "D1_pic.jpg", "c": "D2_pic.jpg"}, names
        )

    def test_generated_prefix_does_not_take_an_original_name(self):
        names = allocate_names(
            [
                NameCandidate("a", "a/pic.jpg", "pic.jpg"),
                NameCandidate("b", "b/pic.jpg", "pic.jpg"),
                NameCandidate("c", "c/D1_pic.jpg", "D1_pic.jpg"),
            ]
        )
        self.assertEqual("pic.jpg", names["a"])
        self.assertEqual("D2_pic.jpg", names["b"])
        self.assertEqual("D1_pic.jpg", names["c"])


if __name__ == "__main__":
    unittest.main()

