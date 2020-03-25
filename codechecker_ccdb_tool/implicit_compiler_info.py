import logging
import os
import re
import subprocess
import tempfile
from collections import defaultdict
# pylint: disable=no-name-in-module
from distutils.spawn import find_executable
from shlex import shlex

from codechecker_ccdb_tool.util import load_json_or_empty

LOG = logging.getLogger('implicit_compiler_info')


def determine_compiler(gcc_command, is_executable_compiler_fun):
    """
    This function determines the compiler from the given compilation command.
    If the first part of the gcc_command is ccache invocation then the rest
    should be a complete compilation command.

    CCache may have three forms:
    1. ccache g++ main.cpp
    2. ccache main.cpp
    3. /usr/lib/ccache/gcc main.cpp
    In the first case this function drops "ccache" from gcc_command and returns
    the next compiler name.
    In the second case the compiler can be given by config files or an
    environment variable. Currently we don't handle this version, and in this
    case the compiler remanis "ccache" and the gcc_command is not changed.
    The two cases are distinguished by checking whether the second parameter is
    an executable or not.
    In the third case gcc is a symlink to ccache, but we can handle
    it as a normal compiler.

    gcc_command -- A split build action as a list which may or may not start
                   with ccache.

    TODO: The second case could be handled if there was a way for querying the
    used compiler from ccache. This can be configured for ccache in config
    files or environment variables.
    """
    if gcc_command[0].endswith('ccache'):
        if is_executable_compiler_fun(gcc_command[1]):
            return gcc_command[1]

    return gcc_command[0]


def filter_compiler_includes_extra_args(compiler_flags):
    """Return the list of flags which affect the list of implicit includes.

    compiler_flags -- A list of compiler flags which may affect the list
                      of implicit compiler include paths, like -std=,
                      --sysroot=, -m32, -m64, -nostdinc or -stdlib=.
    """
    # If these options are present in the original build command, they must
    # be forwarded to get_compiler_includes and get_compiler_defines so the
    # resulting includes point to the target that was used in the build.
    pattern = re.compile('-m(32|64)|-std=|-stdlib=|-nostdinc')
    extra_opts = list(filter(pattern.match, compiler_flags))

    pos = next((pos for pos, val in enumerate(compiler_flags)
                if val.startswith('--sysroot')), None)
    if pos is not None:
        if compiler_flags[pos] == '--sysroot':
            extra_opts.append('--sysroot=' + compiler_flags[pos + 1])
        else:
            extra_opts.append(compiler_flags[pos])

    return extra_opts


class ImplicitCompilerInfo:
    """
    This class helps to fetch and set some additional compiler flags which are
    implicitly added when using GCC.
    """
    # TODO: This dict is mapping compiler to the corresponding information.
    # It may not be enough to use the compiler as a key, because the implicit
    # information depends on other data like language or target architecture.
    compiler_info = defaultdict(dict)
    compiler_isexecutable = {}
    # Store the already detected compiler version information.
    # If the value is False the compiler is not clang otherwise the value
    # should be a clang version information object.
    compiler_versions = {}

    @staticmethod
    def c():
        return "c"

    @staticmethod
    def cpp():
        return "c++"

    @staticmethod
    def is_executable_compiler(compiler):
        if compiler not in ImplicitCompilerInfo.compiler_isexecutable:
            ImplicitCompilerInfo.compiler_isexecutable[compiler] = \
                find_executable(compiler) is not None

        return ImplicitCompilerInfo.compiler_isexecutable[compiler]

    @staticmethod
    def __get_compiler_err(cmd):
        """
        Returns the stderr of a compiler invocation as string
        or None in case of error.
        """
        try:
            proc = subprocess.Popen(
                shlex.split(cmd),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                encoding="utf-8",
                errors="ignore")

            _, err = proc.communicate("")
            return err
        except OSError as oerr:
            LOG.error("Error during process execution: " + cmd + '\n' +
                      oerr.strerror + "\n")

    @staticmethod
    def __parse_compiler_includes(lines):
        """
        Parse the compiler include paths from a string
        """
        start_mark = "#include <...> search starts here:"
        end_mark = "End of search list."

        include_paths = []

        if not lines:
            return include_paths

        do_append = False
        for line in lines.splitlines():
            if line.startswith(end_mark):
                break
            if do_append:
                line = line.strip()
                # On OSX there are framework includes,
                # where we need to strip the "(framework directory)" string.
                # For instance:
                # /System/Library/Frameworks (framework directory)
                fpos = line.find("(framework directory)")
                if fpos == -1:
                    include_paths.append(line)
                else:
                    include_paths.append(line[:fpos - 1])

            if line.startswith(start_mark):
                do_append = True

        return include_paths

    @staticmethod
    def get_compiler_includes(compiler, language, compiler_flags):
        """
        Returns a list of default includes of the given compiler.

        compiler -- The compiler binary of which the implicit include paths are
                    fetched.
        language -- The programming language being compiled (e.g. 'c' or 'c++')
        compiler_flags -- the flags used for compilation
        """
        extra_opts = filter_compiler_includes_extra_args(compiler_flags)
        cmd = compiler + " " + ' '.join(extra_opts) \
              + " -E -x " + language + " - -v "

        LOG.debug("Retrieving default includes via '" + cmd + "'")
        ICI = ImplicitCompilerInfo
        include_dirs = \
            ICI.__parse_compiler_includes(ICI.__get_compiler_err(cmd))

        return list(map(os.path.normpath, include_dirs))

    @staticmethod
    def get_compiler_target(compiler):
        """
        Returns the target triple of the given compiler as a string.

        compiler -- The compiler binary of which the target architecture is
                    fetched.
        """
        lines = ImplicitCompilerInfo.__get_compiler_err(compiler + ' -v')

        if lines is None:
            return ""

        target_label = "Target:"
        target = ""

        for line in lines.splitlines(True):
            line = line.strip().split()
            if len(line) > 1 and line[0] == target_label:
                target = line[1]

        return target

    @staticmethod
    def get_compiler_standard(compiler, language):
        """
        Returns the default compiler standard of the given compiler. The
        standard is determined by the values of __STDC_VERSION__ and
        __cplusplus predefined macros. These values are integers indicating the
        date of the standard. However, GCC supports a GNU extension for each
        standard. For sake of generality we return the GNU extended standard,
        since it should be a superset of the non-extended one, thus applicable
        in a more general manner.

        compiler -- The compiler binary of which the default compiler standard
                    is fetched.
        language -- The programming lenguage being compiled (e.g. 'c' or 'c++')
        """
        VERSION_C = """
#ifdef __STDC_VERSION__
#  if __STDC_VERSION__ >= 201710L
#    error CC_FOUND_STANDARD_VER#17
#  elif __STDC_VERSION__ >= 201112L
#    error CC_FOUND_STANDARD_VER#11
#  elif __STDC_VERSION__ >= 199901L
#    error CC_FOUND_STANDARD_VER#99
#  elif __STDC_VERSION__ >= 199409L
#    error CC_FOUND_STANDARD_VER#94
#  else
#    error CC_FOUND_STANDARD_VER#90
#  endif
#else
#  error CC_FOUND_STANDARD_VER#90
#endif
        """

        VERSION_CPP = """
#ifdef __cplusplus
#  if __cplusplus >= 201703L
#    error CC_FOUND_STANDARD_VER#17
#  elif __cplusplus >= 201402L
#    error CC_FOUND_STANDARD_VER#14
#  elif __cplusplus >= 201103L
#    error CC_FOUND_STANDARD_VER#11
#  elif __cplusplus >= 199711L
#    error CC_FOUND_STANDARD_VER#98
#  else
#    error CC_FOUND_STANDARD_VER#98
#  endif
#else
#  error CC_FOUND_STANDARD_VER#98
#endif
        """

        standard = ""
        with tempfile.NamedTemporaryFile(
                mode='w+',
                suffix=('.c' if language == 'c' else '.cpp'),
                encoding='utf-8') as source:

            with source.file as f:
                f.write(VERSION_C if language == 'c' else VERSION_CPP)

            err = ImplicitCompilerInfo. \
                __get_compiler_err(" ".join([compiler, source.name]))

            if err is not None:
                finding = re.search('CC_FOUND_STANDARD_VER#(.+)', err)
                if finding:
                    standard = finding.group(1)

        if standard:
            if standard == '94':
                # Special case for C94 standard.
                standard = '-std=iso9899:199409'
            else:
                standard = '-std=gnu' \
                           + ('' if language == 'c' else '++') \
                           + standard

        return standard

    @staticmethod
    def load_compiler_info(filename, compiler):
        """Load compiler information from a file."""
        contents = load_json_or_empty(filename, {})
        compiler_info = contents.get(compiler)
        if compiler_info is None:
            LOG.error("Could not find compiler %s in file %s",
                      compiler, filename)
            return

        ICI = ImplicitCompilerInfo

        if not ICI.compiler_info.get(compiler):
            ICI.compiler_info[compiler] = defaultdict(dict)

        # Load for language C
        ICI.compiler_info[compiler][ICI.c()]['compiler_includes'] = []
        c_lang_data = compiler_info.get(ICI.c())
        if c_lang_data:
            for element in map(shlex.split,
                               c_lang_data.get("compiler_includes")):
                element = [x for x in element if x != '-isystem']
                ICI.compiler_info[compiler][ICI.c()]['compiler_includes'] \
                    .extend(element)
            ICI.compiler_info[compiler][ICI.c()]['compiler_standard'] = \
                c_lang_data.get('compiler_standard')
            ICI.compiler_info[compiler][ICI.c()]['target'] = \
                c_lang_data.get('target')

        # Load for language C++
        ICI.compiler_info[compiler][ICI.cpp()]['compiler_includes'] = []
        cpp_lang_data = compiler_info.get(ICI.cpp())
        if cpp_lang_data:
            for element in map(shlex.split,
                               cpp_lang_data.get('compiler_includes')):
                element = [x for x in element if x != '-isystem']
                ICI.compiler_info[compiler][ICI.cpp()]['compiler_includes'] \
                    .extend(element)
            ICI.compiler_info[compiler][ICI.cpp()]['compiler_standard'] = \
                cpp_lang_data.get('compiler_standard')
            ICI.compiler_info[compiler][ICI.cpp()]['target'] = \
                cpp_lang_data.get('target')

    @staticmethod
    def set(details, compiler_info_file=None):
        """Detect and set the impicit compiler information.

        If compiler_info_file is available the implicit compiler
        information will be loaded and set from it.
        """
        ICI = ImplicitCompilerInfo
        compiler = details['compiler']
        if compiler_info_file and os.path.exists(compiler_info_file):
            # Compiler info file exists, load it.
            ICI.load_compiler_info(compiler_info_file, compiler)
        else:
            # Invoke compiler to gather implicit compiler info.
            # Independently of the actual compilation language in the
            # compile command collect the iformation for C and C++.
            if not ICI.compiler_info.get(compiler):
                ICI.compiler_info[compiler] = defaultdict(dict)

                # Collect for C
                ICI.compiler_info[compiler][ICI.c()]['compiler_includes'] = \
                    ICI.get_compiler_includes(compiler, ICI.c(),
                                              details['analyzer_options'])
                ICI.compiler_info[compiler][ICI.c()]['target'] = \
                    ICI.get_compiler_target(compiler)
                ICI.compiler_info[compiler][ICI.c()]['compiler_standard'] = \
                    ICI.get_compiler_standard(compiler, ICI.c())

                # Collect for C++
                ICI.compiler_info[compiler][ICI.cpp()]['compiler_includes'] = \
                    ICI.get_compiler_includes(compiler, ICI.cpp(),
                                              details['analyzer_options'])
                ICI.compiler_info[compiler][ICI.cpp()]['target'] = \
                    ICI.get_compiler_target(compiler)
                ICI.compiler_info[compiler][ICI.cpp()]['compiler_standard'] = \
                    ICI.get_compiler_standard(compiler, ICI.cpp())

        def set_details_from_ICI(key, lang):
            """Set compiler related information in the 'details' dictionary.

            If the language dependent value is not set yet, get the compiler
            information from ICI.
            """

            parsed_value = details[key].get(lang)
            if parsed_value:
                details[key][lang] = parsed_value
            else:
                # Only set what is available from ICI.
                compiler_data = ICI.compiler_info.get(compiler)
                if compiler_data:
                    language_data = compiler_data.get(lang)
                    if language_data:
                        details[key][lang] = language_data.get(key)

        set_details_from_ICI('compiler_includes', ICI.c())
        set_details_from_ICI('compiler_standard', ICI.c())
        set_details_from_ICI('target', ICI.c())

        set_details_from_ICI('compiler_includes', ICI.cpp())
        set_details_from_ICI('compiler_standard', ICI.cpp())
        set_details_from_ICI('target', ICI.cpp())

    @staticmethod
    def get():
        return ImplicitCompilerInfo.compiler_info
