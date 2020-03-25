import os
from collections import defaultdict
from shlex import shlex

from .build_action import BuildAction


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



class BuildActionBuilder:

    @staticmethod
    def __empty_build_action_data(cls):
        return {
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

    @staticmethod
    def make_empty(cls):
        return BuildAction(**cls.__empty_build_action_data())

    @staticmethod
    def form_compilation_database_entry(cls, compilation_db_entry):
        details = cls.__empty_build_action_data()
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
