# Welcome to the PyTorch setup.py.
#
# Environment variables you are probably interestd in:
#
#   DEBUG
#     build with -O0 and -g (debug symbols)
#
#   MAX_JOBS
#     maximum number of compile jobs we should use to compile your code
#
#   NO_CUDA
#     disables CUDA build
#
#   CFLAGS
#     flags to apply to both C and C++ files to be compiled (a quirk of setup.py
#     which we have faithfully adhered to in our build system is that CFLAGS
#     also applies to C++ files, in contrast to the default behavior of autogoo
#     and cmake build systems.)
#
#   CC
#     the C/C++ compiler to use (NB: the CXX flag has no effect for distutils
#     compiles, because distutils always uses CC to compile, even for C++
#     files.
#
# Environment variables for feature toggles:
#
#   NO_CUDNN
#     disables the cuDNN build
#
#   NO_NNPACK
#     disables NNPACK build
#
#   NO_DISTRIBUTED
#     disables THD (distributed) build
#
#   NO_SYSTEM_NCCL
#     disables use of system-wide nccl (we will use our submoduled
#     copy in torch/lib/nccl)
#
#   WITH_GLOO_IBVERBS
#     toggle features related to distributed support
#
#   PYTORCH_BINARY_BUILD
#     toggle static linking against libstdc++, used when we're building
#     binaries for distribution
#
#   PYTORCH_BUILD_VERSION
#   PYTORCH_BUILD_NUMBER
#     specify the version of PyTorch, rather than the hard-coded version
#     in this file; used when we're building binaries for distribution
#
# Environment variables we respect (these environment variables are
# conventional and are often understood/set by other software.)
#
#   CUDA_HOME (Linux/OS X)
#   CUDA_PATH (Windows)
#     specify where CUDA is installed; usually /usr/local/cuda or
#     /usr/local/cuda-x.y
#
#   CUDNN_LIB_DIR
#   CUDNN_INCLUDE_DIR
#   CUDNN_LIBRARY
#     specify where cuDNN is installed
#
#   NCCL_ROOT_DIR
#   NCCL_LIB_DIR
#   NCCL_INCLUDE_DIR
#     specify where nccl is installed
#
#   NVTOOLSEXT_PATH (Windows only)
#     specify where nvtoolsext is installed
#
#   LIBRARY_PATH
#   LD_LIBRARY_PATH
#     we will search for libraries in these paths


from setuptools import setup, Extension, distutils, Command, find_packages
import setuptools.command.build_ext
import setuptools.command.install
import setuptools.command.develop
import setuptools.command.build_py
import distutils.unixccompiler
import distutils.command.build
import distutils.command.clean
import platform
import subprocess
import shutil
import multiprocessing
import sys
import os
import json
import glob
import importlib

from tools.setup_helpers.env import check_env_flag
from tools.setup_helpers.cuda import WITH_CUDA, CUDA_HOME, CUDA_VERSION
from tools.setup_helpers.cudnn import (WITH_CUDNN, CUDNN_LIBRARY,
                                       CUDNN_LIB_DIR, CUDNN_INCLUDE_DIR)
from tools.setup_helpers.nccl import WITH_NCCL, WITH_SYSTEM_NCCL, NCCL_LIB_DIR, \
    NCCL_INCLUDE_DIR, NCCL_ROOT_DIR, NCCL_SYSTEM_LIB
from tools.setup_helpers.nnpack import WITH_NNPACK
from tools.setup_helpers.nvtoolext import NVTOOLEXT_HOME
from tools.setup_helpers.generate_code import generate_code
from tools.setup_helpers.ninja_builder import NinjaBuilder, ninja_build_ext
from tools.setup_helpers.dist_check import WITH_DISTRIBUTED, \
    WITH_DISTRIBUTED_MW, WITH_GLOO_IBVERBS

DEBUG = check_env_flag('DEBUG')

IS_WINDOWS = (platform.system() == 'Windows')
IS_DARWIN = (platform.system() == 'Darwin')
IS_LINUX = (platform.system() == 'Linux')

NUM_JOBS = multiprocessing.cpu_count()
max_jobs = os.getenv("MAX_JOBS")
if max_jobs is not None:
    NUM_JOBS = min(NUM_JOBS, int(max_jobs))

try:
    import ninja
    WITH_NINJA = True
except ImportError:
    WITH_NINJA = False

if not WITH_NINJA:
    ################################################################################
    # Monkey-patch setuptools to compile in parallel
    ################################################################################

    def parallelCCompile(self, sources, output_dir=None, macros=None,
                         include_dirs=None, debug=0, extra_preargs=None,
                         extra_postargs=None, depends=None):
        # those lines are copied from distutils.ccompiler.CCompiler directly
        macros, objects, extra_postargs, pp_opts, build = self._setup_compile(
            output_dir, macros, include_dirs, sources, depends, extra_postargs)
        cc_args = self._get_cc_args(pp_opts, debug, extra_preargs)

        # compile using a thread pool
        import multiprocessing.pool

        def _single_compile(obj):
            src, ext = build[obj]
            self._compile(obj, src, ext, cc_args, extra_postargs, pp_opts)
        multiprocessing.pool.ThreadPool(NUM_JOBS).map(_single_compile, objects)

        return objects
    distutils.ccompiler.CCompiler.compile = parallelCCompile

original_link = distutils.unixccompiler.UnixCCompiler.link


def patched_link(self, *args, **kwargs):
    _cxx = self.compiler_cxx
    self.compiler_cxx = None
    result = original_link(self, *args, **kwargs)
    self.compiler_cxx = _cxx
    return result


distutils.unixccompiler.UnixCCompiler.link = patched_link

################################################################################
# Workaround setuptools -Wstrict-prototypes warnings
# I lifted this code from https://stackoverflow.com/a/29634231/23845
################################################################################
import distutils.sysconfig
cfg_vars = distutils.sysconfig.get_config_vars()
for key, value in cfg_vars.items():
    if type(value) == str:
        cfg_vars[key] = value.replace("-Wstrict-prototypes", "")

################################################################################
# Custom build commands
################################################################################

dep_libs = [
    'nccl', 'ATen',
    'libshm', 'libshm_windows', 'gloo', 'THD', 'nanopb',
]


# global ninja file for building generated code stuff
ninja_global = None
if WITH_NINJA:
    ninja_global = NinjaBuilder('global')


def build_libs(libs):
    for lib in libs:
        assert lib in dep_libs, 'invalid lib: {}'.format(lib)
    if IS_WINDOWS:
        build_libs_cmd = ['torch\\lib\\build_libs.bat']
    else:
        build_libs_cmd = ['bash', 'torch/lib/build_libs.sh']
    my_env = os.environ.copy()
    my_env["PYTORCH_PYTHON"] = sys.executable
    my_env["NUM_JOBS"] = str(NUM_JOBS)
    if not IS_WINDOWS:
        if WITH_NINJA:
            my_env["CMAKE_GENERATOR"] = '-GNinja'
            my_env["CMAKE_INSTALL"] = 'ninja install'
        else:
            my_env['CMAKE_GENERATOR'] = ''
            my_env['CMAKE_INSTALL'] = 'make install'
    if WITH_SYSTEM_NCCL:
        my_env["NCCL_ROOT_DIR"] = NCCL_ROOT_DIR
    if WITH_CUDA:
        my_env["CUDA_BIN_PATH"] = CUDA_HOME
        build_libs_cmd += ['--with-cuda']
    if WITH_NNPACK:
        build_libs_cmd += ['--with-nnpack']
    if WITH_CUDNN:
        my_env["CUDNN_LIB_DIR"] = CUDNN_LIB_DIR
        my_env["CUDNN_LIBRARY"] = CUDNN_LIBRARY
        my_env["CUDNN_INCLUDE_DIR"] = CUDNN_INCLUDE_DIR

    if WITH_GLOO_IBVERBS:
        build_libs_cmd += ['--with-gloo-ibverbs']

    if subprocess.call(build_libs_cmd + libs, env=my_env) != 0:
        sys.exit(1)

missing_pydep = '''
Missing build dependency: Unable to `import {importname}`.
Please install it via `conda install {module}` or `pip install {module}`
'''.strip()


def check_pydep(importname, module):
    try:
        importlib.import_module(importname)
    except ImportError:
        raise RuntimeError(missing_pydep.format(importname=importname, module=module))


class build_deps(Command):
    user_options = []

    def initialize_options(self):
        pass

    def finalize_options(self):
        pass

    def run(self):
        # Check if you remembered to check out submodules
        def check_file(f):
            if not os.path.exists(f):
                print("Could not find {}".format(f))
                print("Did you run 'git submodule update --init'?")
                sys.exit(1)
        check_file(os.path.join(lib_path, "gloo", "CMakeLists.txt"))
        check_file(os.path.join(lib_path, "nanopb", "CMakeLists.txt"))
        check_file(os.path.join(lib_path, "pybind11", "CMakeLists.txt"))

        check_pydep('yaml', 'pyyaml')
        check_pydep('typing', 'typing')

        libs = []
        if WITH_NCCL and not WITH_SYSTEM_NCCL:
            libs += ['nccl']
        libs += ['ATen', 'nanopb']
        if IS_WINDOWS:
            libs += ['libshm_windows']
        else:
            libs += ['libshm']
        if WITH_DISTRIBUTED:
            if sys.platform.startswith('linux'):
                libs += ['gloo']
            libs += ['THD']
        build_libs(libs)


build_dep_cmds = {}

for lib in dep_libs:
    # wrap in function to capture lib
    class build_dep(build_deps):
        description = 'Build {} external library'.format(lib)

        def run(self):
            build_libs([self.lib])
    build_dep.lib = lib
    build_dep_cmds['build_' + lib.lower()] = build_dep


class build_module(Command):
    user_options = []

    def initialize_options(self):
        pass

    def finalize_options(self):
        pass

    def run(self):
        self.run_command('build_py')
        self.run_command('build_ext')


class build_py(setuptools.command.build_py.build_py):

    def run(self):
        self.create_version_file()
        setuptools.command.build_py.build_py.run(self)

    @staticmethod
    def create_version_file():
        global version, cwd
        print('-- Building version ' + version)
        version_path = os.path.join(cwd, 'torch', 'version.py')
        with open(version_path, 'w') as f:
            f.write("__version__ = '{}'\n".format(version))
            # NB: This is not 100% accurate, because you could have built the
            # library code with DEBUG, but csrc without DEBUG (in which case
            # this would claim to be a release build when it's not.)
            f.write("debug = {}\n".format(repr(DEBUG)))
            f.write("cuda = {}\n".format(repr(CUDA_VERSION)))


class develop(setuptools.command.develop.develop):

    def run(self):
        build_py.create_version_file()
        setuptools.command.develop.develop.run(self)
        self.create_compile_commands()

    def create_compile_commands(self):
        def load(filename):
            with open(filename) as f:
                return json.load(f)
        ninja_files = glob.glob('build/*_compile_commands.json')
        cmake_files = glob.glob('torch/lib/build/*/compile_commands.json')
        all_commands = [entry
                        for f in ninja_files + cmake_files
                        for entry in load(f)]
        with open('compile_commands.json', 'w') as f:
            json.dump(all_commands, f, indent=2)
        if not WITH_NINJA:
            print("WARNING: 'develop' is not building C++ code incrementally")
            print("because ninja is not installed. Run this to enable it:")
            print(" > pip install ninja")


def monkey_patch_THD_link_flags():
    '''
    THD's dynamic link deps are not determined until after build_deps is run
    So, we need to monkey-patch them in later
    '''
    # read tmp_install_path/THD_deps.txt for THD's dynamic linkage deps
    with open(tmp_install_path + '/THD_deps.txt', 'r') as f:
        thd_deps_ = f.read()
    thd_deps = []
    # remove empty lines
    for l in thd_deps_.split(';'):
        if l != '':
            thd_deps.append(l)

    C.extra_link_args += thd_deps


build_ext_parent = ninja_build_ext if WITH_NINJA \
    else setuptools.command.build_ext.build_ext


class build_ext(build_ext_parent):

    def run(self):

        # Print build options
        if WITH_NUMPY:
            print('-- Building with NumPy bindings')
        else:
            print('-- NumPy not found')
        if WITH_CUDNN:
            print('-- Detected cuDNN at ' + CUDNN_LIBRARY + ', ' + CUDNN_INCLUDE_DIR)
        else:
            print('-- Not using cuDNN')
        if WITH_CUDA:
            print('-- Detected CUDA at ' + CUDA_HOME)
        else:
            print('-- Not using CUDA')
        if WITH_NCCL and WITH_SYSTEM_NCCL:
            print('-- Using system provided NCCL library at ' +
                  NCCL_SYSTEM_LIB + ', ' + NCCL_INCLUDE_DIR)
        elif WITH_NCCL:
            print('-- Building NCCL library')
        else:
            print('-- Not using NCCL')
        if WITH_DISTRIBUTED:
            print('-- Building with distributed package ')
            monkey_patch_THD_link_flags()
        else:
            print('-- Building without distributed package')

        generate_code(ninja_global)

        if WITH_NINJA:
            # before we start the normal build make sure all generated code
            # gets built
            ninja_global.run()

        # It's an old-style class in Python 2.7...
        setuptools.command.build_ext.build_ext.run(self)


class build(distutils.command.build.build):
    sub_commands = [
        ('build_deps', lambda self: True),
    ] + distutils.command.build.build.sub_commands


class install(setuptools.command.install.install):

    def run(self):
        if not self.skip_build:
            self.run_command('build_deps')

        # Copy headers necessary to compile C++ extensions.
        self.copy_tree('torch/csrc', 'torch/lib/include/torch/csrc/')
        self.copy_tree('torch/lib/pybind11/include/pybind11/',
                       'torch/lib/include/pybind11')
        self.copy_file('torch/torch.h', 'torch/lib/include/torch/torch.h')

        setuptools.command.install.install.run(self)


class clean(distutils.command.clean.clean):

    def run(self):
        import glob
        with open('.gitignore', 'r') as f:
            ignores = f.read()
            for wildcard in filter(bool, ignores.split('\n')):
                for filename in glob.glob(wildcard):
                    try:
                        os.remove(filename)
                    except OSError:
                        shutil.rmtree(filename, ignore_errors=True)

        # It's an old-style class in Python 2.7...
        distutils.command.clean.clean.run(self)


################################################################################
# Configure compile flags
################################################################################

include_dirs = []
library_dirs = []
extra_link_args = []

if IS_WINDOWS:
    extra_compile_args = ['/Z7', '/EHa', '/DNOMINMAX'
                          # /Z7 turns on symbolic debugging information in .obj files
                          # /EHa is about native C++ catch support for asynchronous
                          # structured exception handling (SEH)
                          # /DNOMINMAX removes builtin min/max functions
                          ]
    if sys.version_info[0] == 2:
        # /bigobj increases number of sections in .obj file, which is needed to link
        # against libaries in Python 2.7 under Windows
        extra_compile_args.append('/bigobj')
else:
    extra_compile_args = ['-std=c++11', '-Wno-write-strings',
                          # Python 2.6 requires -fno-strict-aliasing, see
                          # http://legacy.python.org/dev/peps/pep-3123/
                          '-fno-strict-aliasing',
                          # Clang has an unfixed bug leading to spurious missing
                          # braces warnings, see
                          # https://bugs.llvm.org/show_bug.cgi?id=21629
                          '-Wno-missing-braces']

cwd = os.path.dirname(os.path.abspath(__file__))
lib_path = os.path.join(cwd, "torch", "lib")


tmp_install_path = lib_path + "/tmp_install"
include_dirs += [
    cwd,
    os.path.join(cwd, "torch", "csrc"),
    lib_path + "/pybind11/include",
    tmp_install_path + "/include",
    tmp_install_path + "/include/TH",
    tmp_install_path + "/include/THNN",
    tmp_install_path + "/include/ATen",
]

library_dirs.append(lib_path)

# we specify exact lib names to avoid conflict with lua-torch installs
ATEN_LIB = os.path.join(lib_path, 'libATen.so.1')
THD_LIB = os.path.join(lib_path, 'libTHD.a')
NCCL_LIB = os.path.join(lib_path, 'libnccl.so.1')

# static library only
NANOPB_STATIC_LIB = os.path.join(lib_path, 'libprotobuf-nanopb.a')

if IS_DARWIN:
    ATEN_LIB = os.path.join(lib_path, 'libATen.1.dylib')
    NCCL_LIB = os.path.join(lib_path, 'libnccl.1.dylib')

if IS_WINDOWS:
    ATEN_LIB = os.path.join(lib_path, 'ATen.lib')
    NANOPB_STATIC_LIB = os.path.join(lib_path, 'protobuf-nanopb.lib')

main_compile_args = ['-D_THP_CORE']
main_libraries = ['shm']
main_link_args = [ATEN_LIB, NANOPB_STATIC_LIB]
main_sources = [
    "torch/csrc/PtrWrapper.cpp",
    "torch/csrc/Module.cpp",
    "torch/csrc/Generator.cpp",
    "torch/csrc/Size.cpp",
    "torch/csrc/Dtype.cpp",
    "torch/csrc/Exceptions.cpp",
    "torch/csrc/Storage.cpp",
    "torch/csrc/DataLoader.cpp",
    "torch/csrc/DynamicTypes.cpp",
    "torch/csrc/assertions.cpp",
    "torch/csrc/byte_order.cpp",
    "torch/csrc/torch.cpp",
    "torch/csrc/utils.cpp",
    "torch/csrc/utils/cuda_lazy_init.cpp",
    "torch/csrc/utils/invalid_arguments.cpp",
    "torch/csrc/utils/object_ptr.cpp",
    "torch/csrc/utils/python_arg_parser.cpp",
    "torch/csrc/utils/tensor_list.cpp",
    "torch/csrc/utils/tensor_new.cpp",
    "torch/csrc/utils/tensor_numpy.cpp",
    "torch/csrc/utils/tensor_dtypes.cpp",
    "torch/csrc/utils/tensor_types.cpp",
    "torch/csrc/utils/tuple_parser.cpp",
    "torch/csrc/utils/tensor_apply.cpp",
    "torch/csrc/utils/tensor_flatten.cpp",
    "torch/csrc/utils/variadic.cpp",
    "torch/csrc/allocators.cpp",
    "torch/csrc/serialization.cpp",
    "torch/csrc/jit/init.cpp",
    "torch/csrc/jit/interpreter.cpp",
    "torch/csrc/jit/ir.cpp",
    "torch/csrc/jit/fusion_compiler.cpp",
    "torch/csrc/jit/graph_executor.cpp",
    "torch/csrc/jit/python_ir.cpp",
    "torch/csrc/jit/test_jit.cpp",
    "torch/csrc/jit/tracer.cpp",
    "torch/csrc/jit/tracer_state.cpp",
    "torch/csrc/jit/python_tracer.cpp",
    "torch/csrc/jit/passes/shape_analysis.cpp",
    "torch/csrc/jit/interned_strings.cpp",
    "torch/csrc/jit/type.cpp",
    "torch/csrc/jit/export.cpp",
    "torch/csrc/jit/autodiff.cpp",
    "torch/csrc/jit/interpreter_autograd_function.cpp",
    "torch/csrc/jit/python_arg_flatten.cpp",
    "torch/csrc/jit/python_compiled_function.cpp",
    "torch/csrc/jit/variable_flags.cpp",
    "torch/csrc/jit/passes/create_autodiff_subgraphs.cpp",
    "torch/csrc/jit/passes/graph_fuser.cpp",
    "torch/csrc/jit/passes/onnx.cpp",
    "torch/csrc/jit/passes/dead_code_elimination.cpp",
    "torch/csrc/jit/passes/common_subexpression_elimination.cpp",
    "torch/csrc/jit/passes/peephole.cpp",
    "torch/csrc/jit/passes/inplace_check.cpp",
    "torch/csrc/jit/passes/canonicalize.cpp",
    "torch/csrc/jit/passes/batch_mm.cpp",
    "torch/csrc/jit/passes/onnx/peephole.cpp",
    "torch/csrc/jit/generated/aten_dispatch.cpp",
    "torch/csrc/jit/script/lexer.cpp",
    "torch/csrc/jit/script/compiler.cpp",
    "torch/csrc/jit/script/module.cpp",
    "torch/csrc/jit/script/init.cpp",
    "torch/csrc/jit/script/python_tree_views.cpp",
    "torch/csrc/autograd/init.cpp",
    "torch/csrc/autograd/grad_mode.cpp",
    "torch/csrc/autograd/engine.cpp",
    "torch/csrc/autograd/function.cpp",
    "torch/csrc/autograd/variable.cpp",
    "torch/csrc/autograd/saved_variable.cpp",
    "torch/csrc/autograd/input_buffer.cpp",
    "torch/csrc/autograd/profiler.cpp",
    "torch/csrc/autograd/python_function.cpp",
    "torch/csrc/autograd/python_cpp_function.cpp",
    "torch/csrc/autograd/python_variable.cpp",
    "torch/csrc/autograd/python_variable_indexing.cpp",
    "torch/csrc/autograd/python_engine.cpp",
    "torch/csrc/autograd/python_hook.cpp",
    "torch/csrc/autograd/generated/VariableType.cpp",
    "torch/csrc/autograd/generated/Functions.cpp",
    "torch/csrc/autograd/generated/python_torch_functions.cpp",
    "torch/csrc/autograd/generated/python_variable_methods.cpp",
    "torch/csrc/autograd/generated/python_functions.cpp",
    "torch/csrc/autograd/generated/python_nn_functions.cpp",
    "torch/csrc/autograd/functions/basic_ops.cpp",
    "torch/csrc/autograd/functions/tensor.cpp",
    "torch/csrc/autograd/functions/accumulate_grad.cpp",
    "torch/csrc/autograd/functions/special.cpp",
    "torch/csrc/autograd/functions/utils.cpp",
    "torch/csrc/autograd/functions/init.cpp",
    "torch/csrc/nn/THNN.cpp",
    "torch/csrc/tensor/python_tensor.cpp",
    "torch/csrc/onnx/onnx.pb.cpp",
    "torch/csrc/onnx/onnx.cpp",
]

try:
    import numpy as np
    include_dirs += [np.get_include()]
    extra_compile_args += ['-DWITH_NUMPY']
    WITH_NUMPY = True
except ImportError:
    WITH_NUMPY = False

if WITH_DISTRIBUTED:
    extra_compile_args += ['-DWITH_DISTRIBUTED']
    main_sources += [
        "torch/csrc/distributed/Module.cpp",
    ]
    if WITH_DISTRIBUTED_MW:
        main_sources += [
            "torch/csrc/distributed/Tensor.cpp",
            "torch/csrc/distributed/Storage.cpp",
        ]
        extra_compile_args += ['-DWITH_DISTRIBUTED_MW']
    include_dirs += [tmp_install_path + "/include/THD"]
    main_link_args += [THD_LIB]

if WITH_CUDA:
    nvtoolext_lib_name = None
    if IS_WINDOWS:
        cuda_lib_path = CUDA_HOME + '/lib/x64/'
        nvtoolext_lib_path = NVTOOLEXT_HOME + '/lib/x64/'
        nvtoolext_include_path = os.path.join(NVTOOLEXT_HOME, 'include')

        library_dirs.append(nvtoolext_lib_path)
        include_dirs.append(nvtoolext_include_path)

        nvtoolext_lib_name = 'nvToolsExt64_1'

        # MSVC doesn't support runtime symbol resolving, `nvrtc` and `cuda` should be linked
        main_libraries += ['nvrtc', 'cuda']
    else:
        cuda_lib_dirs = ['lib64', 'lib']

        for lib_dir in cuda_lib_dirs:
            cuda_lib_path = os.path.join(CUDA_HOME, lib_dir)
            if os.path.exists(cuda_lib_path):
                break
        extra_link_args.append('-Wl,-rpath,' + cuda_lib_path)

        nvtoolext_lib_name = 'nvToolsExt'

    library_dirs.append(cuda_lib_path)
    cuda_include_path = os.path.join(CUDA_HOME, 'include')
    include_dirs.append(cuda_include_path)
    include_dirs.append(tmp_install_path + "/include/THCUNN")
    extra_compile_args += ['-DWITH_CUDA']
    extra_compile_args += ['-DCUDA_LIB_PATH=' + cuda_lib_path]
    main_libraries += ['cudart', nvtoolext_lib_name]
    main_sources += [
        "torch/csrc/cuda/Module.cpp",
        "torch/csrc/cuda/Storage.cpp",
        "torch/csrc/cuda/Stream.cpp",
        "torch/csrc/cuda/utils.cpp",
        "torch/csrc/cuda/comm.cpp",
        "torch/csrc/cuda/python_comm.cpp",
        "torch/csrc/cuda/serialization.cpp",
        "torch/csrc/nn/THCUNN.cpp",
    ]

if WITH_NCCL:
    if WITH_SYSTEM_NCCL:
        main_link_args += [NCCL_SYSTEM_LIB]
        include_dirs.append(NCCL_INCLUDE_DIR)
    else:
        main_link_args += [NCCL_LIB]
    extra_compile_args += ['-DWITH_NCCL']
    main_sources += [
        "torch/csrc/cuda/nccl.cpp",
        "torch/csrc/cuda/python_nccl.cpp",
    ]
if WITH_CUDNN:
    main_libraries += [CUDNN_LIBRARY]
    # NOTE: these are at the front, in case there's another cuDNN in CUDA path
    include_dirs.insert(0, CUDNN_INCLUDE_DIR)
    if not IS_WINDOWS:
        extra_link_args.insert(0, '-Wl,-rpath,' + CUDNN_LIB_DIR)
    extra_compile_args += ['-DWITH_CUDNN']

if DEBUG:
    if IS_WINDOWS:
        extra_link_args.append('/DEBUG:FULL')
    else:
        extra_compile_args += ['-O0', '-g']
        extra_link_args += ['-O0', '-g']

if os.getenv('PYTORCH_BINARY_BUILD') and platform.system() == 'Linux':
    print('PYTORCH_BINARY_BUILD found. Static linking libstdc++ on Linux')
    # get path of libstdc++ and link manually.
    # for reasons unknown, -static-libstdc++ doesn't fully link some symbols
    CXXNAME = os.getenv('CXX', 'g++')
    STDCPP_LIB = subprocess.check_output([CXXNAME, '-print-file-name=libstdc++.a'])
    STDCPP_LIB = STDCPP_LIB[:-1]
    if type(STDCPP_LIB) != str:  # python 3
        STDCPP_LIB = STDCPP_LIB.decode(sys.stdout.encoding)
    main_link_args += [STDCPP_LIB]
    version_script = os.path.abspath("tools/pytorch.version")
    extra_link_args += ['-Wl,--version-script=' + version_script]


def make_relative_rpath(path):
    if IS_DARWIN:
        return '-Wl,-rpath,@loader_path/' + path
    elif IS_WINDOWS:
        return ''
    else:
        return '-Wl,-rpath,$ORIGIN/' + path

################################################################################
# Declare extensions and package
################################################################################

extensions = []
packages = find_packages(exclude=('tools', 'tools.*',))
C = Extension("torch._C",
              libraries=main_libraries,
              sources=main_sources,
              language='c++',
              extra_compile_args=main_compile_args + extra_compile_args,
              include_dirs=include_dirs,
              library_dirs=library_dirs,
              extra_link_args=extra_link_args + main_link_args + [make_relative_rpath('lib')],
              )
extensions.append(C)

if not IS_WINDOWS:
    DL = Extension("torch._dl",
                   sources=["torch/csrc/dl.c"],
                   language='c',
                   )
    extensions.append(DL)


if WITH_CUDA:
    thnvrtc_link_flags = extra_link_args + [make_relative_rpath('lib')]
    if IS_LINUX:
        thnvrtc_link_flags = thnvrtc_link_flags + ['-Wl,--no-as-needed']
    # these have to be specified as -lcuda in link_flags because they
    # have to come right after the `no-as-needed` option
    if IS_WINDOWS:
        thnvrtc_link_flags += ['cuda.lib', 'nvrtc.lib']
    else:
        thnvrtc_link_flags += ['-lcuda', '-lnvrtc']
    cuda_stub_path = [cuda_lib_path + '/stubs']
    if IS_DARWIN:
        # on macOS this is where the CUDA stub is installed according to the manual
        cuda_stub_path = ["/usr/local/cuda/lib"]
    THNVRTC = Extension("torch._nvrtc",
                        sources=['torch/csrc/nvrtc.cpp'],
                        language='c++',
                        include_dirs=include_dirs,
                        library_dirs=library_dirs + cuda_stub_path,
                        extra_link_args=thnvrtc_link_flags,
                        )
    extensions.append(THNVRTC)

version = '0.4.0a0'
if os.getenv('PYTORCH_BUILD_VERSION'):
    assert os.getenv('PYTORCH_BUILD_NUMBER') is not None
    build_number = int(os.getenv('PYTORCH_BUILD_NUMBER'))
    version = os.getenv('PYTORCH_BUILD_VERSION')
    if build_number > 1:
        version += '.post' + str(build_number)
else:
    try:
        sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=cwd).decode('ascii').strip()
        version += '+' + sha[:7]
    except Exception:
        pass

cmdclass = {
    'build': build,
    'build_py': build_py,
    'build_ext': build_ext,
    'build_deps': build_deps,
    'build_module': build_module,
    'develop': develop,
    'install': install,
    'clean': clean,
}
cmdclass.update(build_dep_cmds)

if __name__ == '__main__':
    setup(
        name="torch",
        version=version,
        description=("Tensors and Dynamic neural networks in "
                     "Python with strong GPU acceleration"),
        ext_modules=extensions,
        cmdclass=cmdclass,
        packages=packages,
        package_data={
            'torch': [
                'lib/*.so*',
                'lib/*.dylib*',
                'lib/*.dll',
                'lib/*.lib',
                'lib/torch_shm_manager',
                'lib/*.h',
                'lib/include/ATen/*.h',
                'lib/include/ATen/cuda/*.h',
                'lib/include/ATen/cuda/*.cuh',
                'lib/include/ATen/cudnn/*.h',
                'lib/include/ATen/cuda/detail/*.cuh',
                'lib/include/pybind11/*.h',
                'lib/include/pybind11/detail/*.h',
                'lib/include/TH/*.h',
                'lib/include/TH/generic/*.h',
                'lib/include/THC/*.h',
                'lib/include/THC/*.cuh',
                'lib/include/THC/generic/*.h',
                'lib/include/THCUNN/*.cuh',
                'lib/include/torch/csrc/*.h',
                'lib/include/torch/csrc/autograd/*.h',
                'lib/include/torch/csrc/jit/*.h',
                'lib/include/torch/csrc/utils/*.h',
                'lib/include/torch/torch.h',
            ]
        })
