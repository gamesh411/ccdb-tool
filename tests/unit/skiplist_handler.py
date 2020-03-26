# -------------------------------------------------------------------------
#                     The CodeChecker Infrastructure
#   This file is distributed under the University of Illinois Open Source
#   License. See LICENSE.TXT for details.
# -------------------------------------------------------------------------
"""
"""


import fnmatch
import re
import os

from logging import getLogger

LOG = getLogger('skiplist_handler')


class SkipListHandler(object):
    """
    Skiplist file format:

    -/skip/all/source/in/directory*
    -/do/not/check/this.file
    +/dir/check.this.file
    -/dir/*
    """

    def __init__(self, skip_file_content=""):
        """
        Process the lines of the skip file.
        """
        self.__skip = []

        self.__skip_file_lines = [line.strip() for line
                                  in skip_file_content.splitlines()
                                  if line.strip()]

        valid_lines = self.__check_line_format(self.__skip_file_lines)
        self.__gen_regex(valid_lines)

    def __gen_regex(self, skip_lines):
        """
        Generate a regular expression from the given skip lines
        and collect them for later match.

        The lines should be checked for validity before generating
        the regular expressions.
        """
        for skip_line in skip_lines:
            norm_skip_path = os.path.normpath(skip_line[1:].strip())
            rexpr = re.compile(
                fnmatch.translate(norm_skip_path + '*'))
            self.__skip.append((skip_line, rexpr))

    def __check_line_format(self, skip_lines):
        """
        Check if the skip line is given in a valid format.
        Returns the list of valid lines.
        """
        valid_lines = []
        for line in skip_lines:
            if len(line) < 2 or line[0] not in ['-', '+']:
                LOG.warning("Skipping malformed skipfile pattern: %s", line)
                continue

            valid_lines.append(line)

        return valid_lines

    @property
    def skip_file_lines(self):
        """
        List of the lines from the skip file without changes.
        """
        return self.__skip_file_lines

    def overwrite_skip_content(self, skip_lines):
        """
        Cleans out the already collected skip regular expressions
        and rebuilds the list from the given skip_lines.
        """
        self.__skip = []
        valid_lines = self.__check_line_format(skip_lines)
        self.__gen_regex(valid_lines)

    def should_skip(self, source):
        """
        Check if the given source should be skipped.
        Should the analyzer skip the given source file?
        """
        if not self.__skip:
            return False

        for line, rexpr in self.__skip:
            if rexpr.match(source):
                sign = line[0]
                return sign == '-'
        return False
