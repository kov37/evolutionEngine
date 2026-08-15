import tempfile
import unittest
from pathlib import Path

from status_report import classes_with_method


class StatusReportTests(unittest.TestCase):
    def test_resolves_conventional_factory_class_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "module.py"
            path.write_text(
                "class ArcsinDistribution:\n"
                "    def _cdf(self, x):\n"
                "        return x\n"
                "class Other:\n"
                "    pass\n"
            )
            self.assertEqual(
                classes_with_method(path, ["Arcsin", "Other"]),
                {"Arcsin": True, "Other": False},
            )


if __name__ == "__main__":
    unittest.main()
