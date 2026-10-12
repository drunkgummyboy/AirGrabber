import unittest

from app.models import ImportResult
from app.ui.views.tracked_view import import_summary


class ImportSummaryTest(unittest.TestCase):
    def test_nothing(self) -> None:
        self.assertEqual(import_summary(ImportResult()), "Nothing to import.")

    def test_added_plural_and_singular(self) -> None:
        self.assertEqual(import_summary(ImportResult(added=("A", "B", "C"))), "Added 3 shows.")
        self.assertEqual(import_summary(ImportResult(added=("A",))), "Added 1 show.")

    def test_already_tracked(self) -> None:
        self.assertEqual(
            import_summary(ImportResult(already_tracked=("Severance",))),
            "Already tracked: 1 (Severance)",
        )

    def test_not_found(self) -> None:
        self.assertEqual(
            import_summary(ImportResult(not_found=("Foo Show", "999999"))),
            "Not found: Foo Show, 999999",
        )

    def test_failed(self) -> None:
        self.assertEqual(import_summary(ImportResult(failed=("Bar",))), "Failed (network): Bar")

    def test_all_groups(self) -> None:
        result = ImportResult(
            added=("A", "B", "C"),
            already_tracked=("Severance",),
            not_found=("Foo Show", "999999"),
            failed=("Bar",),
        )
        self.assertEqual(
            import_summary(result),
            "Added 3 shows.\n"
            "Already tracked: 1 (Severance)\n"
            "Not found: Foo Show, 999999\n"
            "Failed (network): Bar",
        )

    def test_truncates_long_groups(self) -> None:
        names = tuple(f"S{i}" for i in range(1, 14))
        self.assertEqual(
            import_summary(ImportResult(not_found=names)),
            "Not found: S1, S2, S3, S4, S5, S6, S7, S8, S9, S10 … and 3 more",
        )
        self.assertEqual(
            import_summary(ImportResult(already_tracked=names)),
            "Already tracked: 13 (S1, S2, S3, S4, S5, S6, S7, S8, S9, S10 … and 3 more)",
        )

    def test_exactly_ten_not_truncated(self) -> None:
        names = tuple(f"S{i}" for i in range(10))
        self.assertNotIn("more", import_summary(ImportResult(failed=names)))


if __name__ == "__main__":
    unittest.main()
