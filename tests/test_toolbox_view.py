import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ui.toolbox_view import (
    extract_ng_defect_category_ids,
    extract_unique_scan_ids,
    filter_ng_defect_category_ids,
    create_image_clipboard_data,
    find_images_by_identifier,
    highlight_identifier_in_filename,
    read_filenames_from_folder,
)


class ScanIdentifierTests(unittest.TestCase):
    def test_groups_high_low_and_side_names_by_identifier(self):
        names = "\n".join((
            "高1-20261021-1-mark.png",
            "低1-20261021-2-mark.png",
            "侧1-20261021-3-mark.png",
            "高2-20261021-1-mark.png",
        ))

        self.assertEqual(extract_unique_scan_ids(names), ["1", "2"])

    def test_normalizes_leading_zeroes_and_returns_no_unmatched_ids(self):
        self.assertEqual(extract_unique_scan_ids("高01.png 低1.png"), ["1"])
        self.assertEqual(extract_unique_scan_ids("A1-20261021-mark.png"), [])

    def test_reads_filenames_recursively_from_folder(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "高1-20261021-mark.png").touch()
            nested = root / "nested"
            nested.mkdir()
            (nested / "低1-20261022-mark.png").touch()

            filenames = read_filenames_from_folder(temp_dir)

        self.assertCountEqual(
            filenames,
            ["高1-20261021-mark.png", "低1-20261022-mark.png"],
        )
        self.assertEqual(extract_unique_scan_ids("\n".join(filenames)), ["1"])

    def test_counts_ng_identifiers_once_per_defect_category(self):
        category_ids = extract_ng_defect_category_ids([
            "高01-20261021-mark.png",
            "高1-20261022-mark.png",
            "低1-20261021-mark.png",
            "侧2-20261021-mark.png",
            "a3-20261021-mark.png",
            "B4-20261021-mark.png",
            "C5-20261021-mark.png",
            "unknown6-20261021-mark.png",
        ])

        self.assertEqual(
            category_ids,
            {
                "高": {"1"},
                "低": {"1"},
                "侧": {"2"},
                "A": {"3"},
                "B": {"4"},
                "C": {"5"},
            },
        )

    def test_filters_defect_details_to_ng_ids_in_selected_range(self):
        category_ids = extract_ng_defect_category_ids([
            "高2070-20261021-mark.png",
            "高2072-20261021-mark.png",
            "低2075-20261021-mark.png",
        ])

        filtered_ids = filter_ng_defect_category_ids(
            category_ids,
            {"老格式": {"2072", "2075"}},
        )

        self.assertEqual(
            filtered_ids,
            {
                ("老格式", "高"): {"2072"},
                ("老格式", "低"): {"2075"},
            },
        )

    def test_extracts_t_format_category_and_identifier(self):
        category_ids = extract_ng_defect_category_ids([
            "T-NO-A165-20261108-Result=NG_mark.png",
            "T-NO-A165-20261108-Result=NG_mark.jpg",
            "T-OK-B200-20261107.png",
        ])

        self.assertEqual(
            category_ids,
            {
                ("NO", "A"): {"165"},
                ("OK", "B"): {"200"},
            },
        )

    def test_t_format_without_side_prefix_extracts_first_number(self):
        category_ids = extract_ng_defect_category_ids([
            "T-UNKNOWN-12345-20261108.png",
        ])

        self.assertEqual(
            category_ids,
            {
                ("UNKNOWN", "未分类"): {"12345"},
            },
        )

    def test_t_format_with_underscore_separator(self):
        category_ids = extract_ng_defect_category_ids([
            "T_NO_A001_20261108.png",
        ])

        self.assertEqual(
            category_ids,
            {
                ("NO", "A"): {"1"},
            },
        )

    def test_finds_matching_images_recursively_without_partial_number_matches(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            filenames = (
                "A10055.png",
                "B10055-mark.jpg",
                "高10055.png",
                "低10055.png",
                "侧10055.png",
                "10055.png",
                "A100550.png",
                "other10055.png",
                "A10055.txt",
            )
            for filename in filenames:
                (root / filename).touch()
            nested = root / "nested"
            nested.mkdir()
            (nested / "低010055.webp").touch()

            matches = find_images_by_identifier(temp_dir, "10055")

        self.assertEqual(
            {path.name for path in matches},
            {
                "A10055.png",
                "B10055-mark.jpg",
                "高10055.png",
                "低10055.png",
                "侧10055.png",
                "10055.png",
                "低010055.webp",
            },
        )

    def test_rejects_non_numeric_image_search_identifier(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(ValueError):
                find_images_by_identifier(temp_dir, "10055a")

    def test_highlights_only_the_matching_number_and_escapes_filename_html(self):
        highlighted = highlight_identifier_in_filename(
            "A010055 & B100550.png",
            "10055",
        )

        self.assertEqual(
            highlighted,
            'A<span style="color:#e53935;font-weight:600">010055</span> &amp; B100550.png',
        )

    def test_highlights_ng_folder_in_full_image_path(self):
        highlighted = highlight_identifier_in_filename(
            r"D:\判定结果\NG异常目录\A10055.png",
            "10055",
        )

        self.assertIn(
            '<span style="color:#e53935;font-weight:600">NG异常目录</span>',
            highlighted,
        )
        self.assertIn(
            '<span style="color:#e53935;font-weight:600">10055</span>',
            highlighted,
        )

    def test_creates_clipboard_data_with_all_image_file_urls(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            nested = root / "NG"
            nested.mkdir()
            top_image = root / "A10055.png"
            nested_image = nested / "低10055.png"
            top_image.write_bytes(b"top")
            nested_image.write_bytes(b"nested")

            mime_data = create_image_clipboard_data([top_image, nested_image])

            self.assertEqual(
                [Path(url.toLocalFile()) for url in mime_data.urls()],
                [top_image.resolve(), nested_image.resolve()],
            )

    def test_clipboard_data_rejects_empty_or_missing_images(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            image = Path(temp_dir) / "missing.png"
            with self.assertRaises(ValueError):
                create_image_clipboard_data([])
            with self.assertRaises(FileNotFoundError):
                create_image_clipboard_data([image])


if __name__ == "__main__":
    unittest.main()