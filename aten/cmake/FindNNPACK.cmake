# Cribbed from https://github.com/caffe2/caffe2/blob/master/cmake/Modules/FindNNPACK.cmake
#
# - Try to find NNPACK
#
# The following variables are optionally searched for defaults
#  NNPACK_ROOT_DIR:            Base directory where all NNPACK components are found
#
# The following are set after configuration is done:
#  NNPACK_FOUND
#  NNPACK_INCLUDE_DIRS
#  NNPACK_LIBRARIES
#  NNPACK_LIBRARYRARY_DIRS

include(FindPackageHandleStandardArgs)
include(CheckSymbolExists)

set(NNPACK_ROOT_DIR "" CACHE PATH "Folder contains NNPACK")

find_path(NNPACK_INCLUDE_DIR nnpack.h
    PATHS ${NNPACK_ROOT_DIR}
    PATH_SUFFIXES include)

# TODO: deps/pthreadpool/include may also need to be registered as an include directory
# TODO: Conda searching?

find_library(NNPACK_LIBRARY nnpack
    PATHS ${NNPACK_ROOT_DIR}
    PATH_SUFFIXES lib lib64)

find_library(CPUINFO_LIBRARY cpuinfo
    PATHS ${NNPACK_ROOT_DIR}
    PATH_SUFFIXES lib lib64)

find_library(PTHREADPOOL_LIBRARY pthreadpool
    PATHS ${NNPACK_ROOT_DIR}
    PATH_SUFFIXES lib lib64)

find_package_handle_standard_args(NNPACK DEFAULT_MSG NNPACK_INCLUDE_DIR NNPACK_LIBRARY CPUINFO_LIBRARY PTHREADPOOL_LIBRARY)

if(NNPACK_FOUND)
  set(NNPACK_INCLUDE_DIRS ${NNPACK_INCLUDE_DIR})
  set(NNPACK_LIBRARIES ${NNPACK_LIBRARY} ${CPUINFO_LIBRARY} ${PTHREADPOOL_LIBRARY})

  list(APPEND CMAKE_REQUIRED_LIBRARIES ${NNPACK_LIBRARIES})
  list(APPEND CMAKE_REQUIRED_INCLUDES ${NNPACK_INCLUDE_DIRS})
  check_symbol_exists(nnp_convolution_kernel_gradient "nnpack.h" NNPACK_HAS_INFERENCE)

  if(NNPACK_HAS_INFERENCE)
    message(STATUS "Found NNPACK      (include: ${NNPACK_INCLUDE_DIR}, library: ${NNPACK_LIBRARY})")
    message(STATUS "Found CPUINFO     (library: ${CPUINFO_LIBRARY})")
    message(STATUS "Found PTHREADPOOL (library: ${PTHREADPOOL_LIBRARY})")
    mark_as_advanced(NNPACK_ROOT_DIR NNPACK_LIBRARY_RELEASE NNPACK_LIBRARY_DEBUG
                                   NNPACK_LIBRARY NNPACK_INCLUDE_DIR)
  else()
    message(STATUS "Refusing to use incomplete NNPACK (include: ${NNPACK_INCLUDE_DIR}, library: ${NNPACK_LIBRARY}); try reinstalling NNPACK without --inference-only")
  endif()
endif()
