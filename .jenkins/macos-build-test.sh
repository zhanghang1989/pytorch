#!/bin/bash

COMPACT_JOB_NAME=pytorch-macos-10.13-py3-build-test
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

# Set up conda environment
curl https://repo.continuum.io/miniconda/Miniconda3-latest-MacOSX-x86_64.sh -o $PWD/miniconda3.sh
rm -rf $PWD/miniconda3
bash $PWD/miniconda3.sh -b -p $PWD/miniconda3
export PATH="$PWD/miniconda3/bin:$PATH"
source $PWD/miniconda3/bin/activate
conda install -y numpy pyyaml setuptools cmake cffi ninja

# Build and test PyTorch
git submodule update --init --recursive
export CMAKE_PREFIX_PATH=$PWD/miniconda3/

export MACOSX_DEPLOYMENT_TARGET=10.9
export CXX=clang++
export CC=clang
# If we run too many parallel jobs, we will OOM
export MAX_JOBS=2
python setup.py install
echo "Ninja version: $(ninja --version)"
python test/run_test.py --verbose
echo "BUILD PASSED"
