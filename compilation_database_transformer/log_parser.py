# -------------------------------------------------------------------------
#                     The CodeChecker Infrastructure
#   This file is distributed under the University of Illinois Open Source
#   License. See LICENSE.TXT for details.
# -------------------------------------------------------------------------


from collections import defaultdict
# pylint: disable=no-name-in-module
from distutils.spawn import find_executable

import glob
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import tempfile
import traceback

from compilation_database_transformer.build_action import BuildAction
from compilation_database_transformer.clangsa_version \
    import get as clangsa_version_get
from compilation_database_transformer import gcc_toolchain
from compilation_database_transformer.util import load_json_or_empty

LOG = logging.getLogger('buildlogger')

SOURCE_EXTENSIONS = {".c", ".cc", ".cp", ".cpp", ".cxx", ".c++", ".o", ".so",
                     ".a"}

# Replace gcc/g++ build target options with values accepted by Clang.
REPLACE_OPTIONS_MAP = {
    '-mips32': ['-target', 'mips', '-mips32'],
    '-mips64': ['-target', 'mips64', '-mips64'],
    '-mpowerpc': ['-target', 'powerpc'],
    '-mpowerpc64': ['-target', 'powerpc64']
}

# The compilation flags of which the prefix is any of these regular expressions
# will not be included in the output Clang command.
# These flags should be ignored only in case the original compiler is clang.
IGNORED_OPTIONS_CLANG = [
    # Clang gives different warnings than GCC. Thus if these flags are kept,
    # '-Werror', '-pedantic-errors' the analysis with Clang can fail even
    # if the compilation passes with GCC.
    '-Werror',
    '-pedantic-errors',

    # Remove '-w' the option supressing the warnings.
    # This suppressing mechanism is independent of
    # checker enabling/disabling (-W, -W-no), and
    # cannot be overridden by those.
    '-w'
]

# The compilation flags of which the prefix is any of these regular expressions
# will not be included in the output Clang command.
# These flags should be ignored only in case the original compiler is gcc.
IGNORED_OPTIONS_GCC = [
    # --- UNKNOWN BY CLANG --- #
    '-fallow-fetchr-insn',
    '-fcall-saved-',
    '-fcond-mismatch',
    '-fconserve-stack',
    '-fcrossjumping',
    '-fcse-follow-jumps',
    '-fcse-skip-blocks',
    '-ffixed-r2',
    '-ffp$',
    '-fgcse-lm',
    '-fhoist-adjacent-loads',
    '-findirect-inlining',
    '-finline-limit',
    '-finline-local-initialisers',
    '-fipa-sra',
    '-fno-aggressive-loop-optimizations',
    '-fno-delete-null-pointer-checks',
    '-fno-jump-table',
    '-fno-keep-static-consts',
    '-fno-strength-reduce',
    '-fno-toplevel-reorder',
    '-fno-unit-at-a-time',
    '-fno-var-tracking-assignments',
    '-fobjc-link-runtime',
    '-fpartial-inlining',
    '-fpeephole2',
    '-fr$',
    '-fregmove',
    '-frename-registers',
    '-freorder-functions',
    '-frerun-cse-after-loop',
    '-fs$',
    '-fsched-spec',
    '-fstack-reuse',
    '-fthread-jumps',
    '-ftree-pre',
    '-ftree-switch-conversion',
    '-ftree-tail-merge',
    '-m(no-)?abm',
    '-m(no-)?sdata',
    '-m(no-)?spe',
    '-m(no-)?string$',
    '-m(no-)?dsbt',
    '-m(no-)?fixed-ssp',
    '-m(no-)?pointers-to-nested-functions',
    '-mpcrel-func-addr',
    '-maccumulate-outgoing-args',
    '-mcall-aixdesc',
    '-mppa3-addr-bug',
    '-mtraceback=',
    '-mtext=',
    '-misa=',
    '-mfunction-return=',
    '-mindirect-branch-register',
    '-mindirect-branch=',
    '-mfix-cortex-m3-ldrd$',
    '-mmultiple$',
    '-msahf$',
    '-mthumb-interwork$',
    '-mupdate$',

    # Deprecated ARM specific option
    # to Generate a stack frame that is compliant
    # with the ARM Procedure Call Standard.
    '-mapcs',
    '-fno-merge-const-bfstores$',
    '-fno-ipa-sra$',
    '-mno-thumb-interwork$',
    # ARM specific option.
    # Prevent the reordering of
    # instructions in the function prologue.
    '-mno-sched-prolog',
    # This is not unknown but we want to preserve asserts to improve the
    # quality of analysis.
    '-DNDEBUG$',

    # --- IGNORED --- #
    '-save-temps',
    # Clang gives different warnings than GCC. Thus if these flags are kept,
    # '-Werror', '-pedantic-errors' the analysis with Clang can fail even
    # if the compilation passes with GCC.
    '-Werror',
    '-pedantic-errors',
    # Remove the option disabling the warnings.
    '-w',
    '-g(.+)?$',
    # Link Time Optimization:
    '-flto',
    # MicroBlaze Options:
    '-mxl',
    # PowerPC SPE Options:
    '-mfloat-gprs',
    '-mabi'
]

IGNORED_OPTIONS_GCC = re.compile('|'.join(IGNORED_OPTIONS_GCC))
IGNORED_OPTIONS_CLANG = re.compile('|'.join(IGNORED_OPTIONS_CLANG))

# The compilation flags of which the prefix is any of these regular expressions
# will not be included in the output Clang command. These flags have further
# parameters which are also omitted. The number of parameters is indicated in
# this dictionary.
IGNORED_PARAM_OPTIONS = {
    re.compile('-install_name'): 1,
    re.compile('-exported_symbols_list'): 1,
    re.compile('-current_version'): 1,
    re.compile('-compatibility_version'): 1,
    re.compile('-init$'): 1,
    re.compile('-e$'): 1,
    re.compile('-seg1addr'): 1,
    re.compile('-bundle_loader'): 1,
    re.compile('-multiply_defined'): 1,
    re.compile('-sectorder'): 3,
    re.compile('--param$'): 1,
    re.compile('-u$'): 1,
    re.compile('--serialize-diagnostics'): 1,
    re.compile('-framework'): 1,
    # Darwin linker can be given a file with lists the sources for linking.
    re.compile('-filelist'): 1
}


COMPILE_OPTIONS = [
    '-nostdinc',
    r'-nostdinc\+\+',
    '-pedantic',
    '-O[1-3]',
    '-Os',
    '-std=',
    '-stdlib=',
    '-f',
    '-m',
    '-Wno-',
    '--sysroot=',
    '--gcc-toolchain='
]

COMPILE_OPTIONS = re.compile('|'.join(COMPILE_OPTIONS))

COMPILE_OPTIONS_MERGED = [
    '--sysroot',
    '--include',
    '-include',
    '-iquote',
    '-[DIUF]',
    '-idirafter',
    '-isystem',
    '-macros',
    '-isysroot',
    '-iprefix',
    '-iwithprefix',
    '-iwithprefixbefore'
]

INCLUDE_OPTIONS_MERGED = [
    '-iquote',
    '-[IF]',
    '-isystem',
    '-iprefix',
    '-iwithprefix',
    '-iwithprefixbefore'
]

XCLANG_FLAGS_TO_SKIP = ['-module-file-info',
                        '-S',
                        '-emit-llvm',
                        '-emit-llvm-bc',
                        '-emit-llvm-only',
                        '-emit-llvm-uselists',
                        '-rewrite-objc']

COMPILE_OPTIONS_MERGED = \
    re.compile('(' + '|'.join(COMPILE_OPTIONS_MERGED) + ')')

INCLUDE_OPTIONS_MERGED = \
    re.compile('(' + '|'.join(INCLUDE_OPTIONS_MERGED) + ')')


PRECOMPILATION_OPTION = re.compile('-(E|M[G|T|Q|F|J|P|V|M]*)$')

# Match for all of the compiler flags.
CLANG_OPTIONS = re.compile('.*')


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


class ImplicitCompilerInfo(object):
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


class OptionIterator(object):

    def __init__(self, args):
        self._item = None
        self._it = iter(args)

    def __next__(self):
        self._item = next(self._it)
        return self

    next = __next__

    def __iter__(self):
        return self

    @property
    def item(self):
        return self._item


def get_language(extension):
    # TODO: There are even more in the man page of gcc.
    mapping = {'.c': 'c',
               '.cp': 'c++',
               '.cpp': 'c++',
               '.cxx': 'c++',
               '.txx': 'c++',
               '.cc': 'c++',
               '.C': 'c++',
               '.ii': 'c++',
               '.m': 'objective-c',
               '.mm': 'objective-c++'}
    return mapping.get(extension)


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


def __is_not_include_fixed(dirname):
    """
    This function returns True in case the given dirname is NOT a GCC-specific
    include-fixed directory containing standard headers.
    """
    return os.path.basename(os.path.normpath(dirname)) != 'include-fixed'


def __contains_no_intrinsic_headers(dirname):
    """
    Returns True if the given directory doesn't contain any intrinsic headers.
    """
    if not os.path.exists(dirname):
        return True
    if glob.glob(os.path.join(dirname, "*intrin.h")):
        return False
    return True


def __collect_clang_compile_opts(flag_iterator, details):
    """Collect all the options for clang do not filter anything."""
    if CLANG_OPTIONS.match(flag_iterator.item):
        details['analyzer_options'].append(flag_iterator.item)
        return True


def __collect_transform_xclang_opts(flag_iterator, details):
    """Some specific -Xclang constucts need to be filtered out.

       To generate the proper plist reports and not LLVM IR or
       ASCII text as an output the flags need to be removed.
    """
    if flag_iterator.item == "-Xclang":
        next(flag_iterator)
        next_flag = flag_iterator.item
        if next_flag in XCLANG_FLAGS_TO_SKIP:
            return True

        details['analyzer_options'].extend(["-Xclang", next_flag])
        return True

    return False


def __collect_transform_include_opts(flag_iterator, details):
    """
    This function collects the compilation (i.e. not linker or preprocessor)
    flags to the buildaction.
    """

    m = COMPILE_OPTIONS_MERGED.match(flag_iterator.item)

    if m:
        flag = m.group(0)
        together = len(flag) != len(flag_iterator.item)

        if together:
            param = flag_iterator.item[len(flag):]
        else:
            next(flag_iterator)
            param = flag_iterator.item

        # The .plist file contains a section with a list of files. For some
        # further actions these need to be given with an absolute path. Clang
        # prints them with absolute path if the original compiler invocation
        # was given absolute paths.
        # TODO: If Clang will be extended with an extra analyzer option in
        # order to print these absolute paths natively, this conversion will
        # not be necessary.
        flags_with_path = ['-I', '-idirafter', '-imultilib',
                           '-iquote', '-isysroot', '-isystem',
                           '-iwithprefix', '-iwithprefixbefore', '-sysroot',
                           '--sysroot']
        if flag in flags_with_path:
            # --sysroot format can be --sysroot=/path/to/include
            # in this case before the normalization the '='
            # sign must be removed.
            # We put back the original
            # --sysroot=/path/to/include as
            # --sysroot /path/to/include
            # which is a valid format too.
            if param.startswith("="):
                param = param[1:]
                together = False
            param = os.path.normpath(
                os.path.join(details['directory'], param))

        if together:
            details['analyzer_options'].append(flag + param)
        else:
            details['analyzer_options'].extend([flag, param])

        return True
    return False


def __collect_compile_opts(flag_iterator, details):
    """
    This function collects the compilation (i.e. not linker or preprocessor)
    flags to the buildaction.
    """
    if COMPILE_OPTIONS.match(flag_iterator.item):
        details['analyzer_options'].append(flag_iterator.item)
        return True

    return False


def __skip_sources(flag_iterator, _):
    """
    This function skips the compiled source file names (i.e. the arguments
    which don't start with a dash character).
    """
    if flag_iterator.item[0] != '-':
        return True

    return False


def __determine_action_type(flag_iterator, details):
    """
    This function determines whether this is a preprocessing, compilation or
    linking action and sets it in the buildaction object. If the action type is
    set to COMPILE earlier then we don't set it to anything else.
    """
    if flag_iterator.item == '-c':
        details['action_type'] = BuildAction.COMPILE
        return True
    elif flag_iterator.item.startswith('-print-prog-name'):
        if details['action_type'] != BuildAction.COMPILE:
            details['action_type'] = BuildAction.INFO
        return True
    elif PRECOMPILATION_OPTION.match(flag_iterator.item):
        if details['action_type'] != BuildAction.COMPILE:
            details['action_type'] = BuildAction.PREPROCESS
        return True

    return False


def __get_arch(flag_iterator, details):
    """
    This function consumes -arch flag which is followed by the target
    architecture. This is then collected to the buildaction object.
    """
    # TODO: Is this really a target architecture? Have we seen this flag being
    # used in a real project? This -arch flag is not really documented among
    # GCC flags.
    # Where do we use this architecture during analysis and why?
    if flag_iterator.item == '-arch':
        next(flag_iterator)
        details['arch'] = flag_iterator.item
        return True

    return False


def __get_language(flag_iterator, details):
    """
    This function consumes -x flag which is followed by the language. This
    language is then collected to the buildaction object.
    """
    # TODO: Known issue: a -x flag may precede all source files in the build
    # command with different languages.
    if flag_iterator.item.startswith('-x'):
        if flag_iterator.item == '-x':
            next(flag_iterator)
            details['lang'] = flag_iterator.item
        else:
            details['lang'] = flag_iterator.item[2:]  # 2 == len('-x')
        return True
    return False


def __get_output(flag_iterator, details):
    """
    This function consumes -o flag which is followed by the output file of the
    action. This file is then collected to the buildaction object.
    """
    if flag_iterator.item == '-o':
        next(flag_iterator)
        details['output'] = flag_iterator.item
        return True

    return False


def __replace(flag_iterator, details):
    """
    This function extends the analyzer options list with the corresponding
    replacement based on REPLACE_OPTIONS_MAP if the flag_iterator is currently
    pointing to a flag to replace.
    """
    value = REPLACE_OPTIONS_MAP.get(flag_iterator.item)

    if value:
        details['analyzer_options'].extend(value)

    return bool(value)


def __skip_clang(flag_iterator, _):
    """
    This function skips the flag pointed by the given flag_iterator with its
    parameters if any.
    """
    if IGNORED_OPTIONS_CLANG.match(flag_iterator.item):
        return True

    return False


def __skip_gcc(flag_iterator, _):
    """
    This function skips the flag pointed by the given flag_iterator with its
    parameters if any.
    """
    if IGNORED_OPTIONS_GCC.match(flag_iterator.item):
        return True

    for pattern, arg_num in IGNORED_PARAM_OPTIONS.items():
        if pattern.match(flag_iterator.item):
            for _ in range(arg_num):
                next(flag_iterator)
            return True

    return False


def parse_options(compilation_db_entry,
                  compiler_info_file=None,
                  keep_gcc_include_fixed=False,
                  keep_gcc_intrin=False,
                  get_clangsa_version_func=None,
                  env=None):
    """
    This function parses a GCC compilation action and returns a BuildAction
    object which can be the input of Clang analyzer tools.

    compilation_db_entry -- An entry from a valid compilation database JSON
                            file, i.e. a dictionary with the compilation
                            command, the compiled file and the current working
                            directory.
    compiler_info_file -- Contains the path to a compiler info file.
    keep_gcc_include_fixed -- There are some implicit include paths which are
                              only used by GCC (include-fixed). This flag
                              determines whether these should be kept among
                              the implicit include paths.
    keep_gcc_intrin -- There are some implicit include paths which contain
                       GCC-specific header files (those which end with
                       intrin.h). This flag determines whether these should be
                       kept among the implicit include paths. Use this flag if
                       Clang analysis fails with error message related to
                       __builtin symbols.
    get_clangsa_version_func -- Is a function which should return the
                            version information for a clang compiler.
                            It requires the compiler binary and an env.
                            get_clangsa_version_func(compiler_binary, env)
                            Should return false for a non clang compiler.
    env -- Is the environment where a subprocess call should be executed.
    """
    details = {
        'analyzer_options': [],
        'compiler_includes': defaultdict(dict),  # For each language c/cpp.
        'compiler_standard': defaultdict(dict),  # For each language c/cpp.
        'analyzer_type': -1,
        'original_command': '',
        'directory': '',
        'output': '',
        'lang': None,
        'arch': '',  # Target in the compile command set by -arch.
        'target': defaultdict(dict),
        'source': ''}

    if 'arguments' in compilation_db_entry:
        gcc_command = compilation_db_entry['arguments']
        details['original_command'] = ' '.join(gcc_command)
    elif 'command' in compilation_db_entry:
        details['original_command'] = compilation_db_entry['command']
        gcc_command = shlex.split(compilation_db_entry['command'])
    else:
        raise KeyError("No valid 'command' or 'arguments' entry found!")

    details['directory'] = compilation_db_entry['directory']
    details['action_type'] = None
    details['compiler'] = \
        determine_compiler(gcc_command,
                           ImplicitCompilerInfo.is_executable_compiler)
    if '++' in os.path.basename(details['compiler']):
        details['lang'] = 'c++'

    # Source files are skipped first so they are not collected
    # with the other compiler flags together. Source file is handled
    # separately from the compile command json.
    clang_flag_collectors = [
        __skip_sources,
        __skip_clang,
        __collect_transform_xclang_opts,
        __get_output,
        __determine_action_type,
        __get_arch,
        __get_language,
        __collect_transform_include_opts,
        __collect_clang_compile_opts
    ]

    gcc_flag_transformers = [
        __skip_gcc,
        __replace,
        __collect_compile_opts,
        __collect_transform_include_opts,
        __determine_action_type,
        __skip_sources,
        __get_arch,
        __get_language,
        __get_output]

    flag_processors = gcc_flag_transformers

    compiler_version_info = \
        ImplicitCompilerInfo.compiler_versions.get(
            details['compiler'], False)

    if not compiler_version_info and get_clangsa_version_func:

        # did not find in the cache yet
        try:
            compiler_version_info = \
                get_clangsa_version_func(details['compiler'], env)
        except (subprocess.CalledProcessError, OSError) as cerr:
            LOG.error('Failed to get and parse version of: %s',
                      details['compiler'])
            LOG.error(cerr)
            compiler_version_info = False

    ImplicitCompilerInfo.compiler_versions[details['compiler']] \
        = compiler_version_info

    using_clang_to_compile_and_analyze = False
    if ImplicitCompilerInfo.compiler_versions[details['compiler']]:
        # Based on the version information the compiler is clang.
        using_clang_to_compile_and_analyze = True
        flag_processors = clang_flag_collectors

    for it in OptionIterator(gcc_command[1:]):
        for flag_processor in flag_processors:
            if flag_processor(it, details):
                break
        else:
            pass
            # print('Unhandled argument: ' + it.item)

    if details['action_type'] is None:
        details['action_type'] = BuildAction.COMPILE

    details['source'] = compilation_db_entry['file']

    # In case the file attribute in the entry is empty.
    if details['source'] == '.':
        details['source'] = ''

    lang = get_language(os.path.splitext(details['source'])[1])
    if lang:
        if details['lang'] is None:
            details['lang'] = lang
    else:
        details['action_type'] = BuildAction.LINK

    # Option parser detects target architecture but does not know about the
    # language during parsing. Set the collected compilation target for the
    # language detected language.
    details['target'][lang] = details['arch']

    # With gcc-toolchain a non default compiler toolchain can be set. Clang
    # will search for include paths and libraries based on the gcc-toolchain
    # parameter. Detecting extra include paths from the host compiler could
    # conflict with this.

    # For example if the compiler in the compile command is clang and
    # gcc-toolchain is set we will get the include paths for clang and not for
    # the compiler set in gcc-toolchain. This can cause missing headers during
    # the analysis.

    toolchain = \
        gcc_toolchain.toolchain_in_args(details['analyzer_options'])

    # Store the compiler built in include paths and defines.
    # If clang compiler is used for compilation and analysis,
    # do not collect the implicit include paths.
    if (not toolchain and not using_clang_to_compile_and_analyze) or \
            (compiler_info_file and os.path.exists(compiler_info_file)):
        ImplicitCompilerInfo.set(details, compiler_info_file)

    if not keep_gcc_include_fixed:
        for lang, includes in details['compiler_includes'].items():
            details['compiler_includes'][lang] = \
                list(filter(__is_not_include_fixed, includes))

    if not keep_gcc_intrin:
        for lang, includes in details['compiler_includes'].items():
            details['compiler_includes'][lang] = \
                list(filter(__contains_no_intrinsic_headers, includes))

        # filter out intrin directories
        aop_without_intrin = []
        analyzer_options = iter(details['analyzer_options'])

        for aopt in analyzer_options:
            m = INCLUDE_OPTIONS_MERGED.match(aopt)
            if m:
                flag = m.group(0)
                together = len(flag) != len(aopt)

                if together:
                    value = aopt[len(flag):]
                else:
                    flag = aopt
                    value = next(analyzer_options)
                if os.path.isdir(value) and __contains_no_intrinsic_headers(
                        value) or not os.path.isdir(value):
                    if together:
                        aop_without_intrin.append(aopt)
                    else:
                        aop_without_intrin.append(flag)
                        aop_without_intrin.append(value)
            else:
                # no match
                aop_without_intrin.append(aopt)

        details['analyzer_options'] = aop_without_intrin

    return BuildAction(**details)


def process_response_file(response_file):
    """
    Return list of options and source files from the given response file.
    """
    with open(response_file, encoding="utf-8", errors="ignore") as r_file:
        options = shlex.split(r_file)

    sources = [opt for opt in options if not opt.startswith('-') and
               os.path.splitext(opt)[1].lower() in SOURCE_EXTENSIONS]

    return options, sources


def extend_compilation_database_entries(compilation_database):
    """
    Loop through the compilation database entries and whether compilation
    command contains a response file we read those files and replace the
    response file with the options from the file.
    """
    entries = []
    for entry in compilation_database:
        if 'command' in entry and '@' in entry['command']:
            cmd = []
            source_files = []
            source_dir = entry['directory']

            options = shlex.split(entry['command'])
            for opt in options:
                if opt.startswith('@'):
                    response_file = os.path.join(source_dir, opt[1:])
                    if not os.path.exists(response_file):
                        LOG.warning("Response file '%s' does not exists.",
                                    response_file)
                        continue

                    opts, sources = process_response_file(response_file)
                    cmd.extend(opts)
                    source_files.extend(sources)
                else:
                    cmd.append(opt)

            entry['command'] = ' '.join(cmd)

            if '@' in entry['file']:
                for source_file in source_files:
                    new_entry = dict(entry)
                    new_entry['file'] = source_file
                    entries.append(new_entry)
                continue

        entries.append(entry)

    return entries


class CompileCommandEncoder(json.JSONEncoder):
    """JSON serializer for objects not serializable by default json code"""
    # pylint: disable=method-hidden
    def default(self, o):
        if isinstance(o, BuildAction):
            return o.to_dict()
        # Let the base class default method raise the TypeError
        return json.JSONEncoder.default(self, o)


class CompileActionUniqueingType(object):
    NONE = 0  # Full Action text
    SOURCE_ALPHA = 1  # Based on source file, uniqueing by
    # on alphanumerically first target
    SOURCE_REGEX = 2  # Based on source file, uniqueing by regex filter
    STRICT = 3  # Gives error in case of duplicate


def parse_unique_log(compilation_database,
                     report_dir,
                     compile_uniqueing="none",
                     compiler_info_file=None,
                     keep_gcc_include_fixed=False,
                     keep_gcc_intrin=False,
                     analysis_skip_handler=None,
                     pre_analysis_skip_handler=None,
                     ctu_or_stats_enabled=False,
                     env=None):
    """
    This function reads up the compilation_database
    and returns with a list of build actions that is
    prepared (uniqued and skipped) for clang execution together
    with the number of skipped compile commands.

    That means that gcc specific parameters are filtered out
    and gcc built in targets and include paths are added.
    It also filters out duplicate compilation actions based on the
    compile_uniqueing parameter.
    This function also dumps auto-detected the compiler info
    into <report_dir>/compiler_info.json.

    compilation_database -- A compilation database as a list of dict objects.
                            These object should contain "file", "dictionary"
                            and "command" keys. The "command" may be replaced
                            by "arguments" which is a split command. Older
                            versions of intercept-build provide the build
                            command this way.
    report_dir  -- The output report directory. The compiler infos
                   will be written to <report_dir>/compiler.info.json.
    compile_uniqueing -- Compilation database uniqueing mode.
                         If there are more than one compile commands for a
                         target file, only a single one is kept.
    compiler_info_file -- compiler_info.json. If exists, it will be used for
                    analysis.
    keep_gcc_include_fixed -- There are some implicit include paths which are
                              only used by GCC (include-fixed). This flag
                              determines whether these should be kept among
                              the implicit include paths.
    keep_gcc_intrin -- There are some implicit include paths which contain
                       GCC-specific header files (those which end with
                       intrin.h). This flag determines whether these should be
                       kept among the implicit include paths. Use this flag if
                       Clang analysis fails with error message related to
                       __builtin symbols.

    Separate skip handlers are required because it is possible that different
    files are skipped during pre analysis and the actual analysis. In the
    pre analysis step nothing should be skipped to collect the required
    information for the analysis step where not all the files are analyzed.

    analysis_skip_handler -- skip handler for files which should be skipped
                             during analysis
    pre_analysis_skip_handler -- skip handler for files wich should be skipped
                                 during pre analysis
    ctu_or_stats_enabled -- ctu or statistics based analysis was enabled
                            influences the behavior which files are skipped.
    env -- Is the environment where a subprocess call should be executed.
    """
    try:
        uniqued_build_actions = dict()

        if compile_uniqueing == "alpha":
            build_action_uniqueing = CompileActionUniqueingType.SOURCE_ALPHA
        elif compile_uniqueing == "none":
            build_action_uniqueing = CompileActionUniqueingType.NONE
        elif compile_uniqueing == "strict":
            build_action_uniqueing = CompileActionUniqueingType.STRICT
        else:
            build_action_uniqueing = CompileActionUniqueingType.SOURCE_REGEX
            uniqueing_re = re.compile(compile_uniqueing)

        skipped_cmp_cmd_count = 0

        for entry in extend_compilation_database_entries(compilation_database):
            # Normalization needs to be done here, because the skip regex
            # won't match properly in the skiplist handler.
            entry['file'] = os.path.normpath(
                os.path.join(entry['directory'], entry['file']))
            # Skip parsing the compilaton commands if it should be skipped
            # at both analysis phases (pre analysis and analysis).
            # Skipping of the compile commands is done differently if no
            # CTU or statistics related feature was enabled.
            if analysis_skip_handler \
                and analysis_skip_handler.should_skip(entry['file']) \
                and (not ctu_or_stats_enabled or pre_analysis_skip_handler
                     and pre_analysis_skip_handler.should_skip(entry['file'])):
                skipped_cmp_cmd_count += 1
                continue

            action = parse_options(entry,
                                   compiler_info_file,
                                   keep_gcc_include_fixed,
                                   keep_gcc_intrin,
                                   clangsa_version_get,
                                   env)

            if not action.lang:
                continue
            if action.action_type != BuildAction.COMPILE:
                continue
            if build_action_uniqueing == CompileActionUniqueingType.NONE:
                if action.__hash__ not in uniqued_build_actions:
                    uniqued_build_actions[action.__hash__] = action
            elif build_action_uniqueing == CompileActionUniqueingType.STRICT:
                if action.source not in uniqued_build_actions:
                    uniqued_build_actions[action.source] = action
                else:
                    LOG.error("Build Action uniqueing failed"
                              " as both '%s' and '%s'",
                              uniqued_build_actions[action.source]
                              .original_command,
                              action.original_command)
                    sys.exit(1)
            elif build_action_uniqueing == \
                    CompileActionUniqueingType.SOURCE_ALPHA:
                if action.source not in uniqued_build_actions:
                    uniqued_build_actions[action.source] = action
                elif action.output < \
                        uniqued_build_actions[action.source].output:
                    uniqued_build_actions[action.source] = action
            elif build_action_uniqueing == \
                    CompileActionUniqueingType.SOURCE_REGEX:
                LOG.debug("uniqueing regex")
                if action.source not in uniqued_build_actions:
                    uniqued_build_actions[action.source] = action
                elif uniqueing_re.match(action.original_command) and \
                    not uniqueing_re.match(
                        uniqued_build_actions[action.source].original_command):
                    uniqued_build_actions[action.source] = action
                elif uniqueing_re.match(action.original_command) and \
                    uniqueing_re.match(
                        uniqued_build_actions[action.source].original_command):
                    LOG.error("Build Action uniqueing failed as both \n %s"
                              "\n and \n %s \n match regex pattern:%s",
                              uniqued_build_actions[action.source].
                              original_command,
                              action.original_command,
                              compile_uniqueing)
                    sys.exit(1)

        compiler_info_out = os.path.join(report_dir, "compiler_info.json")
        with open(compiler_info_out, 'w',
                  encoding="utf-8", errors="ignore") as f:
            LOG.debug("Writing compiler info into:"+compiler_info_out)
            json.dump(ImplicitCompilerInfo.get(), f)

        LOG.debug('Parsing log file done.')
        return list(uniqued_build_actions.values()), skipped_cmp_cmd_count

    except (ValueError, KeyError, TypeError) as ex:
        if not compilation_database:
            LOG.error('The compile database is empty.')
        else:
            LOG.error('The compile database is not valid.')
        LOG.debug(traceback.format_exc())
        LOG.debug(ex)
        sys.exit(1)
