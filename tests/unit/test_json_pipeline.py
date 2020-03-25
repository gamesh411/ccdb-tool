import unittest

from codechecker_ccdb_tool.pipeline import JsonPipeline

class JsonPipelineTestCase(unittest.TestCase):
    """Test the JSON-reading pipeline."""

    def test_empty_json(self):
        self.assertEqual(True, False)

