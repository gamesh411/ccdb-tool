# -------------------------------------------------------------------------
#                     The CodeChecker Infrastructure
#   This file is distributed under the University of Illinois Open Source
#   License. See LICENSE.TXT for details.
# -------------------------------------------------------------------------
"""Parse the 'clang --version' command output."""

import re
import subprocess


class ClangVersionInfo(object):
    """ClangVersionInfo holds the version information of the used Clang."""

    def __init__(self,
                 major_version=None,
                 minor_version=None,
                 patch_version=None,
                 installed_dir=None,
                 vendor=None):
        self.major_version = int(major_version)
        self.minor_version = int(minor_version)
        self.patch_version = int(patch_version)
        self.installed_dir = str(installed_dir)
        self.vendor = str(vendor)


class ClangVersionInfoParser(object):
    """
    ClangVersionInfoParser is responsible for creating ClangVersionInfo
    instances from the version output of Clang.
    """

    def __init__(self):
        self.clang_version_pattern = (
            r"(?P<vendor>clang|Apple LLVM) version (?P<major_version>[0-9]+)"
            r"\.(?P<minor_version>[0-9]+)\.(?P<patch_version>[0-9]+)")

        self.clang_installed_dir_pattern = \
            r"InstalledDir: (?P<installed_dir>[^\s]*)"

    def parse(self, version_string):
        """Try to parse the version string using the predefined patterns."""
        version_match = re.search(self.clang_version_pattern, version_string)
        installed_dir_match = re.search(
            self.clang_installed_dir_pattern, version_string)

        if not version_match or not installed_dir_match:
            return False

        return ClangVersionInfo(
            version_match.group('major_version'),
            version_match.group('minor_version'),
            version_match.group('patch_version'),
            installed_dir_match.group('installed_dir'),
            version_match.group('vendor'))


def get(clang_binary, env=None):
    """Get and parse the version information from given clang binary

    Should return False for getting the version
    information not from a clang compiler.
    """
    compiler_version = subprocess.check_output(
        [clang_binary, '--version'],
        env=env,
        encoding="utf-8",
        errors="ignore")
    version_parser = ClangVersionInfoParser()
    version_info = version_parser.parse(compiler_version)
    return version_info
