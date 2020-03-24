# -----------------------------------------------------------------------------
#                     The CodeChecker Infrastructure
#   This file is distributed under the University of Illinois Open Source
#   License. See LICENSE.TXT for details.
# -----------------------------------------------------------------------------

"""
This module tests the Pipeline and JsonPipeline classes, which are used
to implement a sequence of transformations on input data.
"""

import unittest

from codechecker_ccdb_tool.pipeline import Pipeline


class PipelineTestCase(unittest.TestCase):
    """ Test the pipeline building and evaluation of pipeline steps. """
    # TODO: Increase coverage of the remaining methods inside Pipeline.

    def test_empty_pipeline_with_builtin(self):
        """Test an empty pipeline. The expected behaviour of feed is to be
        identity."""

        self.assertEqual(Pipeline().feed(0), 0)

    def test_empty_pipeline_with_user_defined(self):
        """Test an empty pipeline. The expected behaviour of feed is to be
        identity."""

        class A:
            pass

        a = A()

        self.assertEquals(Pipeline().feed(a), a)

    def test_single_transform(self):
        """Test single transform, by providing a transform function at
        construction time."""

        pipeline = Pipeline([str.upper])
        self.assertEqual(pipeline.feed('input'), 'INPUT')

    def test_single_transform_building(self):
        """Test single transform, by providing a transform function via
        a builder method."""

        pipeline = Pipeline()
        pipeline.append_transform(str.upper)
        self.assertEqual(pipeline.feed('input'), 'INPUT')

    def test_single_map_empty_list(self):
        """Test single map with an empty list as input."""

        pipeline = Pipeline()
        pipeline.append_map(str.upper)
        self.assertEqual(pipeline.feed([]), [])

    def test_single_map_non_empty(self):
        """Test single map with a non-empty list as input."""

        pipeline = Pipeline()
        pipeline.append_map(str.upper)
        self.assertEqual(pipeline.feed(['input1', 'input2']),
                         ['INPUT1', 'INPUT2'])
